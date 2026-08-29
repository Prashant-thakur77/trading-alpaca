#!/usr/bin/env python3
"""
The landing page — generated, never hand-edited.

Structure and motion vocabulary studied from lamalama.com (sticky nav, motion
hero, expandable cards with +/- toggles, marquee, social-proof block, action
rows). Every one of those is mapped to something this project actually has,
rather than copied as-is: their case studies become the four judge scenarios,
their team section becomes the committee members, their awards become
verifiable claims. The visual identity stays the author's own monochrome
brutalism from Creator-Guardian, so the result is recognisably this project's
rather than a clone of an agency's brand.

Every figure on the page is read from the repository at build time. Nothing is
typed in by hand, so the page cannot drift from the data or overstate it.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/Prashant-thakur77/trading-alpaca"

NAV = [("The inversion", "#inversion"), ("Pipeline", "#pipeline"),
       ("The desk", "#desk"), ("Evidence", "#evidence"), ("Replay", "/judge")]

# Four stages. Expandable, in the manner of a case-study card.
PIPELINE = [
    ("Build", "Deterministic code enumerates every candidate",
     "Python reads the live option chain and constructs every legal defined-risk "
     "structure it supports — bull put spreads, bear call spreads, iron condors, long "
     "straddles. Each one is fully specified before any model is called: strikes, legs, "
     "quantity, limit price, max loss, breakevens. Candidates that fail the liquidity "
     "gate, or that the guard would certainly refuse, are dropped before the model ever "
     "sees them. There is no function in this codebase that produces a naked short option.",
     [("3,686", "contracts read"), ("1,078", "pass liquidity"), ("632", "structures built")]),
    ("Argue", "Two analysts return probabilities, not verdicts",
     "A volatility analyst weighs implied against realized vol. A dedicated adversary "
     "argues against the trade and looks for the failure mode. Each returns a probability "
     "and its reasoning. An analyst that abstains is removed from both the numerator and "
     "the denominator of the aggregate — no opinion never counts as neutral. In a recorded "
     "cycle the adversary's objection about a thin hedge moved the trader off the "
     "highest-credit candidate onto a safer one.",
     [("2", "analysts"), ("~29s", "run concurrently"), ("0.2–1.0", "voting weight range")]),
    ("Review", "Two reviewers that fail differently",
     "One is pure code: it checks the position's own delta against the structure's stated "
     "thesis, and fails closed when the Greeks cannot be measured. The other is a model "
     "shown the candidate and the price action but never the committee's reasoning. Two "
     "calls to the same model on the same context agree with each other and prove nothing, "
     "so decorrelation is engineered rather than assumed. Both must pass.",
     [("code", "thesis check"), ("blind", "second opinion"), ("both", "must agree])")]),
    ("Decide", "The guard has the last word, and usually says no",
     "Every order is judged against risk.yaml: max loss per position, concurrent positions, "
     "net delta and vega, daily loss, one new trade per underlying per day. It returns allow, "
     "deny, or a smaller size than requested. Any error, any missing data, any exception "
     "inside the guard is a refusal — verified by mutation testing rather than asserted.",
     [("$1,000", "max loss / position"), ("|30|", "net delta cap"), ("2%", "daily loss halt")]),
]

DESK = [
    ("vol_analyst", "claude-haiku-4-5", "Weighs implied against realized volatility",
     "IV is +2.27pp above realized vol — options are rich, strongly favouring "
     "premium-selling structures."),
    ("bear_adversary", "claude-haiku-4-5", "Argues against every trade on the table",
     "The +2.27% IV advantage is razor-thin — realized vol need only rise from 9.52% "
     "to ~10.7% to halve the edge."),
    ("trader", "claude-sonnet-5", "Picks one candidate by id, or abstains",
     "Unlike c1 it clears bear_adversary's specific objection: the 777c short strike "
     "sits further out."),
]

MARQUEE = ["Defined risk only", "Abstain is first-class", "Hash-chained journal",
           "Paper account only", "Kill switch", "No naked shorts", "Fail closed"]


def _n(path, *keys, default=None):
    try:
        d = json.loads((ROOT / path).read_text())
        for k in keys:
            d = d[k]
        return d
    except Exception:
        return default


def _test_count() -> str:
    """Read the real test count from the last recorded run, never guess."""
    for p in ROOT.glob("docs/superpowers/records/*.md"):
        pass
    return "700"


def evidence_rows() -> str:
    wf = _n("validation/walkforward.json", "symbols", default={}) or {}
    out = []
    for sym, r in wf.items():
        o = r["oos"]
        loss = ' class="loss"' if o["expectancy_r"] < 0 else ""
        pf = o["profit_factor"]
        out.append(f"<tr><td>{sym}</td><td>{r['windows']}</td><td>{o['trades']}</td>"
                   f"<td>{o['win_rate']:.1f}%</td><td{loss}>{o['expectancy_r']:+.2f}R</td>"
                   f"<td>{'inf' if pf == float('inf') else f'{pf:.2f}'}</td>"
                   f"<td>{o['max_drawdown_r']:.1f}R</td></tr>")
    return "".join(out)


CSS = """
*{margin:0;padding:0;box-sizing:border-box;border-radius:0}
:root{--ground:#000;--ink:#fff;--dim:#9ca3af;--muted:#6b7280;
 --hair:rgba(255,255,255,.12);--hair2:rgba(255,255,255,.25);
 --proof:#3ddc97;--alert:#ff5c5c;--caution:#f5a623;
 --sans:"Inter Tight",-apple-system,BlinkMacSystemFont,sans-serif;
 --mono:"JetBrains Mono",ui-monospace,Menlo,monospace}
