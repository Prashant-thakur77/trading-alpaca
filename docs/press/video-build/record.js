// Records one 1920x1080 webm per scene, each lasting exactly its narration's
// audio duration plus a short tail. Audio is generated FIRST (gen_audio.py
// writes durations.json); video is then cut to fit it, so sync is by
// construction and cannot drift across scenes.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const scenes = JSON.parse(fs.readFileSync(path.join(ROOT, 'scenes.json'), 'utf8')).scenes;
const durations = JSON.parse(fs.readFileSync(path.join(ROOT, 'audio', 'durations.json'), 'utf8'));
const TAIL_MS = 700;
const only = process.argv[2]; // optional: record a single scene id

function readLines(file, spec) {
  let lines = fs.readFileSync(path.join(ROOT, file), 'utf8').split('\n');
  if (spec.only) {
    // keep only lines containing any of the markers, plus a little context
    lines = lines.filter(l => spec.only.some(m => l.includes(m)));
  }
  return lines.map(l => l.replace(/\s+$/, ''));
}

async function act(page, action, budgetMs) {
  const [kind, arg] = action.split(':');
  if (kind === 'hold') await page.waitForTimeout(Number(arg));
  else if (kind === 'scroll') {
    // smooth scroll in steps so it reads as a human, not a jump
    const total = Number(arg), steps = 20;
    for (let i = 0; i < steps; i++) { await page.mouse.wheel(0, total / steps); await page.waitForTimeout(28); }
  } else if (kind === 'clicktext') {
    const loc = page.getByText(new RegExp(arg, 'i')).first();
    try { await loc.click({ timeout: 4000 }); } catch (e) { console.log('   (clicktext miss:', arg + ')'); }
  }
}

(async () => {
  const b = await chromium.launch({ executablePath: '/snap/bin/chromium', headless: true,
                                    args: ['--disable-gpu', '--force-device-scale-factor=1'] });
  fs.mkdirSync(path.join(ROOT, 'video'), { recursive: true });

  for (const s of scenes) {
    if (only && s.id !== only) continue;
    const durMs = Math.round((durations[s.id] || 8) * 1000) + TAIL_MS;
    const ctx = await b.newContext({
      viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1,
      recordVideo: { dir: path.join(ROOT, 'video', 'raw'), size: { width: 1920, height: 1080 } },
    });
    const page = await ctx.newPage();
    const t0 = Date.now();
    console.log(`▶ ${s.id}  target ${(durMs/1000).toFixed(1)}s`);

    if (s.screen.type === 'terminal') {
      await page.goto('http://127.0.0.1:8765/terminal.html');
      const lines = readLines(s.screen.file, s.screen);
      await page.evaluate(spec => window.play(spec), {
        title: s.screen.title, lines, highlight: s.screen.highlight || [],
        footer: s.screen.footer || '', durationMs: durMs - TAIL_MS,
      });
      await page.waitForTimeout(Math.max(0, durMs - (Date.now() - t0)));
    } else {
      await page.goto(s.screen.url, { waitUntil: 'networkidle', timeout: 60000 });
      // spread the scripted actions across the scene; whatever is left is a hold
      for (const a of (s.screen.actions || [])) {
        if (Date.now() - t0 >= durMs) break;
        await act(page, a, durMs);
      }
      await page.waitForTimeout(Math.max(0, durMs - (Date.now() - t0)));
    }

    const v = page.video();
    await ctx.close();
    const raw = await v.path();
    const out = path.join(ROOT, 'video', `${s.id}.webm`);
    fs.renameSync(raw, out);
    console.log(`  ✓ ${path.basename(out)}  ${((Date.now()-t0)/1000).toFixed(1)}s wall`);
  }
  await b.close();
})().catch(e => { console.error('RECORD FAILED:', e.message); process.exit(1); });
