"""Data layer.

Every scan cycle (live, market hours):
  * NseKit-MCP fno_live_option_chain(symbol, expiry) for every F&O stock -> cache/oc_<SYM>.json
  * newest NSE Clearing intraday SPAN file (nsccl.YYYYMMDD.iN.zip) -> span/target_ra.json when a newer one appears
Once per day:
  * NseKit-MCP: F&O universe, lot sizes, ban list, board meetings + corporate actions to expiry, expiry dates
  * NSE exposure-margin (ELM) file and cash-market VaR file
  * NseKit-MCP equity_eod_bhavcopy for any missing past day -> realised volatility (HV20/HV60) in hv.json
"""
import datetime, io, json, logging, math, os, time, urllib.request, zipfile

from . import config
from .parse_span import parse_span

log = logging.getLogger("data")
D = config.DATA_DIR
NSE_ARCH = "https://nsearchives.nseindia.com"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}


def path(*p):
    full = os.path.join(D, *p)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


def jdump(obj, *p):
    tmp = path(*p) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, default=str)
    os.replace(tmp, path(*p))


def jload(*p, default=None):
    try:
        with open(path(*p)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise


def weekdays_back(day, n):
    out, d = [], day
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return out


# ------------------------------------------------------------------ daily ----
def refresh_daily(cli, today):
    st = jload("state_daily.json", default={})
    if st.get("date") == today.isoformat():
        return st
    log.info("daily refresh for %s", today)
    fno = cli.call("list_of_fno_stocks", {"mode": "stocks", "list_only": True})[0]["symbols"]
    jdump(fno, "cache", "fno_list.json")
    lots = cli.call("fno_lot_sizes", {})
    jdump(lots, "cache", "lots_all.json")
    try:
        ban = cli.call("fno_ban_list", {"date": today.strftime("%d-%m-%Y")})
    except Exception as e:  # tool errors on days with no ban file yet
        log.warning("ban list unavailable: %s", e); ban = []
    banned = set()
    for r in ban:
        for v in (r.values() if isinstance(r, dict) else []):
            banned.update(x.strip() for x in str(v).split(",") if x.strip())
    jdump(sorted(banned), "cache", "ban_today.json")
    exp = cli.call("fno_expiry_dates", {"symbol": "SBIN"})
    jdump(exp, "cache", "expiries.json")
    end = pick_expiry(exp, today)[0] if exp else today + datetime.timedelta(days=45)
    for tool, key, args in (("corporate_board_meetings", "board_meetings", {"start_date": today.strftime("%d-%m-%Y"), "end_date": end.strftime("%d-%m-%Y")}),
                            ("corporate_actions", "corp_actions", {"start_date": today.strftime("%d-%m-%Y"), "end_date": end.strftime("%d-%m-%Y")})):
        try:
            jdump(cli.call(tool, args), "cache", key + ".json")
        except Exception as e:
            log.warning("%s failed: %s", tool, e); jdump([], "cache", key + ".json")
    st = {"date": today.isoformat(), "elm": fetch_elm(today), "var": fetch_var(today)}
    update_hv(cli, today, fno)
    jdump(st, "state_daily.json")
    return st


def pick_expiry(exp_rows, today):
    """Return (date, label) of the target monthly expiry. Today's expiry is never scanned."""
    if config.TARGET_EXPIRY.lower() != "auto":
        d = datetime.datetime.strptime(config.TARGET_EXPIRY, "%d-%b-%Y").date()
        if (d - today).days >= config.MIN_DTE:
            return d, d.strftime("%d-%b-%Y")
        log.warning("TARGET_EXPIRY %s is under %d days away; using auto", config.TARGET_EXPIRY, config.MIN_DTE)
    for r in exp_rows:
        d = datetime.datetime.strptime(r["Expiry Date"], "%d-%b-%Y").date()
        if "Month" in str(r.get("Expiry Type", "")) and (d - today).days >= config.MIN_DTE:
            return d, r["Expiry Date"]
    r = exp_rows[-1]
    return datetime.datetime.strptime(r["Expiry Date"], "%d-%b-%Y").date(), r["Expiry Date"]


def fetch_elm(today):
    for d in weekdays_back(today, 6):
        b = http_get(f"{NSE_ARCH}/archives/exp_lim/ael_{d:%d%m%Y}.csv")
        if b and b[:6].startswith(b"Sr No"):
            open(path("elm", "ael_latest.csv"), "wb").write(b)
            return d.isoformat()
    log.warning("ELM file not found; using defaults 3.5%% / 5.25%%")
    return None


def fetch_var(today):
    for d in weekdays_back(today, 6):
        for n in range(6, 0, -1):
            b = http_get(f"{NSE_ARCH}/archives/nsccl/var/C_VAR1_{d:%d%m%Y}_{n}.DAT")
            if b and b[:3] == b"10,":
                open(path("var", "var_latest.dat"), "wb").write(b)
                return f"{d.isoformat()} file {n}"
    return None


def update_hv(cli, today, fno, days=64):
    """Incremental: fetch only bhavcopies not cached yet; compute HV20/HV60 from close vs NSE previous close."""
    fset = set(fno)
    have = 0
    for d in weekdays_back(today - datetime.timedelta(days=1), days):
        key = f"{d:%Y%m%d}"
        if jload("bhav", key + ".json") is not None:
            have += 1
            continue
        try:
            rows = cli.call("equity_eod_bhavcopy", {"date": d.strftime("%d-%m-%Y")}, timeout=120)
            px = {r["TckrSymb"]: [r["ClsPric"], r["PrvsClsgPric"]] for r in rows
                  if isinstance(r, dict) and r.get("SctySrs") == "EQ" and r.get("TckrSymb") in fset}
            jdump(px, "bhav", key + ".json")
        except Exception:
            jdump({}, "bhav", key + ".json")   # holiday / not published: remember it
        have += 1
    files = sorted(f for f in os.listdir(path("bhav", "x")[:-1]) if f.endswith(".json"))[-days:]
    series, last_close = {}, {}
    for f in files:
        for s, (c, p) in (jload("bhav", f) or {}).items():
            if c and p:
                series.setdefault(s, []).append(math.log(c / p))
                last_close[s] = c          # files are in date order, so this ends as the previous close
    hv = {}
    for s, lr in series.items():
        if len(lr) < 22:
            continue
        def sd(x):
            m = sum(x) / len(x)
            return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1)) * math.sqrt(252)
        hv[s] = {"hv20": sd(lr[-20:]), "hv60": sd(lr[-60:]), "n": len(lr), "prev_close": last_close.get(s)}
    jdump(hv, "hv.json")
    log.info("realised vol for %d symbols from %d bhavcopy days", len(hv), len(files))


