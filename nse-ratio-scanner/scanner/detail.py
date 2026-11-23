"""Deep-dive analytics for finalists: payoff grids, pre-expiry sims, adjustments, cost paths, delivery margin."""
import json, math, os, datetime
import numpy as np

from . import engine as E

BASE = E.BASE
SPAN, ELM, VAR = {}, {}, {}


def load_refs():
    """(Re)load SPAN arrays, ELM and cash VaR rates; called once per cycle."""
    SPAN.clear(); ELM.clear(); VAR.clear()
    SPAN.update(json.load(open(os.path.join(BASE, "span/target_ra.json"))))
    ELM.update(E.load_elm())
    p = os.path.join(BASE, "var/var_latest.dat")
    if os.path.exists(p):
        for line in open(p):
            f = line.strip().split(",")
            if len(f) > 9 and f[0] == "20" and f[2] == "EQ":
                VAR[f[1]] = float(f[9]) / 100


def chain_smile(sym, now):
    rows, _ = E.load_chain(sym)
    S = float(rows[0]["Underlying_Value"])
    T = (E.EXPIRY - now).total_seconds() / (365 * 86400)
    fut, phy = SPAN["fut"].get(sym), SPAN["phy"].get(sym)
    F, cd, sm = E.implied_forward_and_smile(rows, S, T, fut["p"] * S / phy["p"] if fut and phy else None)
    return rows, S, T, F, cd, sm


def quote(rows, K, cp):
    side = "CALLS" if cp == "C" else "PUTS"
    for r in rows:
        if float(r["Strike_Price"]) == K:
            return {"bid": r[f"{side}_Bid_Price"], "ask": r[f"{side}_Ask_Price"], "ltp": r[f"{side}_LTP"],
                    "oi": r[f"{side}_OI"], "vol": r[f"{side}_Volume"], "nse_iv": r[f"{side}_IV"]}
    return None


class Pos:
    """legs: list of dict(K, cp, lots, px)  (lots signed: + long / - short)"""

    def __init__(self, sym, legs, lot, S, F, T, smile):
        self.sym, self.legs, self.lot, self.S, self.F, self.T = sym, legs, lot, S, F, T
        self.sig = lambda K: float(np.interp(K, smile[0], smile[1]))
        self.carry = F / (S * math.exp(E.R * T))

    def prem(self):
        return sum(l["lots"] * self.lot * l["px"] for l in self.legs)

    def expiry(self, ST):
        v = 0.0
        for l in self.legs:
            iv = max(ST - l["K"], 0) if l["cp"] == "C" else max(l["K"] - ST, 0)
            v += l["lots"] * self.lot * iv
        return v - self.prem()

    def mtm(self, St, tau, ivshift=0.0):
        Ft = St * math.exp(E.R * tau) * self.carry
        v = 0.0
        for l in self.legs:
            v += l["lots"] * self.lot * float(E.b76(Ft, l["K"], tau, self.sig(l["K"]) + ivshift, l["cp"]))
        return v - self.prem()

    def payoff_stats(self):
        ks = sorted({l["K"] for l in self.legs})
        grid = sorted(set([0.0] + ks + [self.S, ks[-1] * 1.6, self.S * 3] + list(np.linspace(0, self.S * 2, 401))))
        y = [self.expiry(g) for g in grid]
        bes = []
        for i in range(len(grid) - 1):
            if (y[i] <= 0 < y[i + 1]) or (y[i] > 0 >= y[i + 1]):
                bes.append(round(grid[i] + (0 - y[i]) * (grid[i + 1] - grid[i]) / (y[i + 1] - y[i]), 2))
        calls_net = sum(l["lots"] for l in self.legs if l["cp"] == "C")
        unlimited = calls_net < 0
        imax = int(np.argmax(y))
        return {"bes": bes, "maxp": max(y), "maxp_at": grid[imax], "maxl": None if unlimited else min(y),
                "unlimited": unlimited, "loss_at0": y[0]}

    def margin(self):
        ra = SPAN["opt"][self.sym]
        qs = [(l["lots"] * self.lot, ra[f'{l["cp"]}{l["K"]:g}']) for l in self.legs]
        scen = [sum(q * r["ra"][s] for q, r in qs) for s in range(16)]
        scan = max(max(scen), 0)
        nov = sum(q * r["p"] for q, r in qs)
        span_req = max(scan - nov, 0)
        e = ELM.get(self.sym, {})
        pn = (E.load_hv(self.sym) or {}).get("prev_close") or self.S     # ELM notional: previous close
        elm = sum(-l["lots"] * self.lot * pn * (e.get("OTM", .0525) if abs(l["K"] / self.S - 1) > .30 else e.get("OTH", .035))
                  for l in self.legs if l["lots"] < 0)
        prem = self.prem()
        long_prem = sum(l["lots"] * self.lot * l["px"] for l in self.legs if l["lots"] > 0)
        capital = span_req + elm + long_prem      # as the broker blocks it (buy premium in full)
        return {"scan": scan, "nov": nov, "span_req": span_req, "elm": elm, "margin": span_req + elm,
                "prem": prem, "capital": capital, "peak": capital, "long_prem": long_prem,
                "scenarios": scen}


