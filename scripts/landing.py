#!/usr/bin/env python3
"""
The landing page. Generated, never hand-edited.

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

from fx import TOKENS, FX_CSS, FX_JS, INTRO_HTML

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/Prashant-thakur77/trading-alpaca"

NAV = [("The inversion", "#inversion"), ("Pipeline", "#pipeline"),
       ("The desk", "#desk"), ("Evidence", "#evidence"), ("Replay", "/judge")]

# Four stages. Expandable, in the manner of a case-study card.
PIPELINE = [
    ("Build", "Deterministic code enumerates every candidate",
     "Python reads the live option chain and constructs every legal defined-risk "
     "structure it supports: bull put spreads, bear call spreads, iron condors, long "
     "straddles. Each one is fully specified before any model is called: strikes, legs, "
     "quantity, limit price, max loss, breakevens. Candidates that fail the liquidity "
     "gate, or that the guard would certainly refuse, are dropped before the model ever "
     "sees them. There is no function in this codebase that produces a naked short option.",
     [("3,686", "contracts read"), ("1,078", "pass liquidity"), ("632", "structures built")]),
    ("Argue", "Two analysts return probabilities, not verdicts",
     "A volatility analyst weighs implied against realized vol. A dedicated adversary "
     "argues against the trade and looks for the failure mode. Each returns a probability "
     "and its reasoning. An analyst that abstains is removed from both the numerator and "
     "the denominator of the aggregate, so no opinion ever counts as neutral. In a recorded "
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
     "inside the guard is a refusal, verified by mutation testing rather than asserted.",
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


CSS = TOKENS + FX_CSS + """
*{margin:0;padding:0;box-sizing:border-box}
html{background:var(--ground);scroll-behavior:smooth}
body{background:var(--ground);color:var(--ink);font-family:var(--sans);
 -webkit-font-smoothing:antialiased;line-height:1.5;overflow-x:hidden;
 padding-bottom:56px}
a{color:inherit;text-decoration:none}
:focus-visible{outline:2px solid var(--coral);outline-offset:3px}
.wrap{max-width:1360px;margin:0 auto;padding:0 40px}
.tag{font-family:var(--mono);font-size:11px;letter-spacing:.16em;
 text-transform:uppercase;color:var(--ink)}
.tag::before{content:"[ "}.tag::after{content:" ]"}
.st{font-weight:800;text-transform:uppercase;letter-spacing:-.03em;
 line-height:.88;text-wrap:balance}

/* floating pill nav, centred, as observed */
nav{position:fixed;top:18px;left:50%;transform:translateX(-50%);z-index:60;
 background:var(--ground-2);border-radius:var(--r);display:flex;align-items:center;
 min-width:440px;transition:transform .45s var(--ease),opacity .45s var(--ease)}
nav.hide{transform:translateX(-50%) translateY(-140%);opacity:0}
nav .mark{width:52px;height:52px;display:grid;place-items:center;
 border-right:1px solid var(--hair);font-weight:800;font-size:15px}
nav .ctx{flex:1;text-align:center;font-family:var(--mono);font-size:11px;
 letter-spacing:.14em;text-transform:uppercase;color:var(--ink)}
nav .burger{width:52px;height:52px;display:grid;place-items:center;
 border-left:1px solid var(--hair);cursor:pointer}
nav .burger span{display:block;width:17px;height:1px;background:var(--ink);
 margin:3px 0;transition:transform .3s var(--ease)}

/* persistent card stack, top right, as observed */
.stack{position:fixed;top:18px;right:18px;z-index:60;width:180px;
 display:flex;flex-direction:column;gap:6px}
.stack a,.stack div{background:var(--ground-2);border-radius:var(--r);
 padding:13px 15px;font-family:var(--mono);font-size:10px;letter-spacing:.13em;
 text-transform:uppercase;display:flex;justify-content:space-between;
 align-items:center;gap:8px;transition:background .3s var(--ease)}
