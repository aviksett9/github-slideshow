import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';

import worker, { checkAlerts, checkFills, handleUpdate } from '../src/index.js';
import { DeltaClient, buildQuery, hmacSha256Hex } from '../src/delta.js';
import { isTriggered, parseAlert } from '../src/alerts.js';

// ------------------------------------------------------------ test doubles

class MemoryKV {
  constructor() {
    this.map = new Map();
    this.writes = 0;
  }
  async get(key, type) {
    const v = this.map.has(key) ? this.map.get(key) : null;
    if (v === null) return null;
    return type === 'json' ? JSON.parse(v) : v;
  }
  async put(key, value) {
    this.writes += 1;
    this.map.set(key, value);
  }
  async delete(key) {
    this.writes += 1;
    this.map.delete(key);
  }
}

let sent; // Telegram messages
let calls; // every outbound request
let prices; // symbol -> mark price
let fills; // fake /v2/fills result
let deltaPrivateError; // error code to return from private endpoints
const realFetch = globalThis.fetch;

function reply(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

beforeEach(() => {
  sent = [];
  calls = [];
  prices = { BTCUSD: 60000, ETHUSD: 3000 };
  fills = [];
  deltaPrivateError = null;
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url);
    calls.push({ url, init });
    if (url.hostname === 'api.telegram.org') {
      const body = JSON.parse(init.body);
      const method = url.pathname.split('/').pop();
      sent.push({ method, ...body });
      return reply({ ok: true, result: true });
    }
    if (url.pathname.startsWith('/v2/tickers/')) {
      const symbol = decodeURIComponent(url.pathname.split('/').pop());
      if (!(symbol in prices)) return reply({ success: false, error: { code: 'not_found' } }, 404);
      return reply({
        success: true,
        result: { symbol, mark_price: String(prices[symbol]), close: prices[symbol], ltp_change_24h: '-0.22' },
      });
    }
    if (url.pathname === '/v2/fills') {
      if (deltaPrivateError) return reply({ success: false, error: { code: deltaPrivateError } }, 401);
      return reply({ success: true, result: fills });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

function makeEnv(overrides = {}) {
  return {
    TELEGRAM_BOT_TOKEN: 'bot-token',
    TELEGRAM_CHAT_ID: '42',
    TELEGRAM_WEBHOOK_SECRET: 'hook-secret',
    API_TOKEN: 'api-token',
    DELTA_BASE_URL: 'https://api.india.delta.exchange',
    STATE: new MemoryKV(),
    ...overrides,
  };
}

const msg = (text, chatId = 42) => ({ message: { text, chat: { id: chatId } } });

function makeCtx() {
  const pending = [];
  return { pending, waitUntil: (p) => pending.push(p), flush: () => Promise.all(pending) };
}

// ------------------------------------------------------------ signing

test('HMAC signature matches node:crypto', async () => {
  const expected = createHmac('sha256', 's3cret').update('GET1700000000/v2/fills?page_size=50').digest('hex');
  assert.equal(await hmacSha256Hex('s3cret', 'GET1700000000/v2/fills?page_size=50'), expected);
});

test('buildQuery includes leading ? and skips empty values', () => {
  assert.equal(buildQuery({ page_size: 50, after: undefined, x: '' }), '?page_size=50');
  assert.equal(buildQuery({}), '');
  assert.equal(buildQuery(undefined), '');
});

test('private request sends api-key, timestamp and correct signature', async () => {
  const client = new DeltaClient({
    baseUrl: 'https://api.india.delta.exchange',
    apiKey: 'k',
    apiSecret: 'sec',
    now: () => 1_700_000_000_500,
  });
  await client.fills(50);
  const { url, init } = calls[0];
  assert.equal(url.toString(), 'https://api.india.delta.exchange/v2/fills?page_size=50');
  assert.equal(init.headers['api-key'], 'k');
  assert.equal(init.headers.timestamp, '1700000000');
  const expected = createHmac('sha256', 'sec').update('GET1700000000/v2/fills?page_size=50').digest('hex');
  assert.equal(init.headers.signature, expected);
  assert.ok(init.headers['User-Agent']);
});

// ------------------------------------------------------------ alert parsing

test('parseAlert accepts common forms', () => {
  assert.deepEqual(parseAlert('BTCUSD > 65000'), { symbol: 'BTCUSD', op: '>', price: 65000 });
  assert.deepEqual(parseAlert('ethusd below 2500.5'), { symbol: 'ETHUSD', op: '<', price: 2500.5 });
  assert.deepEqual(parseAlert('SOLUSD<=150'), { symbol: 'SOLUSD', op: '<', price: 150 });
  assert.equal(parseAlert('BTCUSD 65000'), null);
  assert.equal(parseAlert(''), null);
  assert.equal(parseAlert('BTCUSD > 0'), null);
});

test('isTriggered', () => {
  assert.equal(isTriggered({ op: '>', price: 100 }, 100), true);
  assert.equal(isTriggered({ op: '>', price: 100 }, 99.9), false);
  assert.equal(isTriggered({ op: '<', price: 100 }, 99), true);
  assert.equal(isTriggered({ op: '<', price: 100 }, undefined), false);
});

// ------------------------------------------------------------ webhook

test('webhook rejects a wrong secret', async () => {
  const env = makeEnv();
  const res = await worker.fetch(
    new Request('https://w.example/telegram', {
      method: 'POST',
      headers: { 'X-Telegram-Bot-Api-Secret-Token': 'nope' },
      body: JSON.stringify(msg('/price')),
    }),
    env,
    makeCtx(),
  );
  assert.equal(res.status, 403);
});

test('webhook answers /price for the configured chat', async () => {
  const env = makeEnv();
  const ctx = makeCtx();
  const res = await worker.fetch(
    new Request('https://w.example/telegram', {
      method: 'POST',
      headers: { 'X-Telegram-Bot-Api-Secret-Token': 'hook-secret' },
      body: JSON.stringify(msg('/price BTCUSD')),
    }),
    env,
    ctx,
  );
  assert.equal(res.status, 200);
  await ctx.flush();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].chat_id, '42');
  assert.match(sent[0].text, /BTCUSD<\/b> mark 60,000/);
});

test('strangers are ignored except /chatid', async () => {
  const env = makeEnv();
  await handleUpdate(msg('/price', 999), env);
  assert.equal(sent.length, 0);
  await handleUpdate(msg('/chatid', 999), env);
  assert.equal(sent.length, 1);
  assert.match(sent[0].text, /999/);
});

// ------------------------------------------------------------ alerts end-to-end

test('alert is added, fires once on cron, then is removed', async () => {
  const env = makeEnv();
  await handleUpdate(msg('/alert BTCUSD > 61000'), env);
  assert.match(sent.at(-1).text, /Added alert #1 BTCUSD ≥ 61,000/);

  const writesAfterAdd = env.STATE.writes;
  await checkAlerts(env); // price 60000: nothing happens, no KV write
  assert.equal(sent.length, 1);
  assert.equal(env.STATE.writes, writesAfterAdd);

  prices.BTCUSD = 61500;
  await checkAlerts(env);
  assert.equal(sent.length, 2);
  assert.match(sent[1].text, /🔔 <b>BTCUSD<\/b> mark price 61,500 is at\/above 61,000 \(alert #1\)/);

  await checkAlerts(env); // already removed
  assert.equal(sent.length, 2);

  await handleUpdate(msg('/alerts'), env);
  assert.match(sent.at(-1).text, /No active alerts/);
});

test('alert that would fire immediately is refused', async () => {
  const env = makeEnv();
  await handleUpdate(msg('/alert BTCUSD < 65000'), env);
  assert.match(sent.at(-1).text, /would fire immediately/);
  assert.equal(await env.STATE.get('alerts'), null);
});

test('alert for an unknown symbol is refused', async () => {
  const env = makeEnv();
  await handleUpdate(msg('/alert NOPEUSD > 1'), env);
  assert.match(sent.at(-1).text, /Couldn't find <b>NOPEUSD<\/b>/);
});

test('/delalert removes by id', async () => {
  const env = makeEnv();
  await handleUpdate(msg('/alert BTCUSD > 70000'), env);
  await handleUpdate(msg('/alert ETHUSD < 2000'), env);
  await handleUpdate(msg('/delalert 1'), env);
  await handleUpdate(msg('/alerts'), env);
  assert.doesNotMatch(sent.at(-1).text, /#1 /);
  assert.match(sent.at(-1).text, /#2 ETHUSD ≤ 2,000/);
});

// ------------------------------------------------------------ fills

test('fills: first run seeds silently, later runs notify only new fills', async () => {
  const env = makeEnv({ DELTA_API_KEY: 'k', DELTA_API_SECRET: 's' });
  fills = [{ id: 1, side: 'buy', size: 10, price: '60000', product_symbol: 'BTCUSD', role: 'taker' }];
  await checkFills(env);
  assert.equal(sent.length, 0);

  await checkFills(env); // nothing new: no message, no write
  const writes = env.STATE.writes;
  await checkFills(env);
  assert.equal(env.STATE.writes, writes);
  assert.equal(sent.length, 0);

  fills = [
    { id: 3, side: 'sell', size: 5, price: '61000', product_symbol: 'BTCUSD', role: 'maker' },
    { id: 2, side: 'buy', size: 1, price: '3000', product_symbol: 'ETHUSD', role: 'taker' },
    ...fills,
  ];
  await checkFills(env);
  assert.equal(sent.length, 2);
  assert.match(sent[0].text, /BUY 1 <b>ETHUSD<\/b> @ 3,000/); // oldest first
  assert.match(sent[1].text, /SELL 5 <b>BTCUSD<\/b> @ 61,000/);
});

test('fills: an auth error is reported once, recovery is reported once', async () => {
  const env = makeEnv({ DELTA_API_KEY: 'k', DELTA_API_SECRET: 's' });
  deltaPrivateError = 'ip_not_whitelisted_for_api_key';
  await checkFills(env);
  await checkFills(env);
  assert.equal(sent.length, 1);
  assert.match(sent[0].text, /IP whitelist/);

  deltaPrivateError = null;
  await checkFills(env);
  assert.equal(sent.length, 2);
  assert.match(sent[1].text, /working again/);
});

test('fills are skipped without API credentials', async () => {
  const env = makeEnv();
  await checkFills(env);
  assert.equal(calls.length, 0);
});

// ------------------------------------------------------------ /notify and /setup

test('/notify requires the bearer token and forwards escaped text', async () => {
  const env = makeEnv();
  let res = await worker.fetch(
    new Request('https://w.example/notify', { method: 'POST', body: 'hi' }),
    env,
    makeCtx(),
  );
  assert.equal(res.status, 401);

  res = await worker.fetch(
    new Request('https://w.example/notify', {
      method: 'POST',
      headers: { Authorization: 'Bearer api-token', 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: 'Order filled <BTCUSD>' }),
    }),
    env,
    makeCtx(),
  );
  assert.equal(res.status, 200);
  assert.equal(sent[0].text, 'Order filled &lt;BTCUSD&gt;');
  assert.equal(sent[0].chat_id, '42');
});

test('/setup registers the webhook with the secret', async () => {
  const env = makeEnv();
  const res = await worker.fetch(new Request('https://w.example/setup?token=api-token'), env, makeCtx());
  assert.equal(res.status, 200);
  const hook = sent.find((s) => s.method === 'setWebhook');
  assert.equal(hook.url, 'https://w.example/telegram');
  assert.equal(hook.secret_token, 'hook-secret');
  assert.ok(sent.find((s) => s.method === 'setMyCommands'));
});

test('/setup rejects a missing token', async () => {
  const res = await worker.fetch(new Request('https://w.example/setup'), makeEnv(), makeCtx());
  assert.equal(res.status, 401);
});
