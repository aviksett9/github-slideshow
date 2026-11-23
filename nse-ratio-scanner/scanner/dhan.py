"""Read-only Dhan margin verification.

Only three Dhan API endpoints can be called, and none of them places, modifies or cancels orders:
  POST /v2/margincalculator/multi   basket margin with hedge benefit
  POST /v2/margincalculator         single-leg margin
  GET  /v2/fundlimit                used only to read the client ID when DHAN_CLIENT_ID is not set
Any other path raises PermissionError before a request is made.

Enable with DHAN_ACCESS_TOKEN (and optionally DHAN_CLIENT_ID) in .env.
"""
import csv, datetime, json, logging, os, time, urllib.request

from . import config

log = logging.getLogger("dhan")
API = "https://api.dhan.co"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
ALLOWED = {("POST", "/v2/margincalculator/multi"), ("POST", "/v2/margincalculator"), ("GET", "/v2/fundlimit")}


class Dhan:
    def __init__(self, token=None, client_id=None):
        self.token = token or os.environ.get("DHAN_ACCESS_TOKEN", "")
        self.client_id = client_id or os.environ.get("DHAN_CLIENT_ID", "")
        self.master = {}
        self.master_date = None

    @property
    def enabled(self):
        return bool(self.token)

    def _req(self, method, path, body=None):
        if (method, path) not in ALLOWED:
            raise PermissionError(f"Dhan endpoint not allowed (read-only module): {method} {path}")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "access-token": self.token, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Dhan {path} HTTP {e.code}: {e.read()[:300].decode(errors='ignore')}")

    def ensure_client_id(self):
        if not self.client_id:
            self.client_id = str(self._req("GET", "/v2/fundlimit").get("dhanClientId", ""))
        return self.client_id

    def load_master(self, cache_dir):
        """Map 'SYMBOL-Mon2026-STRIKE-CE' -> securityId for NSE stock options (refreshed daily)."""
        today = datetime.date.today()
        if self.master and self.master_date == today:
            return
        path = os.path.join(cache_dir, "dhan_scrip_master.csv")
        if not os.path.exists(path) or datetime.date.fromtimestamp(os.path.getmtime(path)) != today:
            req = urllib.request.Request(MASTER_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as r, open(path + ".tmp", "wb") as f:
                f.write(r.read())
            os.replace(path + ".tmp", path)
        m = {}
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if r["SEM_EXM_EXCH_ID"] == "NSE" and r["SEM_INSTRUMENT_NAME"] == "OPTSTK":
                    m[r["SEM_TRADING_SYMBOL"]] = r["SEM_SMST_SECURITY_ID"]
        self.master, self.master_date = m, today
        log.info("Dhan scrip master: %d NSE stock-option contracts", len(m))

    @staticmethod
    def trading_symbol(sym, expiry, K, cp):
        return f"{sym}-{expiry:%b%Y}-{K:g}-{'CE' if cp == 'C' else 'PE'}"

    def basket_margin(self, sym, expiry, cp, legs, lot):
        """legs: [{K, lots(+/-), px}]. Returns Dhan's margin for the basket alone (positions and open orders excluded)."""
        cid = self.ensure_client_id()
        scripts = []
        for l in legs:
            ts = self.trading_symbol(sym, expiry, l["K"], cp)
            sid = self.master.get(ts)
            if not sid:
                raise KeyError(f"{ts} not in Dhan scrip master")
            scripts.append({"exchangeSegment": "NSE_FNO", "transactionType": "BUY" if l["lots"] > 0 else "SELL",
                            "quantity": abs(int(l["lots"])) * int(lot), "productType": "MARGIN",
                            "securityId": str(sid), "price": round(float(l["px"]), 2), "triggerPrice": 0})
        body = {"dhanClientId": cid, "includePosition": False, "includeOrders": False, "includeOrder": False,
                "scripts": scripts, "scripList": scripts}
        r = self._req("POST", "/v2/margincalculator/multi", body)

        def num(*keys):
            for k in keys:
                if r.get(k) not in (None, ""):
                    try:
                        return float(r[k])
                    except (TypeError, ValueError):
                        pass
            return None
        for private in ("userFundLimit", "insufficientFund", "availabelBalance", "clientId"):
            r.pop(private, None)   # never keep account balances
        return {"total": num("total_margin", "totalMargin"), "span": num("span_margin", "spanMargin"),
                "exposure": num("exposure_margin", "exposureMargin", "exposure"), "fo": num("fo_margin", "foMargin"),
                "hedge_benefit": num("hedge_benefit", "hedgeBenefit"), "raw": r,
                "checked_at": config.now_ist().isoformat(timespec="seconds")}
