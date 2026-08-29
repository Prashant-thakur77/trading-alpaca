#!/usr/bin/env python3
"""
Shared interaction layer + design tokens.

Tokens are measured from lamalama.com's live DOM via Playwright, not guessed:
ground rgb(26,28,28), ink rgb(249,244,235) used 2,582 times, hairlines at
cream/10%, coral rgb(231,93,96) and lime rgb(208,255,126) used a handful of
times each, and a 4px radius on 205 elements with 9999px pills. Their faces
(SuisseBPIntl, Sometype) are commercially licensed, so the closest Google
pairing is substituted.

The motion is ported from the author's own Creator-Guardian Fx.tsx: preloader,
cursor chip, dot-matrix wake, magnetic buttons, split-line reveals.
"""

TOKENS = """
:root{
  --ground:#1a1c1c; --ground-2:#010101; --ink:#f9f4eb;
  --hair:rgba(249,244,235,.1); --hair2:rgba(249,244,235,.28);
  --dim:rgba(249,244,235,.62); --muted:rgba(249,244,235,.42);
  --coral:#e75d60; --lime:#d0ff7e; --proof:#3ddc97;
  --sans:"Inter Tight",-apple-system,BlinkMacSystemFont,Helvetica,sans-serif;
  --mono:"Sometype Mono","JetBrains Mono",ui-monospace,Menlo,monospace;
  --ease:cubic-bezier(.22,.61,.36,1); --r:4px;
}
::selection{background:var(--coral);color:var(--ink)}
"""

FX_CSS = """
#intro{position:fixed;inset:0;z-index:10000;background:var(--ground-2);
 display:flex;align-items:center;justify-content:center;
 transition:transform 800ms var(--ease)}
#intro.done{transform:translateY(-101%)}
#intro .n{font-family:var(--mono);font-size:12px;letter-spacing:.16em;
 color:var(--ink);font-variant-numeric:tabular-nums}
#intro .bar{position:absolute;left:0;bottom:0;height:2px;background:var(--coral);width:0%}
html.intro [data-split],html.intro [data-mono]{visibility:hidden}
html.intro [data-split].in,html.intro [data-mono].in{visibility:visible}

.cur-tag{position:fixed;top:0;left:0;z-index:9998;pointer-events:none;opacity:0;
 transition:opacity .22s var(--ease)}
.cur-tag.on{opacity:1}
.cur-chip{transform:translate(14px,14px);background:var(--ink);color:var(--ground);
 font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;
 padding:6px 11px;white-space:nowrap;overflow:hidden;border-radius:var(--r)}
.cur-chip .cc{display:inline-block;transform:translateY(115%);
 animation:cc .4s var(--ease) forwards;animation-delay:var(--d)}
@keyframes cc{to{transform:none}}
.cur-dots{position:fixed;inset:0;z-index:9997;pointer-events:none}

[data-split] .sl-mask{display:inline-block;overflow:hidden;vertical-align:top}
[data-split] .sl{display:inline-block;transform:translateY(112%);
 transition:transform .9s var(--ease);transition-delay:calc(var(--i,0)*95ms)}
[data-split].in .sl{transform:none}
[data-mono]{overflow:hidden}
[data-mono] .mc{display:inline-block;transform:translateY(120%);
 transition:transform .5s var(--ease);transition-delay:var(--d)}
[data-mono].in .mc{transform:none}
.rv{opacity:0;transform:translateY(26px);
 transition:opacity .85s var(--ease),transform .85s var(--ease);
 transition-delay:calc(var(--i,0)*100ms)}
.rv.in{opacity:1;transform:none}
[data-magnetic]{will-change:transform}

@media(prefers-reduced-motion:reduce){
 .cur-tag,.cur-dots,#intro{display:none!important}
 [data-split] .sl,[data-mono] .mc{transform:none;transition:none}
 .rv{opacity:1;transform:none;transition:none}
 html.intro [data-split],html.intro [data-mono]{visibility:visible}
 .mq-t{animation:none}}
@media(pointer:coarse){.cur-tag,.cur-dots{display:none}}
"""

