#!/usr/bin/env python3
"""
Generate the credential-free judge page from the committed scenario fixtures.

The page is generated, never hand-edited, so it can never drift from the
fixtures that `scripts/replay.py --verify` checks. Regenerate with:

    python3 scripts/build_judge_page.py        # writes judge/index.html

Design tokens are shared with the landing page via fx.py, measured from the
reference site's live DOM: cream ink on a warm near-black ground, 4px radius,
coral and lime used sparingly, proof-green reserved for verified states.
"""
import json
import sys
from pathlib import Path

from fx import TOKENS, FX_CSS, FX_JS, INTRO_HTML

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "judge" / "scenarios"
OUT = ROOT / "judge" / "index.html"

ORDER = ["allow", "downsize", "deny", "fail_closed"]

RAIL = [
    ("Snapshot", "What the desk saw"),
    ("Committee", "What the analysts argued"),
    ("Veto", "Two independent reviewers"),
    ("Guard", "The deterministic gate"),
    ("Execution", "What would be sent"),
]


def load() -> dict:
    out = {}
    for name in ORDER:
        path = SCENARIOS / f"{name}.json"
        if not path.exists():
            print(f"  missing fixture: {path}", file=sys.stderr)
            continue
        out[name] = json.loads(path.read_text())
    if not out:
        raise SystemExit("no fixtures found; run the scenario generator first")
    return out