def trade_charges(trades):
    """trades: list of (lots_signed_traded, price, lot) -> charges for one batch of orders."""
    buy = sum(abs(q) * lot * p for q, p, lot in trades if q > 0)
    sell = sum(abs(q) * lot * p for q, p, lot in trades if q < 0)
    return E.charges(buy, sell, len([t for t in trades if t[0] != 0]))


def entry_charges(pos):
    return trade_charges([(l["lots"], l["px"], pos.lot) for l in pos.legs])


def exit_at_expiry_squareoff(pos, ST):
    tr, expired = [], 0
    for l in pos.legs:
        iv = max(ST - l["K"], 0) if l["cp"] == "C" else max(l["K"] - ST, 0)
        if iv <= 0:
            expired += 1
        else:
            tr.append((-l["lots"], iv, pos.lot))
    return trade_charges(tr) + expired * E.BROKERAGE * (1 + E.GST)


def hold_through_expiry(pos, ST):
    """Physical settlement of ITM stock options at settlement price ST (Dhan + statutory)."""
    net_shares = 0.0
    stt_ex = 0.0
    brk = 0.0
    for l in pos.legs:
        itm = (ST > l["K"]) if l["cp"] == "C" else (ST < l["K"])
        brk += E.BROKERAGE  # Rs20 on expired/exercised/assigned contracts
        if not itm:
            continue
        q = l["lots"] * pos.lot
        iv = abs(ST - l["K"])
        if l["lots"] > 0:
            stt_ex += E.STT_EXER * abs(q) * iv
        # delivery direction: long call/short put -> receive shares; short call/long put -> deliver shares
        sign = (1 if l["cp"] == "C" else -1) * (1 if l["lots"] > 0 else -1)
        net_shares += sign * abs(q)
        brk += E.PHYS_BROK * abs(q) * ST
    value = abs(net_shares) * ST
    deliv_stt = E.DELIV_STT * value
    stamp = E.DELIV_STAMP * value if net_shares > 0 else 0
    txn = E.DELIV_TXN * value
    gst = E.GST * (brk + txn)
    return {"net_shares": net_shares, "delivery_value": value, "charges": brk + stt_ex + deliv_stt + stamp + txn + gst,
            "cash_or_stock_needed": "cash to take delivery" if net_shares > 0 else ("shares to deliver" if net_shares < 0 else "none")}


def delivery_margin_path(pos, ST):
    """NSE expiry-week physical-settlement margin on ITM positions: E-4 10%, E-3 25%, E-2 45%, E-1 70% of VaR+ELM."""
    rate = VAR.get(pos.sym)
    if rate is None:
        return None
    h = hold_through_expiry(pos, ST)
    base = h["delivery_value"] * rate
    return {"var_elm_rate": rate, "E-4": .10 * base, "E-3": .25 * base, "E-2": .45 * base, "E-1": .70 * base,
            "expiry_settlement_value": h["delivery_value"]}