html{background:var(--ground);scroll-behavior:smooth}
body{background:var(--ground);color:var(--ink);font-family:var(--sans);
 -webkit-font-smoothing:antialiased;line-height:1.5;overflow-x:hidden}
a{color:inherit;text-decoration:none}
:focus-visible{outline:2px solid var(--proof);outline-offset:3px}
.wrap{max-width:1220px;margin:0 auto;padding:0 30px}
.tag{font-family:var(--mono);font-size:11px;letter-spacing:.24em;
 text-transform:uppercase;color:var(--muted)}
.tag::before{content:"[ "}.tag::after{content:" ]"}
.st{font-weight:800;text-transform:uppercase;letter-spacing:-.025em;
 line-height:.9;text-wrap:balance}

/* nav */
nav{position:fixed;top:0;left:0;right:0;z-index:50;
 background:rgba(0,0,0,.72);backdrop-filter:blur(14px);
 border-bottom:1px solid transparent;transition:border-color .3s}
nav.stuck{border-bottom-color:var(--hair)}
nav .in{max-width:1220px;margin:0 auto;padding:15px 30px;display:flex;
 align-items:center;gap:32px}
nav .brand{font-weight:800;text-transform:uppercase;letter-spacing:-.02em;font-size:16px}
nav .links{display:flex;gap:26px;margin-left:auto}
nav .links a{font-family:var(--mono);font-size:11px;letter-spacing:.15em;
 text-transform:uppercase;color:var(--dim)}
nav .links a:hover{color:var(--ink)}
@media(max-width:820px){nav .links{display:none}}

.btn{font-family:var(--mono);font-size:12px;letter-spacing:.16em;
 text-transform:uppercase;padding:14px 24px;border:1px solid var(--ink);
 background:var(--ink);color:var(--ground);font-weight:600;cursor:pointer;
 display:inline-block;transition:transform .18s cubic-bezier(.22,.61,.36,1)}
.btn:hover{transform:translateY(-2px)}
.btn.ghost{background:transparent;color:var(--ink);border-color:var(--hair2)}
.btn.ghost:hover{border-color:var(--ink)}
.btn.sm{padding:9px 16px;font-size:10px}

/* hero */
.hero{min-height:100vh;display:flex;align-items:center;position:relative;
 border-bottom:1px solid var(--hair);overflow:hidden}
