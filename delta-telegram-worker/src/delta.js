// Minimal Delta Exchange REST client that runs on Cloudflare Workers.
// Private endpoints are signed with HMAC-SHA256 over
//   method + timestamp + path + query_string + body
// and sent with the `api-key`, `timestamp` and `signature` headers.

const encoder = new TextEncoder();

export async function hmacSha256Hex(secret, message) {
  const key = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const sig = await crypto.subtle.sign('HMAC', key, encoder.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// Returns "" or "?a=1&b=2". The leading "?" is part of the signed string.
export function buildQuery(params) {
  if (!params) return '';
  const entries = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => [k, String(v)]);
  return entries.length ? `?${new URLSearchParams(entries)}` : '';
}

export class DeltaError extends Error {
  constructor(code, status, body) {
    super(code);
    this.name = 'DeltaError';
    this.code = code;
    this.status = status;
    this.body = body;
  }
}

export class DeltaClient {
  constructor({ baseUrl, apiKey, apiSecret, now = () => Date.now() }) {
    this.baseUrl = (baseUrl || 'https://api.india.delta.exchange').replace(/\/+$/, '');
    this.apiKey = apiKey || '';
    this.apiSecret = apiSecret || '';
    this.now = now;
  }

  get hasCredentials() {
    return Boolean(this.apiKey && this.apiSecret);
  }

  async request(method, path, { query, body, auth = false } = {}) {
    const qs = buildQuery(query);
    const payload = body === undefined ? '' : JSON.stringify(body);
    const headers = {
      Accept: 'application/json',
      'User-Agent': 'delta-telegram-worker/1.0',
    };
    if (payload) headers['Content-Type'] = 'application/json';

    if (auth) {
      if (!this.hasCredentials) {
        throw new DeltaError('missing_api_credentials', 0, null);
      }
      const timestamp = String(Math.floor(this.now() / 1000));
      headers['api-key'] = this.apiKey;
      headers.timestamp = timestamp;
      headers.signature = await hmacSha256Hex(this.apiSecret, method + timestamp + path + qs + payload);
    }

    const res = await fetch(this.baseUrl + path + qs, {
      method,
      headers,
      body: payload || undefined,
    });

    let data = null;
    try {
      data = await res.json();
    } catch {
      // non-JSON body (e.g. an HTML error page)
    }
    if (!res.ok || !data || data.success === false) {
      const code = data?.error?.code || `http_${res.status}`;
      throw new DeltaError(code, res.status, data);
    }
    return data;
  }

  async ticker(symbol) {
    const data = await this.request('GET', `/v2/tickers/${encodeURIComponent(symbol)}`);
    return data.result;
  }

  async fills(pageSize = 50) {
    const data = await this.request('GET', '/v2/fills', { query: { page_size: pageSize }, auth: true });
    return data.result || [];
  }

  async positions() {
    const data = await this.request('GET', '/v2/positions/margined', { auth: true });
    return data.result || [];
  }

  async balances() {
    const data = await this.request('GET', '/v2/wallet/balances', { auth: true });
    return data.result || [];
  }
}

// Human-friendly explanation for the errors people actually hit.
export function describeDeltaError(err) {
  if (!(err instanceof DeltaError)) return String(err?.message || err);
  switch (err.code) {
    case 'missing_api_credentials':
      return 'DELTA_API_KEY / DELTA_API_SECRET are not set on the worker.';
    case 'ip_not_whitelisted_for_api_key':
      return 'Delta rejected the request: this API key has an IP whitelist, and Cloudflare Workers do not use a fixed IP. Create a read-only key without IP restrictions, or route private calls through a static-IP proxy.';
    case 'invalid_api_key':
      return 'Delta says the API key is invalid (check it was created on the same Delta site as DELTA_BASE_URL).';
    case 'expired_signature':
      return 'Delta says the signature expired (request took more than 5 seconds to arrive).';
    case 'Signature Mismatch':
    case 'signature_mismatch':
      return 'Delta says the signature does not match — check DELTA_API_SECRET.';
    default:
      return `Delta API error: ${err.code}`;
  }
}
