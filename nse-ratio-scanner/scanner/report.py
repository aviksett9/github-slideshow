"""Render a ranked scan + deep dives into a standalone HTML report."""
import os, html

from .costing import cashflow, contract

esc = html.escape
META = {}


def inr(x, dec=0):
    if x is None:
        return "–"
    """Indian digit grouping (₹1,23,456 or ₹1,23,456.78)."""
    neg = x < 0
    s = f"{abs(x):.{dec}f}"
    n, _, frac = s.partition(".")
    if len(n) > 3:
        head, tail = n[:-3], n[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        n = ",".join(parts + [tail])
    return ("−" if neg else "") + "₹" + n + ("." + frac if frac else "")


def k(x):
    return f"{x:g}"


def legs_txt(c, side):
    out = []
    for l in c["legs"]:
        if (l["lots"] > 0) == (side == "buy"):
            out.append(f'{abs(l["lots"])}×{k(l["K"])} {"CE" if c["cp"] == "C" else "PE"} @{l["px"]:.2f}')
    return "<br>".join(out)


def flags(c):
    f = []
    ev = META["events"].get(c["sym"])
    if ev:
        f.append(f'<span class="chip warn" title="{esc(ev)}">event</span>')
    if c.get("carry_dev") is not None and abs(c["carry_dev"]) > 0.01:
        f.append(f'<span class="chip warn" title="Implied forward {c["carry_dev"]*100:+.1f}% vs cost of carry: dividend or event priced in">fwd {c["carry_dev"]*100:+.1f}%</span>')
    if c.get("fwd_src") == "SPAN futures":
        f.append('<span class="chip" title="Forward from NSE Clearing SPAN futures price (parity quotes too wide)">fwd:SPAN</span>')
    if c.get("sig_rw") is None:
        f.append('<span class="chip" title="No price history; EV uses market-implied distribution">no HV</span>')
    if META["extra_elm"].get(c["sym"]):
        f.append(f'<span class="chip warn" title="NSE additional ELM in force">+ELM {META["extra_elm"][c["sym"]]:g}%</span>')
    return " ".join(f)


def maxloss_txt(c):
    if c["unlimited"]:
        return '<span class="risk crit">Unlimited</span>'
    if not c["defined"]:
        return f'<span class="risk high">{inr(c["maxl"])}</span><small>at ₹0; substantial</small>'
    return f'<span class="risk ok">{inr(c["maxl"])}</span>'


def row(c, i):
    prem = c["prem"]
    pd = f'<span class="dr">Dr {inr(prem)}</span>' if prem > 0 else f'<span class="cr">Cr {inr(-prem)}</span>'
    bes = ", ".join(k(round(b, 1)) for b in c["bes"])
    dsig = f'<small>{c["dang_sig"]:.1f}σ</small>' if c.get("dang_sig") else ""
    return f"""<tr><td class="n">{i}</td><td><b>{esc(c['sym'])}</b><small>exp {esc(c.get('expiry') or '')} · lot {c['lot']}</small></td>
<td class="num">{c['spot']:,.2f}</td><td>{c['ratio']}</td><td class="legs">{legs_txt(c,'buy')}</td><td class="legs">{legs_txt(c,'sell')}</td>
<td class="num">{pd}</td><td class="num">{inr(c['capital'])}<small>SPAN {inr(c['span_req'])} · ELM {inr(c['elm'])}</small></td>
<td class="num">{inr(c['charges'])}</td><td class="num">{inr(c['maxp'])}<small>at {k(c['maxp_at'])}</small></td><td class="num">{maxloss_txt(c)}</td>
<td class="num">{bes}{dsig}</td><td class="num"><b>{c['roi_max']*100:.1f}%</b><small>EV {c['roi_ev']*100:+.1f}%</small></td>
<td class="num">{c['p_profit_iv']*100:.0f}%</td><td>{flags(c)}</td></tr>"""


HEAD = """<tr><th>#</th><th>Stock</th><th>Spot</th><th>Ratio</th><th>Buy legs</th><th>Sell legs</th><th>Net debit/credit</th>
<th>Margin ₹<small>total capital</small></th><th>Charges ₹<small>entry+exit</small></th><th>Max profit ₹<small>gross, expiry</small></th>
<th>Max loss ₹</th><th>Breakevens<small>σ to danger</small></th><th>Net ROI %<small>max · EV</small></th><th>P(profit)<small>mkt</small></th><th>Flags</th></tr>"""


def table(rows, caption, note):
    body = "\n".join(row(c, i + 1) for i, c in enumerate(rows)) or '<tr><td colspan="15">No candidate passed the filters.</td></tr>'
    return f"""<section class="tbl"><h3>{caption}</h3><p class="note">{note}</p>
<div class="scroll"><table>{HEAD}{body}</table></div></section>"""


def mini(c):
    prem = c["prem"]
    pd = f"Dr {inr(prem)}" if prem > 0 else f"Cr {inr(-prem)}"
    legs = " · ".join(f'{"BUY" if l["lots"]>0 else "SELL"} {abs(l["lots"])}× {k(l["K"])} {"CE" if c["cp"]=="C" else "PE"} @{l["px"]:.2f}' for l in c["legs"]) + f' · exp {c.get("expiry", "")}'
    ml = "Unlimited" if c["unlimited"] else inr(c["maxl"])
    cap = (f"Dhan margin <b>{inr(c['dhan']['total'])}</b> (est. {inr(c['capital'])})" if c.get("dhan")
           else f"margin {inr(c['capital'])} est.")
    cf = cashflow(c)
    e = cf["entry"]
    best = next((x for x in cf["exits"] if x["label"].startswith("Best")), None)
    worst = next((x for x in cf["exits"] if x["label"].startswith(("Worst", "Rally"))), None)
    cash = (f"Entry: pay {inr(-e['premium_out'])} · receive {inr(e['premium_in'])} · charges {inr(e['charges']['total'], 2)} → "
            f"net {'in' if e['net_entry_cash'] > 0 else 'out'} {inr(abs(e['net_entry_cash']))}"
            + (f" · best exit {inr(best['net_pnl'])}" if best else "") + (f" · worst {inr(worst['net_pnl'])}" if worst else ""))
    return f"""<li><b>{esc(c['sym'])} {'CE' if c['cp']=='C' else 'PE'} {c['ratio']}</b> <span class="mono">{legs}</span>
<span class="kv">{pd} · {cap} · net max {inr(c['net_maxp'])} · max loss {ml} · P(profit) {c['p_profit_iv']*100:.0f}% · EV {inr(c['ev_net'])}</span>
<span class="kv">{cash}</span> {flags(c)}</li>"""


def cash_table(c):
    cf = cashflow(c)
    e = cf["entry"]
    fl = lambda v: f'<td class="num {"pos" if v > 0 else "neg" if v < 0 else ""}">{"+" if v > 0 else ""}{inr(v, 2) if v else "–"}</td>'
    rows = "".join(f"<tr><td>{l['action']} {l['lots']} lot{'s' if l['lots'] > 1 else ''}</td><td>{l['contract']}</td><td class='num'>{l['qty']:,}</td>"
                   f"<td class='num'>{l['price']:.2f} ({l['price_basis']})</td>{fl(l['flow'])}</tr>" for l in e["legs"])
    chg = e["charges"]
    rows += (f"<tr><td colspan='4'>Entry charges: brokerage {inr(chg['brokerage'],2)}, exchange {inr(chg['exchange'],2)}, STT {inr(chg['stt'])}, "
             f"stamp {inr(chg['stamp'])}, SEBI {inr(chg['sebi'],2)}, GST {inr(chg['gst'],2)}</td>{fl(-chg['total'])}</tr>")
    rows += f"<tr><td colspan='4'><b>Net entry cash</b></td>{fl(e['net_entry_cash'])}</tr>"
    mb = cf["margin_blocked"]
    ex = "".join(f"<tr><td>{esc(x['label'])}</td><td class='num'>{x['spot']:,.2f}</td>{fl(x['premium_in'])}{fl(x['premium_out'])}"
                 f"{fl(-x['charges']['total'])}{fl(x['net_exit_cash'])}<td class='num'><b>{'+' if x['net_pnl'] > 0 else ''}{inr(x['net_pnl'])}</b></td></tr>"
                 for x in cf["exits"])
    return f"""<h4>Cash flow per set <small>+ inflow, − outflow · exits square off just before expiry at intrinsic value</small></h4>
<div class="scroll"><table class="compact"><tr><th>Entry</th><th>Contract</th><th>Qty</th><th>Price</th><th>Cash</th></tr>{rows}</table></div>
<p class="note">Funds blocked while open: <b>{inr(mb['amount'])}</b> ({esc(mb['source'])}), which includes the premium paid on buy legs and is released on exit. Funds needed in the account: about {inr(cf['funds_needed'])}.</p>
<div class="scroll"><table class="compact"><tr><th>Exit scenario</th><th>Spot</th><th>Receive</th><th>Pay</th><th>Charges</th><th>Net exit cash</th><th>Total P&amp;L</th></tr>{ex}</table></div>"""


def payoff_svg(dd, c):
    sc = sorted(dd["scenarios"], key=lambda r: r["S"])
    xs = [r["S"] for r in sc]
    lo, hi = min(xs), max(xs)
    ys = [r["expiry"] for r in sc] + [r["d14"] for r in sc] + [r["d7"] for r in sc]
    ymin, ymax = min(ys + [0]), max(ys + [0])
    W, H, P = 640, 240, 44
    X = lambda v: P + (v - lo) / (hi - lo) * (W - 2 * P)
    Y = lambda v: H - P + (ymin - v) / (ymax - ymin) * (H - 2 * P) * -1 if ymax > ymin else H / 2
    def Yf(v):
        return H - P - (v - ymin) / (ymax - ymin) * (H - 2 * P)
    def line(key, cls):
        pts = " ".join(f"{X(r['S']):.1f},{Yf(r[key]):.1f}" for r in sc)
        return f'<polyline class="{cls}" points="{pts}"/>'
    z = Yf(0)
    sx = X(dd["S"])
    ticks = "".join(f'<text x="{X(v):.1f}" y="{H-P+16}" class="ax" text-anchor="middle">{v:,.0f}</text>' for v in [lo, dd["S"], hi])
    yt = "".join(f'<text x="{P-6}" y="{Yf(v)+4:.1f}" class="ax" text-anchor="end">{v/1000:,.0f}k</text>' for v in [ymin, 0, ymax])
    return f"""<svg viewBox="0 0 {W} {H}" class="payoff" role="img" aria-label="Payoff of {esc(c['sym'])}">
<line x1="{P}" x2="{W-P}" y1="{z:.1f}" y2="{z:.1f}" class="zero"/><line x1="{sx:.1f}" x2="{sx:.1f}" y1="{P/2}" y2="{H-P}" class="spot"/>
{line('d14','l14')}{line('d7','l7')}{line('expiry','lexp')}{ticks}{yt}
<text x="{sx+4:.1f}" y="{P/2+10}" class="ax">spot</text></svg>
<div class="legend"><span class="k lexp"></span>Expiry <span class="k l7"></span>7 DTE <span class="k l14"></span>14 DTE (smile unchanged)</div>"""


def deep(key, d, rank):
    c, dd = d["cand"], d["dd"]
    mg, st = dd["margin"], dd["stats"]
    legq = "".join(
        f"<tr><td>{'BUY' if q['lots']>0 else 'SELL'} {abs(q['lots'])} lot{'s' if abs(q['lots'])>1 else ''}</td><td>{esc(contract(c, q['K']))}</td>"
        f"<td class='num'>{q['bid']:.2f} / {q['ask']:.2f}</td><td class='num'>{q['ltp']:.2f}</td><td class='num'><b>{q['px']:.2f}</b></td>"
        f"<td class='num'>{q['oi']:,.0f}</td><td class='num'>{q['vol']:,.0f}</td><td class='num'>{q['iv_model']*100:.1f}%</td><td class='num'>{(q['nse_iv'] or 0):.1f}%</td>"
        + (f"<td class='num'>{q['v_bid']:.2f} / {q['v_ask']:.2f}</td>" if q.get('v_bid') is not None else "<td>–</td>") + "</tr>"
        for q in dd["leg_quotes"])
    scen = "".join(
        f"<tr><td class='num'>{r['S']:,.1f}</td><td class='num'>{r['chg']*100:+.1f}%</td><td class='num {'pos' if r['expiry']>0 else 'neg'}'>{inr(r['expiry'])}</td>"
        f"<td class='num'>{inr(r['d14'])}</td><td class='num'>{inr(r['d14_iv5'])}</td><td class='num'>{inr(r['d7'])}</td><td class='num'>{inr(r['d7_iv5'])}</td></tr>"
        for r in sorted(dd["scenarios"], key=lambda r: r["S"]))
    adj = ""
    for a in dd["adjustments"]:
        if "after" in a:
            af, am = a["after"], a["margin_after"]
            ml = "Unlimited" if af["unlimited"] else inr(af["maxl"])
            adj += (f"<li><b>{esc(a['name'])}</b><br><span class='mono'>{esc(a['trade'])}</span> · cost {inr(a['cost'])} + charges {inr(a['charges'])}"
                    f"<br>After: breakevens {', '.join(k(round(b,1)) for b in af['bes'])} · max profit {inr(af['maxp'])} at {k(af['maxp_at'])} · max loss {ml}"
                    f" · margin/capital {inr(am['capital'])} (SPAN {inr(am['span_req'])}, ELM {inr(am['elm'])})"
                    + (f" · position MTM at trigger before adjusting {inr(a['mtm_before'])}" if a.get('mtm_before') else "") + "</li>")
        elif "mtm_before" in a:
            adj += (f"<li><b>{esc(a['name'])}</b><br>Estimated P&amp;L if closed there: {inr(a['mtm_before'])} (smile unchanged), {inr(a['mtm_iv_up5'])} if IV +5 pts; exit charges {inr(a['charges'])}</li>")
        else:
            adj += f"<li><b>{esc(a['name'])}</b><br>Stop at {inr(a['threshold'])} mark-to-market.</li>"
    dm = dd["delivery_margin"]
    dmh = (f"VaR+ELM rate {dm['var_elm_rate']*100:.2f}% (NSE cash VaR file) on net ITM delivery value {inr(dm['expiry_settlement_value'])} at {k(st['maxp_at'])}: "
           f"E-4 {inr(dm['E-4'])} · E-3 {inr(dm['E-3'])} · E-2 {inr(dm['E-2'])} · E-1 {inr(dm['E-1'])}; on expiry the full delivery value is due.") if dm else "VaR rate unavailable."
    ph = dd["physical"]
    costs = dd["costs"]
    gross = st["maxp"]
    ml = "Unlimited" if st["unlimited"] else inr(st["maxl"]) + ("" if c["defined"] else " if stock → ₹0")
    return f"""<article class="deep"><header><span class="rank">{rank}</span><div><h3>{esc(c['sym'])} {'Call' if c['cp']=='C' else 'Put'} ratio {c['ratio']}</h3>
<p class="mono">{' · '.join(f"{'BUY' if l['lots']>0 else 'SELL'} {abs(l['lots'])}×{k(l['K'])}{'CE' if c['cp']=='C' else 'PE'}" for l in c['legs'])} · lot {c['lot']} · expiry {esc(c.get('expiry') or '')}</p></div></header>
<div class="grid2"><div>{payoff_svg(dd, c)}</div>
<dl class="facts"><dt>Spot / implied forward</dt><dd>{dd['S']:,.2f} / {dd['F']:,.2f}</dd><dt>Net premium</dt><dd>{'Dr' if mg['prem']>0 else 'Cr'} {inr(abs(mg['prem']))}</dd>
<dt>SPAN scan risk (16 NSE scenarios)</dt><dd>{inr(mg['scan'])}</dd><dt>Net option value</dt><dd>{inr(mg['nov'])}</dd>
<dt>SPAN after option value</dt><dd>{inr(mg['span_req'])}</dd><dt>Exposure margin (ELM)</dt><dd>{inr(mg['elm'])}</dd>
<dt>Margin blocked, NSE estimate</dt><dd><b>{inr(mg['capital'])}</b></dd>
<dt>Dhan basket margin</dt><dd>{(f"<b>{inr(c['dhan']['total'])}</b> (SPAN {inr(c['dhan'].get('span'))}, exposure {inr(c['dhan'].get('exposure'))}; {c['dhan']['checked_at'][11:16]} IST)") if c.get('dhan') else ('not checked' + (': ' + esc(c['dhan_error']) if c.get('dhan_error') else ' (no Dhan token)'))}</dd>
<dt>Breakevens</dt><dd>{', '.join(k(round(b,1)) for b in st['bes'])}</dd><dt>Max profit (gross)</dt><dd>{inr(gross)} at {k(st['maxp_at'])}</dd><dt>Max loss</dt><dd>{ml}</dd>
<dt>P(profit) market / realised-vol</dt><dd>{c['p_profit_iv']*100:.0f}% / {c['p_profit_rw']*100:.0f}%</dd></dl></div>
<h4>Option legs <small>scan snapshot {esc(dd['fetch'])} IST{(' · re-verified ' + esc(dd['verify_time'])) if dd.get('verify_time') else ''}</small></h4>
<div class="scroll"><table class="compact"><tr><th>Action</th><th>Contract</th><th>Bid / Ask</th><th>LTP</th><th>Price used</th><th>OI (contracts)</th><th>Volume</th><th>IV (Black-76)</th><th>IV (NSE)</th><th>Re-verified bid / ask</th></tr>{legq}</table></div>
{cash_table(c)}
<h4>Costs (Dhan + statutory)</h4>
<table class="compact"><tr><th>Path</th><th>Charges</th><th>Net max profit</th></tr>
<tr><td>Entry + normal exit (square off near expiry at {k(st['maxp_at'])})</td><td class="num">{inr(costs['entry_exit'])}</td><td class="num">{inr(gross-costs['entry_exit'])}</td></tr>
<tr><td>Entry + one adjustment + exit</td><td class="num">{inr(costs.get('entry_adjust_exit'))}</td><td class="num">{inr(gross-costs['entry_adjust_exit']) if costs.get('entry_adjust_exit') else '–'}</td></tr>
<tr><td>Hold through expiry (physical settlement at {k(st['maxp_at'])})</td><td class="num">{inr(costs['hold_expiry'])}</td><td class="num">{inr(gross-costs['hold_expiry'])}</td></tr></table>
<p class="note">Holding to expiry at {k(st['maxp_at'])} means {ph['cash_or_stock_needed']}: {abs(ph['net_shares']):,.0f} shares, value {inr(ph['delivery_value'])}. Expiry-week delivery margin (estimate): {dmh}</p>
<h4>P&amp;L scenarios <small>Black-76, sticky-strike smile; "+5" = all IVs +5 vol points</small></h4>
<div class="scroll"><table class="compact"><tr><th>Spot</th><th>Move</th><th>At expiry</th><th>14 DTE</th><th>14 DTE, IV+5</th><th>7 DTE</th><th>7 DTE, IV+5</th></tr>{scen}</table></div>
<h4>Adjustment plans <small>indicative; repriced on today's smile and SPAN arrays</small></h4><ul class="adj">{adj}</ul>
</article>"""


def render(RK, DT, meta):
    META.clear(); META.update(meta)
    T = RK["tables"]
    L = RK["lists"]
    sk = "".join(f"<li><b>{esc(s)}</b>: {esc(r)}</li>" for s, r in sorted(RK["skipped"].items()))
    deeps = "".join(deep(k_, d, i + 1) for i, (k_, d) in enumerate(DT.items())) or '<p class="note">No setup passed the quality floor this cycle.</p>'
    m = META
    body = TEMPLATE.format(
        title=m["title"], expiry=m["expiry"], scan_date=m["scan_date"], close_by=m["close_by"], mkt=esc(m["mkt"]),
        n_univ=m["n_universe"], n_scanned=len(RK["scanned"]), n_skipped=len(RK["skipped"]),
        n_cand=f'{RK["n_candidates"]:,}', n_elig=f'{RK["n_eligible"]:,}', fetch_lo=m["fetch_lo"], fetch_hi=m["fetch_hi"],
        span_created=m["span_created"], hv_cov=m["hv_cov"], verify_time=m.get("verify_time", "–"),
        dhan_note=("Shortlists and the top 3 show Dhan's live basket margin (read-only calculator)." if m.get("dhan")
                   else "Set DHAN_ACCESS_TOKEN to have shortlisted setups verified by Dhan's read-only margin calculator."),
        skipped=sk,
        t1a=table(T["C_defined"], "Table 1A · Call ratio structures, defined risk", "Longs ≥ shorts, so loss is capped on both sides. Ranked by composite score."),
        t1b=table(T["C_undefined"], "Table 1B · Call ratio structures, unlimited upside risk", "Ranked separately. Net short calls above the top strike: loss grows without limit on a rally."),
        t2a=table(T["P_defined"], "Table 2A · Put ratio structures, defined risk", "Longs ≥ shorts, so loss is capped. Ranked by composite score."),
        t2b=table(T["P_undefined"], "Table 2B · Put ratio structures, substantial downside risk", "Ranked separately. Net short puts: loss grows to the value shown at ₹0."),
        l1="".join(mini(c) for c in L["low_debit"]) or "<li>None this cycle.</li>", l2="".join(mini(c) for c in L["high_profit"]) or "<li>None this cycle.</li>",
        l3="".join(mini(c) for c in L["hedged"]) or "<li>None this cycle.</li>", l4="".join(mini(c) for c in L["overall"]) or "<li>None this cycle.</li>",
        deeps=deeps)
    return body


def page(body):
    """Wrap the body in a full document for serving directly."""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<meta http-equiv="refresh" content="300"></head><body style="margin:0">' + body + '</body></html>')


TEMPLATE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html"), encoding="utf-8").read()