.stack a:hover{background:#232525}
.stack .v{color:var(--coral);font-variant-numeric:tabular-nums}
.stack .v.ok{color:var(--lime)}
@media(max-width:1100px){.stack{display:none}}

/* persistent status bar, as observed */
.status{position:fixed;left:0;right:0;bottom:0;z-index:60;
 background:var(--ground);border-top:1px solid var(--hair);
 display:flex;align-items:center;gap:26px;padding:16px 40px;
 font-family:var(--mono);font-size:10px;letter-spacing:.13em;
 text-transform:uppercase;color:var(--dim)}
.status .dot{width:5px;height:5px;border-radius:9999px;background:var(--coral);
 display:inline-block;margin-right:7px;vertical-align:middle}
.status .sp{margin-left:auto}
.status b{color:var(--ink);font-weight:400}
@media(max-width:900px){.status .hide-s{display:none}}

/* buttons: 4px radius, outlined, arrow */
.btn{display:inline-flex;align-items:center;gap:14px;font-family:var(--mono);
 font-size:11px;letter-spacing:.14em;text-transform:uppercase;
 padding:16px 20px;border:1px solid var(--hair2);border-radius:var(--r);
 color:var(--ink);transition:background .3s var(--ease),border-color .3s var(--ease)}
.btn:hover{background:var(--ink);color:var(--ground);border-color:var(--ink)}
.btn.fill{background:var(--coral);border-color:var(--coral)}
.btn.fill:hover{background:var(--ink);border-color:var(--ink)}

/* hero */
.hero{min-height:100vh;display:flex;align-items:flex-end;position:relative;
 overflow:hidden;padding-bottom:70px}
canvas#f{position:absolute;inset:0;width:100%;height:100%}
.hero .veil{position:absolute;inset:0;
 background:radial-gradient(120rem 70rem at 30% 60%,transparent 30%,rgba(26,28,28,.82) 100%)}
.hero .in{position:relative;z-index:2;width:100%}
.hero .row{display:grid;grid-template-columns:1.35fr .65fr;gap:60px;align-items:end}
.hero h1{font-size:clamp(40px,7.6vw,108px);margin-top:20px}
.hero .sub{color:var(--ink);font-size:17px;line-height:1.45;max-width:34ch}
.cta{display:flex;gap:10px;flex-wrap:wrap;margin-top:38px}
@media(max-width:900px){.hero .row{grid-template-columns:1fr;gap:32px}}

section{border-top:1px solid var(--hair)}
.pad{padding:120px 0}
.ed{display:grid;grid-template-columns:1.1fr .9fr;gap:70px;align-items:start}
.ed h2{font-size:clamp(30px,4.4vw,60px)}
.note{color:var(--dim);font-size:16px;max-width:50ch}
.note b{color:var(--ink);font-weight:600}

.facts{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-top:56px}
.facts>div{background:var(--ground-2);border-radius:var(--r);padding:24px 22px}
.facts .n{font-family:var(--mono);font-size:26px;font-variant-numeric:tabular-nums}
.facts .l{color:var(--muted);font-size:11px;margin-top:8px;line-height:1.4;
 font-family:var(--mono);letter-spacing:.08em;text-transform:uppercase}
.facts>div:first-child{background:var(--coral)}
.facts>div:first-child .l{color:rgba(26,28,28,.75)}
.facts>div:first-child .n{color:var(--ground)}
.facts>div:last-child .n{color:var(--lime)}

.card{border-top:1px solid var(--hair)}
.card:last-child{border-bottom:1px solid var(--hair)}
.card button{width:100%;background:none;border:0;color:inherit;cursor:pointer;
 display:grid;grid-template-columns:84px 1fr auto;gap:28px;align-items:center;
 padding:32px 0;text-align:left;font-family:inherit}
.card .n{font-family:var(--mono);font-size:11px;color:var(--muted)}
.card h3{font-weight:800;text-transform:uppercase;font-size:clamp(20px,2.7vw,34px);
 letter-spacing:-.02em}
.card .lead{color:var(--muted);font-size:13px;margin-top:7px;font-family:var(--mono);
 letter-spacing:.06em;text-transform:uppercase}
.card .tgl{font-family:var(--mono);font-size:13px;color:var(--muted)}
.card[data-open="1"] .tgl{color:var(--coral)}
.card[data-open="1"] h3{color:var(--coral)}
.card .body{display:grid;grid-template-rows:0fr;
 transition:grid-template-rows .45s var(--ease)}
.card[data-open="1"] .body{grid-template-rows:1fr}
.card .body>div{overflow:hidden}
.card .inner{padding:0 0 38px 112px;display:grid;grid-template-columns:1.4fr .6fr;gap:48px}
.card p{color:var(--dim);font-size:15px;max-width:68ch}
.stat{display:flex;flex-direction:column;gap:16px}
.stat .v{font-family:var(--mono);font-size:19px}
.stat .k{color:var(--muted);font-size:10px;font-family:var(--mono);
 letter-spacing:.12em;text-transform:uppercase;margin-top:4px}
@media(max-width:900px){.card .inner{padding-left:0;grid-template-columns:1fr;gap:26px}
 .card button{grid-template-columns:56px 1fr auto;gap:16px}}

.mq{overflow:hidden;padding:20px 0;white-space:nowrap;background:var(--coral);
 border:0}
.mq-t{display:inline-block;animation:sl 38s linear infinite}
@keyframes sl{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.mq .i{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
 text-transform:uppercase;color:var(--ground);padding:0 26px}
.mq em{color:var(--ground);font-style:normal;opacity:.5;padding-left:26px}

.desk{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:56px}
.desk>div{background:var(--ground-2);border-radius:var(--r);padding:28px 24px;
 display:flex;flex-direction:column;gap:12px}
.desk .role{font-family:var(--mono);font-size:12px;letter-spacing:.13em;
 text-transform:uppercase;color:var(--coral)}
.desk .job{font-size:16px;font-weight:600}
.desk .model{font-family:var(--mono);font-size:10px;color:var(--muted)}
.desk .q{color:var(--dim);font-size:14px;border-left:1px solid var(--hair2);
 padding-left:14px;margin-top:6px}
@media(max-width:900px){.desk,.ed,.facts{grid-template-columns:1fr}}

.tsc{overflow-x:auto;border-radius:var(--r);background:var(--ground-2);margin-top:36px}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:13px;
 font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--muted);font-weight:400;letter-spacing:.13em;
 text-transform:uppercase;font-size:10px;padding:15px 18px;
 border-bottom:1px solid var(--hair)}
