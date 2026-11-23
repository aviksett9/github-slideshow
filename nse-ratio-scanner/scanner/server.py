"""HTTP server + 15-minute scheduler.

GET  /              latest HTML report
GET  /api/alerts    setups that passed the alert rules in the latest scan (+ which are new today)
GET  /api/latest    latest ranked tables and shortlists (JSON)
GET  /api/status    scheduler state, data timestamps, last error
GET  /healthz       liveness
POST /api/scan      run a scan now (header X-Scan-Token if SCAN_TOKEN is set)
"""
import datetime, json, logging, os, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, data, engine as E, alerts
from .rank import rank
from .detail import run_details
from .report import render, page
from .mcpclient import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

STATUS = {"state": "starting", "last_run": None, "last_ok": None, "last_error": None, "next_run": None,
          "last_skip_reason": None, "cycles": 0, "found": 0}
RUN_LOCK = threading.Lock()
TRIGGER = threading.Event()
CLI = Client("scan")


def _strip(c):
    return {k: v for k, v in c.items() if k not in ("T",)}


def build_meta(RK, DT, today, exp_date, mkt, span_state):
    fno = data.jload("cache", "fno_list.json", default=[])
    events = {}
    for r in data.jload("cache", "board_meetings.json", default=[]) or []:
        if isinstance(r, dict) and r.get("bm_symbol") in fno:
            events.setdefault(r["bm_symbol"], set()).add(f'{r.get("bm_purpose")} {r.get("bm_date")}')
    for r in data.jload("cache", "corp_actions.json", default=[]) or []:
        if isinstance(r, dict) and r.get("SYMBOL") in fno:
            events.setdefault(r["SYMBOL"], set()).add(f'{r.get("PURPOSE")} ex {r.get("EX-DATE")}')
    events = {k: "; ".join(sorted(v)) for k, v in events.items()}
    extra = {}
    p = data.path("elm", "ael_latest.csv")
    if os.path.exists(p):
        import csv
        for row in csv.DictReader(open(p)):
            col = next((c for c in row if c.startswith("Additional ELM")), None)
            if col and float(row[col] or 0) > 0:
                extra[row["Symbol"]] = float(row[col])
    times = []
    for s in RK["scanned"]:
        rows, _ = E.load_chain(s)
        if rows:
            times.append(datetime.datetime.strptime(rows[0]["Fetch_Time"], "%d-%b-%Y %H:%M:%S"))
    hv = data.jload("hv.json", default={})
    cr = span_state.get("created") or ""
    return {
        "title": f"{exp_date:%b-%y} Ratio Spread Scan", "expiry": exp_date.strftime("%d-%b-%Y"),
        "scan_date": today.strftime("%d-%b-%Y"), "close_by": (exp_date - datetime.timedelta(days=7)).strftime("%d-%b-%Y"),
        "mkt": mkt or "unknown", "n_universe": len(fno),
        "fetch_lo": min(times).strftime("%H:%M:%S") if times else "–", "fetch_hi": max(times).strftime("%H:%M:%S") if times else "–",
        "span_created": f"{cr[6:8]}-{cr[4:6]}-{cr[:4]} {cr[8:10]}:{cr[10:12]}" if len(cr) >= 12 else "–",
        "hv_cov": sum(1 for s in RK["scanned"] if s in hv), "events": events, "extra_elm": extra,
    }


def verify(DT, expiry_label):
    """Re-fetch the chain for each deep-dive symbol and attach fresh bid/ask to its legs."""
    vt = None
    for d in DT.values():
        c = d["cand"]
        try:
            rows = CLI.call("fno_live_option_chain", {"symbol": c["sym"], "expiry": expiry_label}, timeout=60)
        except Exception as e:
            log.warning("verify %s failed: %s", c["sym"], e); continue
        side = "CALLS" if c["cp"] == "C" else "PUTS"
        vt = rows[0].get("Fetch_Time") if rows else vt
        d["dd"]["verify_time"] = vt
        for q in d["dd"]["leg_quotes"]:
            m = next((r for r in rows if float(r["Strike_Price"]) == q["K"]), None)
            if m:
                q["v_bid"], q["v_ask"] = m[f"{side}_Bid_Price"], m[f"{side}_Ask_Price"]
    return vt


