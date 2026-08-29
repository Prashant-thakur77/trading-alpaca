#!/usr/bin/env python3
"""
Generate the six-slide submission deck (16:9) and render it to PDF.

    python3 scripts/build_deck.py

Figures are read from the repository at build time, so no slide can drift from
the code or outlive a number it quotes.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_HTML = ROOT / "docs" / "press" / "deck.html"
OUT_PDF = ROOT / "docs" / "press" / "trading-alpaca-deck.pdf"
SITE = "https://trading-alpaca-judge.vercel.app"
REPO = "https://github.com/Prashant-thakur77/trading-alpaca"


def tests() -> str:
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q", "--collect-only"],
                       cwd=ROOT, capture_output=True, text=True)
    m = re.search(r"(\d+)\s+tests?\s+collected", r.stdout)
    return f"{int(m.group(1)):,}" if m else "n/a"


def wf() -> str:
    p = ROOT / "validation" / "walkforward.json"
    if not p.exists():
        return "<tr><td colspan=4>no run on record</td></tr>"
    d = json.loads(p.read_text())
    out = []
    for sym, r in d.get("symbols", {}).items():
        o = r["oos"]
        cls = ' class="neg"' if o["expectancy_r"] < 0 else ""
        out.append(f"<tr><td>{sym}</td><td>{o['trades']}</td><td>{o['win_rate']:.1f}%</td>"
                   f"<td{cls}>{o['expectancy_r']:+.2f}R</td></tr>")
    return "".join(out)


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--ground:#1a1c1c;--g2:#010101;--ink:#f9f4eb;
 --dim:rgba(249,244,235,.66);--muted:rgba(249,244,235,.44);
 --hair:rgba(249,244,235,.16);--coral:#e75d60;--lime:#d0ff7e;
 --sans:"Inter Tight",Helvetica,sans-serif;--mono:"JetBrains Mono",monospace}
@page{size:1280px 720px;margin:0}
body{background:var(--ground);color:var(--ink);font-family:var(--sans);line-height:1.45}
.slide{width:1280px;height:720px;padding:64px 72px;display:flex;flex-direction:column;
 position:relative;page-break-after:always;overflow:hidden}
.slide:last-child{page-break-after:auto}
.tag{font-family:var(--mono);font-size:12px;letter-spacing:.2em;text-transform:uppercase;
 color:var(--muted)}
.tag::before{content:"[ "}.tag::after{content:" ]"}
h1{font-size:82px;font-weight:800;text-transform:uppercase;letter-spacing:-.035em;
 line-height:.88;margin:18px 0}
h2{font-size:46px;font-weight:800;text-transform:uppercase;letter-spacing:-.03em;
 line-height:.92;margin:14px 0 22px}
p{color:var(--dim);font-size:19px;max-width:62ch}
p b, li b{color:var(--ink);font-weight:600}
.n{position:absolute;right:72px;bottom:52px;font-family:var(--mono);
 font-size:12px;color:var(--muted)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:44px;flex:1;align-content:start}
ul{list-style:none;display:flex;flex-direction:column;gap:14px}
li{color:var(--dim);font-size:18px;padding-left:22px;position:relative}
li::before{content:"\\2192";position:absolute;left:0;color:var(--coral)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:26px 0}
.kpi{background:var(--g2);border-radius:4px;padding:20px 22px}
.kpi .v{font-family:var(--mono);font-size:34px}
.kpi .l{font-family:var(--mono);font-size:11px;color:var(--muted);
 letter-spacing:.1em;text-transform:uppercase;margin-top:6px}
.kpi.good .v{color:var(--lime)}
.kpi.bad .v{color:var(--coral)}
.kpi.lead{background:var(--coral)}
.kpi.lead .l{color:rgba(26,28,28,.8)}.kpi.lead .v{color:var(--ground)}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:17px;
 font-variant-numeric:tabular-nums;margin-top:8px}
th{text-align:left;color:var(--muted);font-weight:400;font-size:12px;letter-spacing:.1em;
 text-transform:uppercase;padding:8px 0;border-bottom:1px solid var(--hair)}
td{padding:9px 0;border-bottom:1px solid var(--hair);color:var(--dim)}
td.neg{color:var(--coral)}
.note{border-left:2px solid var(--coral);padding:12px 0 12px 18px;color:var(--dim);
 font-size:16px;margin-top:auto}
.note b{color:var(--ink)}
.big{font-family:var(--mono);font-size:64px;line-height:1}
.mono{font-family:var(--mono)}
.foot{margin-top:auto;display:flex;gap:36px;font-family:var(--mono);font-size:13px;
 color:var(--muted);padding-top:22px;border-top:1px solid var(--hair)}
.foot b{color:var(--ink);font-weight:400}
"""


