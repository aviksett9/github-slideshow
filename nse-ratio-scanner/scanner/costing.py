"""Per-setup cash flow: what you pay (outflow) and receive (inflow) at entry and at exit, with itemised charges.

Sign convention: inflow > 0, outflow < 0 (Rs). Exit scenarios assume every leg is squared off just before expiry at
its intrinsic value; legs with no intrinsic value are left to expire worthless (Dhan still charges Rs20 brokerage).
"""
from . import engine as E


def _intrinsic(cp, K, S):
    return max(S - K, 0.0) if cp == "C" else max(K - S, 0.0)


def contract(c, K):
    """Full contract name, e.g. 'ICICIBANK 29-Dec-2026 1640 PE'."""
    return f'{c["sym"]} {c.get("expiry") or E.EXPIRY_LABEL} {K:g} {"CE" if c["cp"] == "C" else "PE"}'


def _pnl_at(c, S):
    return sum(l["lots"] * c["lot"] * _intrinsic(c["cp"], l["K"], S) for l in c["legs"]) - c["prem"]


def _exit(c, S, label, entry_cash):
    rows, buy_t, sell_t, orders, expired = [], 0.0, 0.0, 0, 0
    for l in c["legs"]:
        qty = abs(l["lots"]) * c["lot"]
        v = _intrinsic(c["cp"], l["K"], S)
        name = contract(c, l["K"])
        if v <= 0:
            expired += 1
            rows.append({"action": "expires worthless", "contract": name, "qty": qty, "price": 0.0, "flow": 0.0})
            continue
        orders += 1
        if l["lots"] > 0:                       # close a long: sell -> inflow
            sell_t += qty * v
            rows.append({"action": "SELL to close", "contract": name, "qty": qty, "price": round(v, 2), "flow": round(qty * v, 2)})
        else:                                    # close a short: buy back -> outflow
            buy_t += qty * v
            rows.append({"action": "BUY to close", "contract": name, "qty": qty, "price": round(v, 2), "flow": round(-qty * v, 2)})
    ch = E.charges_breakdown(buy_t, sell_t, orders, expired)
    inflow = round(sell_t, 2)
    outflow = round(-buy_t - ch["total"], 2)
    return {"label": label, "spot": round(S, 2), "legs": rows, "premium_in": inflow, "premium_out": round(-buy_t, 2) + 0.0,
            "charges": ch, "net_exit_cash": round(inflow + outflow, 2),
            "net_pnl": round(entry_cash + inflow + outflow, 2)}


def cashflow(c):
    """c: a scored candidate (engine output, optionally with c['dhan'])."""
    lot, cp = c["lot"], c["cp"]
    legs, buy_t, sell_t = [], 0.0, 0.0
    for l in c["legs"]:
        qty = abs(l["lots"]) * lot
        val = qty * l["px"]
        name = contract(c, l["K"])
        if l["lots"] > 0:
            buy_t += val
            legs.append({"action": "BUY", "contract": name, "lots": abs(l["lots"]), "qty": qty, "price": l["px"],
                         "price_basis": "ask", "flow": round(-val, 2)})
        else:
            sell_t += val
            legs.append({"action": "SELL", "contract": name, "lots": abs(l["lots"]), "qty": qty, "price": l["px"],
                         "price_basis": "bid", "flow": round(val, 2)})
    ch = E.charges_breakdown(buy_t, sell_t, len(c["legs"]))
    net_prem = round(sell_t - buy_t, 2)
    entry_cash = round(net_prem - ch["total"], 2)
    if c.get("dhan") and c["dhan"].get("total"):
        blocked, src = c["dhan"]["total"], "Dhan basket margin"
    else:
        blocked, src = c["capital"], "NSE SPAN + ELM estimate"

    S = c["spot"]
    scen = [(S, f"Stock unchanged at {S:,.2f}"), (c["maxp_at"], f'Best case: expiry at {c["maxp_at"]:g} (max profit)')]
    for b in c["bes"]:
        scen.append((b, f"At breakeven {b:,.2f} (before charges)"))
    if c["unlimited"]:
        x = c.get("S_stress3") or S * 1.3
        scen.append((x, f"Rally to {x:,.0f} (3σ); loss keeps growing above this"))
    else:
        ks = sorted(l["K"] for l in c["legs"])
        cands = ks + [0.0, S * 3]                      # strikes first, so a flat loss zone is named by its strike
        worst = min(cands, key=lambda s: round(_pnl_at(c, s), 2))
        lo = round(_pnl_at(c, worst), 2)
        if worst == 0:
            tag = "stock to ₹0"
        elif cp == "C" and worst == ks[0] and round(_pnl_at(c, 0.0), 2) == lo:
            tag = f"expiry at or below {worst:g}"
        elif cp == "P" and worst == ks[-1] and round(_pnl_at(c, S * 3), 2) == lo:
            tag = f"expiry at or above {worst:g}"
        else:
            tag = f"expiry at {worst:g}"
        scen.append((worst, f"Worst case: {tag} (max loss)"))
    exits, seen = [], set()
    for s, label in scen:
        k = round(s, 2)
        if k in seen:
            continue
        seen.add(k)
        exits.append(_exit(c, s, label, entry_cash))

    return {
        "entry": {"legs": legs, "premium_out": round(-buy_t, 2), "premium_in": round(sell_t, 2),
                  "net_premium": net_prem, "net_premium_is": "inflow (credit)" if net_prem > 0 else "outflow (debit)",
                  "charges": ch, "net_entry_cash": entry_cash},
        "margin_blocked": {"amount": round(blocked, 2), "source": src,
                           "note": "Funds blocked while the position is open (already includes the premium paid on buy legs); released on exit."},
        "funds_needed": round(max(blocked, -entry_cash) + ch["total"], 2),
        "exits": exits,
    }
