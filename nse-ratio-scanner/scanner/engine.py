"""NSE stock-option ratio-spread engine: prices every whole-lot ratio / butterfly structure on the live chain.

Inputs under config.DATA_DIR:
  cache/oc_<SYM>.json      live option chain from NseKit-MCP (bid/ask/LTP/IV/OI/vol, Fetch_Time, Underlying_Value)
  cache/lots_all.json      NSE lot sizes
  cache/ban_today.json     symbols in F&O ban
  hv.json                  realised volatility (HV20/HV60) from bhavcopies
  span/target_ra.json      NSE Clearing SPAN risk arrays for the target expiry
  elm/ael_latest.csv       NSE exposure-margin (ELM) percentages
"""
import json, math, csv, os, itertools, datetime, sys
import numpy as np

from . import config

BASE = config.DATA_DIR
IST = config.IST
# target expiry; set by set_expiry() before each run
EXPIRY = None          # datetime, 15:30 IST on expiry day
EXPIRY_LABEL = None    # e.g. "29-Dec-2026" (as NSE prints it)
LOT_COL = None         # e.g. "DEC-26" (column in the NSE lot-size file)


def set_expiry(d):
    """d: datetime.date of the target expiry."""
    global EXPIRY, EXPIRY_LABEL, LOT_COL
    EXPIRY = datetime.datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST)
    EXPIRY_LABEL = d.strftime("%d-%b-%Y")
    LOT_COL = d.strftime("%b-%y").upper()
R = 0.07  # risk-free rate used in the NSE SPAN file (<intrRate><val>0.07)

# ---------------- Dhan + statutory charges (dhan.co/pricing, read 23-Nov-2026) ----------------
BROKERAGE = 20.0           # per executed order (also on expired/exercised/assigned contracts)
TXN = 0.0355299 / 100      # NSE options, on premium turnover
STT_SELL = 0.15 / 100      # on sell-side premium
STT_EXER = 0.15 / 100      # on intrinsic value of exercised options
SEBI = 0.0001 / 100        # of turnover
STAMP = 0.003 / 100        # on buy premium
IPFT = 0.0000001 / 100     # of turnover
GST = 0.18                 # on brokerage + txn + SEBI + IPFT
PHYS_BROK = 0.10 / 100     # Dhan brokerage on contract value of physically settled derivatives
DELIV_STT = 0.10 / 100     # equity delivery STT (buy & sell)
DELIV_STAMP = 0.015 / 100  # equity delivery stamp duty (buy)
DELIV_TXN = 0.0030699 / 100


def charges(buy_turn, sell_turn, orders):
    brk = BROKERAGE * orders
    txn = TXN * (buy_turn + sell_turn)
    sebi = SEBI * (buy_turn + sell_turn)
    ipft = IPFT * (buy_turn + sell_turn)
    stt = round(STT_SELL * sell_turn)
    stamp = round(STAMP * buy_turn)
    gst = GST * (brk + txn + sebi + ipft)
    return brk + txn + sebi + ipft + stt + stamp + gst


# ---------------- maths ----------------
def ncdf(x):
    x = np.asarray(x, dtype=float)
    # Abramowitz-Stegun 7.1.26 via erf, |err| < 1.5e-7
    z = np.abs(x) / math.sqrt(2)
    t = 1 / (1 + 0.3275911 * z)
    y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-z * z)
    return 0.5 * (1 + np.sign(x) * y)