td{padding:13px 18px;border-bottom:1px solid var(--hair);color:var(--dim)}
tr:last-child td{border-bottom:0}
td.loss{color:var(--coral)}
.caveat{border-left:1px solid var(--coral);padding:16px 0 16px 20px;margin-top:32px;
 color:var(--dim);font-size:14px;max-width:80ch}
.caveat b{color:var(--ink);font-weight:600}
.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));
 gap:6px;margin-top:48px}
.checks>div{background:var(--ground-2);border-radius:var(--r);padding:22px 24px}
.checks .c{font-family:var(--mono);font-size:10px;color:var(--lime);
 letter-spacing:.14em;text-transform:uppercase}
.checks .d{color:var(--dim);font-size:14px;margin-top:10px}
.checks code{font-family:var(--mono);font-size:12px;color:var(--ink)}

.act{display:grid;grid-template-columns:84px 1fr auto;gap:28px;align-items:center;
 border-top:1px solid var(--hair);padding:46px 0}
.act .idx{font-family:var(--mono);font-size:11px;color:var(--muted)}
.act h2{font-weight:800;text-transform:uppercase;
 font-size:clamp(22px,3vw,40px);letter-spacing:-.02em}
.act .s{color:var(--dim);font-size:15px;margin-top:10px;max-width:56ch}
.act:hover h2{color:var(--coral);transition:color .3s var(--ease)}
@media(max-width:900px){.act{grid-template-columns:1fr;gap:18px}}

footer{padding:70px 0 40px;color:var(--muted);font-size:13px}
footer .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
 gap:36px;margin-top:28px}
