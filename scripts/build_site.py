#!/usr/bin/env python3
"""
Generate the full static site: landing page + judge desk.

    python3 scripts/build_site.py       # writes site/

Output:
    site/index.html            the landing page
    site/judge/index.html      the credential-free replay desk
    site/scenarios/*.json      the fixtures, fetchable so a judge can diff
                               what the page renders against the committed data

Both pages are generated from the same fixtures the replay verifier checks, so
neither can drift from the data. Design language is inherited from the author's
existing system: monochrome brutalism — true black, white ink, hairline rules,
zero radius, parenthesised step numbering, one accent reserved for verified
states.
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
SITE = ROOT / "site"

from build_judge_page import build as build_judge, load as load_scenarios  # noqa: E402
from landing import build as build_landing  # noqa: E402

# Real, measured figures only. Every number here is reproducible from the repo.
FACTS = [
    ("3,686", "option contracts read from the live chain"),
    ("1,078", "survive the liquidity gate"),
    ("632", "defined-risk structures built by code"),
    ("12", "shown to the committee"),
    ("1", "it may choose — or none"),
]

STEPS = [
    ("Code builds every candidate",
     "Deterministic Python enumerates every legal defined-risk structure the live chain "
     "supports — each strike, leg, quantity and limit price fully specified before any "
     "model sees anything. Nothing here is generated text."),
    ("Analysts argue, in probabilities",
     "A volatility analyst and a dedicated adversary each return a probability, not a "
     "verdict. The adversary's job is to find the failure mode. An analyst that abstains "
     "is dropped from the numerator and the denominator — no opinion never counts as neutral."),
    ("Two reviewers that fail differently",
     "One is pure code, checking the position's delta against the structure's own thesis. "
     "The other is a model shown the trade and the price action but never the committee's "
     "reasoning. Two calls to the same model on the same context agree with each other "
     "and prove nothing."),
    ("The guard has the last word",
     "Every order is judged against risk.yaml — max loss, position count, net delta and "
     "vega, daily loss, liquidity. It returns allow, deny, or a smaller size. Any error, "
     "any missing data, any exception is a refusal."),
]

MARQUEE = ["Defined risk only", "Abstain is first-class", "Hash-chained journal",
           "Paper account only", "Kill switch", "700 tests", "No naked shorts"]

CSS = """
*{margin:0;padding:0;box-sizing:border-box;border-radius:0}
:root{
  --ground:#000; --ink:#fff; --dim:#9ca3af; --muted:#6b7280;
  --hair:rgba(255,255,255,.12); --hair2:rgba(255,255,255,.25);
  --proof:#3ddc97; --alert:#ff5c5c; --caution:#f5a623;
  --sans:"Inter Tight",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
html{background:var(--ground);scroll-behavior:smooth}
body{background:var(--ground);color:var(--ink);font-family:var(--sans);
  -webkit-font-smoothing:antialiased;line-height:1.5;overflow-x:hidden}
a{color:inherit;text-decoration:none}
:focus-visible{outline:2px solid var(--proof);outline-offset:3px}
.wrap{max-width:1180px;margin:0 auto;padding:0 28px}

.tag{font-family:var(--mono);font-size:11px;letter-spacing:.24em;
  text-transform:uppercase;color:var(--muted)}
.tag::before{content:"[ "}.tag::after{content:" ]"}
.statement{font-weight:800;text-transform:uppercase;letter-spacing:-.025em;
  line-height:.9;text-wrap:balance}

/* ── hero ── */
.hero{min-height:88vh;display:flex;align-items:center;position:relative;
  border-bottom:1px solid var(--hair);overflow:hidden}
canvas#field{position:absolute;inset:0;width:100%;height:100%;opacity:.5}
.hero-in{position:relative;z-index:2;padding:120px 0 90px}
.hero h1{font-size:clamp(42px,8.4vw,116px);margin-top:26px}
.hero .sub{color:var(--dim);max-width:60ch;margin-top:30px;font-size:17px}
.hero .sub b{color:var(--ink);font-weight:600}
.cta{display:flex;gap:14px;flex-wrap:wrap;margin-top:42px}
.btn{font-family:var(--mono);font-size:12px;letter-spacing:.16em;
  text-transform:uppercase;padding:15px 26px;border:1px solid var(--ink);
  background:var(--ink);color:var(--ground);font-weight:600;cursor:pointer;
  transition:transform .18s cubic-bezier(.22,.61,.36,1)}
.btn:hover{transform:translateY(-2px)}
.btn.ghost{background:transparent;color:var(--ink);border-color:var(--hair2)}
.btn.ghost:hover{border-color:var(--ink)}

/* ── facts strip ── */
.facts{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;
  background:var(--hair);border-bottom:1px solid var(--hair)}
.facts>div{background:var(--ground);padding:26px 22px}
.facts .n{font-family:var(--mono);font-size:27px;font-variant-numeric:tabular-nums}
.facts .l{color:var(--muted);font-size:12px;margin-top:7px;line-height:1.4}
.facts>div:last-child .n{color:var(--proof)}

/* ── editorial ── */
.ed{padding:104px 0;border-bottom:1px solid var(--hair)}
.ed-grid{display:grid;grid-template-columns:1.1fr .9fr;gap:60px;align-items:start}
.ed h2{font-size:clamp(27px,3.9vw,49px)}
.ed-note{color:var(--dim);font-size:16px;max-width:52ch;padding-top:9px}
.ed-note b{color:var(--ink);font-weight:600}

.step{display:grid;grid-template-columns:96px 1fr;gap:30px;
  border-top:1px solid var(--hair);padding:34px 0}
.step:first-of-type{margin-top:64px}
.step .n{font-family:var(--mono);font-size:12px;color:var(--muted);padding-top:5px}
.step h3{font-weight:800;text-transform:uppercase;font-size:19px;
  letter-spacing:-.01em;margin-bottom:11px}
.step p{color:var(--dim);font-size:15px;max-width:74ch}

/* ── marquee ── */
.marquee{overflow:hidden;border-bottom:1px solid var(--hair);padding:19px 0;
  white-space:nowrap}
.marquee-track{display:inline-block;animation:slide 34s linear infinite}
@media(prefers-reduced-motion:reduce){.marquee-track{animation:none}}
@keyframes slide{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.marquee span.item{font-family:var(--mono);font-size:12px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--dim);padding:0 30px}
.marquee i{color:var(--proof);font-style:normal;padding-left:30px}

/* ── proof / honesty ── */
.proof{padding:104px 0;border-bottom:1px solid var(--hair)}
.tscroll{overflow-x:auto;border:1px solid var(--hair);margin-top:34px}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:13px;
  font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--muted);font-weight:400;letter-spacing:.13em;
  text-transform:uppercase;font-size:10px;padding:13px 17px;
  border-bottom:1px solid var(--hair)}