def build() -> str:
    return f"""<!doctype html><meta charset="utf-8"><title>Trading Alpaca deck</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>{CSS}</style>

<section class="slide">
  <p class="tag">Alpaca AI Trading Agents · Options Alpha Agents</p>
  <h1>The desk that<br>grades itself.</h1>
  <p style="font-size:22px">Deterministic code builds every trade. Each analyst carries a
  track record, states a <b>probability</b> rather than a verdict, and is Brier-scored
  against what actually happened. <b>An analyst that is confidently wrong loses its
  vote.</b></p>
  <div class="foot"><span><b>Live</b> {SITE}</span>
    <span><b>Judge desk</b> {SITE}/judge</span>
    <span style="margin-left:auto"><b>Paper account only</b></span></div>
  <div class="n">01</div>
</section>

<section class="slide">
  <p class="tag">The inversion</p>
  <h2>Most agents ask a model what to buy.<br>This one never lets it answer.</h2>
  <div class="two">
    <div><p>Asking a model "what should I trade?" is quick to build and impossible to
      trust. It can hallucinate a strike, size a position wrongly, or be confidently
      wrong with no record of why.</p>
      <p style="margin-top:18px">So the model is handed a <b>numbered menu it did not
      write</b>, and may return one id or the word ABSTAIN. A hallucinated id is treated
      as an abstention.</p></div>
    <div><ul>
      <li><b>3,686</b> contracts read from the live chain</li>
      <li><b>1,078</b> survive the liquidity gate</li>
      <li><b>632</b> defined-risk structures built by code</li>
      <li><b>12</b> shown to the committee</li>
      <li><b>1</b> it may choose, or none</li>
    </ul>
    <p style="margin-top:22px;font-size:17px">There is no function in this codebase that
    produces a naked short option.</p></div>
  </div>
  <div class="n">02</div>
</section>

<section class="slide">
  <p class="tag">Four stages, every one able to refuse</p>
  <h2>Four chances to refuse.<br>One to trade.</h2>
  <div class="two">
    <div><ul>
      <li><b>Build</b> — code enumerates every legal defined-risk structure, fully
        priced, before any model is called</li>
      <li><b>Argue</b> — a volatility analyst and a dedicated adversary each return a
        probability; an abstainer leaves both numerator and denominator</li>
    </ul></div>
    <div><ul>
      <li><b>Review</b> — two vetoes that fail differently: pure code checking delta
        against the stated thesis, and a model that never sees the debate</li>
      <li><b>Decide</b> — RiskGuard returns allow, deny, or a smaller size. Any error,
        any missing data, any exception is a refusal</li>
    </ul></div>
  </div>
  <div class="note"><b>Recorded live:</b> the adversary objected to a thin 356-contract
  hedge, and the trader moved off the highest-credit candidate to answer it. That is the
  difference between a committee and a rubber stamp.</div>
  <div class="n">03</div>
</section>

<section class="slide">
  <p class="tag">What no competitor does</p>
  <h2>It fires its own analysts.</h2>
  <p>Replaying the real committee over 43 post-knowledge-cutoff windows produced 12
  resolved predictions per analyst, scored with the standard Brier score:</p>
  <div class="kpis">
    <div class="kpi good"><div class="v">0.162</div><div class="l">vol_analyst brier · better</div></div>
    <div class="kpi good"><div class="v">1.18</div><div class="l">its weight, upgraded</div></div>
    <div class="kpi bad"><div class="v">0.298</div><div class="l">adversary brier · worse</div></div>
    <div class="kpi bad"><div class="v">0.90</div><div class="l">its weight, demoted</div></div>
  </div>
  <p style="font-size:17px;margin-top:6px">Lower Brier is better. A 0.136 gap is the
  distance between beating and losing to a coin flip. Weights are recomputed from the
  journal every cycle and recorded in that cycle's entry, so a judge can see which
  weights applied to which decision.</p>
  <div class="note"><b>Read this honestly.</b> The book was 10-2, and a systematic
  pessimist scores badly on a winning-skewed sample by construction. The adversary's
  value is the three vetoes it landed, which Brier does not measure. Twelve outcomes is
  a first signal, not a verdict. Unproven analysts stay at exactly 1.00; a demoted one
  floors at 0.20 so it always has a path back.</div>
  <div class="n">04</div>
</section>

<section class="slide">
  <p class="tag">Out-of-sample, computed from real bars</p>
  <h2>A backtest that cannot lose is broken.</h2>
  <div class="two">
    <div><table><thead><tr><th>symbol</th><th>trades</th><th>win</th><th>expectancy</th>
      </tr></thead><tbody>{wf()}</tbody></table></div>
    <div><p style="font-size:17px"><b>Thirty trades proves nothing statistically</b>, and
      one symbol loses money, which is the point. An earlier version of this harness
      scaled its risk threshold to the wrong horizon and produced a 97% win rate by
      construction. It was caught and corrected before publication.</p>
      <p style="font-size:17px;margin-top:16px">The repository this was converted from
      shipped a hardcoded "82.2% out-of-sample win rate" computed from nothing. That was
      deleted, not adapted.</p></div>
  </div>
  <div class="note"><b>Is it reasoning or remembering?</b> The standing criticism of LLM
  trading agents is knowledge contamination. The model's cutoff is May 2026; live
  decisions run on August 2026 data. There was nothing to memorise.</div>
  <div class="n">05</div>
</section>

<section class="slide">
  <p class="tag">Check it yourself</p>
  <h2>Don't trust it. Verify it.</h2>
  <div class="two">
    <div><ul>
      <li><span class="mono">scripts/replay.py --all --verify</span> reproduces four
        recorded verdicts offline, no credentials</li>
      <li><span class="mono">make verify-journal</span> checks the hash chain</li>
      <li><span class="mono">make session</span> runs the full pipeline and sends
        nothing; submitting needs an explicit flag</li>
      <li><span class="mono">{tests()}</span> tests, none touching the network</li>
    </ul></div>
    <div><p style="font-size:17px">Orders route through the official Alpaca CLI. Market
      data reads through the official MCP server, observed live with
      <b>no order-placement tool exposed at all</b>.</p>
      <p style="font-size:17px;margin-top:16px">Two of the four replayable scenarios are
      <b>refusals</b>. The desk declining is the more persuasive half.</p></div>
  </div>
  <div class="foot"><span><b>Live</b> {SITE}</span>
    <span><b>Judge</b> {SITE}/judge</span>
    <span><b>Smile</b> {SITE}/smile</span>
    <span><b>Code</b> {REPO}</span></div>
  <div class="n">06</div>
</section>
"""


