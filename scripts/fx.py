#!/usr/bin/env python3
"""
The shared interaction layer, ported to vanilla JS from the author's own
Creator-Guardian `Fx.tsx` so both pages behave identically:

  * preloader — mono percentage counting 0-100 on the ground colour, wiping up
  * cursor chip — a flat mono label trailing the pointer over [data-cursor]
    zones, its text revealing letter by letter
  * dot-matrix wake — a screen-anchored dot grid the pointer illuminates, each
    lit cell fading in place rather than following the cursor
  * magnetic buttons — [data-magnetic] pulls toward the pointer, clamped
  * line-split headline reveals — [data-split] words are grouped into lines and
    each line slides up from a mask, staggered
  * mono character reveals — [data-mono] labels roll in per character
  * hide-on-scroll navigation

Everything degrades: `prefers-reduced-motion` disables all of it, coarse
pointers skip the cursor and its wake entirely, and every reveal target is
visible with JavaScript off.

Palette is the author's own: ember as the colour-block field, proof-green
reserved for verified states, live-red for refusals.
"""

TOKENS = """
:root{
  --ink:#fff; --ink-dim:#b9bdc7; --muted:#8a8f9a;
  --ground:#0b0c0f; --ground-2:#141519;
  --ember:#cd5a1e; --ember-deep:#b04a14;
  --live:#eb0400; --proof:#3ddc97; --caution:#f5a623;
  --hair:rgba(255,255,255,.14); --hair2:rgba(255,255,255,.28);
  --sans:"Inter Tight",-apple-system,BlinkMacSystemFont,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,Menlo,monospace;
  --ease:cubic-bezier(.22,.61,.36,1);
}
::selection{background:var(--ember);color:#fff}
"""

FX_CSS = """
/* ── preloader ── */
#intro{position:fixed;inset:0;z-index:10000;background:var(--ground);
 display:flex;align-items:center;justify-content:center;
 transition:transform 800ms var(--ease)}
#intro.done{transform:translateY(-101%)}
#intro .n{font-family:var(--mono);font-size:12px;font-weight:600;
 letter-spacing:.16em;color:var(--ink);font-variant-numeric:tabular-nums}
#intro .bar{position:absolute;left:0;bottom:0;height:2px;background:var(--ember);
 width:0%;transition:width .1s linear}
html.intro [data-split],html.intro [data-mono]{visibility:hidden}
html.intro [data-split].in,html.intro [data-mono].in{visibility:visible}

/* ── cursor chip + dot wake ── */
.cur-tag{position:fixed;top:0;left:0;z-index:9998;pointer-events:none;
 opacity:0;transition:opacity .22s var(--ease)}
.cur-tag.on{opacity:1}
.cur-chip{transform:translate(14px,14px);background:var(--ember);color:#fff;
 font-family:var(--mono);font-size:10px;letter-spacing:.14em;
 text-transform:uppercase;padding:6px 10px;white-space:nowrap;overflow:hidden}
.cur-chip .cc{display:inline-block;transform:translateY(115%);
 animation:cc .4s var(--ease) forwards;animation-delay:var(--d)}
@keyframes cc{to{transform:none}}
.cur-dots{position:fixed;inset:0;z-index:9997;pointer-events:none}

/* ── split-line + mono reveals ── */
[data-split] .sl-mask{display:inline-block;overflow:hidden;vertical-align:top}
[data-split] .sl{display:inline-block;transform:translateY(112%);
 transition:transform .85s var(--ease);transition-delay:calc(var(--i,0)*90ms)}
[data-split].in .sl{transform:none}
[data-mono]{overflow:hidden}
[data-mono] .mc{display:inline-block;transform:translateY(120%);
 transition:transform .5s var(--ease);transition-delay:var(--d)}
[data-mono].in .mc{transform:none}
.rv{opacity:0;transform:translateY(26px);
 transition:opacity .8s var(--ease),transform .8s var(--ease);
 transition-delay:calc(var(--i,0)*100ms)}
.rv.in{opacity:1;transform:none}
[data-magnetic]{will-change:transform}

/* ── nav hide-on-scroll ── */
nav{transition:transform .45s var(--ease),border-color .3s}
nav.hide{transform:translateY(-102%)}

@media(prefers-reduced-motion:reduce){
 .cur-tag,.cur-dots,#intro{display:none!important}
 [data-split] .sl,[data-mono] .mc{transform:none;transition:none}
 .rv{opacity:1;transform:none;transition:none}
 html.intro [data-split],html.intro [data-mono]{visibility:visible}
 .mq-t{animation:none}
}
@media(pointer:coarse){.cur-tag,.cur-dots{display:none}}
"""

