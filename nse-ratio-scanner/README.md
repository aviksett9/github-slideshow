# NSE ratio-spread scanner

A small server that scans every NSE stock-option chain every 15 minutes during market hours and reports
call and put ratio spreads, butterflies and broken-wing butterflies that pass your rules. Market data comes from
[NseKit-MCP](https://github.com/Prasad1612/NseKit-MCP), and margin comes from NSE Clearing's own SPAN and exposure files.
It never places orders.

## Run it

```bash
cp .env.example .env        # optional: edit thresholds, add Telegram or a webhook
docker compose up -d --build
docker compose logs -f
```

Then open http://localhost:8080 for the latest report, or query the API:

```bash
curl -s localhost:8080/api/alerts
```

The host needs outbound HTTPS to `www.nseindia.in` and `nsearchives.nseindia.com` (and `api.telegram.org` if you use
Telegram). Scan results persist in the `nse-data` Docker volume.

## What a cycle does

Every `SCAN_INTERVAL_MIN` minutes, while NSE's capital market is open:

1. **Live data:** calls `fno_live_option_chain(symbol, expiry)` for each F&O stock (about 218, about 5 minutes).
   Every price used is that snapshot's bid/ask, with NSE's fetch time.
2. **Margin data:** downloads the newest NSE Clearing intraday SPAN file if one has been published since the last cycle.
3. **Pricing:** whole-lot structures (1:2, 1:3, 1:4, 2:3, 2:5, 1:2:1, 1:3:2, 1:3:1, 2:3:1, 1:4:3, 1:4:2, 2:5:3) for calls and puts.
   Each one is priced at ask for buys and bid for sells, with SPAN margin from NSE's 16 risk scenarios, ELM, Dhan charges,
   breakevens, max profit and loss, smile-aware P(profit), and expected value from realised volatility.
4. **Ranking:** defined-risk and uncapped structures are ranked separately. Deep dives cover the top 3, re-checked
   against fresh quotes.
5. **Alerts:** writes the HTML report, `latest.json` and alerts.

The cycle takes about 7 minutes in total. Once a day it also refreshes the F&O list, lot sizes, ban list, corporate events,
the ELM and VaR files, and realised volatility. Volatility comes from the last ~64 `equity_eod_bhavcopy` files, the one
input that is end-of-day by definition.

## Expiry

`TARGET_EXPIRY=auto` (the default) scans the first monthly expiry at least `MIN_DTE` days away (default 7). Today's
expiry is never scanned. On 23-Nov-2026 it scans 29-Dec-2026 and keeps doing so until 22-Dec, then rolls to January.
To pin an expiry, set `TARGET_EXPIRY=29-Dec-2026`.

## When a setup counts as "found"

A setup is found only if it passes every rule below. Each found setup appears in `/api/alerts` on every scan.
Telegram, the webhook and the log announce it only the first time it appears that day.

| Variable | Default | Meaning |
|---|---|---|
| `ALERT_DEFINED_ONLY` | `true` | only capped-risk structures (longs ≥ shorts) |
| `ALERT_MIN_SCORE` | `0.75` | composite percentile score (defined-risk tops out near 0.78) |
| `ALERT_MIN_PPROFIT` | `0.35` | probability of finishing profitable, from the market's IV smile |
| `ALERT_MIN_EV` | `0` | expected net P&L in ₹ after charges, realised-vol model |
| `ALERT_MIN_ROI` | `0.15` | net max profit ÷ total capital |
| `ALERT_MAX_CAPITAL` | `300000` | total capital (SPAN + ELM + debit) |
| `ALERT_TOP_N` | `5` | at most this many setups, one per stock |

## Dhan margin check (optional, read-only)

Set `DHAN_ACCESS_TOKEN` (and optionally `DHAN_CLIENT_ID`) in `.env`. Each cycle, the found setups, shortlists and
top 3 are re-priced by Dhan's basket margin calculator, with your existing positions and orders excluded. Alerts then
report Dhan's figure and say so in `margin_source`. A setup whose Dhan margin exceeds `ALERT_MAX_CAPITAL` is dropped.

`scanner/dhan.py` can call only `POST /v2/margincalculator/multi`, `POST /v2/margincalculator` and
`GET /v2/fundlimit` (used only to read your client ID). Any other path raises an error before a request is sent, and
no order-placing code exists. A Dhan access token can still place orders if misused, so keep `.env` private and revoke
tokens you no longer need. The host also needs `api.dhan.co` and `images.dhan.co` (the instrument list).

Tested on 23-Nov-2026 against Dhan: across 18 shortlisted structures (hedged 2:3:1 and 1:2:1, and uncapped 1:2 and
2:3 ratios), the scanner's NSE estimate was within 0–1.6% of Dhan's figure. The one large gap was a naked short call,
where Dhan charged about 40% more SPAN. Alerts use the Dhan figure whenever a token is set.

For Telegram, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. For your own endpoint, set `WEBHOOK_URL`: each new
setup is POSTed there as JSON.

## API

| Endpoint | Returns |
|---|---|
| `GET /` | latest HTML report (auto-refreshes every 5 minutes) |
| `GET /api/alerts` | setups found in the latest scan, and which are new today |
| `GET /api/latest` | all ranked tables and shortlists as JSON |
| `GET /api/status` | scheduler state, next run, last error, SPAN file in use |
| `POST /api/scan` | run a scan now, even outside market hours (send `X-Scan-Token` if `SCAN_TOKEN` is set) |
| `GET /healthz` | liveness |

## Limits

* **Margin.** Without a Dhan token, margin is an estimate from NSE's SPAN risk arrays and ELM file. With a token, alerted
  and shortlisted setups show Dhan's own figure. Dhan marks these as indicative for the current session, so re-check in
  the basket before trading.
* **Physical settlement.** Stock options settle physically. Delivery margins apply from four days before expiry, so
  close positions before then.
* **Model outputs, not forecasts.** Probabilities and expected values come from the IV smile and realised volatility.
* **Dhan charges are hard-coded** in `scanner/engine.py` from Dhan's tariff as published on 23-Nov-2026. Update them
  if Dhan changes its pricing.
* **Data availability.** NseKit-MCP scrapes NSE's public site. If NSE changes its pages, or blocks your IP, chain
  fetches fail. Failures are listed in the report's skipped section and in `/api/status`.
