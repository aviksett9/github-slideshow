"""Score and rank candidates. Defined-risk and uncapped structures are ranked separately."""
import numpy as np

from . import config


def eligible(c):
    if c["capital"] > config.CAP_MAX or c["min_oi"] < config.MIN_OI or c["worst_spr"] > config.MAX_SPR:
        return False
    if c["net_maxp"] <= 0:
        return False
    if not c["defined"] and (c["dang_sig"] is None or c["dang_sig"] < config.MIN_DANGER_SIGMA):
        return False
    return True


def pct_rank(vals):
    v = np.asarray(vals, float)
    return v.argsort().argsort() / max(len(v) - 1, 1)


def top_unique(items, key, n, reverse=True):
    out, seen = [], set()
    for x in sorted(items, key=key, reverse=reverse):
        if x["sym"] in seen:
            continue
        seen.add(x["sym"]); out.append(x)
        if len(out) == n:
            break
    return out


def rank(D):
    groups = {}
    for c in D["candidates"]:
        if eligible(c):
            groups.setdefault((c["cp"], c["defined"]), []).append(c)
    for (cp, dfn), g in groups.items():
        cap = np.array([x["capital"] for x in g])
        roi_ev = np.array([x["ev_net"] for x in g]) / cap
        roi_max = np.array([x["net_maxp"] for x in g]) / cap
        pp = np.array([x["p_profit_iv"] for x in g])
        liq = np.log1p([x["min_oi"] for x in g])
        chr_ = -np.array([x["charges"] / x["maxp"] for x in g])
        if dfn:
            risk = np.array([x["maxl"] for x in g]) / cap
            s = (0.30 * pct_rank(roi_ev) + 0.20 * pct_rank(pp) + 0.20 * pct_rank(roi_max)
                 + 0.15 * pct_rank(risk) + 0.08 * pct_rank(liq) + 0.07 * pct_rank(chr_))
        else:
            dsig = np.array([x["dang_sig"] for x in g])
            stress = np.array([x["stress"] for x in g]) / cap
            s = (0.25 * pct_rank(roi_ev) + 0.15 * pct_rank(pp) + 0.15 * pct_rank(roi_max)
                 + 0.20 * pct_rank(dsig) + 0.15 * pct_rank(stress) + 0.05 * pct_rank(liq) + 0.05 * pct_rank(chr_))
        for x, sc in zip(g, s):
            x["score"] = float(sc)
            worst = abs(x["maxl"]) if dfn else abs(min(x["stress3"], 0))
            x["rar"] = x["ev_net"] / max(worst, 0.10 * x["capital"])

    res = {"now": D["now"], "skipped": D["skipped"], "scanned": D["scanned"],
           "n_candidates": len(D["candidates"]), "n_eligible": sum(len(g) for g in groups.values()), "tables": {}, "lists": {}}
    for cp in ("C", "P"):
        for dfn in (True, False):
            res["tables"][f"{cp}_{'defined' if dfn else 'undefined'}"] = top_unique(groups.get((cp, dfn), []), lambda x: x["score"], 15)
    alle = [x for g in groups.values() for x in g]
    quality = [x for x in alle if x["score"] >= 0.70 and x["p_profit_iv"] >= 0.35 and x["ev_net"] > 0]
    res["lists"]["low_debit"] = top_unique(quality, lambda x: x["prem"], 5, reverse=False)
    res["lists"]["high_profit"] = top_unique(quality, lambda x: x["net_maxp"] / x["capital"], 5)
    res["lists"]["hedged"] = top_unique([x for x in alle if x["defined"]], lambda x: x["score"], 5)
    res["lists"]["overall"] = top_unique(quality, lambda x: x["rar"], 3)
    res["_eligible"] = alle
    return res
