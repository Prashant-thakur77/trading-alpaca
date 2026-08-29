#!/usr/bin/env python3
"""
Render the fitted volatility smile per expiry as a standalone page.

    python3 scripts/build_smile_page.py            # live chain
    python3 scripts/build_smile_page.py --out PATH

Presents the fit as a MEASUREMENT, not as a claim of edge. The measured
deviation is smaller than the spread it would have to cross (see
docs/research/smile-feasibility.md), so the page marks strikes rich or cheap
only to show how a tie between otherwise-viable strikes is broken, and says so
on its face. Every strike whose deviation sits inside its own bid-ask noise is
drawn grey and scores exactly zero.

SVG is generated rather than hand-authored so the chart cannot drift from the
data it claims to plot.
"""
import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import fit_smile, implied_vol, richness, time_to_expiry_years  # noqa: E402
from candidate_builder import (  # noqa: E402
    MAX_SPREAD_PCT_OF_MID, MIN_OPEN_INTEREST,
)

W, H = 560, 300
PAD_L, PAD_R, PAD_T, PAD_B = 52, 16, 22, 40


def _liquid(chain):
    return [q for q in chain
            if q.open_interest >= MIN_OPEN_INTEREST
            and q.spread_pct <= MAX_SPREAD_PCT_OF_MID]


def _points(quotes, spot):
    """(moneyness, iv, richness-eligible) per strike, rejecting bad solves."""
    out = []
    for q in quotes:
        t = time_to_expiry_years(q.dte)
        iv = implied_vol(q.mid, spot, q.strike, t, q.right)
        if iv is None or not (0.01 < iv < 1.5):
            continue
        m = math.log(q.strike / spot) / math.sqrt(max(t, 1e-9))
        out.append((m, iv, q))
    return out


def _svg(fit, pts, spot) -> str:
    """One chart. Grey dots are inside their own quote noise and score zero."""
    inside = [p for p in pts if abs(p[0]) <= fit.band]
    if not inside:
        return ""
    ms = [p[0] for p in inside]
    ivs = [p[1] for p in inside]
    m0, m1 = min(ms), max(ms)
    v0, v1 = min(ivs), max(ivs)
    if m1 - m0 < 1e-9 or v1 - v0 < 1e-9:
        return ""
    pad_v = (v1 - v0) * 0.18
    v0, v1 = v0 - pad_v, v1 + pad_v

    def X(m): return PAD_L + (m - m0) / (m1 - m0) * (W - PAD_L - PAD_R)
    def Y(v): return PAD_T + (1 - (v - v0) / (v1 - v0)) * (H - PAD_T - PAD_B)

    curve = []
    for i in range(81):
        m = m0 + (m1 - m0) * i / 80
        curve.append(f"{X(m):.1f},{Y(fit.curve_at(m)):.1f}")

    dots = []
    scored = 0
    for m, iv, q in inside:
        r = richness(q, fit, spot)
        if r == 0.0:
            col, rad = "rgba(249,244,235,.28)", 2.2
        else:
            scored += 1
            col, rad = ("#e75d60", 3.4) if r > 0 else ("#d0ff7e", 3.4)
        dots.append(f'<circle cx="{X(m):.1f}" cy="{Y(iv):.1f}" r="{rad}" fill="{col}"/>')

    ticks = []
    for k in range(4):
        v = v0 + (v1 - v0) * k / 3
        ticks.append(
            f'<line x1="{PAD_L}" y1="{Y(v):.1f}" x2="{W-PAD_R}" y2="{Y(v):.1f}" '
            f'stroke="rgba(249,244,235,.08)"/>'
            f'<text x="{PAD_L-8}" y="{Y(v)+3:.1f}" text-anchor="end" class="ax">{v*100:.1f}</text>')
    for k in range(5):
        m = m0 + (m1 - m0) * k / 4
        ticks.append(f'<text x="{X(m):.1f}" y="{H-PAD_B+16}" text-anchor="middle" '
                     f'class="ax">{m:+.2f}</text>')

    return f"""<figure>
  <figcaption><b>{fit.expiry}</b> · {'calls' if fit.right=='c' else 'puts'}
    <span class="meta">n={fit.n_points} · degree {fit.degree} · rmse {fit.rmse*100:.3f}v
    · {scored} of {len(inside)} clear their own spread</span></figcaption>
  <svg viewBox="0 0 {W} {H}" role="img"
       aria-label="Fitted volatility smile for {fit.expiry} {fit.right}">
    {''.join(ticks)}
    <polyline points="{' '.join(curve)}" fill="none" stroke="#f9f4eb"
              stroke-width="1.4" opacity=".85"/>
    {''.join(dots)}
    <text x="{PAD_L}" y="{H-6}" class="ax">moneyness  ln(K/S)/sqrt(T)</text>
    <text x="12" y="{PAD_T-8}" class="ax">IV %</text>
  </svg>
</figure>"""


