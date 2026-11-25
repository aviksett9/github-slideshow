// Price-alert parsing and evaluation (pure functions, easy to test).

export const MAX_ALERTS = 30; // keeps each cron run under the free-plan subrequest limit

const ALERT_RE = /^([A-Za-z0-9_.-]+)\s*(>=|<=|>|<|above|below)\s*([0-9]+(?:\.[0-9]+)?)$/i;

// "BTCUSD > 65000", "ethusd below 2500", "SOLUSD<=150.5"
export function parseAlert(text) {
  const m = String(text || '').trim().match(ALERT_RE);
  if (!m) return null;
  const word = m[2].toLowerCase();
  const op = word === '>' || word === '>=' || word === 'above' ? '>' : '<';
  const price = Number(m[3]);
  if (!Number.isFinite(price) || price <= 0) return null;
  return { symbol: m[1].toUpperCase(), op, price };
}

export function isTriggered(alert, price) {
  if (!Number.isFinite(price)) return false;
  return alert.op === '>' ? price >= alert.price : price <= alert.price;
}

export function formatNumber(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  const abs = Math.abs(n);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 8;
  return n.toLocaleString('en-US', { maximumFractionDigits: digits });
}

export function describeAlert(alert) {
  return `#${alert.id} ${alert.symbol} ${alert.op === '>' ? '≥' : '≤'} ${formatNumber(alert.price)}`;
}