def adjustments(pos, x, rows):
    """Adjustment plans re-priced with Black-76 on the current smile (sticky strike) at the trigger."""
    cp = pos.legs[0]["cp"]
    shorts = [l for l in pos.legs if l["lots"] < 0]
    longs = [l for l in pos.legs if l["lots"] > 0]
    K2 = shorts[0]["K"]
    excess = -sum(l["lots"] for l in pos.legs)  # net short lots
    tau14 = 14 / 365
    plans = []
    trig = K2  # trigger: spot reaches the short strike with 14 days left
    base_val = pos.mtm(trig, tau14)
    Ft = trig * math.exp(E.R * tau14) * pos.carry
    strikes = sorted({float(r["Strike_Price"]) for r in rows})
    def model_px(K, cpx):
        return float(E.b76(Ft, K, tau14, pos.sig(K), cpx))
    if excess > 0:
        # A) buy back the excess shorts
        p = model_px(K2, cp)
        newlegs = [dict(l) for l in pos.legs] + [{"K": K2, "cp": cp, "lots": excess, "px": p}]
        P = Pos(pos.sym, newlegs, pos.lot, pos.S, pos.F, pos.T, (np.array(strikes), np.array([pos.sig(k) for k in strikes])))
        st = P.payoff_stats()
        plans.append({"name": f"A. Close {excess} excess short lot(s) at {K2:g} when spot hits {trig:g} (14 DTE)",
                      "trade": f"BUY {excess} lot {K2:g}{cp}E @ ~{p:.2f} (model)",
                      "cost": excess * pos.lot * p, "charges": trade_charges([(excess, p, pos.lot)]),
                      "mtm_before": base_val, "after": st, "margin_after": P.margin()})
        # B) add a protective long wing beyond the short strike
        wing_ks = [k for k in strikes if (k > K2 if cp == "C" else k < K2)]
        wing_ks = sorted(wing_ks, key=lambda k: abs(k - K2))
        for K3 in wing_ks[1:3] if len(wing_ks) > 2 else wing_ks[:1]:
            p3 = model_px(K3, cp)
            newlegs = [dict(l) for l in pos.legs] + [{"K": K3, "cp": cp, "lots": excess, "px": p3}]
            P = Pos(pos.sym, newlegs, pos.lot, pos.S, pos.F, pos.T, (np.array(strikes), np.array([pos.sig(k) for k in strikes])))
            st = P.payoff_stats()
            plans.append({"name": f"B. Add {excess} long wing lot(s) at {K3:g} -> defined-risk BWB (trigger {trig:g}, 14 DTE)",
                          "trade": f"BUY {excess} lot {K3:g}{cp}E @ ~{p3:.2f} (model)",
                          "cost": excess * pos.lot * p3, "charges": trade_charges([(excess, p3, pos.lot)]),
                          "mtm_before": base_val, "after": st, "margin_after": P.margin()})
        # C) convert to defined-risk now, at today's live ask
        cand = [k for k in wing_ks if quote(rows, k, cp) and (quote(rows, k, cp)["ask"] or 0) > 0]
        if cand:
            K3 = cand[min(1, len(cand) - 1)]
            p3 = quote(rows, K3, cp)["ask"]
            newlegs = [dict(l) for l in pos.legs] + [{"K": K3, "cp": cp, "lots": excess, "px": p3}]
            P = Pos(pos.sym, newlegs, pos.lot, pos.S, pos.F, pos.T, (np.array(strikes), np.array([pos.sig(k) for k in strikes])))
            if f"{cp}{K3:g}" in SPAN["opt"][pos.sym]:
                plans.append({"name": f"C. Convert to defined risk at entry: add {excess} lot(s) {K3:g}{cp}E at live ask",
                              "trade": f"BUY {excess} lot {K3:g}{cp}E @ {p3:.2f} (live ask)",
                              "cost": excess * pos.lot * p3, "charges": trade_charges([(excess, p3, pos.lot)]),
                              "mtm_before": 0.0, "after": P.payoff_stats(), "margin_after": P.margin()})
    # D) full exit at a risk threshold: spot halfway from short strike to dangerous breakeven, 7 and 14 DTE
    dbe = x.get("dang_be")
    if dbe:
        thr = K2 + (dbe - K2) * 0.5
        for tau in (14, 7):
            v = pos.mtm(thr, tau / 365)
            v_iv = pos.mtm(thr, tau / 365, 0.05)
            Ftt = thr * math.exp(E.R * tau / 365) * pos.carry
            tr = [(-l["lots"], float(E.b76(Ftt, l["K"], tau / 365, pos.sig(l["K"]), l["cp"])), pos.lot) for l in pos.legs]
            plans.append({"name": f"D. Exit everything if spot reaches {thr:.1f} with {tau} DTE",
                          "trade": "Square off all legs", "mtm_before": v, "mtm_iv_up5": v_iv,
                          "charges": trade_charges(tr)})
    else:
        # defined-risk: stop at 50% of max loss
        ml = x.get("maxl")
        if ml:
            plans.append({"name": "D. Exit everything if mark-to-market loss reaches 50% of max loss",
                          "trade": "Square off all legs", "threshold": 0.5 * ml})
    return plans