FX_JS = r"""
const L=(a,b,n)=>a+(b-a)*n;
const RM=matchMedia('(prefers-reduced-motion: reduce)').matches;
document.documentElement.classList.add('intro');

// ── preloader: count to 100, wipe up, then let reveals run ───────────
const intro=document.getElementById('intro');
let introDone;
if(intro&&!RM){
  introDone=new Promise(res=>{
    const n=intro.querySelector('.n'),bar=intro.querySelector('.bar');
    const t0=performance.now(),D=850;
    (function step(t){
      const p=Math.min(1,(t-t0)/D),e=(1-Math.pow(1-p,3));
      n.textContent=Math.round(e*100)+'%'; bar.style.width=(e*100)+'%';
      if(p<1)requestAnimationFrame(step);
      else{intro.classList.add('done');
        setTimeout(()=>{intro.remove();document.documentElement.classList.remove('intro');},850);
        setTimeout(res,480);}
    })(performance.now());
  });
}else{intro&&intro.remove();document.documentElement.classList.remove('intro');
  introDone=Promise.resolve();}

// ── cursor chip + screen-anchored dot wake ───────────────────────────
if(!RM&&matchMedia('(pointer: fine)').matches){
  const wrap=document.createElement('div');wrap.className='cur-tag';
  const chip=document.createElement('div');chip.className='cur-chip';
  wrap.appendChild(chip);document.body.appendChild(wrap);

  const PITCH=7,dpr=Math.min(2,devicePixelRatio||1);
  const cv=document.createElement('canvas');cv.className='cur-dots';
  document.body.appendChild(cv);const cx2=cv.getContext('2d');
  let cols=0,rows=0,heat=new Float32Array(0);const active=new Set();
  const size=()=>{cv.width=innerWidth*dpr;cv.height=innerHeight*dpr;
    cv.style.width=innerWidth+'px';cv.style.height=innerHeight+'px';
    cx2.setTransform(dpr,0,0,dpr,0,0);
    cols=Math.ceil(innerWidth/PITCH);rows=Math.ceil(innerHeight/PITCH);
    heat=new Float32Array(cols*rows);active.clear();};
  size();addEventListener('resize',size);

  let mx=-100,my=-100,cx=-100,cy=-100,txt='',dx=-300,dy=-300,last=0;
  addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY;last=performance.now();},{passive:true});
  requestAnimationFrame(function tick(now){
    cx=L(cx,mx,.35);cy=L(cy,my,.35);
    wrap.style.transform=`translate(${cx.toFixed(1)}px,${cy.toFixed(1)}px)`;
    dx=L(dx,mx,.22);dy=L(dy,my,.22);const t=now/1000;
    if(now-last<450){
      const gxc=Math.round(dx/PITCH),gyc=Math.round(dy/PITCH),R=6;
      for(let gx=gxc-R;gx<=gxc+R;gx++){ if(gx<0||gx>=cols)continue;
        for(let gy=gyc-R;gy<=gyc+R;gy++){ if(gy<0||gy>=rows)continue;
          const ox=(gx*PITCH+PITCH/2-dx)/1.25, oy=gy*PITCH+PITCH/2-dy;
          const d=Math.hypot(ox,oy);
          const nz=Math.sin(gx*1.7+t*2.3)+Math.sin(gy*2.1-t*1.6)+Math.sin((gx+gy)*.9+t*1.1);
          const r=30+nz*6.5;
          if(d<r){const i=gy*cols+gx,v=d>r*.72?.5:.92;
            if(v>heat[i]){heat[i]=v;active.add(i);}}}}}
    cx2.clearRect(0,0,innerWidth,innerHeight);
    for(const i of active){const v=heat[i];
      cx2.globalAlpha=Math.min(1,v);
      cx2.fillStyle = v>.7 ? '#cd5a1e' : '#fff';
      const gx=i%cols,gy=(i/cols)|0;
      cx2.fillRect(gx*PITCH+PITCH/2-1,gy*PITCH+PITCH/2-1,2,2);
      heat[i]=v*.88; if(heat[i]<.03){heat[i]=0;active.delete(i);}}
    cx2.globalAlpha=1;requestAnimationFrame(tick);});

  document.addEventListener('mouseover',e=>{
    const el=e.target;
    const inter=el?.closest?.('a,button,[role=button],input,textarea,select');
    const tag=inter?null:el?.closest?.('[data-cursor]');
    const next=tag?.getAttribute('data-cursor')||'';
    if(next===txt)return; txt=next;
    if(txt){chip.innerHTML=[...txt].map((c,i)=>
      `<span class="cc" style="--d:${i*22}ms">${c===' '?'&nbsp;':c}</span>`).join('');
      wrap.classList.add('on');}
    else wrap.classList.remove('on');},true);
  document.documentElement.addEventListener('mouseleave',()=>{
    txt='';wrap.classList.remove('on');});
}

// ── line-split headlines, mono labels, scroll reveals ────────────────
const splitLines=el=>{
  const orig=el.dataset.splitText??el.textContent??'';
  el.dataset.splitText=orig;
  el.innerHTML=orig.split(/\s+/).filter(Boolean).map(w=>`<span class="sw">${w}</span>`).join(' ');
  const lines=[];let top=null;
  el.querySelectorAll('.sw').forEach(s=>{
    if(s.offsetTop!==top){top=s.offsetTop;lines.push([]);}
    lines[lines.length-1].push(s.textContent??'');});
  el.innerHTML=lines.map((ws,i)=>
    `<span class="sl-mask"><span class="sl" style="--i:${i}">${ws.join(' ')}</span></span>`).join(' ');
};
const splitMono=el=>{
  const orig=el.dataset.monoText??el.textContent??'';
  el.dataset.monoText=orig;
  el.innerHTML=[...orig].map((c,i)=>
    `<span class="mc" style="--d:${i*18}ms">${c===' '?'&nbsp;':c}</span>`).join('');
};
Promise.all([document.fonts?document.fonts.ready:0,introDone]).then(()=>{
  if(!RM){
    document.querySelectorAll('[data-split]').forEach(splitLines);
    document.querySelectorAll('[data-mono]').forEach(splitMono);
  }
  const io=new IntersectionObserver(es=>es.forEach(e=>{
    if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target);}}),
    {threshold:.15});
  document.querySelectorAll('[data-split],[data-mono],.rv').forEach(e=>io.observe(e));
  let rt;addEventListener('resize',()=>{clearTimeout(rt);
    rt=setTimeout(()=>{if(!RM)document.querySelectorAll('[data-split]').forEach(splitLines);},180);});
});

// ── magnetic buttons ─────────────────────────────────────────────────
if(!RM)document.querySelectorAll('[data-magnetic]').forEach(el=>{
  let tx=0,ty=0,cx=0,cy=0,raf=0,run=false;
  const tick=()=>{cx=L(cx,tx,.18);cy=L(cy,ty,.18);
    el.style.transform=`translate(${cx.toFixed(2)}px,${cy.toFixed(2)}px)`;
    if(Math.abs(cx-tx)>.1||Math.abs(cy-ty)>.1)raf=requestAnimationFrame(tick);else run=false;};
  const wake=()=>{if(!run){run=true;raf=requestAnimationFrame(tick);}};
  el.addEventListener('mousemove',e=>{const r=el.getBoundingClientRect();
    tx=Math.max(-12,Math.min(12,(e.clientX-r.left-r.width/2)*.28));
    ty=Math.max(-10,Math.min(10,(e.clientY-r.top-r.height/2)*.28));wake();});
  el.addEventListener('mouseleave',()=>{tx=0;ty=0;wake();});
});

// ── hide-on-scroll nav ───────────────────────────────────────────────
const nav=document.querySelector('nav');let ly=0;
addEventListener('scroll',()=>{const y=scrollY;
  if(nav){nav.classList.toggle('stuck',y>10);
    if(y>140&&y>ly+2)nav.classList.add('hide');
    else if(y<ly-2||y<=140)nav.classList.remove('hide');}
  ly=y;},{passive:true});
"""

INTRO_HTML = '<div id="intro"><div class="n">0%</div><div class="bar"></div></div>'
