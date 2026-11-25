# Delta Exchange → Telegram notifier (Cloudflare Worker)

A small Cloudflare Worker that watches Delta Exchange and messages you on Telegram. It has no server to manage, and the free plan is enough.

| Feature | How | Needs a Delta API key? |
|---|---|---|
| Price alerts | `/alert BTCUSD > 65000` in Telegram; checked every minute against the mark price | No |
| Live prices | `/price BTCUSD ETHUSD` | No |
| New trade fills | Pushed automatically (polled every minute) | Yes (read-only is enough) |
| Positions / balances | `/positions`, `/balance` | Yes |
| Messages from your own bot | `POST /notify` with a bearer token | No |

## ⚠️ Read this first: Delta API keys and IP whitelisting

Cloudflare Workers make outbound requests from Cloudflare's shared IP ranges, **not a fixed IP**.
If your Delta API key has an IP whitelist, the private features (fills, positions, balance) will fail with
`ip_not_whitelisted_for_api_key`. The bot tells you about this once in Telegram instead of failing silently.

- **Price alerts, `/price` and `/notify` always work**, because they use only public endpoints.
- For fills, positions and balance, create a **read-only** key with **no IP restriction**, if Delta lets you for your account.
- If Delta requires a whitelist, run your trading bot on a static-IP VPS and let it call `/notify` on this Worker. Telegram delivery then still runs on Cloudflare.

**Never give this Worker a key with trading or withdrawal permission.** It only needs read access.

## Setup (about 10 minutes)

You need Node 18+ and a free Cloudflare account.

1. **Create the Telegram bot.** Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. **Install and log in.**
   ```bash
   cd delta-telegram-worker
   npm install
   npx wrangler login
   ```
3. **Create the KV store** (it holds your alerts and the IDs of fills already sent):
   ```bash
   npx wrangler kv namespace create STATE
   ```
   Paste the printed `id` into `wrangler.toml`, replacing `REPLACE_WITH_YOUR_KV_NAMESPACE_ID`.
   If you use Delta global rather than Delta India, also change `DELTA_BASE_URL` to `https://api.delta.exchange`.
4. **Set secrets.** Each command prompts you for the value:
   ```bash
   npx wrangler secret put TELEGRAM_BOT_TOKEN
   npx wrangler secret put TELEGRAM_WEBHOOK_SECRET   # any random string: letters, digits, _ or -
   npx wrangler secret put API_TOKEN                 # another random string
   npx wrangler secret put TELEGRAM_CHAT_ID          # use 0 for now; step 6 gives the real one
   # optional, enables fills/positions/balance:
   npx wrangler secret put DELTA_API_KEY
   npx wrangler secret put DELTA_API_SECRET
   ```
   To generate random strings: `openssl rand -hex 24`
5. **Deploy and connect Telegram.**
   ```bash
   npm run deploy
   curl -H "Authorization: Bearer <API_TOKEN>" https://delta-telegram-worker.<you>.workers.dev/setup
   ```
   You should get `{"ok":true,...}`. You can also open `…/setup?token=<API_TOKEN>` in a browser.
6. **Find your chat ID.** Send `/chatid` to your bot, then set it. The change takes effect right away, with no redeploy:
   ```bash
   npx wrangler secret put TELEGRAM_CHAT_ID
   ```
   The bot answers commands only from this chat. Everyone else is ignored, except for `/chatid`.
7. Send `/help` to the bot.

## Sending notifications from your own trading bot

Any script (Python, Node, an Amibroker/TradingView bridge and so on) can push a message to your chat:

```bash
curl -X POST https://delta-telegram-worker.<you>.workers.dev/notify \
  -H "Authorization: Bearer <API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"text": "BTCUSD long opened @ 60000"}'
```

```python
import requests
requests.post(
    "https://delta-telegram-worker.<you>.workers.dev/notify",
    headers={"Authorization": "Bearer <API_TOKEN>"},
    json={"text": "BTCUSD long opened @ 60000"},
    timeout=10,
)
```

The text is sent as plain text. Add `"parse_mode": "HTML"` to use Telegram formatting.

## Commands

```
/price BTCUSD ETHUSD     mark & last price, 24h change
/alert BTCUSD > 65000    one-shot alert (also <, >=, <=, above, below)
/alerts                  list alerts
/delalert 3              delete alert #3   (/delalert all clears)
/positions               open positions
/balance                 wallet balances
/chatid                  show this chat's id
```

## How it stays within the free plan

- The cron runs every minute (1,440 runs a day; the free limit is 100,000 requests a day).
- KV is written **only when something changes**: an alert fires, a new fill arrives or an error changes. A quiet day uses almost no writes. The free plan allows 1,000 writes a day.
- Up to 30 alerts, so each run stays under the free plan's subrequest limit.

## Limits

- A cron check runs at most once a minute, so a brief spike that reverses within the minute can be missed. For tick-level alerts you need a WebSocket on a Durable Object or a VPS.
- Fill polling picks up the latest 50 fills each minute. More than 50 fills in one minute would be partly skipped.

## Development

```bash
npm test                         # unit tests (Node's built-in runner, no extra deps)
cp .dev.vars.example .dev.vars   # fill in values
npm run dev                      # local Worker; trigger cron with:
curl "http://localhost:8787/__scheduled?cron=*+*+*+*+*"
```
