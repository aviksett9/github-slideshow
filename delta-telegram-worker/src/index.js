// Delta Exchange -> Telegram notifier on Cloudflare Workers.
//
//  * Cron (every minute): price alerts + new trade fills -> Telegram
//  * POST /telegram  : Telegram webhook for bot commands (/price, /alert, ...)
//  * POST /notify    : lets any other bot/script push a message to your chat
//  * GET  /setup     : registers the Telegram webhook and command menu
//
// KV writes only happen when something changes, so a 1-minute cron stays
// inside the Workers free plan (1,000 KV writes/day).

import { DeltaClient, describeDeltaError } from './delta.js';
import { Telegram, escapeHtml } from './telegram.js';
import { MAX_ALERTS, describeAlert, formatNumber, isTriggered, parseAlert } from './alerts.js';

const KEY_ALERTS = 'alerts';
const KEY_FILLS_SEEN = 'fills_seen';
const KEY_FILLS_ERROR = 'fills_error';
const SEEN_FILLS_KEEP = 200;

const COMMANDS = [
  ['price', 'Prices: /price BTCUSD ETHUSD'],
  ['alert', 'Add alert: /alert BTCUSD > 65000'],
  ['alerts', 'List active alerts'],
  ['delalert', 'Delete alert: /delalert 3 (or all)'],
  ['positions', 'Open positions (needs API key)'],
  ['balance', 'Wallet balances (needs API key)'],
  ['help', 'Show help'],
];

const HELP = [
  '<b>Delta Exchange notifier</b>',
  '',
  '/price BTCUSD ETHUSD — mark &amp; last price',
  '/alert BTCUSD &gt; 65000 — one-shot alert on mark price (&gt; or &lt;)',
  '/alerts — list alerts',
  '/delalert 3 — delete alert #3 (/delalert all clears)',
  '/positions — open positions',
  '/balance — wallet balances',
  '/chatid — show this chat id',
  '',
  'New trade fills are pushed automatically when an API key is configured.',
].join('\n');

// ---------------------------------------------------------------- helpers

function deps(env) {
  return {
    delta: new DeltaClient({
      baseUrl: env.DELTA_BASE_URL,
      apiKey: env.DELTA_API_KEY,
      apiSecret: env.DELTA_API_SECRET,
    }),
    tg: new Telegram(env.TELEGRAM_BOT_TOKEN),
    kv: env.STATE,
    chatId: String(env.TELEGRAM_CHAT_ID || ''),
  };
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

async function sha256(value) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)));
}

// Constant-time string comparison (hash both so lengths match).
export async function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || !a || !b) return false;
  const [x, y] = await Promise.all([sha256(a), sha256(b)]);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function bearer(request) {
  const h = request.headers.get('Authorization') || '';
  return h.startsWith('Bearer ') ? h.slice(7).trim() : '';
}

async function loadAlerts(kv) {
  return (await kv.get(KEY_ALERTS, 'json')) || { nextId: 1, items: [] };
}

async function saveAlerts(kv, state) {
  await kv.put(KEY_ALERTS, JSON.stringify(state));
}

function errorText(err) {
  return `⚠️ ${escapeHtml(describeDeltaError(err))}`;
}

// ---------------------------------------------------------------- commands

async function cmdPrice(d, args) {
  const symbols = (args.length ? args : ['BTCUSD', 'ETHUSD']).slice(0, 10).map((s) => s.toUpperCase());
  const lines = await Promise.all(
    symbols.map(async (s) => {
      try {
        const t = await d.delta.ticker(s);
        const chg = Number(t.ltp_change_24h ?? t.mark_change_24h);
        const chgText = Number.isFinite(chg) ? ` · 24h ${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%` : '';
        return `<b>${escapeHtml(s)}</b> mark ${formatNumber(t.mark_price)} · last ${formatNumber(t.close)}${chgText}`;
      } catch (err) {
        return `<b>${escapeHtml(s)}</b> — ${escapeHtml(describeDeltaError(err))}`;
      }
    }),
  );
  return lines.join('\n');
}