CSS = TOKENS + FX_CSS + """
*{margin:0;padding:0;box-sizing:border-box}
html{background:var(--ground)}
body{
  background:var(--ground); color:var(--ink); font-family:var(--sans);
  -webkit-font-smoothing:antialiased; line-height:1.5;
}
.wrap{max-width:1180px;margin:0 auto;padding:0 28px}
a{color:inherit}
:focus-visible{outline:2px solid var(--proof);outline-offset:2px}

.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.22em;
  text-transform:uppercase;color:var(--muted)}
h1{font-weight:800;text-transform:uppercase;letter-spacing:-.02em;
  line-height:.92;font-size:clamp(34px,5.5vw,60px);text-wrap:balance}
h2{font-weight:800;text-transform:uppercase;letter-spacing:-.01em;font-size:15px}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}

header{border-bottom:1px solid var(--hair);padding:34px 0}
.home{font-family:var(--mono);font-size:11px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--muted);text-decoration:none;
  display:inline-block;margin-bottom:26px}
.home:hover{color:var(--ink)}
.lede{color:var(--ink-dim);max-width:66ch;margin-top:18px;font-size:16px}
.lede strong{color:var(--ink);font-weight:600}

/* trace rail: a real pipeline sequence, hence numbered */
.rail{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;
  background:var(--hair);border-top:1px solid var(--hair);
  border-bottom:1px solid var(--hair)}
.rail div{background:var(--ground);padding:16px 14px}
.rail .n{font-family:var(--mono);font-size:11px;color:var(--muted)}
.rail .t{font-weight:800;text-transform:uppercase;font-size:13px;
  letter-spacing:-.01em;margin-top:6px}
.rail .s{font-size:11px;color:var(--muted);margin-top:3px}

/* scenario selector */
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:34px 0 0}
.tab{background:var(--ground-2);border:0;border-radius:var(--r);color:var(--ink-dim);cursor:pointer;
  font-family:var(--mono);font-size:12px;letter-spacing:.12em;
  text-transform:uppercase;padding:13px 20px;flex:1 1 auto;text-align:left}
.tab:hover{color:var(--ink)}
.tab[aria-selected="true"]{background:var(--coral);color:var(--ink);font-weight:600}
.tab .dot{display:inline-block;width:7px;height:7px;margin-right:9px;
  vertical-align:middle}

/* verdict */
.verdict{display:flex;align-items:baseline;gap:18px;flex-wrap:wrap;
  padding:30px 0 8px;border-bottom:1px solid var(--hair)}
.pill{font-family:var(--mono);font-size:13px;letter-spacing:.16em;
  text-transform:uppercase;padding:7px 15px;color:var(--ground);font-weight:600;
  border-radius:var(--r)}
.pill.allow{background:var(--proof)}
.pill.downsize{background:var(--lime)}
.pill.deny,.pill.fail{background:var(--live)}
.verdict .why{color:var(--ink-dim);font-size:14px;max-width:70ch}

.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(184px,1fr));
  gap:6px;margin:28px 0}
.kpi{background:var(--ground-2);padding:16px 18px;border-radius:var(--r)}
.kpi .k{font-family:var(--mono);font-size:10px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--muted)}
.kpi .v{font-family:var(--mono);font-size:19px;margin-top:7px;
  font-variant-numeric:tabular-nums}
.kpi .v.good{color:var(--proof)} .kpi .v.bad{color:var(--live)}
.kpi .v.warn{color:var(--lime)}

/* the not-green explainer: present only when something is not green */
.notes{border:1px solid var(--live);border-radius:var(--r);padding:20px 22px;margin:0 0 30px}
.notes h2{color:var(--live);margin-bottom:12px}
.notes ul{list-style:none;display:flex;flex-direction:column;gap:9px}
.notes li{font-size:14px;color:var(--ink-dim);padding-left:17px;position:relative}
.notes li::before{content:"\\2022";position:absolute;left:0;color:var(--live)}

section.panel{border-top:1px solid var(--hair);padding:28px 0}
.phead{display:flex;align-items:baseline;gap:13px;margin-bottom:16px}
.phead .num{font-family:var(--mono);font-size:11px;color:var(--muted)}
.sub{color:var(--muted);font-size:12px;font-family:var(--mono)}

.view{background:var(--ground-2);border-radius:var(--r);padding:17px 19px;margin-bottom:6px}
.view .r{display:flex;align-items:baseline;gap:13px;flex-wrap:wrap}
.view .role{font-family:var(--mono);font-size:12px;letter-spacing:.12em;
  text-transform:uppercase}
.view .p{font-family:var(--mono);font-size:20px;font-variant-numeric:tabular-nums}
.view .model{font-family:var(--mono);font-size:10px;color:var(--muted);
  margin-left:auto}
.view .txt{color:var(--ink-dim);font-size:14px;margin-top:10px;max-width:78ch}

.grid2{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.grid2>div{background:var(--ground-2);padding:17px 19px;border-radius:var(--r)}
@media(max-width:760px){.grid2,.rail{grid-template-columns:1fr}}

.badge{font-family:var(--mono);font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;padding:3px 9px;border:1px solid currentColor}
.badge.ok{color:var(--proof)} .badge.no{color:var(--live)}
.badge.na{color:var(--muted)}

pre{font-family:var(--mono);font-size:12px;line-height:1.62;color:var(--ink-dim);
  background:var(--ground-2);border:1px solid var(--hair);padding:17px 19px;
  overflow-x:auto;white-space:pre}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px;
  font-variant-numeric:tabular-nums}
.tscroll{overflow-x:auto;border:1px solid var(--hair)}
th{text-align:left;color:var(--muted);font-weight:400;letter-spacing:.12em;
  text-transform:uppercase;font-size:10px;padding:10px 13px;
  border-bottom:1px solid var(--hair)}
td{padding:9px 13px;border-bottom:1px solid var(--hair);color:var(--ink-dim)}
tr:last-child td{border-bottom:0}
td.pick{color:var(--ink)} tr.chosen{background:rgba(208,255,126,.07)}
tr.chosen td{color:var(--ink)}

.prov{border-left:2px solid var(--hair2);padding:11px 0 11px 16px;
  color:var(--muted);font-size:12.5px;max-width:80ch;margin-top:16px}
.prov b{color:var(--ink-dim);font-weight:600}

footer{border-top:1px solid var(--hair);margin-top:44px;padding:34px 0 60px;
  color:var(--muted);font-size:13px}
footer .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));
  gap:28px;margin-top:18px}
footer li{list-style:none;margin-bottom:7px;padding-left:15px;position:relative}
footer li::before{content:"·";position:absolute;left:3px}
code{font-family:var(--mono);color:var(--ink-dim);font-size:12px}
@media(prefers-reduced-motion:no-preference){
  .fade{animation:f .45s cubic-bezier(.22,.61,.36,1) both}
  @keyframes f{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:none}}
}
"""