FX_JS = r"""
const L=(a,b,n)=>a+(b-a)*n;
const RM=matchMedia('(prefers-reduced-motion: reduce)').matches;
document.documentElement.classList.add('intro');
const intro=document.getElementById('intro');let introDone;
if(intro&&!RM){introDone=new Promise(res=>{
  const n=intro.querySelector('.n'),bar=intro.querySelector('.bar');
  const t0=performance.now(),D=900;
  (function s(t){const p=Math.min(1,(t-t0)/D),e=1-Math.pow(1-p,3);
   n.textContent=Math.round(e*100)+'%';bar.style.width=(e*100)+'%';
   if(p<1)requestAnimationFrame(s);
   else{intro.classList.add('done');
     setTimeout(()=>{intro.remove();document.documentElement.classList.remove('intro');},850);
     setTimeout(res,480);}})(performance.now());});
}else{intro&&intro.remove();document.documentElement.classList.remove('intro');
  introDone=Promise.resolve();}

if(!RM&&matchMedia('(pointer: fine)').matches){
 const wrap=document.createElement('div');wrap.className='cur-tag';
 const chip=document.createElement('div');chip.className='cur-chip';
 wrap.appendChild(chip);document.body.appendChild(wrap);
 const P=7,dpr=Math.min(2,devicePixelRatio||1);
 const cv=document.createElement('canvas');cv.className='cur-dots';
 document.body.appendChild(cv);const g=cv.getContext('2d');
 let cols=0,rows=0,heat=new Float32Array(0);const on=new Set();
 const size=()=>{cv.width=innerWidth*dpr;cv.height=innerHeight*dpr;
  cv.style.width=innerWidth+'px';cv.style.height=innerHeight+'px';
  g.setTransform(dpr,0,0,dpr,0,0);cols=Math.ceil(innerWidth/P);rows=Math.ceil(innerHeight/P);
  heat=new Float32Array(cols*rows);on.clear();};
 size();addEventListener('resize',size);
 let mx=-100,my=-100,cx=-100,cy=-100,txt='',dx=-300,dy=-300,last=0;
 addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY;last=performance.now();},{passive:true});
 requestAnimationFrame(function tick(now){
  cx=L(cx,mx,.35);cy=L(cy,my,.35);
  wrap.style.transform=`translate(${cx.toFixed(1)}px,${cy.toFixed(1)}px)`;
  dx=L(dx,mx,.22);dy=L(dy,my,.22);const t=now/1000;
  if(now-last<450){const gx0=Math.round(dx/P),gy0=Math.round(dy/P),R=6;
   for(let gx=gx0-R;gx<=gx0+R;gx++){if(gx<0||gx>=cols)continue;
    for(let gy=gy0-R;gy<=gy0+R;gy++){if(gy<0||gy>=rows)continue;
     const ox=(gx*P+P/2-dx)/1.25,oy=gy*P+P/2-dy,d=Math.hypot(ox,oy);
     const nz=Math.sin(gx*1.7+t*2.3)+Math.sin(gy*2.1-t*1.6)+Math.sin((gx+gy)*.9+t*1.1);
     const r=30+nz*6.5;
     if(d<r){const i=gy*cols+gx,v=d>r*.72?.5:.92;if(v>heat[i]){heat[i]=v;on.add(i);}}}}}
  g.clearRect(0,0,innerWidth,innerHeight);
  for(const i of on){const v=heat[i];g.globalAlpha=Math.min(1,v);
   g.fillStyle=v>.7?'#e75d60':'#f9f4eb';
   const gx=i%cols,gy=(i/cols)|0;g.fillRect(gx*P+P/2-1,gy*P+P/2-1,2,2);
   heat[i]=v*.88;if(heat[i]<.03){heat[i]=0;on.delete(i);}}
  g.globalAlpha=1;requestAnimationFrame(tick);});
 document.addEventListener('mouseover',e=>{const el=e.target;
  const it=el?.closest?.('a,button,[role=button],input,textarea,select');
  const tg=it?null:el?.closest?.('[data-cursor]');
  const nx=tg?.getAttribute('data-cursor')||'';if(nx===txt)return;txt=nx;
  if(txt){chip.innerHTML=[...txt].map((c,i)=>
   `<span class="cc" style="--d:${i*22}ms">${c===' '?'&nbsp;':c}</span>`).join('');
   wrap.classList.add('on');}else wrap.classList.remove('on');},true);
 document.documentElement.addEventListener('mouseleave',()=>{txt='';wrap.classList.remove('on');});}

const splitLines=el=>{const o=el.dataset.splitText??
 // <br> carries a word boundary that textContent drops, joining words together
 (el.innerHTML.replace(/<br\s*\/?>/gi,' ').replace(/<[^>]+>/g,'')).replace(/\s+/g,' ').trim();
 el.dataset.splitText=o;
 el.innerHTML=o.split(/\s+/).filter(Boolean).map(w=>`<span class="sw">${w}</span>`).join(' ');
 const ls=[];let top=null;
 el.querySelectorAll('.sw').forEach(s=>{if(s.offsetTop!==top){top=s.offsetTop;ls.push([]);}
  ls[ls.length-1].push(s.textContent??'');});
 el.innerHTML=ls.map((ws,i)=>
  `<span class="sl-mask"><span class="sl" style="--i:${i}">${ws.join(' ')}</span></span>`).join(' ');};
const splitMono=el=>{const o=el.dataset.monoText??el.textContent??'';el.dataset.monoText=o;
 el.innerHTML=[...o].map((c,i)=>
  `<span class="mc" style="--d:${i*18}ms">${c===' '?'&nbsp;':c}</span>`).join('');};
Promise.all([document.fonts?document.fonts.ready:0,introDone]).then(()=>{
 if(!RM){document.querySelectorAll('[data-split]').forEach(splitLines);
  document.querySelectorAll('[data-mono]').forEach(splitMono);}
 const io=new IntersectionObserver(es=>es.forEach(e=>{
  if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target);}}),{threshold:.15});
 document.querySelectorAll('[data-split],[data-mono],.rv').forEach(e=>io.observe(e));
 let rt;addEventListener('resize',()=>{clearTimeout(rt);
  rt=setTimeout(()=>{if(!RM)document.querySelectorAll('[data-split]').forEach(splitLines);},180);});});

if(!RM)document.querySelectorAll('[data-magnetic]').forEach(el=>{
 let tx=0,ty=0,cx=0,cy=0,raf=0,run=false;
 const tick=()=>{cx=L(cx,tx,.18);cy=L(cy,ty,.18);
  el.style.transform=`translate(${cx.toFixed(2)}px,${cy.toFixed(2)}px)`;
  if(Math.abs(cx-tx)>.1||Math.abs(cy-ty)>.1)raf=requestAnimationFrame(tick);else run=false;};
 const wake=()=>{if(!run){run=true;raf=requestAnimationFrame(tick);}};
 el.addEventListener('mousemove',e=>{const r=el.getBoundingClientRect();
  tx=Math.max(-12,Math.min(12,(e.clientX-r.left-r.width/2)*.28));
  ty=Math.max(-10,Math.min(10,(e.clientY-r.top-r.height/2)*.28));wake();});
 el.addEventListener('mouseleave',()=>{tx=0;ty=0;wake();});});

// live clock in the status bar, as on the reference
const clk=document.getElementById('clk');
if(clk)setInterval(()=>{const d=new Date();
 clk.textContent=[d.getHours(),d.getMinutes(),d.getSeconds()]
  .map(n=>String(n).padStart(2,'0')).join(' : ');},1000);
"""

INTRO_HTML = '<div id="intro"><div class="n">0%</div><div class="bar"></div></div>'