def deep_dive(x, now):
    rows, S, T, F, cd, sm = chain_smile(x["sym"], now)
    legs = [{"K": l["K"], "cp": x["cp"], "lots": l["lots"], "px": l["px"]} for l in x["legs"]]
    pos = Pos(x["sym"], legs, x["lot"], S, F, T, sm)
    st = pos.payoff_stats()
    mg = pos.margin()
    pct = [-0.20, -0.15, -0.10, -0.05, -0.025, 0, 0.025, 0.05, 0.10, 0.15, 0.20]
    keyS = sorted(set([round(S * (1 + p), 1) for p in pct] + [l["K"] for l in legs] + [b for b in st["bes"]]))
    ent = entry_charges(pos)
    scen = []
    for s_ in keyS:
        scen.append({"S": s_, "chg": s_ / S - 1, "expiry": pos.expiry(s_), "d14": pos.mtm(s_, 14 / 365),
                     "d14_iv5": pos.mtm(s_, 14 / 365, 0.05), "d7": pos.mtm(s_, 7 / 365), "d7_iv5": pos.mtm(s_, 7 / 365, 0.05)})
    Sx = st["maxp_at"]
    cost_paths = {
        "entry": ent,
        "entry_exit": ent + exit_at_expiry_squareoff(pos, Sx),
        "hold_expiry": ent + hold_through_expiry(pos, Sx)["charges"],
    }
    adj = adjustments(pos, x, rows)
    if adj and "charges" in adj[0]:
        cost_paths["entry_adjust_exit"] = ent + adj[0]["charges"] + exit_at_expiry_squareoff(pos, Sx) + E.BROKERAGE * (1 + E.GST)
    return {"S": S, "F": F, "carry_dev": cd, "T": T, "stats": st, "margin": mg, "scenarios": scen,
            "costs": cost_paths, "physical": hold_through_expiry(pos, Sx), "delivery_margin": delivery_margin_path(pos, Sx),
            "adjustments": adj, "leg_quotes": [dict(quote(rows, l["K"], x["cp"]), K=l["K"], lots=l["lots"], px=l["px"],
                                                     iv_model=pos.sig(l["K"])) for l in legs],
            "fetch": rows[0]["Fetch_Time"]}


def run_details(RK, now, keys=("overall",)):
    load_refs()
    out = {}
    for lst in keys:
        for x in RK["lists"][lst]:
            k = f'{x["sym"]}_{x["cp"]}_{x["ratio"]}_{"-".join(f"{l['K']:g}" for l in x["legs"])}'
            if k not in out:
                out[k] = {"cand": x, "dd": deep_dive(x, now)}
    return out