async function cmdAlert(d, argText) {
  const parsed = parseAlert(argText);
  if (!parsed) return 'Usage: <code>/alert BTCUSD &gt; 65000</code> or <code>/alert ETHUSD &lt; 2500</code>';

  const state = await loadAlerts(d.kv);
  if (state.items.length >= MAX_ALERTS) return `You already have ${MAX_ALERTS} alerts. Delete some with /delalert.`;

  let ticker;
  try {
    ticker = await d.delta.ticker(parsed.symbol);
  } catch (err) {
    return `Couldn't find <b>${escapeHtml(parsed.symbol)}</b>: ${escapeHtml(describeDeltaError(err))}`;
  }
  const mark = Number(ticker.mark_price);
  if (isTriggered(parsed, mark)) {
    return `${escapeHtml(parsed.symbol)} mark price is already ${formatNumber(mark)}, so that alert would fire immediately. Not added.`;
  }

  const alert = { id: state.nextId, ...parsed, createdAt: new Date().toISOString() };
  state.nextId += 1;
  state.items.push(alert);
  await saveAlerts(d.kv, state);
  return `✅ Added alert ${escapeHtml(describeAlert(alert))} (now ${formatNumber(mark)})`;
}

async function cmdAlerts(d) {
  const { items } = await loadAlerts(d.kv);
  if (!items.length) return 'No active alerts. Add one with /alert BTCUSD &gt; 65000';
  return ['<b>Active alerts</b>', ...items.map((a) => escapeHtml(describeAlert(a)))].join('\n');
}