# ------------------------------------------------------------- per cycle ----
def refresh_span(today, target):
    """Download the freshest NSE Clearing SPAN file if newer than the one parsed; parse risk arrays for `target` expiry."""
    st = jload("state_span.json", default={})
    cands = []
    for i, d in enumerate(weekdays_back(today, 4)):
        tags = (["s"] + [f"i{n}" for n in range(9, 0, -1)]) if i == 0 else ["s"]
        cands += [(d, t) for t in tags]
    for d, t in cands:
        name = f"nsccl.{d:%Y%m%d}.{t}.zip"
        if st.get("file") == name and st.get("target") == target:
            return st          # already have the freshest available
        b = http_get(f"{NSE_ARCH}/archives/nsccl/span/{name}", timeout=180)
        if not b or b[:2] != b"PK":
            continue
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            spn = [n for n in z.namelist() if n.endswith(".spn")][0]
            z.extract(spn, path("span", "raw", "x")[:-1])
        t0 = time.time()
        created = parse_span(path("span", "raw", spn), path("span", "target_ra.json"), target)
        for f in os.listdir(path("span", "raw", "x")[:-1]):
            os.remove(path("span", "raw", f))
        st = {"file": name, "created": created, "target": target}
        jdump(st, "state_span.json")
        log.info("SPAN %s parsed in %.0fs (created %s)", name, time.time() - t0, created)
        return st
    raise RuntimeError("no NSE SPAN file found for the last 4 weekdays")


def market_open(cli):
    rows = cli.call("market_live_status", {})
    for r in rows:
        if isinstance(r, dict) and r.get("market") == "Capital Market":
            return str(r.get("marketStatus", "")).lower() == "open", r
    return False, None


def fetch_chains(cli, symbols, expiry_label):
    ok, bad = 0, {}
    for s in symbols:
        try:
            rows = cli.call("fno_live_option_chain", {"symbol": s, "expiry": expiry_label}, timeout=60)
            jdump({"fetched_at": config.now_ist().isoformat(timespec="seconds"), "rows": rows}, "cache", f"oc_{s}.json")
            ok += 1
        except Exception as e:
            bad[s] = str(e)[:160]
            try:
                os.remove(path("cache", f"oc_{s}.json"))
            except OSError:
                pass
    return ok, bad