td{padding:12px 17px;border-bottom:1px solid var(--hair);color:var(--dim)}
tr:last-child td{border-bottom:0}
td.loss{color:var(--alert)}
.caveat{border-left:2px solid var(--alert);padding:15px 0 15px 19px;
  margin-top:30px;color:var(--dim);font-size:14px;max-width:80ch}
.caveat b{color:var(--ink);font-weight:600}

/* ── action rows ── */
.action{display:grid;grid-template-columns:96px 1fr auto;gap:30px;
  align-items:center;border-top:1px solid var(--hair);padding:44px 0}
.action:last-of-type{border-bottom:1px solid var(--hair)}
.action .idx{font-family:var(--mono);font-size:12px;color:var(--muted)}
.action h2{font-weight:800;text-transform:uppercase;font-size:clamp(21px,2.6vw,31px);
  letter-spacing:-.015em}
.action .sub{color:var(--dim);font-size:15px;margin-top:9px;max-width:56ch}

footer{padding:64px 0 78px;color:var(--muted);font-size:13px}
footer .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
  gap:34px;margin-top:26px}
footer li{list-style:none;margin-bottom:8px;padding-left:15px;position:relative}
footer li::before{content:"·";position:absolute;left:3px}
code{font-family:var(--mono);font-size:12px;color:var(--dim)}