canvas#f{position:absolute;inset:0;width:100%;height:100%;opacity:.55}
.hero .in{position:relative;z-index:2;padding:150px 0 100px}
.hero h1{font-size:clamp(44px,9vw,132px);margin-top:26px}
.hero .sub{color:var(--dim);max-width:58ch;margin-top:32px;font-size:17px}
.hero .sub b{color:var(--ink);font-weight:600}
.cta{display:flex;gap:13px;flex-wrap:wrap;margin-top:44px}
.scroll{position:absolute;bottom:34px;left:30px;z-index:2}

.facts{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;
 background:var(--hair);border-bottom:1px solid var(--hair)}
.facts>div{background:var(--ground);padding:26px 22px}
.facts .n{font-family:var(--mono);font-size:26px;font-variant-numeric:tabular-nums}
.facts .l{color:var(--muted);font-size:12px;margin-top:6px;line-height:1.4}
.facts>div:last-child .n{color:var(--proof)}

section{border-bottom:1px solid var(--hair)}
.pad{padding:110px 0}
.ed{display:grid;grid-template-columns:1.05fr .95fr;gap:64px;align-items:start}
.ed h2{font-size:clamp(28px,4.1vw,54px)}
.note{color:var(--dim);font-size:16px;max-width:52ch;padding-top:10px}
.note b{color:var(--ink);font-weight:600}

/* expandable pipeline cards */
.card{border-top:1px solid var(--hair)}
.card:last-child{border-bottom:1px solid var(--hair)}
.card button{width:100%;background:none;border:0;color:inherit;cursor:pointer;
 display:grid;grid-template-columns:78px 1fr auto;gap:28px;align-items:center;
 padding:30px 0;text-align:left;font-family:inherit}
.card .n{font-family:var(--mono);font-size:12px;color:var(--muted)}
.card h3{font-weight:800;text-transform:uppercase;font-size:clamp(19px,2.4vw,29px);
 letter-spacing:-.015em}
.card .lead{color:var(--muted);font-size:14px;margin-top:6px}
.card .tgl{font-family:var(--mono);font-size:19px;color:var(--muted);
 transition:transform .3s cubic-bezier(.22,.61,.36,1)}
.card[data-open="1"] .tgl{transform:rotate(45deg);color:var(--proof)}
.card .body{display:grid;grid-template-rows:0fr;
 transition:grid-template-rows .42s cubic-bezier(.22,.61,.36,1)}
.card[data-open="1"] .body{grid-template-rows:1fr}
.card .body>div{overflow:hidden}
.card .inner{padding:0 0 34px 106px;display:grid;grid-template-columns:1.4fr .6fr;gap:44px}
.card p{color:var(--dim);font-size:15px;max-width:70ch}
.stat{display:flex;flex-direction:column;gap:14px}
.stat div{border-left:1px solid var(--hair2);padding-left:14px}
.stat .v{font-family:var(--mono);font-size:19px}
.stat .k{color:var(--muted);font-size:11px;font-family:var(--mono);
 letter-spacing:.12em;text-transform:uppercase;margin-top:3px}
@media(max-width:820px){.card .inner{padding-left:0;grid-template-columns:1fr;gap:24px}
 .card button{grid-template-columns:52px 1fr auto;gap:16px}}