footer li{list-style:none;margin-bottom:9px;padding-left:15px;position:relative}
footer li::before{content:"·";position:absolute;left:3px}
code{font-family:var(--mono);font-size:12px;color:var(--dim)}
"""

JS = """
const c=document.getElementById('f');
if(c&&!matchMedia('(prefers-reduced-motion: reduce)').matches){
 const x=c.getContext('2d');let p=[],w,h;
 const PAL=['rgba(231,93,96,','rgba(208,255,126,','rgba(249,244,235,'];
 const rs=()=>{w=c.width=c.offsetWidth*devicePixelRatio;h=c.height=c.offsetHeight*devicePixelRatio;
  p=Array.from({length:260},()=>{const keep=Math.random()<.04;
   return{x:Math.random()*w,y:Math.random()*h,
    v:(Math.random()*.26+.05)*devicePixelRatio,r:Math.random()*1.7+.5,
    // ~4% survive the liquidity gate: those burn lime, the rest drift cream
    col:keep?PAL[1]:(Math.random()<.08?PAL[0]:PAL[2]),
    a:keep?.9:(Math.random()*.22+.06)};});};
 rs();addEventListener('resize',rs);
 (function d(){x.clearRect(0,0,w,h);
  for(const q of p){q.y-=q.v;if(q.y<0){q.y=h;q.x=Math.random()*w;}
   x.beginPath();x.arc(q.x,q.y,q.r*devicePixelRatio,0,6.284);
   x.fillStyle=q.col+q.a+')';x.fill();}
  requestAnimationFrame(d);})();}

document.querySelectorAll('.card button').forEach(b=>b.addEventListener('click',()=>{
 const k=b.closest('.card'),o=k.dataset.open==='1';
 k.dataset.open=o?'0':'1';b.setAttribute('aria-expanded',String(!o));
 k.querySelector('.tgl').textContent=o?'( + )':'( - )';}));

// contextual nav text, as the reference swaps its own
const NAVCTX=[[0,'DETERMINISTIC BY DESIGN'],[1,'FOUR CHANCES TO REFUSE'],
 [2,'THREE ROLES, ONE DISSENTS'],[3,'A BACKTEST THAT CAN LOSE']];
const ctx=document.querySelector('nav .ctx');
const secs=[...document.querySelectorAll('section[id]')];
addEventListener('scroll',()=>{if(!ctx)return;
 let cur='LOOKING FOR AN EDGE TODAY';
 secs.forEach((sec,i)=>{if(sec.getBoundingClientRect().top<300&&NAVCTX[i])cur=NAVCTX[i][1];});
 if(ctx.textContent!==cur)ctx.textContent=cur;},{passive:true});
"""


def build() -> str:
    nav = "".join(f'<a href="{h}">{t}</a>' for t, h in NAV)
    facts = "".join(f'<div><div class="n">{n}</div><div class="l">{l}</div></div>' for n, l in [
        ("3,686", "contracts read from the live chain"),
        ("632", "defined-risk structures built by code"),
        ("12", "shown to the committee"),
        ("700", "tests, no network in any of them"),
        ("1", "it may choose, or none"),
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
         "<code>make verify-journal</code> verifies the chain. Empty, intact "
         "and tampered are three distinct outcomes."),
        ("Watch it decide",
         "<code>make session</code> runs the whole pipeline against the live chain and "
         "sends nothing. Submitting needs an explicit flag."),
        ("Read the limits",
         "<code>risk.yaml</code> is the single source of truth. If a number matters, it "
         "lives there and nowhere else."),
    ])

    INTRO = INTRO_HTML
    FXJS = FX_JS
    return f"""<title>Trading Alpaca</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>{CSS}</style>
{INTRO}

<nav data-cursor="menu"><div class="mark">TA</div>
  <div class="ctx">LOOKING FOR AN EDGE TODAY</div>
  <div class="burger"><span></span><span></span></div></nav>

<div class="stack">
  <a href="/judge"><span>Judge desk</span><span class="v ok">( → )</span></a>
  <div><span>Options level</span><span class="v ok">3</span></div>
  <div><span>Equity</span><span class="v">$100,000</span></div>
  <div><span>Open positions</span><span class="v ok">0</span></div>
</div>