async function cmdDelAlert(d, args) {
  const state = await loadAlerts(d.kv);
  if (args[0]?.toLowerCase() === 'all') {
    const n = state.items.length;
    state.items = [];
    await saveAlerts(d.kv, state);
    return `Deleted ${n} alert(s).`;
  }
  const id = Number(String(args[0] || '').replace(/^#/, ''));
  const before = state.items.length;
  state.items = state.items.filter((a) => a.id !== id);
  if (state.items.length === before) return 'Usage: <code>/delalert 3</code> (see /alerts for ids)';
  await saveAlerts(d.kv, state);
  return `Deleted alert #${id}.`;
}

async function cmdPositions(d) {
  try {
    const positions = (await d.delta.positions()).filter((p) => Number(p.size) !== 0);
    if (!positions.length) return 'No open positions.';
    return [
      '<b>Open positions</b>',
      ...positions.map((p) => {
        const size = Number(p.size);
        const side = size > 0 ? '🟢 LONG' : '🔴 SHORT';
        const parts = [
          `<b>${escapeHtml(p.product_symbol || p.product_id)}</b> ${side} ${Math.abs(size)} @ ${formatNumber(p.entry_price)}`,
        ];
        if (p.liquidation_price) parts.push(`liq ${formatNumber(p.liquidation_price)}`);
        if (p.margin) parts.push(`margin ${formatNumber(p.margin)}`);
        if (p.unrealized_pnl !== undefined) parts.push(`uPnL ${formatNumber(p.unrealized_pnl)}`);
        return parts.join(' · ');
      }),
    ].join('\n');
  } catch (err) {
    return errorText(err);
  }
}

async function cmdBalance(d) {
  try {
    const balances = (await d.delta.balances()).filter((b) => Number(b.balance) !== 0);
    if (!balances.length) return 'All balances are zero.';
    return [
      '<b>Balances</b>',
      ...balances.map(
        (b) =>
          `<b>${escapeHtml(b.asset_symbol || b.asset_id)}</b> ${formatNumber(b.balance)}` +
          (b.available_balance !== undefined ? ` (available ${formatNumber(b.available_balance)})` : ''),
      ),
    ].join('\n');
  } catch (err) {
    return errorText(err);
  }
}

async function runCommand(d, command, args, argText) {
  switch (command) {
    case '/start':
    case '/help':
      return HELP;
    case '/price':
      return cmdPrice(d, args);
    case '/alert':
      return cmdAlert(d, argText);
    case '/alerts':
      return cmdAlerts(d);
    case '/delalert':
      return cmdDelAlert(d, args);
    case '/positions':
      return cmdPositions(d);
    case '/balance':
      return cmdBalance(d);
    default:
      return 'Unknown command. Send /help';
  }
}

export async function handleUpdate(update, env) {
  const msg = update?.message;
  if (!msg || typeof msg.text !== 'string' || !msg.text.startsWith('/')) return;
  const d = deps(env);
  const chatId = String(msg.chat?.id);
  const [head, ...args] = msg.text.trim().split(/\s+/);
  const command = head.split('@')[0].toLowerCase();
  const argText = msg.text.trim().slice(head.length).trim();

  // Anyone may ask for their chat id (needed during setup); everything else
  // is restricted to the configured chat.
  if (command === '/chatid') {
    await d.tg.sendMessage(chatId, `Chat ID: <code>${escapeHtml(chatId)}</code>`);
    return;
  }
  if (!d.chatId || chatId !== d.chatId) return;

  const reply = await runCommand(d, command, args, argText);
  if (reply) await d.tg.sendMessage(chatId, reply);
}

// ---------------------------------------------------------------- cron jobs

export async function checkAlerts(env) {
  const d = deps(env);
  const state = await loadAlerts(d.kv);
  if (!state.items.length) return;

  const symbols = [...new Set(state.items.map((a) => a.symbol))];
  const prices = new Map();
  await Promise.all(
    symbols.map(async (s) => {
      try {
        prices.set(s, Number((await d.delta.ticker(s)).mark_price));
      } catch (err) {
        console.error(`ticker ${s} failed: ${describeDeltaError(err)}`);
      }
    }),
  );

  const fired = state.items.filter((a) => isTriggered(a, prices.get(a.symbol)));
  if (!fired.length) return;

  const sent = new Set();
  for (const a of fired) {
    try {
      await d.tg.sendMessage(
        d.chatId,
        `🔔 <b>${escapeHtml(a.symbol)}</b> mark price ${formatNumber(prices.get(a.symbol))} ` +
          `is ${a.op === '>' ? 'at/above' : 'at/below'} ${formatNumber(a.price)} (alert #${a.id})`,
      );
      sent.add(a.id);
    } catch (err) {
      console.error(String(err));
    }
  }
  if (!sent.size) return;

  // Re-read just before writing so an alert added meanwhile isn't lost.
  const latest = await loadAlerts(d.kv);
  latest.items = latest.items.filter((a) => !sent.has(a.id));
  await saveAlerts(d.kv, latest);
}

function describeFill(f) {
  const side = String(f.side || '').toLowerCase() === 'sell' ? '🔴 SELL' : '🟢 BUY';
  const parts = [`${side} ${f.size} <b>${escapeHtml(f.product_symbol || f.product_id)}</b> @ ${formatNumber(f.price)}`];
  if (f.role) parts.push(escapeHtml(f.role));
  if (f.commission !== undefined && f.commission !== null) parts.push(`fee ${formatNumber(f.commission)}`);
  return `✅ Fill: ${parts.join(' · ')}`;
}

export async function checkFills(env) {
  const d = deps(env);
  if (!d.delta.hasCredentials) return;

  let fills;
  try {
    fills = await d.delta.fills(50);
  } catch (err) {
    // Tell the user once per distinct error instead of every minute.
    const message = describeDeltaError(err);
    if ((await d.kv.get(KEY_FILLS_ERROR)) !== message) {
      await d.tg.sendMessage(d.chatId, `⚠️ Fill notifications paused: ${escapeHtml(message)}`);
      await d.kv.put(KEY_FILLS_ERROR, message);
    }
    return;
  }

  if ((await d.kv.get(KEY_FILLS_ERROR)) !== null) {
    await d.kv.delete(KEY_FILLS_ERROR);
    await d.tg.sendMessage(d.chatId, '✅ Fill notifications are working again.');
  }

  const ids = fills.map((f) => String(f.id));
  const seen = await d.kv.get(KEY_FILLS_SEEN, 'json');
  if (seen === null) {
    // First run: remember existing fills without spamming the chat.
    await d.kv.put(KEY_FILLS_SEEN, JSON.stringify(ids));
    return;
  }

  const seenSet = new Set(seen);
  const fresh = fills.filter((f) => !seenSet.has(String(f.id))).reverse(); // oldest first
  if (!fresh.length) return;

  for (const f of fresh) await d.tg.sendMessage(d.chatId, describeFill(f));
  const merged = [...new Set([...ids, ...seen])].slice(0, SEEN_FILLS_KEEP);
  await d.kv.put(KEY_FILLS_SEEN, JSON.stringify(merged));
}

export async function runScheduled(env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID || !env.STATE) {
    console.error('TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and the STATE KV binding are required');
    return;
  }
  const results = await Promise.allSettled([checkAlerts(env), checkFills(env)]);
  for (const r of results) if (r.status === 'rejected') console.error(String(r.reason?.stack || r.reason));
}

// ---------------------------------------------------------------- HTTP routes

async function handleTelegramWebhook(request, env, ctx) {
  const secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token') || '';
  if (!(await safeEqual(secret, env.TELEGRAM_WEBHOOK_SECRET))) return new Response('forbidden', { status: 403 });
  const update = await request.json().catch(() => null);
  if (update) {
    ctx.waitUntil(handleUpdate(update, env).catch((err) => console.error(String(err?.stack || err))));
  }
  return new Response('ok');
}

async function handleNotify(request, env) {
  if (!(await safeEqual(bearer(request), env.API_TOKEN))) return json({ ok: false, error: 'unauthorized' }, 401);

  let text;
  let parseMode;
  if ((request.headers.get('Content-Type') || '').includes('application/json')) {
    const body = await request.json().catch(() => null);
    text = body?.text;
    parseMode = body?.parse_mode;
  } else {
    text = await request.text();
  }
  if (typeof text !== 'string' || !text.trim()) return json({ ok: false, error: 'text is required' }, 400);

  const { tg, chatId } = deps(env);
  try {
    if (parseMode) await tg.sendMessage(chatId, text, { parse_mode: parseMode });
    else await tg.sendMessage(chatId, escapeHtml(text));
  } catch (err) {
    return json({ ok: false, error: String(err.message || err) }, 502);
  }
  return json({ ok: true });
}

async function handleSetup(request, env) {
  const url = new URL(request.url);
  const token = bearer(request) || url.searchParams.get('token') || '';
  if (!(await safeEqual(token, env.API_TOKEN))) return json({ ok: false, error: 'unauthorized' }, 401);
  if (!env.TELEGRAM_WEBHOOK_SECRET) return json({ ok: false, error: 'TELEGRAM_WEBHOOK_SECRET is not set' }, 500);

  const { tg } = deps(env);
  const webhookUrl = `${url.origin}/telegram`;
  try {
    await tg.call('setWebhook', {
      url: webhookUrl,
      secret_token: env.TELEGRAM_WEBHOOK_SECRET,
      allowed_updates: ['message'],
      drop_pending_updates: true,
    });
    await tg.call('setMyCommands', {
      commands: COMMANDS.map(([command, description]) => ({ command, description })),
    });
  } catch (err) {
    return json({ ok: false, error: String(err.message || err) }, 502);
  }
  return json({ ok: true, webhook: webhookUrl });
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    if (request.method === 'POST' && pathname === '/telegram') return handleTelegramWebhook(request, env, ctx);
    if (request.method === 'POST' && pathname === '/notify') return handleNotify(request, env);
    if (pathname === '/setup') return handleSetup(request, env);
    if (request.method === 'GET' && pathname === '/') {
      return json({
        ok: true,
        telegram: Boolean(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID),
        kv: Boolean(env.STATE),
        fills: Boolean(env.DELTA_API_KEY && env.DELTA_API_SECRET),
      });
    }
    return new Response('not found', { status: 404 });
  },

  async scheduled(controller, env, ctx) {
    ctx.waitUntil(runScheduled(env));
  },
};