def b76(F, K, T, sig, cp):
    """Black-76 on the forward (European, NSE stock options are European)."""
    F = np.asarray(F, float)
    if T <= 0:
        return np.maximum(F - K, 0) if cp == "C" else np.maximum(K - F, 0)
    sig = np.maximum(sig, 1e-4)
    d1 = (np.log(F / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    df = math.exp(-R * T)
    if cp == "C":
        return df * (F * ncdf(d1) - K * ncdf(d2))
    return df * (K * ncdf(-d2) - F * ncdf(-d1))


def implied_vol(price, F, K, T, cp):
    df = math.exp(-R * T)
    intrinsic = df * (max(F - K, 0) if cp == "C" else max(K - F, 0))
    if price <= intrinsic + 1e-6:
        return None
    lo, hi = 0.01, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if float(b76(F, K, T, mid, cp)) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def implied_forward_and_smile(rows, S, T, F_fallback=None):
    """Forward from put-call parity on near-ATM strikes (same snapshot), then an OTM-side IV smile."""
    df = math.exp(-R * T)
    fw = []
    for r in rows:
        K = float(r["Strike_Price"])
        if abs(K / S - 1) > 0.06:
            continue
        cb, ca = r.get("CALLS_Bid_Price") or 0, r.get("CALLS_Ask_Price") or 0
        pb, pa = r.get("PUTS_Bid_Price") or 0, r.get("PUTS_Ask_Price") or 0
        if min(cb, ca, pb, pa) <= 0:
            continue
        if (ca - cb) / ((ca + cb) / 2) > 0.10 or (pa - pb) / ((pa + pb) / 2) > 0.10:
            continue
        fw.append(K + ((ca + cb) / 2 - (pa + pb) / 2) / df)
    if len(fw) >= 2:
        F = float(np.median(fw))
    elif F_fallback:
        F = float(F_fallback)   # NSE Clearing SPAN Dec-futures price scaled to the live spot
    else:
        return None, None, None
    carry_dev = F / (S / df) - 1      # deviation of implied forward from cost-of-carry (dividend/event hint)
    ks, vs = [], []
    for r in rows:
        K = float(r["Strike_Price"])
        side, cp = ("CALLS", "C") if K >= F else ("PUTS", "P")
        b, a = r.get(f"{side}_Bid_Price") or 0, r.get(f"{side}_Ask_Price") or 0
        if b <= 0 or a <= 0 or (a - b) / ((a + b) / 2) > 0.25:
            continue
        iv = implied_vol((a + b) / 2, F, K, T, cp)
        if iv:
            ks.append(K); vs.append(iv)
    if len(ks) < 3:
        return None, None, None
    return F, carry_dev, (np.array(ks), np.array(vs))


# ---------------- data loading ----------------
def load_lots():
    out = {}
    for raw in json.load(open(os.path.join(BASE, "cache/lots_all.json"))):
        r = {k.strip(): str(v).strip() for k, v in raw.items()}
        if r.get(LOT_COL, "").isdigit():
            out[r["SYMBOL"]] = int(r[LOT_COL])
    return out


def load_elm():
    out = {}
    p = os.path.join(BASE, "elm/ael_latest.csv")
    if not os.path.exists(p):
        return out
    for row in csv.DictReader(open(p)):
        out.setdefault(row["Symbol"], {})[row["Instrument Type"]] = float(row["Total applicable ELM%"]) / 100
    return out


_HV = {}


def load_hv(sym):
    if not _HV:
        try:
            _HV.update(json.load(open(os.path.join(BASE, "hv.json"))))
        except (OSError, ValueError):
            _HV["__none__"] = True
    return _HV.get(sym)


def load_chain(sym):
    try:
        d = json.load(open(os.path.join(BASE, f"cache/oc_{sym}.json")))
    except (OSError, ValueError):
        return None, "option chain not fetched this cycle"
    rows = [r for r in d.get("rows", []) if isinstance(r, dict)]
    if not rows:
        return None, "option-chain tool returned no rows"
    if {r.get("Expiry_Date") for r in rows} != {EXPIRY_LABEL}:
        return None, f"{EXPIRY_LABEL} expiry not returned (no contracts listed for it)"
    return rows, None


SPAN_FUT, SPAN_PHY = {}, {}

# ---------------- structures ----------------
TWO_LEG = [(1, 2), (1, 3), (1, 4), (2, 3), (2, 5)]
THREE_LEG = [(1, 2, 1), (1, 3, 2), (1, 3, 1), (2, 3, 1), (1, 4, 3), (1, 4, 2), (2, 5, 3)]
MAX_LOTS_LEG = 5


def scan_symbol(sym, lot, span_opts, elm, hv, now):
    rows, err = load_chain(sym)
    if err:
        return [], err
    S = float(rows[0]["Underlying_Value"])
    fetch = rows[0]["Fetch_Time"]
    T = max((EXPIRY - now).total_seconds(), 0) / (365 * 86400)
    disc = math.exp(-R * T)
    elm_oth = elm.get("OTH", 0.035)
    elm_otm = elm.get("OTM", 0.0525)
    fut, phy = SPAN_FUT.get(sym), SPAN_PHY.get(sym)
    F_fb = fut["p"] * S / phy["p"] if fut and phy else None
    F, carry_dev, smile = implied_forward_and_smile(rows, S, T, F_fb)
    fwd_src = "parity" if F != F_fb else "SPAN futures"
    if F is None:
        return [], "could not infer forward (no two-sided ATM call+put quotes)"
    sig_atm = float(np.interp(F, smile[0], smile[1]))
    sigf = lambda X: np.interp(X, smile[0], smile[1])
    # market-implied (smile-aware) terminal distribution: P(S_T > X) ~= N(d2(X, sigma(X)))
    xg = F * np.exp(np.linspace(-6, 6, 1201) * sig_atm * math.sqrt(T))
    sx = sigf(xg)
    Dx = ncdf((np.log(F / xg) - 0.5 * sx * sx * T) / (sx * math.sqrt(T)))
    Dx = np.minimum.accumulate(Dx)
    ST_mk = np.concatenate([[xg[0]], 0.5 * (xg[1:] + xg[:-1]), [xg[-1]]])
    w_mk = np.concatenate([[1 - Dx[0]], Dx[:-1] - Dx[1:], [Dx[-1]]]); w_mk /= w_mk.sum()
    # real-world: log-normal around the forward with sigma = max(HV20, HV60); falls back to market measure
    if hv:
        sig_rw = max(hv["hv20"], hv["hv60"])
        z = np.linspace(-6, 6, 1201)
        ST_rw = F * np.exp(-0.5 * sig_rw ** 2 * T + sig_rw * math.sqrt(T) * z)
        w_rw = np.exp(-0.5 * z * z); w_rw /= w_rw.sum()
    else:
        sig_rw, ST_rw, w_rw = None, ST_mk, w_mk
    out = []
    for cp, side in (("C", "CALLS"), ("P", "PUTS")):
        legs = []
        for r in rows:
            K = float(r["Strike_Price"])
            bid, ask = r.get(f"{side}_Bid_Price") or 0, r.get(f"{side}_Ask_Price") or 0
            oi, vol = r.get(f"{side}_OI") or 0, r.get(f"{side}_Volume") or 0
            key = f"{cp}{K:g}"
            if bid <= 0 or ask <= 0 or ask < bid or key not in span_opts:
                continue
            mid = (bid + ask) / 2
            spr = (ask - bid) / mid
            if spr > 0.20 or (oi < 20 and vol < 20):
                continue
            iv = implied_vol(mid, F, K, T, cp)
            if iv is None:
                continue
            legs.append({"K": K, "bid": bid, "ask": ask, "ltp": r.get(f"{side}_LTP"), "oi": oi, "vol": vol,
                         "spr": spr, "iv": iv, "nse_iv": r.get(f"{side}_IV"), "ra": span_opts[key]["ra"],
                         "pspan": span_opts[key]["p"]})
        legs.sort(key=lambda x: x["K"], reverse=(cp == "P"))  # walk away from spot in the OTM direction
        if len(legs) < 3:
            continue
        iv_atm = sig_atm
        n = len(legs)
        Ks = np.array([l["K"] for l in legs])
        ask = np.array([l["ask"] for l in legs])
        bid = np.array([l["bid"] for l in legs])
        RA = np.array([l["ra"] for l in legs])           # loss per share for a LONG position, 16 scenarios
        PS = np.array([l["pspan"] for l in legs])
        # evaluation grid: every strike in the chain (all kinks) + extremes
        allK = sorted({float(r["Strike_Price"]) for r in rows})
        grid = np.array(sorted(set([0.0] + allK + [S, max(allK) * 1.6, S * 3])))
        I = np.maximum(grid[None, :] - Ks[:, None], 0) if cp == "C" else np.maximum(Ks[:, None] - grid[None, :], 0)
        ST_iv, w_iv = ST_mk, w_mk
        I_iv = np.maximum(ST_iv[None, :] - Ks[:, None], 0) if cp == "C" else np.maximum(Ks[:, None] - ST_iv[None, :], 0)
        I_rw = np.maximum(ST_rw[None, :] - Ks[:, None], 0) if cp == "C" else np.maximum(Ks[:, None] - ST_rw[None, :], 0)

        # moneyness window for each leg role (in the OTM direction)
        def mny(k):
            return k / S - 1 if cp == "C" else 1 - k / S
        M = np.array([mny(k) for k in Ks])
        i1s = [i for i in range(n) if -0.10 <= M[i] <= 0.12]
        combos = []
        for i in i1s:
            for j in range(i + 1, n):
                if M[j] > 0.30:
                    break
                for (a, b) in TWO_LEG:
                    combos.append(((i, a), (j, -b), None))
                for k in range(j + 1, n):
                    if M[k] > 0.40:
                        break
                    for (a, b, c) in THREE_LEG:
                        combos.append(((i, a), (j, -b), (k, c)))
        if not combos:
            continue
        L = len(combos)
        idx = np.zeros((L, 3), int); q = np.zeros((L, 3))
        for m, cmb in enumerate(combos):
            for p, leg in enumerate(cmb):
                if leg is not None:
                    idx[m, p], q[m, p] = leg
        qs = q * lot                                   # signed shares
        px = np.where(q > 0, ask[idx], bid[idx])       # buy at ask, sell at bid
        prem = (qs * px).sum(1)                        # +debit / -credit (Rs)
        pnl = (qs[:, :, None] * I[idx]).sum(1) - prem[:, None]           # expiry P&L on grid
        # SPAN scan risk (worst of 16 NSE scenarios), net option value, ELM on short legs
        scan = np.maximum((qs[:, :, None] * RA[idx]).sum(1).max(1), 0)
        nov = (qs * PS[idx]).sum(1)
        span_req = np.maximum(scan - nov, 0)
        short_m = np.where(q < 0, M[idx], 0)
        elm_rate = np.where(np.abs(short_m) > 0.30, elm_otm, elm_oth)
        # ELM notional uses the previous close (matches NSE/Dhan exposure figures), falling back to live spot
        Pn = float((hv or {}).get("prev_close") or S)
        elm_amt = (np.where(q < 0, -qs, 0) * Pn * elm_rate).sum(1)
        margin = span_req + elm_amt                    # SPAN (scan risk net of option value) + ELM
        long_prem = (np.where(q > 0, qs, 0) * px).sum(1)
        # capital as the broker blocks it: SPAN + ELM + full premium of the buy legs
        # (verified against Dhan's basket margin calculator: short-leg premium is not netted off)
        capital = margin + long_prem
        peak = capital
        # tails
        net_q = q.sum(1)
        if cp == "C":
            unlimited = net_q < 0
            tail_loss_at0 = pnl[:, 0]
        else:
            unlimited = np.zeros(L, bool)
            tail_loss_at0 = pnl[:, 0]                  # P&L if stock goes to zero
        defined = net_q >= 0
        maxp = pnl.max(1)
        argmaxp = grid[pnl.argmax(1)]
        minl = pnl.min(1)
        # probabilities / EV
        pnl_iv = (qs[:, :, None] * I_iv[idx]).sum(1) - prem[:, None]
        pnl_rw = (qs[:, :, None] * I_rw[idx]).sum(1) - prem[:, None]
        p_profit_iv = ((pnl_iv > 0) * w_iv).sum(1)
        p_profit_rw = ((pnl_rw > 0) * w_rw).sum(1)
        ev_rw = (pnl_rw * w_rw).sum(1)
        ev_iv = (pnl_iv * w_iv).sum(1)
        # stress: 2.5 sigma adverse move at expiry, sigma = max(ATM IV, realised, smile IV 15% OTM on that side)
        tail_iv = float(sigf(F * (1.15 if cp == "C" else 0.85)))
        sig_st = max(iv_atm, sig_rw or 0, tail_iv)
        sdev = sig_st * math.sqrt(T)
        S_stress = F * math.exp(2.5 * sdev) if cp == "C" else F * math.exp(-2.5 * sdev)
        I_st = np.maximum(S_stress - Ks, 0) if cp == "C" else np.maximum(Ks - S_stress, 0)
        stress = (qs * I_st[idx]).sum(1) - prem
        S_st3 = F * math.exp(3 * sdev) if cp == "C" else F * math.exp(-3 * sdev)
        I_st3 = np.maximum(S_st3 - Ks, 0) if cp == "C" else np.maximum(Ks - S_st3, 0)
        stress3 = (qs * I_st3[idx]).sum(1) - prem
        # vectorised pre-filter: keep the best few hundred per symbol/side on several criteria
        ok = (np.abs(q).max(1) <= MAX_LOTS_LEG) & (maxp > 0) & (capital > 0)
        cap_safe = np.where(capital > 0, capital, np.inf)
        keep = set()
        def top(metric, k, mask=ok):
            cand = np.where(mask)[0]
            if len(cand):
                keep.update(cand[np.argsort(-metric[cand])[:k]].tolist())
        top(maxp / cap_safe, 60)
        top(ev_rw / cap_safe, 80)
        top(p_profit_iv, 40)
        top(-prem, 40)
        top(ev_rw / cap_safe, 60, ok & defined)
        top(maxp / cap_safe, 40, ok & defined)
        top(stress / cap_safe, 40, ok & ~defined)
        for m in sorted(keep):
            nlegs = 3 if combos[m][2] else 2
            # breakevens (exact for piecewise-linear payoff)
            y = pnl[m]
            bes = []
            for g in range(len(grid) - 1):
                if (y[g] <= 0 < y[g + 1]) or (y[g] > 0 >= y[g + 1]):
                    x0, x1 = grid[g], grid[g + 1]
                    bes.append(round(x0 + (0 - y[g]) * (x1 - x0) / (y[g + 1] - y[g]), 2))
            if cp == "C" and unlimited[m]:
                dang = max([b for b in bes if b > argmaxp[m]], default=None)
            elif cp == "P" and not defined[m]:
                dang = min([b for b in bes if b < argmaxp[m]], default=None)
            else:
                dang = None
            buy_turn = float(sum(qs[m, p] * px[m, p] for p in range(nlegs) if q[m, p] > 0))
            sell_turn = float(sum(-qs[m, p] * px[m, p] for p in range(nlegs) if q[m, p] < 0))
            entry_ch = charges(buy_turn, sell_turn, nlegs)
            # normal exit: square off every leg at intrinsic near expiry at the max-profit price
            Sx = argmaxp[m]
            exit_buy = exit_sell = 0.0
            exit_orders = 0
            for p in range(nlegs):
                K = Ks[idx[m, p]]
                iv_ = max(Sx - K, 0) if cp == "C" else max(K - Sx, 0)
                if iv_ <= 0:
                    continue  # worthless leg left to expire (still Rs20 brokerage on expired contract)
                exit_orders += 1
                if q[m, p] > 0:
                    exit_sell += qs[m, p] * iv_
                else:
                    exit_buy += -qs[m, p] * iv_
            expired_legs = nlegs - exit_orders
            exit_ch = charges(exit_buy, exit_sell, exit_orders) + expired_legs * BROKERAGE * (1 + GST)
            tot_ch = entry_ch + exit_ch
            net_maxp = maxp[m] - tot_ch
            ev_net = ev_rw[m] - entry_ch - exit_ch
            liq = min(legs[idx[m, p]]["oi"] for p in range(nlegs))
            worst_spr = max(legs[idx[m, p]]["spr"] for p in range(nlegs))
            out.append({
                "sym": sym, "cp": cp, "spot": S, "fetch": fetch, "lot": lot, "T": T,
                "ratio": ":".join(str(int(abs(q[m, p]))) for p in range(nlegs)),
                "legs": [{"K": float(Ks[idx[m, p]]), "lots": int(q[m, p]), "px": float(px[m, p]),
                          "bid": legs[idx[m, p]]["bid"], "ask": legs[idx[m, p]]["ask"], "ltp": legs[idx[m, p]]["ltp"],
                          "oi": legs[idx[m, p]]["oi"], "vol": legs[idx[m, p]]["vol"], "iv": round(legs[idx[m, p]]["iv"] * 100, 2),
                          "nse_iv": legs[idx[m, p]]["nse_iv"]} for p in range(nlegs)],
                "prem": float(prem[m]), "scan": float(scan[m]), "nov": float(nov[m]), "span_req": float(span_req[m]),
                "elm": float(elm_amt[m]), "margin": float(margin[m]), "capital": float(capital[m]), "peak": float(peak[m]),
                "long_prem": float(long_prem[m]), "entry_ch": entry_ch, "exit_ch": exit_ch, "charges": tot_ch,
                "maxp": float(maxp[m]), "maxp_at": float(argmaxp[m]), "net_maxp": float(net_maxp),
                "maxl": None if unlimited[m] else float(minl[m]), "unlimited": bool(unlimited[m]), "defined": bool(defined[m]),
                "loss_at0": float(tail_loss_at0[m]), "bes": bes, "dang_be": dang,
                "dang_dist": (abs(dang / S - 1) if dang else None),
                "dang_sig": (abs(math.log(dang / F)) / (float(sigf(dang)) * math.sqrt(T)) if dang else None),
                "F": F, "carry_dev": carry_dev, "fwd_src": fwd_src,
                "p_profit_iv": float(p_profit_iv[m]), "p_profit_rw": float(p_profit_rw[m]),
                "ev_rw": float(ev_rw[m]), "ev_iv": float(ev_iv[m]), "ev_net": float(ev_net),
                "stress": float(stress[m]), "S_stress": S_stress, "stress3": float(stress3[m]), "S_stress3": S_st3, "iv_atm": iv_atm, "sig_rw": sig_rw,
                "roi_max": float(net_maxp / capital[m]) if capital[m] > 0 else None,
                "roi_ev": float(ev_net / capital[m]) if capital[m] > 0 else None,
                "min_oi": liq, "worst_spr": worst_spr,
            })
    return out, None


def run(now):
    """Price every structure for every symbol. Returns {now, candidates, skipped, scanned}."""
    _HV.clear()
    syms = json.load(open(os.path.join(BASE, "cache/fno_list.json")))
    lots = load_lots(); elm = load_elm(); span = json.load(open(os.path.join(BASE, "span/target_ra.json")))
    SPAN_FUT.clear(); SPAN_PHY.clear()
    SPAN_FUT.update(span["fut"]); SPAN_PHY.update(span["phy"])
    banned = set(json.load(open(os.path.join(BASE, "cache/ban_today.json"))))
    allc, skipped, scanned = [], {}, []
    for s in syms:
        if s in banned:
            skipped[s] = "in NSE F&O ban period (no fresh positions)"; continue
        if s not in lots:
            skipped[s] = f"no {LOT_COL} lot size (no contracts for this expiry)"; continue
        if s not in span["opt"]:
            skipped[s] = "no SPAN risk arrays for this expiry"; continue
        c, err = scan_symbol(s, lots[s], span["opt"][s], elm.get(s, {}), load_hv(s), now)
        if err:
            skipped[s] = err; continue
        if not c:
            skipped[s] = "no liquid strike set (bid/ask, spread<=20%, OI/vol>=20) for any valid structure"; continue
        scanned.append(s)
        allc.extend(c)
    return {"now": now.isoformat(), "candidates": allc, "skipped": skipped, "scanned": scanned}