JS = """
const S = window.__SCENARIOS__;
const RAIL = window.__RAIL__;
const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const num = (v, d=2) => (v === null || v === undefined) ? 'n/a' : Number(v).toFixed(d);

function verdictOf(d){
  if (d.scenario === 'fail_closed') return {cls:'fail', label:'FAIL-CLOSED'};
  const g = d.recorded_outcome && d.recorded_outcome.guard;
  if (!g) return {cls:'fail', label:'NO TRADE'};
  if (g.decision === 'ALLOW') return {cls:'allow', label:'ALLOW'};
  if (g.decision === 'ALLOW_WITH_DOWNSIZE') return {cls:'downsize', label:'DOWNSIZE'};
  return {cls:'deny', label:'DENY'};
}

// Everything that is not fully green, stated plainly. A refusal is a
// demonstrated capability, not a failure to explain away.
function notGreen(d){
  const out = [];
  const ro = d.recorded_outcome || {};
  const g = ro.guard;
  if (d.scenario === 'fail_closed')
    out.push('Upstream data could not be fetched. The desk reports an outage rather than a market judgement, and sends nothing.');
  if (g && g.decision === 'DENY') out.push('RiskGuard refused this order: ' + g.reason);
  if (g && g.decision === 'ALLOW_WITH_DOWNSIZE')
    out.push('RiskGuard approved a smaller position than requested: ' +
      d.requested_contracts + ' contract(s) requested, ' + g.approved_contracts + ' approved. ' + g.reason);
  const v = d.veto || {};
  if (v.thesis && v.thesis.ok === false) out.push('Thesis check vetoed: ' + v.thesis.reason);
  if (v.blind && v.blind.ok === false) out.push('Blind reviewer vetoed: ' + v.blind.reason);
  (d.committee && d.committee.views || []).forEach(x => {
    if (x.abstained) out.push('Analyst ' + x.role + ' abstained: ' + (x.abstain_reason || 'no reason recorded'));
  });
  return out;
}

function render(key){
  const d = S[key];
  const ro = d.recorded_outcome || {};
  const g = ro.guard, ci = ro.chosen_intent, v = d.veto || {}, m = d.market || {};
  const vd = verdictOf(d);
  const ng = notGreen(d);

  document.querySelectorAll('.tab').forEach(t =>
    t.setAttribute('aria-selected', String(t.dataset.k === key)));

  const kpi = (k, val, cls='') =>
    `<div class="kpi"><div class="k">${k}</div><div class="v ${cls}">${val}</div></div>`;

  const badge = (ok, naText) => ok === true ? '<span class="badge ok">Pass</span>'
    : ok === false ? '<span class="badge no">Veto</span>'
    : `<span class="badge na">${esc(naText || 'not run')}</span>`;

  let h = '';

  h += `<div class="verdict fade">
      <span class="pill ${vd.cls}">${vd.label}</span>
      <span class="why">${esc(ro.summary || (g && g.reason) || d.note ||
        'No order was sent.')}</span></div>`;

  h += '<div class="kpis fade">' +
    kpi('Underlying', esc(m.underlying || 'n/a')) +
    kpi('Spot', m.spot ? '$' + num(m.spot) : 'n/a') +
    kpi('ATM IV vs realized', m.atm_iv != null && m.realized_vol != null
        ? ((m.atm_iv - m.realized_vol) * 100 >= 0 ? '+' : '') +
          num((m.atm_iv - m.realized_vol) * 100) + 'pp' : 'n/a') +
    kpi('Candidates built', (d.candidates ? d.candidates.length : 0) + ' shown') +
    kpi('Committee choice', esc(d.chosen_id || 'ABSTAIN'),
        d.chosen_id && d.chosen_id !== 'ABSTAIN' ? '' : 'warn') +
    kpi('Contracts', g ? `${d.requested_contracts} → ${g.approved_contracts}` : '0',
        g && g.approved_contracts < d.requested_contracts ? 'warn' : '') +
    '</div>';

  if (ng.length) {
    h += '<div class="notes fade"><h2>Why this is not fully green</h2><ul>' +
      ng.map(x => `<li>${esc(x)}</li>`).join('') + '</ul></div>';
  }

  // 1. Snapshot
  h += panel(1, `<div class="grid2">
      <div><div class="eyebrow">Realized volatility</div>
        <div class="mono" style="font-size:22px;margin-top:7px">${num(m.realized_vol*100)}%</div></div>
      <div><div class="eyebrow">At-the-money implied</div>
        <div class="mono" style="font-size:22px;margin-top:7px">${m.atm_iv != null ? num(m.atm_iv*100)+'%' : 'unavailable'}</div></div>
    </div>${m.iv_minus_realized_note ? `<div class="prov">${esc(m.iv_minus_realized_note)}</div>` : ''}
    ${candidateTable(d)}`);

  // 2. Committee
  const views = (d.committee && d.committee.views) || [];
  h += panel(2, views.length ? views.map(x => `
      <div class="view"><div class="r">
        <span class="role">${esc(x.role)}</span>
        <span class="p" style="color:${x.abstained ? 'var(--muted)' : 'var(--ink)'}">${
          x.abstained ? 'ABSTAIN' : 'p=' + num(x.probability)}</span>
        <span class="model">${esc(x.model || '')}</span></div>
        <div class="txt">${esc(x.abstained ? (x.abstain_reason||'') : (x.reasoning||''))}</div>
      </div>`).join('') + `
      <div class="view"><div class="r">
        <span class="role">Trader</span>
        <span class="p">${esc((d.committee.trader && d.committee.trader.choice_id) || 'ABSTAIN')}</span>
        <span class="model">${esc((d.committee.trader && d.committee.trader.model) || '')}</span></div>
        <div class="txt">${esc((d.committee.trader && d.committee.trader.reasoning) || '')}</div></div>
      <div class="prov">Aggregate probability <b>${num(d.committee.aggregate_probability)}</b>.
        An abstaining analyst is excluded from both the numerator and the denominator,
        so "no opinion" never counts as neutral.</div>`
    : '<div class="prov">No committee ran. The cycle stopped before any model was called.</div>');

  // 3. Veto
  h += panel(3, `<div class="grid2">
      <div><div class="eyebrow">Thesis check · pure code</div>
        <div style="margin:10px 0">${badge(v.thesis ? v.thesis.ok : null)}</div>
        <div class="txt" style="color:var(--ink-dim);font-size:13px">${esc((v.thesis && v.thesis.reason) || 'not run')}</div></div>
      <div><div class="eyebrow">Blind reviewer · starved of the debate</div>
        <div style="margin:10px 0">${badge(v.blind ? v.blind.ok : null)}</div>
        <div class="txt" style="color:var(--ink-dim);font-size:13px">${esc((v.blind && v.blind.reason) || 'not run')}</div></div>
    </div>
    <div class="prov">Two reviewers that fail differently. One is deterministic code checking the
      position's delta against the structure's own thesis. The other is a model shown the
      candidate and price action but <b>never the committee's reasoning</b>. Two calls to the same model on the
      same context would agree with each other and prove nothing.</div>`);

  // 4. Guard
  h += panel(4, g ? `<div class="grid2">
      <div><div class="eyebrow">Verdict</div>
        <div class="mono" style="font-size:20px;margin-top:8px;color:${
          g.decision==='DENY'?'var(--live)':g.decision==='ALLOW'?'var(--proof)':'var(--lime)'}">${esc(g.decision)}</div></div>
      <div><div class="eyebrow">Approved contracts</div>
        <div class="mono" style="font-size:20px;margin-top:8px">${g.approved_contracts}</div></div>
    </div><div class="prov">${esc(g.reason)}</div>
    ${ci ? `<div class="prov">Position Greeks: delta <b>${num(ro.position_delta,1)}</b>,
       vega <b>${num(ro.position_vega,1)}</b>. Limits are |delta| ≤ 30 and |vega| ≤ 200,
       with max loss capped at $1,000 per position.</div>` : ''}`
    : '<div class="prov">The guard was never reached. Nothing was proposed to it.</div>');

  // 5. Execution
  h += panel(5, ro.payload
    ? `<pre>${esc(JSON.stringify(ro.payload, null, 2))}</pre>
       <div class="prov">A negative <code>limit_price</code> is Alpaca's convention for a net credit
       received. The <code>client_order_id</code> is derived deterministically from the legs and the
       UTC date, so a retry is rejected by the broker as a duplicate rather than opening a second
       position.</div>`
    : '<div class="prov">No payload. Nothing was sent, and nothing would have been.</div>');

  if (d.provenance) {
    const p = d.provenance;
    h += `<section class="panel"><div class="phead"><h2>Provenance</h2>
      <span class="sub">where this data came from</span></div>
      <div class="prov"><b>${esc(p.kind)}</b>: ` +
      Object.entries(p).filter(([k]) => k !== 'kind')
        .map(([k, val]) => `<br><b>${esc(k)}:</b> ${esc(val)}`).join('') + '</div></section>';
  }

  document.getElementById('body').innerHTML = h;
}

function panel(n, inner){
  const [t, s] = RAIL[n-1];
  return `<section class="panel fade"><div class="phead"><span class="num">${String(n).padStart(2,'0')}</span>
    <h2>${t}</h2><span class="sub">${s}</span></div>${inner}</section>`;
}

function candidateTable(d){
  const cs = d.candidates || [];
  if (!cs.length) return '';
  return `<div class="tscroll" style="margin-top:18px"><table>
    <thead><tr><th>id</th><th>structure</th><th>dte</th><th>credit</th>
      <th>max loss</th><th>breakeven</th></tr></thead><tbody>` +
    cs.map(c => `<tr class="${c.id === d.chosen_id ? 'chosen' : ''}">
      <td class="pick">${esc(c.id)}</td><td>${esc(c.structure)}</td><td>${c.dte}</td>
      <td>${c.net_credit != null ? '$' + num(c.net_credit) : 'n/a'}</td>
      <td>${c.max_loss != null ? '$' + num(c.max_loss, 0) : 'n/a'}</td>
      <td>${c.breakevens && c.breakevens.length ? num(c.breakevens[0]) : 'n/a'}</td>
    </tr>`).join('') + `</tbody></table></div>
    <div class="prov">Every one of these was built by deterministic code before any model saw
    anything. The committee may pick one <b>by id</b> or abstain. It cannot invent a strike,
    change a quantity, or move a price, because no code path allows it.</div>`;
}

document.querySelectorAll('.tab').forEach(t =>
  t.addEventListener('click', () => render(t.dataset.k)));
render(Object.keys(S)[0]);
"""