def build(chain, spot, underlying: str) -> str:
    groups = defaultdict(list)
    for q in _liquid(chain):
        groups[(q.expiry, q.right)].append(q)

    charts, fitted, total = [], 0, 0
    for (exp, right), quotes in sorted(groups.items()):
        total += 1
        fit = fit_smile(quotes, spot)
        if fit is None or not fit.is_measured:
            continue
        svg = _svg(fit, _points(quotes, spot), spot)
        if svg:
            charts.append(svg)
            fitted += 1

    body = "".join(charts) or '<p class="note">No expiry had enough liquid strikes to fit.</p>'
    return f"""<title>Volatility smile</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{--ground:#1a1c1c;--ground-2:#010101;--ink:#f9f4eb;
 --dim:rgba(249,244,235,.62);--muted:rgba(249,244,235,.42);
 --hair:rgba(249,244,235,.14);--coral:#e75d60;--lime:#d0ff7e;
 --sans:"Inter Tight",Helvetica,sans-serif;--mono:"JetBrains Mono",monospace}}
body{{background:var(--ground);color:var(--ink);font-family:var(--sans);
 line-height:1.5;padding:44px 40px 80px;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1220px;margin:0 auto}}
.tag{{font-family:var(--mono);font-size:11px;letter-spacing:.2em;
 text-transform:uppercase;color:var(--muted)}}
.tag::before{{content:"[ "}}.tag::after{{content:" ]"}}
h1{{font-weight:800;text-transform:uppercase;letter-spacing:-.03em;
 font-size:clamp(30px,5vw,64px);line-height:.9;margin:14px 0 18px}}
.lede{{color:var(--dim);max-width:74ch;font-size:16px}}
.lede b{{color:var(--ink);font-weight:600}}
.key{{display:flex;gap:22px;flex-wrap:wrap;margin:26px 0 34px;
 font-family:var(--mono);font-size:11px;letter-spacing:.08em;color:var(--dim)}}
.key i{{display:inline-block;width:9px;height:9px;border-radius:9999px;
 margin-right:7px;vertical-align:middle;font-style:normal}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:6px}}
figure{{background:var(--ground-2);border-radius:4px;padding:16px 18px 8px}}
figcaption{{font-family:var(--mono);font-size:11px;color:var(--ink);
 letter-spacing:.06em;margin-bottom:6px}}
figcaption .meta{{color:var(--muted);display:block;margin-top:4px}}
svg{{width:100%;height:auto}}
.ax{{fill:rgba(249,244,235,.42);font-family:"JetBrains Mono",monospace;font-size:9px}}
.note{{border-left:1px solid var(--coral);padding:14px 0 14px 18px;
 margin-top:36px;color:var(--dim);font-size:14px;max-width:82ch}}
.note b{{color:var(--ink);font-weight:600}}
a{{color:inherit}}
</style>
<div class="wrap">
  <p class="tag">{underlying} · spot {spot:.2f} · {fitted} of {total} liquid groups fitted</p>
  <h1>The smile,<br>and its noise floor.</h1>
  <p class="lede">Implied volatility per strike against moneyness, with a cubic fitted
  across the liquid strikes only. A dot is coloured <b>only if its distance from the
  curve exceeds that strike's own bid-ask noise</b>. Everything grey scores exactly
  zero and is ignored.</p>
  <div class="key">
    <span><i style="background:#e75d60"></i>rich · IV above the curve</span>
    <span><i style="background:#d0ff7e"></i>cheap · IV below the curve</span>
    <span><i style="background:rgba(249,244,235,.28)"></i>inside its own spread · scores zero</span>
  </div>
  <div class="grid">{body}</div>
  <div class="note"><b>This is a measurement, not an edge.</b> Across the live chain
  only about a fifth of liquid strikes clear their own spread at all, and the median
  one that does is worth roughly <b>$3.94 per contract against a $4.00 median quoted
  spread</b>. Richness is therefore used only to break ties between strikes that are
  already viable on liquidity, DTE and risk grounds. It is never a filter and never a
  reason to trade. Method and limits: <code>docs/research/smile-feasibility.md</code>.</div>
</div>"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Render the fitted vol smile")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--out", default=str(ROOT / "site" / "smile" / "index.html"))
    a = ap.parse_args()

    from alpaca_data import AlpacaData
    d = AlpacaData.from_env()
    spot = float(d.get_stock_bars(a.symbol, days=10)["close"].iloc[-1])
    chain = d.get_option_chain(a.symbol)

    html = build(chain, spot, a.symbol)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"  wrote {out} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