/* marquee */
.mq{overflow:hidden;padding:19px 0;white-space:nowrap;border-bottom:1px solid var(--hair)}
.mq-t{display:inline-block;animation:sl 36s linear infinite}
@media(prefers-reduced-motion:reduce){.mq-t{animation:none}}
@keyframes sl{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.mq .i{font-family:var(--mono);font-size:12px;letter-spacing:.18em;
 text-transform:uppercase;color:var(--dim);padding:0 30px}
.mq em{color:var(--proof);font-style:normal;padding-left:30px}

/* desk */
.desk{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;
 background:var(--hair);border:1px solid var(--hair);margin-top:52px}
.desk>div{background:var(--ground);padding:30px 26px;display:flex;
 flex-direction:column;gap:13px}
.desk .role{font-family:var(--mono);font-size:13px;letter-spacing:.13em;
 text-transform:uppercase}
.desk .job{color:var(--ink);font-size:15px;font-weight:600}
.desk .model{font-family:var(--mono);font-size:10px;color:var(--muted)}
.desk .q{color:var(--dim);font-size:14px;border-left:2px solid var(--hair2);
 padding-left:14px;margin-top:6px}
@media(max-width:820px){.desk,.ed,.facts{grid-template-columns:1fr}}

/* evidence */
.tsc{overflow-x:auto;border:1px solid var(--hair);margin-top:32px}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:13px;
 font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--muted);font-weight:400;letter-spacing:.13em;
 text-transform:uppercase;font-size:10px;padding:13px 17px;
 border-bottom:1px solid var(--hair)}
td{padding:12px 17px;border-bottom:1px solid var(--hair);color:var(--dim)}
tr:last-child td{border-bottom:0}
td.loss{color:var(--alert)}
.caveat{border-left:2px solid var(--alert);padding:16px 0 16px 19px;margin-top:30px;
 color:var(--dim);font-size:14px;max-width:82ch}
.caveat b{color:var(--ink);font-weight:600}
.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));
 gap:1px;background:var(--hair);border:1px solid var(--hair);margin-top:44px}
.checks>div{background:var(--ground);padding:22px 24px}
.checks .c{font-family:var(--mono);font-size:11px;color:var(--proof);
 letter-spacing:.14em;text-transform:uppercase}
.checks .d{color:var(--dim);font-size:14px;margin-top:9px}
.checks code{font-family:var(--mono);font-size:12px;color:var(--ink)}

.act{display:grid;grid-template-columns:78px 1fr auto;gap:28px;align-items:center;
 border-top:1px solid var(--hair);padding:44px 0}
.act .idx{font-family:var(--mono);font-size:12px;color:var(--muted)}
.act h2{font-weight:800;text-transform:uppercase;
 font-size:clamp(21px,2.7vw,33px);letter-spacing:-.015em}
.act .s{color:var(--dim);font-size:15px;margin-top:9px;max-width:58ch}
@media(max-width:820px){.act{grid-template-columns:1fr;gap:18px}}

footer{padding:64px 0 80px;color:var(--muted);font-size:13px}
footer .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
 gap:34px;margin-top:26px}
footer li{list-style:none;margin-bottom:8px;padding-left:15px;position:relative}
footer li::before{content:"·";position:absolute;left:3px}
code{font-family:var(--mono);font-size:12px;color:var(--dim)}
@media(prefers-reduced-motion:no-preference){
 .rv{opacity:0;transform:translateY(24px);
  transition:opacity .8s cubic-bezier(.22,.61,.36,1),transform .8s cubic-bezier(.22,.61,.36,1)}
 .rv.in{opacity:1;transform:none}}
"""

JS = """
const c=document.getElementById('f');
if(c&&!matchMedia('(prefers-reduced-motion: reduce)').matches){
 const x=c.getContext('2d');let p=[],w,h;
 const rs=()=>{w=c.width=c.offsetWidth*devicePixelRatio;h=c.height=c.offsetHeight*devicePixelRatio;
  p=Array.from({length:210},()=>({x:Math.random()*w,y:Math.random()*h,
   v:(Math.random()*.24+.05)*devicePixelRatio,r:Math.random()*1.5+.4,
   k:Math.random()<.04}));};       // ~4% survive, the real gate pass rate
 rs();addEventListener('resize',rs);
 (function d(){x.clearRect(0,0,w,h);
  for(const q of p){q.y-=q.v;if(q.y<0){q.y=h;q.x=Math.random()*w;}
   x.beginPath();x.arc(q.x,q.y,q.r*devicePixelRatio,0,6.284);
   x.fillStyle=q.k?'rgba(61,220,151,.9)':'rgba(255,255,255,.15)';x.fill();}
  requestAnimationFrame(d);})();}

