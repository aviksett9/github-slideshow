"""Runtime configuration, read from environment variables (see .env.example)."""
import os
import datetime

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _f(name, default):
    return float(os.environ.get(name, default))


def _b(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8080"))

# Expiry: "auto" = first monthly stock-option expiry with at least MIN_DTE days left (never today's expiry),
# or a fixed date like 29-Dec-2026 (falls back to auto once that date is within MIN_DTE days)
TARGET_EXPIRY = os.environ.get("TARGET_EXPIRY", "auto")
MIN_DTE = max(1, int(os.environ.get("MIN_DTE", "7")))

# Scheduler
INTERVAL_MIN = int(os.environ.get("SCAN_INTERVAL_MIN", "15"))
MARKET_HOURS_ONLY = _b("MARKET_HOURS_ONLY", True)   # skip scans when NSE capital market is closed
RUN_ON_START = _b("RUN_ON_START", True)
SCAN_TOKEN = os.environ.get("SCAN_TOKEN", "")      # if set, POST /api/scan needs header X-Scan-Token
NSEKIT_CMD = os.environ.get("NSEKIT_CMD", "nsekit-mcp").split()

# Ranking filters
CAP_MAX = _f("CAP_MAX", 500000)          # max total capital per structure (Rs)
MIN_OI = _f("MIN_OI", 50)                # min OI (contracts) on every leg
MAX_SPR = _f("MAX_SPR", 0.10)            # max bid/ask spread as fraction of mid, every leg
MIN_DANGER_SIGMA = _f("MIN_DANGER_SIGMA", 1.0)  # uncapped structures: dangerous breakeven >= this many sigma away

# Alerts: a setup is "found" when it passes all of these
ALERT_DEFINED_ONLY = _b("ALERT_DEFINED_ONLY", True)
ALERT_MIN_SCORE = _f("ALERT_MIN_SCORE", 0.75)   # composite percentile score; defined-risk tops out ~0.78
ALERT_MIN_PPROFIT = _f("ALERT_MIN_PPROFIT", 0.35)
ALERT_MIN_EV = _f("ALERT_MIN_EV", 0)              # expected net P&L (Rs), realised-vol model
ALERT_MIN_ROI = _f("ALERT_MIN_ROI", 0.15)         # net max profit / capital
ALERT_MAX_CAPITAL = _f("ALERT_MAX_CAPITAL", 300000)
ALERT_TOP_N = int(os.environ.get("ALERT_TOP_N", "5"))

# Optional notification channels
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")


def now_ist():
    return datetime.datetime.now(IST)