def build() -> str:
    data = load()
    tabs = "".join(
        f'<button class="tab" role="tab" data-k="{k}" aria-selected="false">'
        f'<span class="dot" style="background:{c}"></span>{lbl}</button>'
        for k, lbl, c in [
            ("allow", "Allow", "#3ddc97"),
            ("downsize", "Downsize", "#f5a623"),
            ("deny", "Deny", "#ff5c5c"),
            ("fail_closed", "Fail-closed", "#ff5c5c"),
        ] if k in data
    )
    rail = "".join(
        f'<div><div class="n">{i:02d}</div><div class="t">{t}</div><div class="s">{s}</div></div>'
        for i, (t, s) in enumerate(RAIL, 1)
    )
    return f"""<title>Trading Alpaca Judge Desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>{CSS}</style>
{INTRO_HTML}

<nav style="position:static;background:none;border:0"></nav>
<header><div class="wrap">
  <a class="home" href="/">← Trading Alpaca</a>
  <div class="eyebrow">Alpaca AI Trading Agents · Options Alpha</div>
  <h1>The options desk<br>that grades itself</h1>
  <p class="lede">Deterministic code builds every trade. The model's only power is to
  <strong>choose one or refuse</strong>. Replay four real decisions below, including two
  refusals, and verify every one yourself with no credentials, no API keys, and
  no model calls.</p>
</div></header>

<div class="wrap"><div class="rail">{rail}</div>
  <div class="tabs" role="tablist">{tabs}</div>
  <div id="body"></div>

  <footer>
    <div class="eyebrow">What this page does not claim</div>
    <div class="cols">
      <ul>
        <li>Paper trading only. Never live capital, by construction.</li>
        <li>A handful of trades proves nothing statistically, and we don't imply otherwise.</li>
        <li>The <code>fail-closed</code> scenario is constructed. No live cycle has yet hit a data outage.</li>
      </ul>
      <ul>
        <li>Committee text is the real recorded model output, not written for this page.</li>
        <li>Guard verdicts and payloads are <b>recomputed</b> here, not copied.</li>
        <li>Verify locally: <code>python3 scripts/replay.py --all --verify</code></li>
      </ul>
    </div>
  </footer>
</div>

<script>
window.__SCENARIOS__ = {json.dumps(data)};
window.__RAIL__ = {json.dumps(RAIL)};
</script>
<script>{FX_JS}</script>
<script>{JS}</script>
"""


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    html = build()
    OUT.write_text(html)
    print(f"  wrote {OUT}  ({len(html):,} bytes, {len(load())} scenarios)")