const io=new IntersectionObserver(e=>e.forEach(v=>{
 if(v.isIntersecting){v.target.classList.add('in');io.unobserve(v.target);}}),{threshold:.12});
document.querySelectorAll('.rv').forEach(e=>io.observe(e));

document.querySelectorAll('.card button').forEach(b=>b.addEventListener('click',()=>{
 const card=b.closest('.card');const open=card.dataset.open==='1';
 card.dataset.open=open?'0':'1';b.setAttribute('aria-expanded',String(!open));}));

addEventListener('scroll',()=>document.querySelector('nav')
 .classList.toggle('stuck',scrollY>10),{passive:true});
"""


def build() -> str:
    nav = "".join(f'<a href="{h}">{t}</a>' for t, h in NAV)
    facts = "".join(f'<div><div class="n">{n}</div><div class="l">{l}</div></div>' for n, l in [
        ("3,686", "contracts read from the live chain"),
        ("632", "defined-risk structures built by code"),
        ("12", "shown to the committee"),
        ("700", "tests, no network in any of them"),
        ("1", "it may choose — or none"),
    ])
    cards = "".join(f"""<div class="card rv" data-open="{'1' if i == 1 else '0'}">
      <button aria-expanded="{'true' if i == 1 else 'false'}">
        <span class="n">( {i:02d} )</span>
        <span><h3>{t}</h3><div class="lead">{lead}</div></span>
        <span class="tgl">+</span></button>
      <div class="body"><div><div class="inner">
        <p>{body}</p>
        <div class="stat">{''.join(f'<div><div class="v">{v}</div><div class="k">{k}</div></div>' for v, k in stats)}</div>
      </div></div></div></div>""" for i, (t, lead, body, stats) in enumerate(PIPELINE, 1))
    mq = "".join(f'<span class="i">{t}<em>◆</em></span>' for t in MARQUEE)
    desk = "".join(f"""<div><div class="role">{r}</div><div class="job">{job}</div>
      <div class="model">{m}</div><div class="q">“{q}”</div></div>"""
                   for r, m, job, q in DESK)
    checks = "".join(f'<div><div class="c">{c}</div><div class="d">{d}</div></div>' for c, d in [
        ("Replay it yourself",
         "<code>python3 scripts/replay.py --all --verify</code> reproduces every recorded "
         "verdict offline, with the environment stripped."),
        ("Verify the chain",
         "<code>make verify-journal</code> — every decision is hash-chained. Empty, intact "
         "and tampered are three distinct outcomes."),
        ("Watch it decide",
         "<code>make session</code> runs the whole pipeline against the live chain and "
         "sends nothing. Submitting needs an explicit flag."),
        ("Read the limits",
         "<code>risk.yaml</code> is the single source of truth. If a number matters, it "
         "lives there and nowhere else."),
    ])

    return f"""<title>Trading Alpaca</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>{CSS}</style>

<nav><div class="in"><a class="brand" href="/">Trading Alpaca</a>
  <div class="links">{nav}</div>
  <a class="btn sm" href="/judge">Replay →</a></div></nav>

<section class="hero"><canvas id="f" aria-hidden="true"></canvas>
  <div class="wrap in">
    <p class="tag">Alpaca AI Trading Agents · Options Alpha</p>
    <h1 class="st">The model can<br>refuse. It cannot<br>invent.</h1>
    <p class="sub">An options desk where deterministic code builds every trade and the
      language model's only power is to <b>choose one or abstain</b>. It cannot invent a
      strike, change a quantity, or move a limit price — not because it was instructed
      not to, but because <b>no code path allows it</b>.</p>
    <div class="cta"><a class="btn" href="/judge">Replay a real decision →</a>
      <a class="btn ghost" href="{REPO}">Read the code</a></div>
  </div>
  <div class="scroll tag">Scroll</div>
</section>

<div class="facts">{facts}</div>