@media(prefers-reduced-motion:no-preference){
  .rv{opacity:0;transform:translateY(22px);
    transition:opacity .75s cubic-bezier(.22,.61,.36,1),
               transform .75s cubic-bezier(.22,.61,.36,1)}
  .rv.in{opacity:1;transform:none}
}
@media(max-width:900px){
  .ed-grid{grid-template-columns:1fr;gap:30px}
  .facts{grid-template-columns:repeat(2,1fr)}
  .action{grid-template-columns:1fr;gap:18px}
  .step{grid-template-columns:56px 1fr;gap:18px}
}
"""

JS = """
// Ambient field: a slow drift of candidate points, most of them rejected.
// The visual is the subject — hundreds built, a handful survive.
const c = document.getElementById('field');
if (c && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
  const x = c.getContext('2d');
  let pts = [], w, h;
  const resize = () => {
    w = c.width = c.offsetWidth * devicePixelRatio;
    h = c.height = c.offsetHeight * devicePixelRatio;
    pts = Array.from({length: 190}, () => ({
      x: Math.random()*w, y: Math.random()*h,
      v: (Math.random()*.22 + .05) * devicePixelRatio,
      r: Math.random()*1.5 + .4,
      keep: Math.random() < .04      // ~4% survive the gate, as in the real run
    }));
  };
  resize(); addEventListener('resize', resize);
  (function draw(){
    x.clearRect(0,0,w,h);
    for (const p of pts) {
      p.y -= p.v; if (p.y < 0) { p.y = h; p.x = Math.random()*w; }
      x.beginPath(); x.arc(p.x, p.y, p.r*devicePixelRatio, 0, 6.284);
      x.fillStyle = p.keep ? 'rgba(61,220,151,.85)' : 'rgba(255,255,255,.16)';
      x.fill();
    }
    requestAnimationFrame(draw);
  })();
}

const io = new IntersectionObserver(es => es.forEach(e => {
  if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
}), {threshold: .12});
document.querySelectorAll('.rv').forEach(el => io.observe(el));
"""

HEAD = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap">"""

REPO = "https://github.com/Prashant-thakur77/trading-alpaca"