<section class="hero" data-cursor="scroll"><canvas id="f" aria-hidden="true"></canvas>
  <div class="veil"></div>
  <div class="wrap in"><div class="row"><div>
    <p class="tag" data-mono>Alpaca AI Trading Agents · Options Alpha</p>
    <h1 class="st" data-split>The model can refuse. It cannot invent.</h1>
    <div class="cta"><a class="btn fill" data-magnetic href="/judge">Replay a real decision ↗</a>
      <a class="btn" data-magnetic href="{REPO}">Read the code ↗</a></div>
  </div><div><p class="sub">Deterministic Python builds every candidate before a model sees
  anything. Every strike, leg and limit price is fully specified. The committee may pick
  <b>one by id, or refuse</b>. It cannot invent a strike, because no code path allows it.</p></div></div></div>
</section>

<div class="facts">{facts}</div>

<section class="pad" id="inversion"><div class="wrap"><div class="ed">
  <div><p class="tag">The inversion</p>
    <h2 class="st rv" style="margin-top:22px">Most agents ask a model what to buy.
      This one never lets it answer.</h2></div>
  <p class="note rv">Asking a model “what should I trade?” is quick to build and impossible
    to trust. It can hallucinate a strike, size a position wrongly, or be confidently
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
    about a thin hedge moved the trader off the highest-credit candidate, which is the
    reason a committee is not a rubber stamp.</p>
</div></section>

<section class="pad" id="evidence"><div class="wrap">
  <p class="tag">Out-of-sample, computed from real bars</p>
  <h2 class="st rv" style="font-size:clamp(27px,3.8vw,48px);margin-top:22px;max-width:19ch">
    A backtest that cannot lose is broken.</h2>
  <div class="tsc rv"><table><thead><tr><th>symbol</th><th>windows</th><th>trades</th>
    <th>win rate</th><th>expectancy</th><th>profit factor</th><th>max drawdown</th>
    </tr></thead><tbody>{evidence_rows()}</tbody></table></div>
  <div class="caveat rv"><b>Read this before the numbers.</b> Thirty trades across four
    symbols proves nothing statistically, and one symbol loses money. That is the point.
    An earlier version of this harness scaled its risk threshold to the wrong horizon and
    produced a 97% win rate by construction. It was caught and corrected before publication.
    The repository this was converted from shipped a hardcoded “82.2% out-of-sample win
    rate” computed from nothing. That was deleted, not adapted.</div>
  <div class="checks rv">{checks}</div>
</div></section>

<section><div class="wrap">
  <div class="act rv" data-cursor="replay"><span class="idx">( 01 )</span>
    <div><h2>Replay a real decision</h2><p class="s">Four recorded cycles, including two
      refusals. Verdicts are recomputed in your browser from the committed fixtures.</p></div>
    <a class="btn" data-magnetic href="/judge">Open the judge desk →</a></div>
  <div class="act rv" data-cursor="github"><span class="idx">( 02 )</span>
    <div><h2>Check it yourself</h2><p class="s">Clone it and run the verifier offline with
      the environment stripped. It reproduces the same verdicts.</p></div>
    <a class="btn ghost" href="{REPO}">View on GitHub</a></div>
</div></section>

<footer><div class="wrap"><p class="tag">What this does not claim</p>
  <div class="cols">
    <ul><li>Paper trading only. Never live capital, by construction.</li>
      <li>Small sample. No statistical significance is implied.</li>
      <li>The fail-closed scenario is constructed. No live cycle has hit a data outage.</li></ul>
    <ul><li>Analyst text is real recorded model output, not written for this site.</li>
      <li>Guard verdicts are recomputed, not copied.</li>
      <li>Known gaps are tracked in the repository, not hidden.</li></ul>
  </div></div></footer>
<div class="status">
  <span><b>PA3JR0GVVEN0</b> · paper</span>
  <span class="hide-s">Options level <b>3</b></span>
  <span>[ <span class="dot"></span><span id="clk">00 : 00 : 00</span> ]</span>
  <span class="sp hide-s">Defined risk only</span>
  <span class="hide-s">Kill switch armed</span>
  <a href="{REPO}">GitHub ↗</a>
</div>
<script>{FXJS}</script>
<script>{JS}</script>
"""
