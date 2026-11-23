"""Decide which setups count as 'found', remember what was already sent today, and notify."""
import datetime, json, logging, urllib.request

from . import config
from .data import jdump, jload, path

log = logging.getLogger("alerts")


def _key(c):
    return f'{c["sym"]}|{c["cp"]}|' + ",".join(f'{l["lots"]:+d}@{l["K"]:g}' for l in c["legs"])


def summarize(c, events):
    side = "CE" if c["cp"] == "C" else "PE"
    legs = [f'{"BUY" if l["lots"] > 0 else "SELL"} {abs(l["lots"])}x{l["K"]:g} {side} @{l["px"]:.2f}' for l in c["legs"]]
    if c["unlimited"]:
        ml = "Unlimited"
    elif not c["defined"]:
        ml = f'{c["maxl"]:,.0f} (if stock -> 0)'
    else:
        ml = f'{c["maxl"]:,.0f}'
    flags = []
    if c["sym"] in events:
        flags.append("event: " + events[c["sym"]])
    if c.get("carry_dev") is not None and abs(c["carry_dev"]) > 0.01:
        flags.append(f'forward {c["carry_dev"]*100:+.1f}% vs carry (dividend/event?)')
    return {
        "key": _key(c), "symbol": c["sym"], "type": side, "ratio": c["ratio"], "legs": legs, "lot": c["lot"],
        "spot": c["spot"], "quote_time": c["fetch"], "net_premium": round(c["prem"], 2),
        "debit_or_credit": "debit" if c["prem"] > 0 else "credit",
        "capital_required": round(c["dhan"]["total"]) if c.get("dhan") else round(c["capital"]),
        "margin_source": (f'Dhan basket margin, checked {c["dhan"]["checked_at"][11:16]} IST' if c.get("dhan")
                          else "NSE SPAN + ELM estimate (unverified)" + (f' – Dhan check failed: {c["dhan_error"]}' if c.get("dhan_error") else "")),
        "nse_estimate": round(c["capital"]),
        "span": round(c["dhan"]["span"]) if c.get("dhan") and c["dhan"].get("span") is not None else round(c["span_req"]),
        "elm": round(c["dhan"]["exposure"]) if c.get("dhan") and c["dhan"].get("exposure") is not None else round(c["elm"]),
        "charges_entry_exit": round(c["charges"]), "max_profit": round(c["maxp"]), "max_profit_at": c["maxp_at"],
        "net_max_profit": round(c["net_maxp"]), "max_loss": ml, "defined_risk": c["defined"],
        "breakevens": [round(b, 2) for b in c["bes"]],
        "net_roi_pct": round(c["net_maxp"] / (c["dhan"]["total"] if c.get("dhan") else c["capital"]) * 100, 1),
        "p_profit_pct": round(c["p_profit_iv"] * 100, 1), "expected_net_pnl": round(c["ev_net"]),
        "score": round(c["score"], 3), "flags": flags,
    }


def select(RK):
    """Candidates passing the alert rules, best score first, one per stock (before any broker margin check)."""
    pool = [c for c in RK["_eligible"]
            if (c["defined"] or not config.ALERT_DEFINED_ONLY)
            and c["score"] >= config.ALERT_MIN_SCORE and c["p_profit_iv"] >= config.ALERT_MIN_PPROFIT
            and c["ev_net"] > config.ALERT_MIN_EV and c["roi_max"] >= config.ALERT_MIN_ROI
            and c["capital"] <= config.ALERT_MAX_CAPITAL]
    out, seen = [], set()
    for c in sorted(pool, key=lambda x: -x["score"]):
        if c["sym"] in seen:
            continue
        seen.add(c["sym"]); out.append(c)
        if len(out) == config.ALERT_TOP_N * 2:   # spare candidates in case a broker check rules some out
            break
    return out


def finalize(cands, events):
    """Drop setups whose verified broker margin exceeds the capital limit; summarise the rest."""
    out = []
    for c in cands:
        if c.get("dhan") and c["dhan"].get("total") and c["dhan"]["total"] > config.ALERT_MAX_CAPITAL:
            continue
        out.append(summarize(c, events))
        if len(out) == config.ALERT_TOP_N:
            break
    return out


def process(found, scan_time):
    """Return the subset not yet sent today; record + notify them."""
    today = scan_time[:10]
    sent = jload("alerts_sent.json", default={})
    sent = {k: v for k, v in sent.items() if v == today}
    new = [a for a in found if a["key"] not in sent]
    for a in new:
        sent[a["key"]] = today
    jdump(sent, "alerts_sent.json")
    if found:
        with open(path("alerts.jsonl"), "a") as f:
            for a in found:
                f.write(json.dumps({"scan_time": scan_time, "new": a in new, **a}) + "\n")
    if new:
        notify(new, scan_time)
    return new


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def notify(new, scan_time):
    for a in new:
        log.info("FOUND %s %s %s | %s | capital %s | max profit %s | max loss %s | P(profit) %s%%",
                 a["symbol"], a["type"], a["ratio"], "; ".join(a["legs"]), a["capital_required"],
                 a["net_max_profit"], a["max_loss"], a["p_profit_pct"])
    if config.WEBHOOK_URL:
        try:
            _post(config.WEBHOOK_URL, {"scan_time": scan_time, "setups": new})
        except Exception as e:
            log.warning("webhook failed: %s", e)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        lines = [f"<b>NSE ratio scan {scan_time[11:16]} IST: {len(new)} new setup(s)</b>"]
        for a in new:
            lines.append(
                f"\n<b>{a['symbol']} {a['type']} {a['ratio']}</b> (spot {a['spot']:,.2f}, quotes {a['quote_time'][-8:]})\n"
                + "\n".join(a["legs"])
                + f"\n{a['debit_or_credit'].title()} ₹{abs(a['net_premium']):,.0f} · margin ₹{a['capital_required']:,} ({'Dhan' if a['margin_source'].startswith('Dhan') else 'est.'})"
                + f"\nMax profit ₹{a['net_max_profit']:,} net · max loss {a['max_loss']}"
                + f"\nBE {', '.join(f'{b:g}' for b in a['breakevens'])} · P(profit) {a['p_profit_pct']}% · ROI {a['net_roi_pct']}%"
                + (f"\n⚠ {'; '.join(a['flags'])}" if a["flags"] else ""))
        lines.append("\nAnalysis only. Verify margin in your broker basket before trading.")
        try:
            _post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                  {"chat_id": config.TELEGRAM_CHAT_ID, "text": "\n".join(lines)[:4000], "parse_mode": "HTML",
                   "disable_web_page_preview": True})
        except Exception as e:
            log.warning("telegram failed: %s", e)