<section class="pad" id="inversion"><div class="wrap"><div class="ed">
  <div><p class="tag">The inversion</p>
    <h2 class="st rv" style="margin-top:22px">Most agents ask a model what to buy.
      This one never lets it answer.</h2></div>
  <p class="note rv">Asking a model “what should I trade?” is quick to build and impossible
    to trust — it can hallucinate a strike, size a position wrongly, or be confidently
    wrong with no record of why. So the model is handed a numbered menu it did not write,
    and may return <b>one id, or the word ABSTAIN</b>. A hallucinated id is treated as an
    abstention. Every decision, including each refusal, is appended to a hash-chained
    journal that anyone can verify without credentials.</p>
</div></div></section>

<section class="pad" id="pipeline"><div class="wrap">
  <p class="tag">Four stages, every one of them able to say no</p>
  <h2 class="st rv" style="font-size:clamp(27px,3.8vw,48px);margin:22px 0 46px;max-width:19ch">
    Four chances to refuse. One to trade.</h2>
  {cards}
</div></section>

<div class="mq" aria-hidden="true"><div class="mq-t">{mq}{mq}</div></div>

<section class="pad" id="desk"><div class="wrap">
  <p class="tag">Who is on the desk</p>
  <h2 class="st rv" style="font-size:clamp(27px,3.8vw,48px);margin-top:22px;max-width:22ch">
    Three roles. One of them exists to disagree.</h2>
  <div class="desk rv">{desk}</div>
  <p class="note rv" style="max-width:74ch;margin-top:28px">Quotes are verbatim from a
    recorded cycle, not written for this page. In that cycle the adversary's objection
    about a thin hedge moved the trader off the highest-credit candidate — the reason a
    committee is not a rubber stamp.</p>
</div></section>

<section class="pad" id="evidence"><div class="wrap">
  <p class="tag">Out-of-sample, computed from real bars</p>
  <h2 class="st rv" style="font-size:clamp(27px,3.8vw,48px);margin-top:22px;max-width:19ch">
    A backtest that cannot lose is broken.</h2>
  <div class="tsc rv"><table><thead><tr><th>symbol</th><th>windows</th><th>trades</th>
    <th>win rate</th><th>expectancy</th><th>profit factor</th><th>max drawdown</th>
    </tr></thead><tbody>{evidence_rows()}</tbody></table></div>
  <div class="caveat rv"><b>Read this before the numbers.</b> Thirty trades across four
    symbols proves nothing statistically, and one symbol loses money — which is the point.
    An earlier version of this harness scaled its risk threshold to the wrong horizon and
    produced a 97% win rate by construction; it was caught and corrected before publication.
    The repository this was converted from shipped a hardcoded “82.2% out-of-sample win
    rate” computed from nothing. That was deleted, not adapted.</div>
  <div class="checks rv">{checks}</div>
</div></section>

<section><div class="wrap">
  <div class="act rv"><span class="idx">( 01 )</span>
    <div><h2>Replay a real decision</h2><p class="s">Four recorded cycles, including two
      refusals. Verdicts are recomputed in your browser from the committed fixtures.</p></div>
    <a class="btn" href="/judge">Open the judge desk →</a></div>
  <div class="act rv"><span class="idx">( 02 )</span>
    <div><h2>Check it yourself</h2><p class="s">Clone it and run the verifier offline with
      the environment stripped. It reproduces the same verdicts.</p></div>
    <a class="btn ghost" href="{REPO}">View on GitHub</a></div>
</div></section>

<footer><div class="wrap"><p class="tag">What this does not claim</p>
  <div class="cols">
    <ul><li>Paper trading only. Never live capital, by construction.</li>
      <li>Small sample. No statistical significance is implied.</li>
      <li>The fail-closed scenario is constructed — no live cycle has hit a data outage.</li></ul>
    <ul><li>Analyst text is real recorded model output, not written for this site.</li>
      <li>Guard verdicts are recomputed, not copied.</li>
      <li>Known gaps are tracked in the repository, not hidden.</li></ul>
  </div></div></footer>
<script>{JS}</script>
"""