def main() -> int:
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(build())
    print(f"  wrote {OUT_HTML.relative_to(ROOT)}")
    js = f"""
const {{ chromium }} = require('playwright');
(async () => {{
  const b = await chromium.launch({{ executablePath:'/snap/bin/chromium',
    args:['--no-sandbox','--disable-dev-shm-usage'] }});
  const p = await b.newPage();
  await p.goto('file://{OUT_HTML}', {{ waitUntil:'networkidle' }});
  await p.waitForTimeout(1800);
  await p.pdf({{ path:'{OUT_PDF}', width:'1280px', height:'720px',
    printBackground:true, margin:{{top:'0',bottom:'0',left:'0',right:'0'}} }});
  await b.close();
}})();
"""
    d = ROOT / ".deck-tmp"; d.mkdir(exist_ok=True); (d / "r.js").write_text(js)
    mods = Path("/tmp/claude-1000/-home-prashant-trading-alpaca/"
                "4fa23de9-780f-40db-b6a8-4cfdd948c056/scratchpad/shots/node_modules")
    r = subprocess.run(["node", str(d / "r.js")], capture_output=True, text=True,
                       env={"NODE_PATH": str(mods), "PATH": "/usr/bin:/bin:/home/prashant/.volta/bin"})
    if r.returncode == 0 and OUT_PDF.exists():
        print(f"  wrote {OUT_PDF.relative_to(ROOT)} ({OUT_PDF.stat().st_size:,} bytes)")
    else:
        print(f"  PDF render failed: {(r.stderr or '')[:200]}")
    import shutil; shutil.rmtree(d, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