def run_cycle(force=False):
    if not RUN_LOCK.acquire(blocking=False):
        log.info("previous scan still running; skipping this tick")
        return
    t0 = time.time()
    try:
        STATUS.update(state="running", last_run=config.now_ist().isoformat(timespec="seconds"))
        now = config.now_ist()
        today = now.date()
        is_open, mrow = data.market_open(CLI)
        mkt = f'{mrow.get("marketStatus")} ({mrow.get("tradeDate")})' if mrow else "unknown"
        if config.MARKET_HOURS_ONLY and not is_open and not force:
            STATUS.update(state="idle", last_skip_reason=f"market not open: {mkt}")
            log.info("market not open (%s); scan skipped", mkt)
            return
        data.refresh_daily(CLI, today)
        exp_date, exp_label = data.pick_expiry(data.jload("cache", "expiries.json", default=[]), today)
        E.set_expiry(exp_date)
        span_state = data.refresh_span(today, exp_date.strftime("%Y%m%d"))
        fno = data.jload("cache", "fno_list.json")
        ok, bad = data.fetch_chains(CLI, fno, E.EXPIRY_LABEL)
        log.info("chains: %d ok, %d failed (%.0fs)", ok, len(bad), time.time() - t0)
        scan_now = config.now_ist()
        D = E.run(scan_now)
        for s, err in bad.items():
            D["skipped"][s] = f"option-chain fetch failed: {err}"
        RK = rank(D)
        DT = run_details(RK, scan_now)
        vt = verify(DT, E.EXPIRY_LABEL)
        meta = build_meta(RK, DT, today, exp_date, mkt, span_state)
        meta["verify_time"] = vt or "–"
        html_body = render(RK, DT, meta)
        with open(data.path("report", "index.html"), "w", encoding="utf-8") as f:
            f.write(page(html_body))
        found = alerts.find(RK, meta["events"])
        new = alerts.process(found, scan_now.isoformat(timespec="seconds"))
        latest = {"scan_time": scan_now.isoformat(timespec="seconds"), "expiry": E.EXPIRY_LABEL, "market": mkt,
                  "span_file": span_state.get("file"), "scanned": len(RK["scanned"]), "skipped": RK["skipped"],
                  "found": found, "new_today": [a["key"] for a in new],
                  "tables": {k: [alerts.summarize(c, meta["events"]) for c in v] for k, v in RK["tables"].items()},
                  "lists": {k: [alerts.summarize(c, meta["events"]) for c in v] for k, v in RK["lists"].items()}}
        data.jdump(latest, "latest.json")
        STATUS.update(state="idle", last_ok=latest["scan_time"], last_error=None, cycles=STATUS["cycles"] + 1,
                      found=len(found), last_skip_reason=None, last_duration_s=round(time.time() - t0),
                      expiry=E.EXPIRY_LABEL, span_file=span_state.get("file"), scanned=len(RK["scanned"]))
        log.info("scan done in %.0fs: %d scanned, %d eligible, %d found, %d new", time.time() - t0,
                 len(RK["scanned"]), RK["n_eligible"], len(found), len(new))
    except Exception as e:
        STATUS.update(state="error", last_error=f"{type(e).__name__}: {e}")
        log.error("scan failed: %s\n%s", e, traceback.format_exc())
    finally:
        RUN_LOCK.release()


def scheduler():
    if config.RUN_ON_START:
        run_cycle(force=True)
    step = config.INTERVAL_MIN * 60
    while True:
        now = time.time()
        nxt = (int(now // step) + 1) * step
        STATUS["next_run"] = datetime.datetime.fromtimestamp(nxt, config.IST).isoformat(timespec="seconds")
        manual = TRIGGER.wait(timeout=max(1, nxt - now))
        TRIGGER.clear()
        run_cycle(force=manual)


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/healthz":
            return self._send(200, '{"ok": true}')
        if p == "/api/status":
            return self._send(200, json.dumps(STATUS, indent=1))
        if p in ("/api/latest", "/api/alerts"):
            lt = data.jload("latest.json")
            if lt is None:
                return self._send(503, json.dumps({"error": "no completed scan yet", "status": STATUS}))
            if p == "/api/alerts":
                lt = {k: lt[k] for k in ("scan_time", "expiry", "market", "found", "new_today")}
            return self._send(200, json.dumps(lt, indent=1, default=str))
        if p in ("/", "/index.html"):
            f = data.path("report", "index.html")
            if os.path.exists(f):
                return self._send(200, open(f, encoding="utf-8").read(), "text/html")
            return self._send(200, f"<p style='font-family:sans-serif'>No completed scan yet. Status: {STATUS['state']}. "
                                   f"This page refreshes after the first scan.</p><meta http-equiv='refresh' content='60'>", "text/html")
        self._send(404, '{"error": "not found"}')

    def do_POST(self):
        if self.path.split("?")[0] != "/api/scan":
            return self._send(404, '{"error": "not found"}')
        if config.SCAN_TOKEN and self.headers.get("X-Scan-Token") != config.SCAN_TOKEN:
            return self._send(403, '{"error": "bad or missing X-Scan-Token"}')
        TRIGGER.set()
        self._send(202, '{"queued": true}')


def main():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    threading.Thread(target=scheduler, daemon=True).start()
    log.info("serving on :%d (interval %d min, market-hours-only %s)", config.PORT, config.INTERVAL_MIN, config.MARKET_HOURS_ONLY)
    ThreadingHTTPServer(("0.0.0.0", config.PORT), H).serve_forever()


if __name__ == "__main__":
    main()