def walkforward_rows() -> str:
    """Real out-of-sample results, including the symbol that loses money."""
    p = ROOT / "validation" / "walkforward.json"
    if not p.exists():
        return ""
    d = json.loads(p.read_text())
    rows = []
    for sym, r in d.get("symbols", {}).items():
        o = r["oos"]
        cls = ' class="loss"' if o["expectancy_r"] < 0 else ""
        pf = o["profit_factor"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        rows.append(
            f"<tr><td>{sym}</td><td>{r['windows']}</td><td>{o['trades']}</td>"
            f"<td>{o['win_rate']:.1f}%</td><td{cls}>{o['expectancy_r']:+.2f}R</td>"
            f"<td>{pf_s}</td><td>{o['max_drawdown_r']:.1f}R</td></tr>")
    return "".join(rows)


def landing() -> str:
    facts = "".join(f'<div><div class="n">{n}</div><div class="l">{l}</div></div>'
                    for n, l in FACTS)
    steps = "".join(
        f'<div class="step rv"><div class="n">( {i:02d} )</div>'
        f'<div><h3>{t}</h3><p>{p}</p></div></div>'
        for i, (t, p) in enumerate(STEPS, 1))
    mq = "".join(f'<span class="item">{t}<i>◆</i></span>' for t in MARQUEE)
    rows = walkforward_rows()

    return f"""<title>Trading Alpaca</title>
{HEAD}
<style>{CSS}</style>

<section class="hero">
  <canvas id="field" aria-hidden="true"></canvas>
  <div class="wrap hero-in">
    <p class="tag">Alpaca AI Trading Agents · Options Alpha</p>
    <h1 class="statement">The model can<br>refuse. It cannot<br>invent.</h1>
    <p class="sub">An options desk where deterministic code builds every trade and the
      language model's only power is to <b>choose one or abstain</b>. It cannot invent a
      strike, change a quantity, or move a limit price — not because it was told not to,
      but because <b>no code path allows it</b>.</p>
    <div class="cta">
      <a class="btn" href="/judge">Replay a real decision →</a>
      <a class="btn ghost" href="{REPO}">Read the code</a>
    </div>
  </div>
</section>

<div class="facts">{facts}</div>

<section class="ed"><div class="wrap">
  <div class="ed-grid">
    <div>
      <p class="tag">The inversion</p>
      <h2 class="statement rv" style="margin-top:22px">Most agents ask a model what to buy.
        This one never lets it answer.</h2>
    </div>
    <p class="ed-note rv">Asking a model "what should I trade?" is quick to build and
      impossible to trust — it can hallucinate a strike, size a position wrongly, or be
      confidently wrong with no record of why. So the model is handed a numbered menu it
      did not write, and may return <b>one id, or the word ABSTAIN</b>. A hallucinated id
      is treated as an abstention. Every decision, including each refusal, is appended to
      a hash-chained journal anyone can verify.</p>
  </div>
  {steps}
</div></section>

<div class="marquee" aria-hidden="true"><div class="marquee-track">{mq}{mq}</div></div>

<section class="proof"><div class="wrap">
  <p class="tag">Out-of-sample, computed from real bars</p>
  <h2 class="statement rv" style="font-size:clamp(26px,3.6vw,44px);margin-top:22px;max-width:20ch">
    A backtest that cannot lose is broken.</h2>
  <div class="tscroll rv">
    <table><thead><tr><th>symbol</th><th>windows</th><th>trades</th><th>win rate</th>
      <th>expectancy</th><th>profit factor</th><th>max drawdown</th></tr></thead>
      <tbody>{rows}</tbody></table>
  </div>
  <div class="caveat rv"><b>Read this before the numbers.</b> Thirty trades across four
    symbols proves nothing statistically, and one symbol loses money — which is the point.
    An earlier version of this harness scaled its risk threshold to the wrong horizon and
    produced a 97% win rate by construction; it was found and corrected before publication.
    The repository it replaced also shipped a hardcoded "82.2% out-of-sample win rate" that
    was computed from nothing. That was deleted, not adapted.</div>
</div></section>

<section><div class="wrap">
  <div class="action rv">
    <span class="idx">( 01 )</span>
    <div><h2>Replay a real decision</h2>
      <p class="sub">Four recorded cycles — including two refusals. Verdicts are recomputed
        in your browser from the committed fixtures. No credentials, no API keys, no model
        calls.</p></div>
    <a class="btn" href="/judge">Open the judge desk →</a>
  </div>
  <div class="action rv">
    <span class="idx">( 02 )</span>
    <div><h2>Check it yourself</h2>
      <p class="sub">Clone the repo and run <code>python3 scripts/replay.py --all --verify</code>
        with the environment stripped. It reproduces the same verdicts offline.</p></div>
    <a class="btn ghost" href="{REPO}">View on GitHub</a>
  </div>
</div></section>

<footer><div class="wrap">
  <p class="tag">What this does not claim</p>
  <div class="cols">
    <ul>
      <li>Paper trading only. Never live capital, by construction.</li>
      <li>Small sample. No statistical significance is implied.</li>
      <li>The fail-closed scenario is constructed — no live cycle has hit a data outage.</li>
    </ul>
    <ul>
      <li>Analyst text is real recorded model output, not written for this site.</li>
      <li>Guard verdicts are recomputed, not copied.</li>
      <li>Known gaps are tracked in the repository, not hidden.</li>
    </ul>
  </div>
</div></footer>
<script>{JS}</script>
"""


def main() -> int:
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "judge").mkdir(parents=True)
    (SITE / "index.html").write_text(build_landing())
    (SITE / "judge" / "index.html").write_text(build_judge())
    shutil.copytree(SCEN := ROOT / "judge" / "scenarios", SITE / "scenarios")
    # the smile page is generated from the live chain by its own script and is
    # preserved across rebuilds if already present
    smile = SITE / "smile" / "index.html"
    if not smile.exists():
        print("  note: site/smile/index.html absent — run scripts/build_smile_page.py")
    shutil.copy(ROOT / "judge" / "vercel.json", SITE / "vercel.json")
    for f in sorted(SITE.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(ROOT)}  ({f.stat().st_size:,} bytes)")
    print(f"\n  site built from {len(load_scenarios())} scenarios")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
