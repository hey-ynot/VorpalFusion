// VorpalFusion page checks, in headless system Chrome.
//
//   node checks/check.mjs            the page against checks/fake.py (no GPU)
//   node checks/check.mjs --live     one end-to-end pass against the real sidecar, measured
//
// GV_URL is the sidecar (default http://127.0.0.1:8766/, fake.py's port); PLAYWRIGHT_DIR a node_modules holding playwright
// (default: resolved from this file, so `npm install playwright` at the repository root); GV_OUT where screenshots and
// measurements go (default gv-checks in the system temp folder); GV_INPUTS the pictures the checks drop (portrait.png,
// street.png; default: drawn by fake.py into GV_OUT/inputs).
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const require = createRequire(process.env.PLAYWRIGHT_DIR ? `${process.env.PLAYWRIGHT_DIR}/` : import.meta.url);
const { chromium } = require('playwright');
const URL = process.env.GV_URL || 'http://127.0.0.1:8766/';
const OUT = process.env.GV_OUT || join(tmpdir(), 'gv-checks');
const KEPT = join(tmpdir(), 'gv-checks', 'kept');  // fake.py's Keep folder
const INPUTS = process.env.GV_INPUTS || join(OUT, 'inputs');
mkdirSync(INPUTS, { recursive: true });
for (const name of ['portrait', 'street']) {  // fixture pictures from the fake, where none are given
  if (existsSync(`${INPUTS}/${name}.png`)) continue;
  const r = await fetch(`${URL}fake/picture/${name}`).catch(() => null);
  if (r?.ok) writeFileSync(`${INPUTS}/${name}.png`, Buffer.from(await r.arrayBuffer()));
}

const results = [];
async function check(name, fn) {
  const started = Date.now();
  try { const note = await fn(); results.push({ name, ok: true, note }); console.log(`PASS ${name}${note ? ` · ${note}` : ''} (${Date.now() - started} ms)`); }
  catch (error) { results.push({ name, ok: false, note: String(error.message || error) }); console.log(`FAIL ${name}: ${error.message || error}`); }
}
const assert = (ok, message) => { if (!ok) throw new Error(message); };

const browser = await chromium.launch({ channel: 'chrome', headless: true });
async function open(options = {}, path = '') {
  const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, ...options });
  await context.addInitScript(() => {
    window.__status = []; window.__sends = []; window.__keys = []; window.__srcs = [];
    const fetch0 = window.fetch;  // the states the page sends, by version, and when; for words age
    window.fetch = (url, init) => {
      if (String(url).endsWith('state') && init?.method === 'POST') { try { window.__sends.push({ v: JSON.parse(init.body).v, t: performance.now() }); } catch {} }
      return fetch0(url, init);
    };
    addEventListener('input', () => window.__keys.push(performance.now()), true);
    addEventListener('DOMContentLoaded', () => {
      for (const id of ['a', 'b']) {  // every picture the stage shows, for the settle's drift
        const img = document.getElementById(id); if (!img) continue;
        new MutationObserver(() => {  // every picture shown, kept whole only for the last 60 (the drift needs the latest)
          window.__srcs.push({ t: performance.now(), src: img.getAttribute('src') });
          if (window.__srcs.length > 60) window.__srcs[window.__srcs.length - 61].src = null;
        }).observe(img, { attributes: true, attributeFilter: ['src'] });
      }
    });
    addEventListener('DOMContentLoaded', () => {
      const el = document.getElementById('status'); if (!el) return;
      new MutationObserver(() => window.__status.push({ t: performance.now(), text: el.textContent })).observe(el, { childList: true, characterData: true, subtree: true });
    });
  });
  const page = await context.newPage();
  page.errors = [];
  page.on('pageerror', (e) => page.errors.push(e.message));
  await page.goto(URL + path);
  return { context, page };
}
const statusText = (p) => p.textContent('#status');
async function waitStatus(p, pattern, timeout = 15000) {
  await p.waitForFunction((source) => window.__status.some((s) => new RegExp(source).test(s.text)) || new RegExp(source).test(document.getElementById('status').textContent),
    pattern.source, { timeout, polling: 20 });
}
const clearStatus = (p) => p.evaluate(() => { window.__status = []; });
const ready = (p) => p.waitForFunction(() => typeof profile !== 'undefined' && profile && document.querySelector('#template option'), null, { timeout: 15000 });
async function words(p, text, delay = 0) {
  await p.click('#prompt'); await p.keyboard.press('ControlOrMeta+a'); await p.keyboard.press('Backspace');
  await p.keyboard.type(text, { delay });
}
// Settled for the words last typed: the debounced send has gone out and its settle landed (not an older settle's class).
const settled = async (p, timeout = 15000) => { await p.waitForTimeout(300);
  await p.waitForFunction(() => document.body.classList.contains('settled') && typeof settledV !== 'undefined' && settledV === sentV, null, { timeout, polling: 20 }); };
async function choose(p, name) { await p.selectOption('#template', name); await p.waitForFunction((n) => profile?.name === n, name); }
const last = async () => (await fetch(URL + 'fake/last')).json();
const shot = (p, name) => p.screenshot({ path: `${OUT}/${name}.png` });

if (!process.argv.includes('--live')) {
  const { context, page: p } = await open();
  await ready(p);

  await check('status lines: loading, live, refining, finishing, settled', async () => {
    await clearStatus(p);
    await choose(p, 'image_z_image_turbo');
    await waitStatus(p, /^Loading Z-Image-Turbo/);
    await words(p, 'a red tram in the rain');
    await waitStatus(p, /^Live · \d+\.\d s$/);
    await waitStatus(p, /^Refining \d\/8$/);
    await waitStatus(p, /^Finishing \(decoding 1 MP\)$/);
    await waitStatus(p, /^Settled · \d+\.\d s$/);
    const seen = [...new Set(await p.evaluate(() => window.__status.map((s) => s.text)))];
    await shot(p, 'status-settled');
    return seen.slice(0, 6).join(' → ');
  });

  await check('failure reads as a sentence, dims the older picture, clears on the next success', async () => {
    await words(p, 'a red tram in the rain #fail');
    await waitStatus(p, /could not start/);
    const text = await statusText(p);
    assert(!/Prompt outputs|Value not in list/.test(text), `raw failure shown: ${text}`);
    assert(await p.evaluate(() => document.getElementById('stage').classList.contains('stale')), 'stage not dimmed');
    assert((await p.textContent('#tag')) === 'older picture', 'no "older picture" label');
    await shot(p, 'failure-stale');
    await clearStatus(p);
    await words(p, 'a red tram in the rain at dusk');
    await waitStatus(p, /^Live/);
    await p.waitForFunction(() => shownV >= epochV && window.gvLog.length);
    assert(!(await p.evaluate(() => document.getElementById('stage').classList.contains('stale'))), 'still dimmed after success');
    await settled(p);
    return text;
  });

  await check('GPU shared: says so, submits nothing, recovers', async () => {
    await words(p, 'a red tram #busy');
    await waitStatus(p, /^GPU shared · \d+ ahead$/);
    const text = await statusText(p);
    await words(p, 'a red tram at noon');
    await waitStatus(p, /^Live/, 8000);
    await settled(p);
    return text;
  });

  await check('empty words: says so, dims the older picture', async () => {
    await words(p, '');
    await p.keyboard.type(' '); await p.keyboard.press('Backspace');
    await waitStatus(p, /^Type to see it develop$/);
    assert(await p.evaluate(() => document.getElementById('stage').classList.contains('stale')), 'stage not dimmed');
  });

  await check('stale frames: a frame for the old template never shows after a switch', async () => {
    await words(p, 'a lighthouse #slow');
    await p.waitForTimeout(150);
    await choose(p, 'image_flux3_klein_distilled_t2i');
    const epoch = await p.evaluate(() => epochV);
    const switched = await p.evaluate(() => performance.now());
    assert(await p.evaluate(() => document.getElementById('stage').classList.contains('stale')), 'old picture not dimmed after the switch');
    await words(p, 'a lighthouse on a cliff');
    await settled(p);
    const shown = await p.evaluate((t) => window.gvLog.filter((f) => f.t > t), switched);
    const older = shown.filter((f) => f.v < epoch);
    assert(!older.length, `${older.length} frames from before the switch were shown`);
    return `${shown.length} frames after the switch, all for the new template`;
  });

  await check('compare: holding B shows the overlay, the stage is untouched', async () => {
    await words(p, 'a lighthouse on a cliff at dawn');
    await settled(p);
    const before = await p.evaluate(() => [document.getElementById('a').src, document.getElementById('b').src, front.id]);
    await p.focus('#hudbtn');
    await p.keyboard.down('b');
    const held = await p.evaluate(() => ({ shown: !document.getElementById('compare').hidden, src: document.getElementById('compare').src, tag: document.getElementById('tag').textContent }));
    await shot(p, 'compare-held');
    await p.keyboard.up('b');
    const after = await p.evaluate(() => [document.getElementById('a').src, document.getElementById('b').src, front.id]);
    const hidden = await p.evaluate(() => document.getElementById('compare').hidden);
    assert(held.shown && held.src, 'overlay not shown while B held');
    assert(hidden, 'overlay still shown after release');
    assert(JSON.stringify(before) === JSON.stringify(after), 'stage images changed');
    return held.tag;
  });

  await check('snapshots: branch, restore, take, persist across reload', async () => {
    const count0 = await p.evaluate(() => snaps.length);
    const first = await p.evaluate(() => snaps[snaps.length - 2]?.id);
    const firstWords = await p.evaluate(() => snaps[snaps.length - 2]?.prompt);
    await p.click(`.node[data-id="${first}"]`);
    await p.waitForFunction((w) => document.getElementById('prompt').value === w && head === snaps[snaps.length - 2].id, firstWords, { timeout: 5000 })
      .catch(async () => { throw new Error(`restore did not bring the words back: "${await p.inputValue('#prompt')}" vs "${firstWords}"`); });
    await p.click('#prompt'); await p.keyboard.press('End'); await p.keyboard.type(', fog');
    await settled(p);
    const branch = await p.evaluate(() => snaps[snaps.length - 1]);
    assert(branch.parent === first, `new snapshot's parent is ${branch.parent}, not the restored ${first}`);
    const lanes = await p.evaluate(() => new Set([...document.querySelectorAll('.node')].map((n) => n.style.top)).size);
    assert(lanes >= 2, 'the branch did not get its own lane');
    await p.focus('#hudbtn'); await p.keyboard.press('s');
    await p.waitForFunction(() => !snaps[snaps.length - 1].auto);
    const kept = await p.evaluate(() => ({ n: snaps.length, head, prompt: document.getElementById('prompt').value, seed, template }));
    await shot(p, 'tree-branch');
    await p.reload(); await ready(p);
    const back = await p.evaluate(() => ({ n: snaps.length, head, prompt: document.getElementById('prompt').value, seed, template }));
    assert(JSON.stringify(kept) === JSON.stringify(back), `after reload ${JSON.stringify(back)} != ${JSON.stringify(kept)}`);
    return `${count0 + 1} snapshots, ${lanes} lanes, restored after reload`;
  });

  await check('export and import the tree as JSON', async () => {
    const [download] = await Promise.all([p.waitForEvent('download'), p.click('#export')]);
    const file = `${OUT}/snapshots.json`; await download.saveAs(file);
    const data = JSON.parse(readFileSync(file, 'utf8'));
    assert(data.snaps.every((s) => s.parent !== undefined && Array.isArray(s.sources)), 'snapshots lack parent or sources');
    await p.evaluate(() => { snaps = []; head = null; keepTree(); });
    await p.setInputFiles('#importfile', file);
    await p.waitForFunction((n) => snaps.length === n, data.snaps.length);
    return `${data.snaps.length} snapshots, ${(JSON.stringify(data).length / 1024).toFixed(0)} KB`;
  });

  await check('references by role: an edit template waits for its picture, each slot fills its own input', async () => {
    await clearStatus(p);
    await choose(p, 'image_qwen_image_2_1_image_edit');
    await p.evaluate(() => { sources = []; renderControls(); send(); });
    await waitStatus(p, /^Drop a picture to edit$/, 6000).catch(async () => { throw new Error(`status "${await statusText(p)}" seen ${await p.evaluate(() => JSON.stringify(window.__status.map((s) => s.text)))} errors ${p.errors}`); });
    const labels = await p.$$eval('#tray .pick span', (els) => els.map((e) => e.textContent));
    assert(JSON.stringify(labels) === '["source","reference 1"]', `tray labels ${labels}`);
    for (const [i, file] of ['portrait.png', 'street.png'].entries()) {
      const [chooser] = await Promise.all([p.waitForEvent('filechooser'), p.click(`#tray .pick >> nth=${i}`)]);
      await chooser.setFiles(`${INPUTS}/${file}`);
      await p.waitForFunction((n) => sources.length === n, i + 1, { timeout: 5000 })
        .catch(async () => { throw new Error(`sources ${await p.evaluate(() => JSON.stringify(sources))} status ${await statusText(p)}`); });
    }
    await words(p, 'she wears a yellow raincoat');
    await settled(p);
    const { takes, state } = await last();
    const bySlot = Object.fromEntries(state.sources.map((s) => [s.slot, s.id]));
    assert(takes.some(([node, , id]) => node === '470' && id === bySlot['470.image']), 'source did not fill 470.image');
    assert(takes.some(([node, , id]) => node === '475' && id === bySlot['475.image']), 'reference did not fill 475.image');
    assert(bySlot['470.image'] !== bySlot['475.image'], 'both slots hold the same picture');
    await shot(p, 'references');
    return takes.map(([n, k]) => `${n}.${k}`).join(', ');
  });

  await check('settings drawer: a value applies to live and settle', async () => {
    await choose(p, 'image_z_image_turbo');
    await p.click('#settings');
    await p.waitForSelector('#drawer:not([hidden])');
    const address = await p.evaluate(() => profile.settings.find((c) => c.control === 'slider' && /shift|cfg/.test(c.label)).address);
    await p.$eval(`#controls input[data-a="${address}"]`, (el) => { el.value = 4.2; el.dispatchEvent(new Event('input', { bubbles: true })); });
    const value = await p.evaluate((a) => values[a], address);
    await p.keyboard.press('Escape');
    assert(await p.evaluate(() => document.getElementById('drawer').hidden), 'Esc did not close the drawer');
    await words(p, 'a teapot on a table');
    await settled(p);
    const { state, graph, settle } = await last();
    const slot = (await p.evaluate(() => profile.settings)).find((c) => c.address === address);
    assert(state.values[address] === value, 'value not in the state');
    assert(settle, 'last job was not the settle');
    const nodes = Object.values(graph).filter((n) => Object.values(n.inputs).includes(value));
    assert(nodes.length, `no node in the settle graph holds ${value}`);
    await shot(p, 'settings');
    return `${slot.label} = ${value} in live and settle`;
  });

  await check('clip drafts: the frames decode with the tiny decoder paired with the VAE, the sound with its own; the full clip with the template\'s; a mid-text swap drafts only the whole word', async () => {
    await choose(p, 'video_fastvideo_fasth3_t2v');
    await words(p, 'a pirate shaking the screen');
    let draft = null;
    for (let i = 0; i < 100 && !draft; i++) { const l = await last(); if (l.lane === 'live' && /pirate/.test(l.state.prompt)) draft = l.graph; else await p.waitForTimeout(50); }
    assert(draft, 'no draft job');
    const decodes = (g) => Object.values(g).filter((n) => /^VAEDecode/.test(n.class_type)).map((n) => [n.class_type, g[n.inputs.vae[0]]?.inputs.vae_name]);
    const d = decodes(draft);
    assert(draft['gv:tiny']?.inputs.vae_name === 'taeh3.pth' && d.some(([c, v]) => c === 'VAEDecode' && v === 'taeh3.pth') && d.some(([c, v]) => c === 'VAEDecodeAudio' && v !== 'taeh3.pth'), `draft decodes ${JSON.stringify(d)}`);
    await settled(p, 25000);
    const full = (await last()).graph;
    assert(!full['gv:tiny'] && !decodes(full).some(([, v]) => v === 'taeh3.pth'), `full clip decodes ${JSON.stringify(decodes(full))}`);
    // a subject swap mid-text: no draft for the half-typed word, one for the whole word once the keys rest
    await p.evaluate(() => { const el = $('prompt'); el.focus(); el.setSelectionRange(2, 8); });
    const seen = [];
    for (const ch of 'grandma') { await p.keyboard.type(ch); await p.waitForTimeout(60); const l = await last(); if (l.lane === 'live') seen.push(l.state.prompt.split(' ')[1]); }
    for (let i = 0; i < 60 && !seen.includes('grandma'); i++) { await p.waitForTimeout(50); const l = await last(); if (l.lane === 'live') seen.push(l.state.prompt.split(' ')[1]); }
    const halves = [...new Set(seen)].filter((w) => w !== 'pirate' && w !== 'grandma');
    assert(seen.includes('grandma') && !halves.length, `drafts for ${JSON.stringify([...new Set(seen)])}`);
    return `draft ${JSON.stringify(d)}; full clip ${JSON.stringify(decodes(full))}; a swap drafted only the whole word`;
  });

  await check('clips: history shows a poster and a ▶ badge', async () => {
    await choose(p, 'video_fastvideo_fasth3_t2v');
    await words(p, 'a paper boat on a pond');
    await settled(p, 25000);
    await p.waitForFunction(() => snaps[snaps.length - 1].media === 'video' && snaps[snaps.length - 1].thumb, null, { timeout: 8000 });
    const node = await p.$eval('.node:last-of-type', (n) => ({ clip: n.classList.contains('clip'), bg: getComputedStyle(n).backgroundImage.slice(0, 30), badge: getComputedStyle(n, '::after').content }));
    assert(node.clip && node.bg.startsWith('url("data:image/jpeg'), `clip node ${JSON.stringify(node)}`);
    await shot(p, 'clip-history');
    return `${node.badge} badge, poster ${node.bg.slice(5, 25)}…`;
  });

  await check('offline: says so, reconnects, resends', async () => {
    await choose(p, 'image_z_image_turbo');
    await context.setOffline(true);
    await p.evaluate(() => dispatchEvent(new Event('offline')));
    await waitStatus(p, /^Offline: reconnecting$/);
    await context.setOffline(false);
    await p.evaluate(() => dispatchEvent(new Event('online')));
    await words(p, 'a kayak on a lake');
    await settled(p);
    return await statusText(p);
  });

  await check('keyboard only: every control reachable, tree nodes are buttons, Esc returns focus', async () => {
    await p.click('#prompt');
    const seen = [];
    for (let i = 0; i < 40; i++) {
      await p.keyboard.press('Tab');
      seen.push(await p.evaluate(() => { const e = document.activeElement; return e.id || e.className || e.tagName; }));
    }
    for (const want of ['template', 'pick', 'seeds', 'reroll', 'settings', 'hudbtn', 'export', 'node']) {
      assert(seen.some((s) => s.includes(want)), `${want} not reachable by Tab (${seen.join(' ')})`);
    }
    await p.click('#settings');  // the seed, speed and live steps live in Settings, reachable once it opens
    const inSettings = [];
    for (let i = 0; i < 12; i++) { await p.keyboard.press('Tab'); inSettings.push(await p.evaluate(() => document.activeElement.id)); }
    assert(inSettings.includes('seedv') && inSettings.includes('lp'), `seed or live steps not reachable in Settings (${inSettings.join(' ')})`);
    await p.keyboard.press('Escape');
    const grid = await p.evaluate(() => [...document.querySelectorAll('#grid button')].some((b) => b === document.activeElement));
    assert(!grid, 'a closed seed tile took focus');
    const node = await p.$('.node:not(.head)');
    await node.focus();
    const wanted = await node.evaluate((n) => snaps.find((s) => s.id === n.dataset.id).prompt);
    await p.keyboard.press('Enter');
    assert((await p.inputValue('#prompt')) === wanted, 'Enter on a node did not restore it');
    await p.focus('#seeds');
    await p.keyboard.press('Enter');
    assert(await p.evaluate(() => !document.getElementById('field').inert && document.activeElement.closest('#grid')), 'the seed field did not open with focus inside');
    await p.keyboard.press('Escape');
    assert(await p.evaluate(() => document.getElementById('field').inert && document.activeElement.id === 'seeds'), 'Esc did not return focus to Seeds');
    const names = await p.evaluate(() => [...document.querySelectorAll('input[type=range], textarea')].filter((e) => !e.closest('[hidden]')).map((e) => e.labels?.[0]?.textContent.trim() || e.getAttribute('aria-label')));
    assert(names.every(Boolean), 'an unnamed slider or prompt');
    const alt = await p.evaluate(() => front.alt);
    assert(alt.length > 0, 'the stage picture has no alt text');
    return `${new Set(seen).size} stops; stage alt "${alt}"`;
  });

  await check('a silent job: the line says so and the older picture dims, then live again', async () => {
    await choose(p, 'image_z_image_turbo');
    await words(p, 'a kayak on a lake');
    await settled(p);
    await clearStatus(p);
    await words(p, 'a kayak on a lake at dusk #slow');
    await waitStatus(p, /^Waiting on the GPU$|^Loading /, 2500);
    const dim = await p.evaluate(() => [document.getElementById('stage').classList.contains('stale'), document.getElementById('tag').textContent]);
    await waitStatus(p, /^Live/, 8000);
    assert(dim[0] && dim[1] === 'older picture', `not dimmed while silent: ${dim}`);
    await settled(p);
    return (await p.evaluate(() => window.__status.map((s) => s.text))).slice(0, 3).join(' → ');
  });

  await check('words go out on a word boundary, the whole text once the keys rest', async () => {
    await p.evaluate(() => { window.__sends.length = 0; });
    await words(p, 'a tram crossing a bridge', 100);
    await settled(p);
    const sent = await p.evaluate(() => window.__sends.length);
    const { state } = await last();
    assert(state.prompt === 'a tram crossing a bridge', `settled on "${state.prompt}"`);
    assert(sent <= 7, `${sent} states sent for 24 characters`);
    return `${sent} states for 24 characters at 10/s`;
  });

  await check('pipeline: words typed during a live job go ahead, queued behind it, so the next frame starts as it ends', async () => {
    const v0 = await p.evaluate(() => sentV + 1);
    await words(p, 'a red kite over a green hill in the wind', 60);
    await settled(p);
    const jobs = (await (await fetch(URL + 'fake/live')).json()).filter((j) => j.v >= v0 && j.ended).sort((a, b) => a.started - b.started);
    const pairs = jobs.slice(1).map((j, i) => ({ ahead: j.submitted < jobs[i].ended, gap: j.started - jobs[i].ended, overlap: j.started < jobs[i].ended }));
    const ahead = pairs.filter((x) => x.ahead), after = pairs.filter((x) => !x.ahead);
    const med = (xs) => xs.length ? xs.map((x) => x.gap).sort((a, b) => a - b)[xs.length >> 1] : null;
    assert(!pairs.some((x) => x.overlap), 'two live jobs ran at once');
    assert(ahead.length >= 1, `no frame went ahead in ${jobs.length} live jobs`);
    assert(med(ahead) < 25, `a frame that went ahead started ${med(ahead)} ms after the one before`);
    return `${jobs.length} live jobs, ${ahead.length} went ahead (next start ${med(ahead)} ms after the end), ${after.length} after the end (${med(after)} ms)`;
  });

  await check('a live job served from cache still shows its picture', async () => {
    const red = `a red tram number ${Date.now() % 1000}`;
    await words(p, red);
    await settled(p);
    await words(p, 'a blue tram');
    await settled(p);
    const t = await p.evaluate(() => performance.now());
    await words(p, red);
    await p.waitForFunction((t) => window.gvLog.some((f) => f.t > t && f.stage === 'live' && f.kind === 'final'), t, { timeout: 5000 }).catch(() => {});
    const live = await p.evaluate((t) => window.gvLog.filter((f) => f.t > t && f.stage === 'live' && f.kind === 'final').length, t);
    await p.waitForFunction(() => document.body.classList.contains('settled') && ph === 'settled', null, { timeout: 15000 });
    assert(live > 0, 'no live picture for words ComfyUI served from cache');
    return `${live} live pictures for the repeated words`;
  });

  await check('enrich: what the model read, its words marked, copyable into the box', async () => {
    await choose(p, 'image_flux3_klein_distilled_t2i');
    await words(p, 'a fox in the snow');
    await settled(p);
    await p.click('#enrich');
    await p.waitForSelector('#read:not([hidden])', { timeout: 10000 });
    const marks = await p.evaluate(() => ({ ins: document.querySelectorAll('#modelread ins').length, del: document.querySelectorAll('#modelread del').length }));
    await shot(p, 'enrich-read');
    await p.click('#usewords');
    const box = await p.inputValue('#prompt');
    assert(marks.ins && marks.del, `diff marks ${JSON.stringify(marks)}`);
    assert(/cinematic lighting/.test(box), `box holds "${box}"`);
    await settled(p);
    return `${marks.ins} added, ${marks.del} dropped; box "${box}"`;
  });

  await check('reroll: R shows a seed the field rendered, at once; ⇧R goes back', async () => {
    await p.waitForFunction(() => field.length >= 3, null, { timeout: 15000 });
    const before = await p.evaluate(() => seed);
    await p.focus('#hudbtn'); await p.keyboard.press('r');
    await p.waitForFunction(() => front.src === field.find((t) => t.seed === seed)?.src, null, { timeout: 300 }).catch(() => {});
    const after = await p.evaluate(() => [seed, front.src === field.find((t) => t.seed === seed)?.src]);
    await p.keyboard.press('Shift+R');
    const back = await p.evaluate(() => seed);
    assert(after[0] !== before && after[1], `R gave ${after}`);
    assert(back === before, `⇧R gave ${back}, not ${before}`);
    await settled(p);
    return `${before} → ${after[0]} → ${back}`;
  });

  await check('step timeline: ← walks back through the job\'s step previews', async () => {
    await words(p, 'a fox in the snow at night');
    await settled(p);
    await p.focus('#hudbtn'); await p.keyboard.press('ArrowLeft'); await p.keyboard.press('ArrowLeft');
    const at = await p.evaluate(() => [timeline.at, timeline.frames.length]);
    assert(at[0] !== null && at[0] < at[1] - 1, `timeline at ${at}`);
    return `step ${at[0] + 1} of ${at[1]}`;
  });

  const rungs = async () => (await fetch(URL + 'fake/rungs')).json();
  const lastSnap = (p) => p.evaluate(() => snaps[snaps.length - 1]);
  // A picture made in the page, w x h, for "match picture".
  const canvasPicture = (p, w, h) => p.evaluate(async ([w, h]) => {
    const c = Object.assign(document.createElement('canvas'), { width: w, height: h }); const x = c.getContext('2d');
    x.fillStyle = '#3a6'; x.fillRect(0, 0, w, h); x.fillStyle = '#fc3'; x.fillRect(w / 4, h / 4, w / 2, h / 2);
    const blob = await new Promise((done) => c.toBlob(done, 'image/png'));
    await useSource(new File([blob], 'wide.png', { type: 'image/png' }), profile.media[0].address);
  }, [w, h]);

  await check('ladder: Graduate climbs Final then 2K as child snapshots, live keeps working, Keep saves the highest rung', async () => {
    await choose(p, 'image_flux3_klein_distilled_t2i');
    await words(p, 'a fox on a frozen lake');
    await settled(p);
    const settledSnap = await p.evaluate(() => head);
    assert((await p.textContent('#rung')) === 'Settled', `badge ${await p.textContent('#rung')}`);
    assert((await p.textContent('#grad')).includes('Final'), `graduate reads ${await p.textContent('#grad')}`);
    await clearStatus(p);
    await p.click('#grad');
    await p.waitForFunction((id) => snaps.at(-1).rung === 'final' && snaps.at(-1).parent === id, settledSnap, { timeout: 15000 });
    await waitStatus(p, /^Final \d\/\d|^Graduating to Final/);
    await waitStatus(p, /^Final · \d+\.\d s/);
    const finalGraph = (await rungs()).at(-1).graph;
    assert(finalGraph['gv:final'] && Object.keys(finalGraph).some((k) => k.startsWith('gv:e:')), 'Final graph lacks its refine or the enhancer copy');
    assert((await p.textContent('#rung')) === 'Final' && (await p.evaluate(() => head === snaps.at(-1).id)), 'the stage did not take the Final');
    const finalSnap = await p.evaluate(() => head);
    await p.waitForSelector('#read:not([hidden])', { timeout: 5000 });  // what the enhancer wrote, from the Final
    await p.keyboard.press('Escape');
    // 2K by the manifest's path (a tool over the Final's picture); typing while it climbs keeps live working
    await p.focus('#hudbtn'); await p.keyboard.press('u');
    const t = await p.evaluate(() => performance.now());
    await p.waitForFunction((id) => snaps.at(-1).rung === '2k' && snaps.at(-1).parent === id, finalSnap, { timeout: 20000 });
    const tool = (await rungs()).at(-1);
    assert(Object.values(tool.graph).some((n) => /SeedVR2/.test(n.class_type)) && tool.takes.length === 1, '2K did not run the upscale tool on one picture');
    assert(Object.values(tool.graph).every((n) => n.class_type !== 'ImageCompare'), 'the tool kept its compare viewer');
    const node = await p.$eval(`.node[data-id="${await p.evaluate(() => snaps.at(-1).id)}"]`, (n) => n.dataset.rung);
    assert(node === '2K', `tree badge ${node}`);
    await shot(p, 'ladder-2k');
    await clearStatus(p);
    await p.click('#keep');
    await waitStatus(p, /^Kept · /, 8000);
    const kept = (await statusText(p)).replace('Kept · ', '');
    const record = JSON.parse(readFileSync(`${KEPT}/${kept.replace(/\.\w+$/, '.json')}`, 'utf8'));
    assert(record.rung === '2k' && record.words === 'a fox on a frozen lake' && record.lineage.length >= 3, `keep JSON ${JSON.stringify(record).slice(0, 200)}`);
    // input during a rung: live frames keep coming, the rung lands once the loop rests
    await p.click(`.node[data-id="${settledSnap}"]`);
    await p.waitForFunction((id) => head === id, settledSnap);
    await p.focus('#hudbtn'); await p.keyboard.press('u');
    await words(p, 'a fox on a frozen lake at dusk');
    await p.waitForFunction((t) => window.gvLog.some((f) => f.t > t && f.stage === 'live' && f.kind === 'final'), t, { timeout: 5000 });
    await settled(p);
    return `settled → Final (${Object.keys(finalGraph).filter((k) => k.startsWith('gv:e:')).length} enhancer nodes) → 2K by ${tool.graph ? 'the upscale tool' : '?'}; kept ${kept} (${record.lineage.map((l) => l.rung).join(' ← ')})`;
  });

  await check('panels: 4 seeds in one batched job, none settles until picked, the pick settles that panel, alternatives in the tree', async () => {
    await choose(p, 'image_z_image_turbo');
    await p.click('#split button[data-n="4"]');
    await words(p, 'a red bicycle against a blue wall');
    await p.waitForFunction(() => panelSet?.srcs?.length === 4 && [...document.querySelectorAll('#cells img')].every((i) => i.src), null, { timeout: 10000 });
    const { state, graph } = await last();
    assert(state.panels === 4 && Object.values(graph).some((n) => n.inputs.batch_size === 4), `the live job was not one batch of 4: panels ${state.panels}, ${JSON.stringify(Object.values(graph).filter((n) => 'batch_size' in n.inputs).map((n) => n.inputs.batch_size))}, lane ${(await last()).lane}`);
    await p.waitForTimeout(1500);
    assert(!(await p.evaluate(() => document.body.classList.contains('settled'))), 'split panels settled before a pick');
    await shot(p, 'panels-4');
    const n0 = await p.evaluate(() => snaps.length);
    await p.focus('#hudbtn'); await p.keyboard.press('3');
    await settled(p);
    const settleGraph = (await last()).graph;
    assert(settleGraph['gv:pick']?.inputs.batch_index === 2, `settle picked ${JSON.stringify(settleGraph['gv:pick'])}`);
    const alts = await p.evaluate((n0) => snaps.slice(n0).map((s) => [s.panel, s.rung, s.seed]), n0);
    assert(alts.filter(([pn]) => pn !== 2).length === 3 && alts.some(([pn, r]) => pn === 2 && r === 'settled'), `alternatives ${JSON.stringify(alts)}`);
    const alt = await p.evaluate((n0) => snaps.slice(n0).find((s) => s.panel === 0).id, n0);
    await p.click(`.node[data-id="${alt}"]`);
    await settled(p);
    assert((await last()).graph['gv:pick']?.inputs.batch_index === 0, 'restoring panel 1 did not reproduce (seed, 0)');
    await p.click('#split button[data-n="1"]');
    await settled(p);
    return `batch of 4, pick 3 → batch_index 2, ${alts.length - 1} alternatives; restored panel 1 → batch_index 0`;
  });

  const idle = async () => (await fetch(URL + 'fake/idle')).json();
  const latentArea = (graph) => { const n = Object.values(graph).find((x) => /Latent/.test(x.class_type) && Number.isInteger(x.inputs.width)); return n ? n.inputs.width * n.inputs.height / 1e6 : null; };

  await check('speed: calibrates on Live-on, fast typing renders a size smaller until the pause, the dial picks steps and size, the meter says so', async () => {
    await choose(p, 'image_z_image_turbo');
    await words(p, 'a quiet harbour');
    await settled(p);
    await p.waitForFunction(() => /ms\/step/.test(document.getElementById('meter').textContent), null, { timeout: 15000 });
    const calib = (await idle()).filter((j) => j.lane === 'calib' && j.state.template === 'image_z_image_turbo');
    assert(calib.length >= 4, `calibration jobs ${calib.length}`);
    // keys 60 ms apart: the live jobs while typing are one bucket smaller; the settle is not
    const sizes = [];
    await p.click('#prompt'); await p.keyboard.press('ControlOrMeta+a'); await p.keyboard.press('Backspace');
    for (const ch of 'a quiet harbour at dawn with boats') { await p.keyboard.type(ch); await p.waitForTimeout(60); const l = await last(); if (l.lane === 'live') sizes.push([l.state.fast, latentArea(l.graph)]); }
    await settled(p);
    const fast = sizes.filter(([f]) => f), slow = (await last()).graph;
    assert(fast.length && fast.every(([, a]) => a < 0.2), `fast typing sizes ${JSON.stringify(sizes.slice(0, 6))}`);
    // the dial: snappy picks a smaller size or fewer steps than pretty, both within the floors
    await p.click('#settings');  // the speed dial lives in Settings
    await p.click('#speed button[data-s="snappy"]'); await words(p, 'a quiet harbour at noon'); await settled(p);
    const snappy = await p.evaluate(() => lastMeter);
    await p.click('#speed button[data-s="pretty"]'); await words(p, 'a quiet harbour at night'); await settled(p);
    const pretty = await p.evaluate(() => lastMeter);
    await p.click('#speed button[data-s=""]');
    await p.keyboard.press('Escape');
    const meterText = await p.textContent('#meter');
    assert(snappy.steps * snappy.mp <= pretty.steps * pretty.mp && snappy.steps >= 4 && snappy.mp >= 0.1, `snappy ${JSON.stringify(snappy)} pretty ${JSON.stringify(pretty)}`);
    assert(/ms\/step · (resident|partly offloaded) · \d+ steps · [\d.]+ MP · nobody ahead/.test(meterText), `meter "${meterText}"`);
    return `calibrated in ${calib.length} jobs; typing ${fast.length} live jobs at ${fast[0][1].toFixed(2)} MP, settle from ${latentArea(slow)?.toFixed(2) ?? 'its own'} MP; snappy ${snappy.steps}×${snappy.mp}, pretty ${pretty.steps}×${pretty.mp}; "${meterText}"`;
  });

  await check('step lock: composition held at step k of n on the locked words, new words after it; unlock renders whole', async () => {
    await choose(p, 'image_z_image_turbo');
    await words(p, 'a castle on a hill');
    await settled(p);
    await p.focus('#hudbtn'); await p.keyboard.press('l');
    await settled(p);
    await words(p, 'a castle on a hill in snow');
    await p.waitForFunction(() => /^Composition locked at step \d of \d/.test(document.getElementById('status').textContent) || window.__status.some((s) => /^Composition locked/.test(s.text)), null, { timeout: 10000 });
    const said = (await p.evaluate(() => window.__status.map((s) => s.text))).find((t) => /^Composition locked/.test(t));
    await settled(p);
    const locked = (await last()).graph;
    const first = locked['gv:lock'], words0 = Object.entries(locked).filter(([k]) => k.startsWith('gv:w:')).map(([, n]) => JSON.stringify(n.inputs)).join(' ');
    assert(first?.class_type === 'KSamplerAdvanced' && first.inputs.end_at_step >= 1 && /a castle on a hill"/.test(words0), `lock graph ${JSON.stringify(first)} ${words0.slice(0, 120)}`);
    assert(Object.values(locked).some((n) => n.class_type === 'KSamplerAdvanced' && n.inputs.add_noise === 'disable' && n.inputs.start_at_step === first.inputs.end_at_step), 'no second half from step k');
    await p.focus('#hudbtn'); await p.keyboard.press('l');
    await settled(p);
    assert(!(await last()).graph['gv:lock'], 'unlock still locked');
    return `"${said}"; first half to step ${first.inputs.end_at_step} on the old words, second half without noise; unlocked`;
  });

  await check('layout boxes: drawn, named and dragged into the template\'s layout node, in a snapshot and restored; H hides them', async () => {
    await choose(p, 'image_flux3_klein_distilled_t2i');
    await words(p, 'a cat in a sunlit garden');
    await settled(p);
    assert(await p.isVisible('#boxbtn'), 'no Box tool on a template with layout nodes');
    const r = await p.locator('#boxes').boundingBox(), xy = (fx, fy) => [r.x + r.width * fx, r.y + r.height * fy];
    await p.focus('#hudbtn'); await p.keyboard.press('o');
    await p.mouse.move(...xy(0.2, 0.6)); await p.mouse.down(); await p.mouse.move(...xy(0.3, 0.7)); await p.mouse.move(...xy(0.45, 0.9)); await p.mouse.up();
    await p.waitForFunction(() => document.activeElement?.matches('#boxes .box input'));
    await p.keyboard.type('cat'); await p.keyboard.press('Enter');
    await settled(p);
    const graphBoxes = (g) => { const n = Object.values(g).find((x) => x.class_type === 'CreateBoundingBoxes'); return n?.inputs.bboxes ? JSON.parse(n.inputs.bboxes) : []; };
    const wired = (g) => { const enc = Object.values(g).find((x) => x.class_type === 'CLIPTextEncode' && Array.isArray(x.inputs.text) && g[x.inputs.text[0]]?.class_type === 'Flux3LayoutPrompt'); return !!enc; };
    let g = (await last()).graph, b = graphBoxes(g);
    assert(b.length === 1 && b[0].desc === 'cat' && Math.abs(b[0].bbox[0] - 600) < 15 && Math.abs(b[0].bbox[1] - 200) < 15 && Math.abs(b[0].bbox[3] - 450) < 15, `graph boxes ${JSON.stringify(b)}`);
    assert(wired(g), 'the layout node is not between the words and the encoder');
    const drawn = b[0].bbox;
    const sends0 = await p.evaluate(() => window.__sends.length);
    await p.mouse.move(...xy(0.3, 0.75)); await p.mouse.down();
    for (let i = 1; i <= 8; i++) { await p.mouse.move(...xy(0.3 + 0.04 * i, 0.75 - 0.06 * i)); await p.waitForTimeout(40); }
    await p.mouse.up();
    await settled(p);
    const moves = (await p.evaluate(() => window.__sends.length)) - sends0;
    b = graphBoxes((await last()).graph);
    assert(b[0].bbox[1] > drawn[1] + 250 && b[0].bbox[0] < drawn[0] - 400, `dragged box ${JSON.stringify(b[0].bbox)} from ${JSON.stringify(drawn)}`);
    assert(moves >= 3, `a drag sent ${moves} states`);
    await p.focus('#hudbtn'); await p.keyboard.press('s');
    await p.waitForFunction(() => !snaps.at(-1).auto && snaps.at(-1).boxes?.length === 1);
    const snapId = await p.evaluate(() => snaps.at(-1).id);
    await p.click('#boxes .box .x');
    await settled(p);
    assert(!graphBoxes((await last()).graph).length && !wired((await last()).graph), 'a removed box is still in the graph');
    await p.click(`.node[data-id="${snapId}"]`);
    await p.waitForFunction(() => document.querySelectorAll('#boxes .box').length === 1);
    await settled(p);
    b = graphBoxes((await last()).graph);
    assert(b.length === 1 && b[0].desc === 'cat', `restored graph boxes ${JSON.stringify(b)}`);
    await p.focus('#hudbtn'); await p.keyboard.press('h');
    const hidden = await p.evaluate(() => document.getElementById('boxes').classList.contains('off') && !document.querySelector('#boxes .box').offsetWidth);
    await p.keyboard.press('h');
    assert(hidden, 'H did not hide the boxes');
    await shot(p, 'boxes');
    return `drawn ${JSON.stringify(drawn)} "cat", dragged to ${JSON.stringify(b[0].bbox)} in ${moves} live states; snapshot restored; H hides`;
  });

  await check('pin: a drawn region keeps the stage picture under a noise mask, the settle too; Esc unpins', async () => {
    await p.evaluate(() => { boxes = []; paintBoxes(); });
    await words(p, 'a fox on a rock');
    await settled(p);
    const r = await p.locator('#boxes').boundingBox(), xy = (fx, fy) => [r.x + r.width * fx, r.y + r.height * fy];
    await clearStatus(p);
    await p.focus('#hudbtn'); await p.keyboard.press('p');
    await p.mouse.move(...xy(0.05, 0.05)); await p.mouse.down(); await p.mouse.move(...xy(0.3, 0.5)); await p.mouse.move(...xy(0.5, 0.95)); await p.mouse.up();
    await waitStatus(p, /^Pinned · typing changes the rest/);
    await words(p, 'a snowy village at night');
    await settled(p);
    const { graph: g, takes } = await last();
    const sampler = Object.values(g).find((n) => n.class_type === 'KSampler' && Array.isArray(n.inputs.latent_image) && n.inputs.latent_image[0] === 'gv:pinned');
    assert(sampler && g['gv:pinned']?.class_type === 'SetLatentNoiseMask' && g['gv:pinmix']?.inputs.operation === 'multiply', 'no pinned latent in the graph');
    assert(takes.some((t) => t[0] === 'gv:pinload'), `the pin's picture is not taken: ${JSON.stringify(takes)}`);
    const hole = g['gv:pinhole'].inputs, mix = g['gv:pinmix'].inputs;
    assert(Math.abs(mix.x - 13) <= 2 && Math.abs(mix.y - 13) <= 2 && Math.abs(hole.width - 115) <= 3 && Math.abs(hole.height - 230) <= 3, `pin rect ${JSON.stringify({ mix, hole })}`);
    assert(g['gv:uppin']?.inputs.mask?.[0] === 'gv:pinmask', 'the settle does not carry the pin');
    await p.focus('#hudbtn');
    for (let i = 0; i < 4 && await p.evaluate(() => !!pinned); i++) await p.keyboard.press('Escape');  // Esc closes what is open first
    await settled(p);
    assert(!(await last()).graph['gv:pinned'], 'Esc did not unpin');
    await shot(p, 'pin');
    return `pin at ${mix.x},${mix.y} ${hole.width}×${hole.height} of 256; sampler from gv:pinned, the settle masked too; unpinned by Esc`;
  });

  await check('pin as reference: F puts the stage picture into the edit template\'s slot; references at live size live, the template\'s above', async () => {
    await choose(p, 'image_flux3_klein_distilled_t2i');
    await words(p, 'a lighthouse at dusk');
    await settled(p);
    await p.focus('#hudbtn'); await p.keyboard.press('f');
    await p.waitForFunction(() => profile?.name === 'image_flux3_klein_distilled_image_edit' && sources.some((x) => x.slot === profile.media[0].address), null, { timeout: 10000 });
    await settled(p);
    const settleGraph = (await last()).graph;
    const refs = (g) => Object.entries(g).filter(([, n]) => n.class_type === 'ReferenceMultiLatent').map(([k, n]) => `${k}:${n.inputs['resolution.megapixels']}`).sort();
    const refine = settleGraph['gv:refine'];
    assert(refs(settleGraph).includes('gv:r:17:1') && refs(settleGraph).some((r) => /^17:0\.(1|16|25|36|5)$/.test(r)), `settle refs ${refs(settleGraph)}`);
    assert(refine?.inputs.positive?.[0] === 'gv:r:17', `the settle refines on ${JSON.stringify(refine?.inputs.positive)}`);
    const { takes } = await last();
    assert(takes.some((t) => t[0] === '11'), `the picture is not in the slot: ${JSON.stringify(takes)}`);
    return `switched to the edit template with the stage picture; refs ${refs(settleGraph).join(', ')}; the settle refines on gv:r:17`;
  });

  await check('flow: W chains each frame on the last; the level follows the word diff; the chain re-roots every 10 frames; the settle refines the head', async () => {
    await choose(p, 'image_flux3_klein_distilled_t2i');
    await p.evaluate(() => { boxes = []; pinned = null; paintBoxes(); });
    await words(p, 'a cat on a windowsill');
    await settled(p);
    await p.focus('#hudbtn'); await p.keyboard.press('w');
    assert(await p.evaluate(() => flowOn && document.getElementById('flowbtn').getAttribute('aria-pressed') === 'true'), 'W did not turn Flow on');
    const step = async (text) => {
      const v = await p.evaluate((t) => { document.getElementById('prompt').value = t; send(); return sentV; }, text);
      await p.waitForFunction((v0) => window.gvLog.some((f) => f.v >= v0 && f.stage === 'live' && f.kind === 'final'), v, { timeout: 15000 });
      const g = (await last()).graph;
      const heads = Object.keys(g).filter((k) => /^gv:fl\d+$/.test(k)).sort((x, y) => +x.slice(5) - +y.slice(5));
      const head = g[heads.at(-1)];
      return { n: heads.length, steps: head?.inputs.steps, denoise: head?.inputs.denoise, root: !!g['gv:fl:load'], mix: g[`${heads.at(-1)}:mix`]?.inputs.ratio, g };
    };
    const seen = [];
    try {
    let r = await step('a cat on a windowsill at dawn');  // first Flow frame: from noise
    seen.push(`reset n${r.n}`);
    assert(r.n === 0, `the first Flow frame is not from noise: ${r.n} chained`);
    r = await step('a cat on a windowsill at dawn, sleeping'); seen.push(`word ${r.steps}/${r.denoise}`);
    assert(r.steps === 2 && r.denoise === 0.5 && r.mix === 0.85, `one word: ${JSON.stringify([r.steps, r.denoise, r.mix])}`);
    r = await step('a fox on a wooden windowsill at night, sleeping'); seen.push(`phrase ${r.steps}/${r.denoise}`);
    assert(r.steps === 3 && r.denoise === 0.75, `a phrase: ${r.steps}/${r.denoise}`);
    r = await step('a fox on a wooden windowsill at night, sleeping'); seen.push(`same ${r.steps}/${r.denoise}`);
    assert(r.steps === 1 && r.denoise === 0.0625, `same words: ${r.steps}/${r.denoise}`);
    const status = await p.evaluate(() => document.getElementById('status').textContent);
    let rooted = null;
    for (let i = 0; i < 9 && !rooted; i++) {
      r = await step(`a fox on a wooden windowsill at night, sleeping ${'z'.repeat(i + 1)}`);
      if (r.root) rooted = { at: i, n: r.n };
    }
    assert(rooted, 'no re-root within 13 frames');
    const { takes } = await last();
    assert(takes.some((t) => t[0] === 'gv:fl:load') && takes.some((t) => t[0] === 'gv:fl:aload'), `re-root pictures not taken: ${JSON.stringify(takes)}`);
    await p.evaluate(() => { document.getElementById('prompt').value += ' quietly'; send(); });
    await settled(p);
    const sg = (await last()).graph;
    const head = Object.keys(sg).filter((k) => /^gv:fl\d+$/.test(k)).sort((x, y) => +x.slice(5) - +y.slice(5)).at(-1);
    assert(sg['gv:look']?.inputs.samples?.[0] === head && sg['gv:fl:load'], `the settle does not refine the chain's head: ${JSON.stringify(sg['gv:look']?.inputs)}`);
    assert(/^Flow · /.test(status), `status "${status}"`);
    return `${seen.join(', ')}; re-rooted after ${rooted.at + 5} frames (chain ${rooted.n}); "${status}"; the settle refines ${head}`;
    } finally { await p.evaluate(() => flowOn && toggleFlow(false)); }
  });

  await check('flow on the node pack: each frame recalls the last one\'s latent in ComfyUI, the graph stays one size, nothing re-roots', async () => {
    await fetch(URL + 'fake/pack', { method: 'POST', body: JSON.stringify({ on: true }) });
    try {
      await choose(p, 'image_z_image_turbo'); await choose(p, 'image_flux3_klein_distilled_t2i');
      await words(p, 'a cat on a windowsill');
      await settled(p);
      await p.focus('#hudbtn'); await p.keyboard.press('w');
      const step = async (text) => {
        const v = await p.evaluate((t) => { document.getElementById('prompt').value = t; send(); return sentV; }, text);
        await p.waitForFunction((v0) => window.gvLog.some((f) => f.v >= v0 && f.stage === 'live' && f.kind === 'final'), v, { timeout: 15000 });
        return (await last()).graph;
      };
      let g = await step('lighthouse at dusk');  // no word kept: the stream starts from noise
      assert(!g['gv:fl:prev'] && g['gv:fl:store']?.class_type === 'GVLatentStore', `the first frame is not from noise, stored: ${Object.keys(g).filter((k) => k.startsWith('gv:fl'))}`);
      const session = g['gv:fl:store'].inputs.session;
      let stored = g['gv:fl:store'].inputs.frame, anchor = stored;
      const sizes = new Set();
      for (let i = 0; i < 14; i++) {
        const edit = i % 2 === 0;
        g = await step(`lighthouse at dusk, foggy ${'z'.repeat(edit ? i + 1 : i)}`);
        assert(!g['gv:fl:load'], `frame ${i} re-rooted`);
        const prev = g['gv:fl:prev']?.inputs, anc = g['gv:fl:anchor']?.inputs, store = g['gv:fl:store']?.inputs;
        assert(prev?.session === session && prev.frame === stored, `frame ${i} recalls ${JSON.stringify(prev)}, not frame ${stored}`);
        assert(anc?.frame === anchor, `frame ${i} anchors on ${anc?.frame}, not ${anchor}`);
        stored = store.frame;
        if (edit) anchor = stored;
        sizes.add(Object.keys(g).length);
      }
      assert(sizes.size === 1, `graph sizes ${[...sizes]}`);
      await settled(p);
      const sg = (await last()).graph;
      assert(sg['gv:fl:mix'] && sg['gv:refine'] && !sg['gv:fl:store'], 'the settle does not refine the recalled head');
      return `15 frames of ${[...sizes][0]} nodes, each recalling the one before, anchors on the newest edit; the settle refines the head`;
    } finally {
      await p.evaluate(() => flowOn && toggleFlow(false));
      await fetch(URL + 'fake/pack', { method: 'POST', body: JSON.stringify({ on: false }) });
      await choose(p, 'image_z_image_turbo');
    }
  });

  await check('model: a fine-tune is offered where the template uses its file and the endpoint has it; the swap lands in live and settle; snapshots keep it', async () => {
    await choose(p, 'image_flux3_klein_distilled_t2i');
    assert(await p.isHidden('#model'), 'a Model choice on a template with no fine-tune');
    await choose(p, 'image_krea2_turbo_t2i');
    const offered = await p.$$eval('#model option', (o) => o.map((x) => x.value));
    assert(await p.isVisible('#model') && offered.includes('Tune') && !offered.includes('Absent'), `offered ${JSON.stringify(offered)}`);
    await words(p, 'a lighthouse on a cliff');
    await settled(p);
    const unet = (g) => Object.values(g).filter((n) => n.class_type === 'UNETLoader').map((n) => n.inputs.unet_name).join();
    assert(unet((await last()).graph) === 'krea2_turbo_fp8_scaled.safetensors', `the template's own: ${unet((await last()).graph)}`);
    await p.selectOption('#model', 'Tune');
    await settled(p);
    const { graph, state } = await last();
    assert(unet(graph) === 'tune-krea.safetensors' && state.model === 'Tune', `with Tune: ${unet(graph)} ${state.model}`);
    await p.focus('#hudbtn'); await p.keyboard.press('s');
    await p.waitForFunction(() => snaps.at(-1).model === 'Tune' && !snaps.at(-1).auto);
    await p.selectOption('#model', '');
    await settled(p);
    assert(unet((await last()).graph) === 'krea2_turbo_fp8_scaled.safetensors', 'the template\'s own did not come back');
    return `offered ${offered.filter(Boolean).join(', ')} on Krea 2, none on FLUX 3; Tune → tune-krea.safetensors in live and settle; in the snapshot`;
  });

  await check('variations: the field renders 8 variations in one batch; a tile is (seed, index) and settles that picture', async () => {
    await choose(p, 'image_flux3_klein_distilled_t2i');
    await p.focus('#hudbtn'); await p.keyboard.press('g');
    await p.click('#fieldmode button[data-m="variations"]');
    await p.keyboard.press('Escape');
    await words(p, 'a lantern in the fog');
    await settled(p);
    const t0 = await p.evaluate(() => performance.now());
    await p.waitForFunction(() => field.length === 9 && field.slice(1).every((t) => t.index != null), null, { timeout: 15000 });
    const fill = Math.round((await p.evaluate(() => performance.now())) - t0);
    const vary = (await idle()).filter((j) => j.lane === 'vary').at(-1);
    assert(vary && Object.values(vary.graph).some((n) => n.class_type === 'RepeatLatentBatch' && n.inputs.amount === 8), 'no batched variations job');
    await p.focus('#hudbtn'); await p.keyboard.press('g');
    await p.click('#grid button[data-i="3"]');
    await settled(p);
    const settle = (await last()).graph, picked = await p.evaluate(() => variation);
    assert(settle['gv:pick']?.inputs.batch_index === 2 && picked.index === 2, `picked ${JSON.stringify(picked)} settle ${JSON.stringify(settle['gv:pick'])}`);
    await p.focus('#hudbtn'); await p.keyboard.press('g'); await p.click('#fieldmode button[data-m="seeds"]'); await p.keyboard.press('Escape');
    await settled(p);
    return `8 variations in ${fill} ms after the settle (one batch, strength ${vary.state.vary.strength}); tile 3 → (seed ${picked.seed}, index 2) settles batch_index 2`;
  });

  await check('speculation: when idle, the active knob\'s next positions render ahead, and moving there shows its picture at once', async () => {
    await choose(p, 'image_z_image_turbo');
    await words(p, 'a fern in the rain');
    await settled(p);
    const knob = await p.$('#tknobs input[type=range]');
    assert(knob, 'no knob on Z-Image');
    const at = +(await knob.inputValue()), step = +(await knob.getAttribute('step'));
    await knob.evaluate((el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); }, String(+(at + step).toFixed(6)));
    await settled(p);
    const t = Date.now();
    let spec = [];
    while (Date.now() - t < 15000 && spec.length < 2) { await p.waitForTimeout(250); spec = (await idle()).filter((j) => j.lane === 'spec'); }
    assert(spec.length >= 2, `speculative jobs ${spec.length}`);
    await p.waitForTimeout(1500);  // the last one finishes
    const ahead = +(at + 2 * step).toFixed(6);
    const t0 = await p.evaluate(() => performance.now());
    await knob.evaluate((el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); }, String(ahead));
    await p.waitForFunction((t0) => window.gvLog.some((f) => f.t > t0 && f.stage === 'live' && f.kind === 'final'), t0, { timeout: 5000 });
    const ms = Math.round((await p.evaluate((t0) => window.gvLog.find((f) => f.t > t0 && f.stage === 'live' && f.kind === 'final').t, t0)) - t0);
    await settled(p);
    return `${spec.length} positions rendered ahead; the next one showed ${ms} ms after the move (debounce 60 ms included)`;
  });

  await check('download: D saves the stage picture as the original PNG named by words, seed and rung, with the workflow inside; a selection as a zip with a manifest', async () => {
    await choose(p, 'image_flux3_klein_distilled_t2i');
    await words(p, 'A lighthouse, at dusk!');
    await settled(p);
    await p.focus('#hudbtn');
    const [got] = await Promise.all([p.waitForEvent('download', { timeout: 10000 }), p.keyboard.press('d')]);
    const name = got.suggestedFilename(), file = `${OUT}/${name}`; await got.saveAs(file);
    const seedNow = await p.evaluate(() => seed);
    assert(name === `a-lighthouse-at-dusk_seed${seedNow}_settled.png`, `file name ${name}`);
    const png = readFileSync(file), chunks = {};
    for (let at = 8; at < png.length;) {
      const len = png.readUInt32BE(at), type = png.toString('latin1', at + 4, at + 8), body = png.subarray(at + 8, at + 8 + len);
      if (type === 'iTXt' || type === 'tEXt') { const z = body.indexOf(0); const key = body.toString('latin1', 0, z);
        let text = body.subarray(z + 1); if (type === 'iTXt') { let i = 2; i = text.indexOf(0, i) + 1; i = text.indexOf(0, i) + 1; text = text.subarray(i); } chunks[key] = text.toString('utf8'); }
      at += 12 + len;
    }
    const graph = JSON.parse(chunks.prompt || '{}'), meta = JSON.parse(chunks['gpu-vision'] || '{}');
    // ComfyUI's loader (frontend scripts/app.ts isApiJson): every node an object with a class_type string and an inputs object
    assert(Object.keys(graph).length > 5 && Object.values(graph).every((n) => typeof n.class_type === 'string' && n.inputs && typeof n.inputs === 'object' && !Array.isArray(n.inputs)), 'prompt chunk is not an API graph');
    assert(Object.values(graph).some((n) => n.class_type === 'KSampler') && graph['gv:refine'], 'prompt chunk is not the settle graph');
    assert(meta.words === 'A lighthouse, at dusk!' && meta.seed === seedNow && meta.template === 'image_flux3_klein_distilled_t2i' && meta.rung === 'settled', `gpu-vision chunk ${chunks['gpu-vision']}`);
    // a seed tile downloads as its live frame, a PNG with the live graph
    await p.waitForFunction(() => field.length >= 3, null, { timeout: 15000 });
    await p.focus('#hudbtn'); await p.keyboard.press('g'); await p.focus('#grid button[data-i="2"]');
    const [tile] = await Promise.all([p.waitForEvent('download', { timeout: 10000 }), p.keyboard.press('d')]);
    await p.keyboard.press('Escape');
    const tileSeed = await p.evaluate(() => field[2].seed);
    assert(tile.suggestedFilename() === `a-lighthouse-at-dusk_seed${tileSeed}_live.png`, `tile file ${tile.suggestedFilename()}`);
    // shift-click two snapshots, download them as one zip with a manifest
    const ids = await p.evaluate(() => snaps.slice(-2).map((s) => s.id));
    for (const id of ids) await p.click(`.node[data-id="${id}"]`, { modifiers: ['Shift'] });
    assert(await p.isVisible('#dlsel'), 'no Download button for the selection');
    const [zip] = await Promise.all([p.waitForEvent('download', { timeout: 15000 }), p.click('#dlsel')]);
    const zfile = `${OUT}/${zip.suggestedFilename()}`; await zip.saveAs(zfile);
    const manifest = JSON.parse(execFileSync('unzip', ['-p', zfile, 'manifest.json']).toString());
    const listed = execFileSync('unzip', ['-Z1', zfile]).toString().trim().split('\n');
    assert(manifest.schema === 'gpu-vision.download' && manifest.files.length === 2 && manifest.files.every((f) => listed.includes(f.file) && f.words), `manifest ${JSON.stringify(manifest).slice(0, 300)}`);
    for (const id of ids) await p.click(`.node[data-id="${id}"]`, { modifiers: ['Shift'] });
    return `${name} (prompt: ${Object.keys(graph).length} nodes, gpu-vision: words, seed, template, rung); tile ${tile.suggestedFilename()}; ${zip.suggestedFilename()}: ${listed.join(', ')}`;
  });

  await check('size control: Z-Image chips set width × height in live and settle; match picture; LTX-2.5 and H3 show their own menu as chips', async () => {
    await choose(p, 'image_z_image_turbo');
    const chips = await p.$$eval('#size button', (b) => b.map((x) => x.textContent));
    assert(['1:1', '4:5', '3:4', '9:16', '16:9', '21:9', 'Draft', 'HD', 'Max'].every((c) => chips.includes(c)), `chips ${chips}`);
    await p.click('#settings');
    const raw = await p.$$eval('#controls [data-a]', (els) => els.map((e) => e.dataset.a));
    await p.keyboard.press('Escape');
    assert(!raw.includes('57.width') && !raw.includes('57.height'), 'raw width/height outside node plumbing');
    await p.click('#size button:text-is("16:9")');
    const wh = await p.evaluate(() => [values['57.width'], values['57.height']]);
    assert(wh[0] === 1376 && wh[1] === 768, `16:9 at 1 MP gave ${wh}`);
    await words(p, 'a lighthouse at dusk, wide');
    await settled(p);
    const settle = (await last()).graph, live = Object.values(settle).find((n) => n.class_type === 'EmptySD3LatentImage').inputs;
    assert(Math.abs(live.width / live.height - 16 / 9) < 0.08, `live latent ${live.width}x${live.height}`);
    assert(Math.abs(settle['gv:resize'].inputs.megapixels - 1376 * 768 / 1e6) < 0.01, `settle at ${settle['gv:resize'].inputs.megapixels} MP`);
    await p.click('#size button:text-is("Draft")');
    assert((await p.evaluate(() => values['57.width'] * values['57.height'])) < 300000, 'Draft did not quarter the area');
    await canvasPicture(p, 1280, 640);
    await p.click('#size button:text-is("match picture")');
    const matched = await p.evaluate(() => values['57.width'] / values['57.height']);
    assert(Math.abs(matched - 2) < 0.1, `match picture gave ${matched}`);
    await p.evaluate(() => { sources = []; values = {}; renderControls(); });
    for (const name of ['video_ltx2_5_t2v', 'video_minimax_h3_t2v']) {
      await choose(p, name);
      const own = await p.evaluate(() => profile.sizing.choices.length);
      const shown = await p.$$eval('#size button[data-aspect]', (b) => b.length);
      assert(shown === own && own >= 6, `${name}: ${shown} chips for ${own} choices`);
      assert(await p.$$eval('#size button[data-duration]', (b) => b.length) === 3, `${name}: no duration chips`);
    }
    await p.click('#size button[data-aspect^="9:16"]');
    await p.click('#size button[data-duration="10"]');
    const v = await p.evaluate(() => ({ ...values }));
    await words(p, 'a paper boat in the rain');
    await settled(p, 25000);
    const g = (await last()).graph;
    assert(g['115'].inputs.aspect_ratio.startsWith('9:16'), `H3 aspect in the settle ${g['115'].inputs.aspect_ratio}`);
    await shot(p, 'size-chips');
    return `Z-Image 16:9 → 1376×768 (live ${live.width}×${live.height}); match picture 2:1; H3 ${JSON.stringify(v)}`;
  });

  await check('clip chrome: the bar advances and loops, seeks by mouse and keys; sound toggle for a clip with sound, none without', async () => {
    await choose(p, 'video_fastvideo_fasth3_t2v');
    await words(p, 'a paper boat on a pond #audio');
    await settled(p, 25000);
    await p.waitForFunction(() => !vfront.classList.contains('back') && vfront.duration > 0 && !vfront.paused, null, { timeout: 8000 });
    await p.waitForSelector('#scrub:not([hidden])');
    const widths = await p.evaluate(async () => { const out = []; for (let i = 0; i < 30; i++) { out.push(parseFloat(document.getElementById('fill').style.width)); await new Promise((r) => setTimeout(r, 50)); } return out; });
    assert(widths.some((w, i) => i && w > widths[i - 1]), `the bar did not advance ${widths.slice(0, 8)}`);
    assert(widths.some((w, i) => i && w < widths[i - 1] - 30), 'the bar did not loop');
    const box = await p.$eval('#scrub', (e) => e.getBoundingClientRect().toJSON());
    await p.mouse.click(box.x + box.width * 0.75, box.y + box.height - 2);
    const at = await p.evaluate(() => vfront.currentTime / vfront.duration);
    assert(at > 0.6 && at < 0.95, `mouse seek to 75% gave ${at}`);
    await p.evaluate(() => { vfront.pause(); vfront.currentTime = 0.1; });
    await p.focus('#scrub'); await p.keyboard.press('ArrowRight');
    const stepped = await p.evaluate(() => vfront.currentTime);
    assert(Math.abs(stepped - 0.6) < 0.05, `→ stepped to ${stepped}`);
    await p.waitForTimeout(250);  // the bar thickens over 120 ms
    const tall = await p.$eval('#scrub .track', (e) => e.getBoundingClientRect().height);
    assert(tall >= 5, `focused bar ${tall} px`);
    await p.evaluate(() => vfront.play().catch(() => {}));
    await p.waitForSelector('#vol:not([hidden])', { timeout: 5000 });
    assert(await p.evaluate(() => vfront.muted), 'a clip started with sound');
    await p.click('#vol');
    assert(await p.evaluate(() => !vfront.muted && JSON.parse(localStorage.getItem('gv.sound')) === true), 'the toggle did not unmute or persist');
    await shot(p, 'clip-chrome');
    await p.click('#vol');
    await choose(p, 'video_ltx2_5_t2v');
    await words(p, 'a paper boat on a lake');
    await settled(p, 25000);
    await p.waitForFunction(() => vfront.currentTime > 0.4, null, { timeout: 8000 });
    assert(await p.isHidden('#vol'), 'a clip without sound shows the sound toggle');
    return `bar ${widths[0]}→…, seek 75% → ${at.toFixed(2)}, → ${stepped.toFixed(2)} s, toggle unmutes`;
  });

  await check('clip ladder: a full clip graduates to Smooth by the interpolation tool, as a child snapshot', async () => {
    await choose(p, 'video_fastvideo_fasth3_t2v');
    await words(p, 'a paper boat drifting');
    await settled(p, 25000);
    await p.waitForFunction(() => snaps.at(-1).media === 'video' && snaps.at(-1).job, null, { timeout: 8000 });
    const clip = await p.evaluate(() => head);
    assert((await p.textContent('#grad')).includes('Smooth'), `a clip graduates to ${await p.textContent('#grad')}`);
    assert(/motion can differ from the draft/.test(await statusText(p)), `the line does not say a full clip can move differently: ${await statusText(p)}`);
    await p.click('#grad');
    await p.waitForFunction((id) => snaps.at(-1).rung === 'smooth' && snaps.at(-1).parent === id, clip, { timeout: 25000 });
    const tool = (await rungs()).at(-1);
    assert(Object.values(tool.graph).some((n) => n.class_type === 'FrameInterpolate') && tool.takes.length === 1, 'Smooth did not run the interpolation tool on the clip');
    return `full clip → Smooth (${Object.values(tool.graph).find((n) => n.class_type === 'PrimitiveInt')?.inputs.value}×)`;
  });

  await check('clip early: its step previews show on the stage before the clip lands, and Cancel stops it until the next change', async () => {
    await choose(p, 'video_ltx2_5_t2v');
    await settled(p, 25000);
    await p.evaluate(() => { vfront.removeAttribute('src'); vfront.classList.add('back'); });  // no clip playing yet
    await clearStatus(p);
    await p.evaluate(() => { document.getElementById('prompt').value = 'a lighthouse beam sweeping fog #long'; typedAt = -1e9; send(); });  // one state, steps 0.5 s apart
    await p.waitForFunction(() => window.gvLog.some((f) => f.v === sentV && f.stage === 'live' && f.kind === 'frame'), null, { timeout: 15000, polling: 20 });
    await p.waitForFunction(() => front.src === document.getElementById('nextimg').src && !front.classList.contains('back'), null, { timeout: 400, polling: 20 }).catch(() => {});
    const early = await p.evaluate(() => ({ stage: front.src === document.getElementById('nextimg').src && !front.classList.contains('back'), cancel: !document.getElementById('cancel').hidden }));
    assert(early.stage && early.cancel, `early preview ${JSON.stringify(early)}`);
    await p.click('#cancel');
    await waitStatus(p, /^Canceled/, 5000);
    const t1 = await p.evaluate(() => performance.now());
    await p.waitForTimeout(2500);
    const after = await p.evaluate((t) => window.gvLog.filter((f) => f.t > t + 200 && f.stage === 'live').length, t1);
    assert(after === 0, `${after} live frames after Cancel`);
    await words(p, 'a lighthouse beam sweeping fog');
    await settled(p, 25000);
    return 'step 1 on the stage with Cancel; canceled: nothing more until the words changed, then the clip';
  });

  await check('clip timeline: FLF2V first and last frames fill and swap by keys; multiframe guides move; drag from the library', async () => {
    await choose(p, 'video_ltx2_5_flf2v');
    assert(await p.isVisible('#timeline') && await p.isHidden('#tray'), 'no timeline for a clip template');
    const left = await p.$$eval('#track .card', (c) => c.map((x) => x.style.left));
    assert(JSON.stringify(left) === '["0%","100%"]', `cards at ${left}`);
    for (const [i, file] of ['portrait.png', 'street.png'].entries()) {
      const [chooser] = await Promise.all([p.waitForEvent('filechooser'), p.click(`#track .card >> nth=${i}`)]);
      await chooser.setFiles(`${INPUTS}/${file}`);
      await p.waitForFunction((n) => sources.length === n, i + 1);
    }
    const before = await p.evaluate(() => Object.fromEntries(sources.map((s) => [s.slot, s.id])));
    await p.focus('#track .card[data-slot="31.image"]'); await p.keyboard.press('ArrowRight'); await p.waitForTimeout(100);
    const after = await p.evaluate(() => Object.fromEntries(sources.map((s) => [s.slot, s.id])));
    assert(after['31.image'] === before['39.image'] && after['39.image'] === before['31.image'], 'the frames did not swap');
    assert(await p.evaluate(() => document.activeElement.dataset.slot === '39.image'), 'focus did not follow the card');
    await words(p, 'the hand turns into light');
    await settled(p, 25000);
    const takes = (await last()).takes;
    assert(takes.some(([n, , id]) => n === '31' && id === before['39.image']) && takes.some(([n, , id]) => n === '39' && id === before['31.image']), `takes ${JSON.stringify(takes)}`);
    await p.focus('#track .card[data-slot="39.image"]'); await p.keyboard.press('Delete');
    assert(await p.evaluate(() => !sources.some((s) => s.slot === '39.image')), 'Delete did not remove');
    await choose(p, 'video_minimax_h3_multiframe_reference');
    const cards = await p.$$eval('#track .card', (c) => c.map((x) => [x.dataset.slot, x.style.left]));
    const refs = await p.$$eval('#refs .card .chip', (c) => c.map((x) => x.textContent));
    assert(cards.length === 3 && refs[0] === 'subject', `cards ${JSON.stringify(cards)} refs ${refs}`);
    const [chooser] = await Promise.all([p.waitForEvent('filechooser'), p.click('#track .card[data-slot="157.image"]')]);
    await chooser.setFiles(`${INPUTS}/portrait.png`);
    await p.waitForFunction(() => sources.some((s) => s.slot === '157.image'));
    await p.focus('#track .card[data-slot="157.image"]'); await p.keyboard.press('ArrowRight');
    assert((await p.evaluate(() => values['154.value'])) === 2, `the guide moved to ${await p.evaluate(() => values['154.value'])}`);
    const track = await p.$eval('#track', (e) => e.getBoundingClientRect().toJSON());
    await p.dragAndDrop('#track .card[data-slot="157.image"]', '#track', { targetPosition: { x: track.width * 0.8, y: 20 } });
    const dragged = await p.evaluate(() => values['154.value']);
    assert(dragged > 3.5 && dragged < 4.5, `dragged to ${dragged}`);
    // a library result dragged onto a card fills that slot and keeps its library id
    await p.click('#libbtn');
    await p.waitForSelector('#libgrid button', { timeout: 10000 });
    await p.dragAndDrop('#libgrid button >> nth=0', '#track .card[data-slot="159.image"]');
    await p.waitForFunction(() => sources.some((s) => s.slot === '159.image' && /^art_/.test(s.parent || '')), null, { timeout: 10000 });
    await p.keyboard.press('Escape');
    await shot(p, 'timeline');
    await p.evaluate(() => { sources = []; values = {}; renderControls(); });
    return `FLF2V swapped by →, Delete removed; multiframe guide 1.5 → 2 → ${dragged} s; library result in 159.image`;
  });

  await check('library: a result dragged from the drawer fills the slot it lands on, by role; a tree node and a seed tile drag onto slots too', async () => {
    await choose(p, 'image_qwen_image_2_1_image_edit');
    await p.click('#libbtn');
    await p.waitForSelector('#libgrid button', { timeout: 10000 });
    await p.dragAndDrop('#libgrid button >> nth=0', '#tray .pick[data-slot="475.image"]');
    await p.waitForFunction(() => sources.some((s) => s.slot === '475.image' && /^art_/.test(s.parent || '')), null, { timeout: 10000 });
    await p.keyboard.press('Escape');
    const node = await p.evaluate(() => snaps.find((s) => s.thumb)?.id);
    await p.dragAndDrop(`.node[data-id="${node}"]`, '#tray .pick[data-slot="470.image"]');
    await p.waitForFunction(() => sources.some((s) => s.slot === '470.image'), null, { timeout: 5000 });
    await words(p, 'she wears the jacket from the second picture');
    await settled(p);
    const { takes, state } = await last();
    const by = Object.fromEntries(state.sources.map((s) => [s.slot, s]));
    assert(takes.some(([n, , id]) => n === '475' && id === by['475.image'].id) && by['475.image'].parent.startsWith('art_'), 'the library picture did not fill 475');
    await p.evaluate(() => { sources = []; renderControls(); });
    return `475.image ← library ${by['475.image'].parent}; 470.image ← a snapshot`;
  });

  // Detection: the fixtures as an endpoint's own templates (no Saddle rows), in a page of their own.
  await fetch(`${URL}fake/standalone`, { method: 'POST', body: JSON.stringify({ on: true }) });
  const { context: dc, page: q } = await open();
  const option = (n) => q.$eval(`#template option[value="${n}"]`, (o) => ({ text: o.textContent, disabled: o.disabled })).catch(() => null);

  await check('detection: a template outside the tuned list stays out of the chooser until checked and Settings shows untuned ones, then runs live on the generic profile', async () => {
    await ready(q);
    const first = await option('image_fake_untuned_t2i'), tuned = await option('image_z_image_turbo');
    assert(!first, `listed while unchecked: ${JSON.stringify(first)}`);
    assert(tuned && !tuned.disabled && !/untuned|checking/.test(tuned.text), `a tuned template: ${JSON.stringify(tuned)}`);
    await q.waitForFunction(() => templates.length && !templates.some((t) => t.checking), null, { timeout: 15000 });
    assert(!(await option('image_fake_untuned_t2i')), 'an untuned template is listed by default');
    await q.evaluate(() => { const b = document.getElementById('showuntuned'); b.checked = true; b.dispatchEvent(new Event('change')); });
    const after = await option('image_fake_untuned_t2i');
    assert(after && !after.disabled && /· untuned$/.test(after.text), `after its check: ${JSON.stringify(after)}`);
    await choose(q, 'image_fake_untuned_t2i');
    assert(await q.evaluate(() => profile.tuned === false), 'the profile is not the generic one');
    await words(q, 'a blue boat on a still lake');
    await waitStatus(q, /^Live/);
    await settled(q);
    const latent = Object.values((await last()).graph).find((n) => n.class_type === 'EmptySD3LatentImage').inputs;
    const mp = latent.width * latent.height / 1e6;
    assert(mp < 0.13, `live at ${mp} MP, not the smallest bucket`);
    return `${after.text}; live at ${mp.toFixed(3)} MP`;
  });

  await check('detection: a template without a prompt or a seed slot, or a partner one, is not offered', async () => {
    await q.waitForFunction(() => templates.length && !templates.some((t) => t.checking), null, { timeout: 15000 });
    for (const n of ['image_fake_noseed_t2i', 'image_fake_noprompt_t2i', 'api_fake_partner_t2i']) assert(!(await option(n)), `${n} is offered`);
    assert(!q.errors.length, q.errors.join('; '));
    return `${await q.$$eval('#template option', (o) => o.filter((x) => / · untuned$/.test(x.textContent)).length)} untuned offered`;
  });
  await fetch(`${URL}fake/standalone`, { method: 'POST', body: JSON.stringify({ on: false }) });
  await dc.close();

  await check('no page errors', async () => { assert(!p.errors.length, p.errors.join('; ')); });
  await context.close();

  await check('375 px with a 40-snapshot tree: the tree scrolls in its lane, nothing else moves', async () => {
    const { context: c, page } = await open({ viewport: { width: 375, height: 812 } });
    await ready(page);
    assert(await page.isVisible('#import'), 'Import hidden on an empty tree');
    const snaps = Array.from({ length: 40 }, (_, i) => ({ id: `s${i}`, parent: i ? `s${i - (i % 7 === 0 ? 3 : 1)}` : null, auto: i % 3 > 0, t: i,
      template: 'image_z_image_turbo', prompt: `snapshot ${i}`, seed: i, values: {}, sources: [], strength: 0.6, aspect: 1, media: 'image', thumb: null }));
    const file = `${OUT}/tree-40.json`; writeFileSync(file, JSON.stringify({ schema: 'gpu-vision.snapshots', head: 's39', snaps }));
    await page.setInputFiles('#importfile', file);
    await page.waitForFunction(() => snaps.length === 40);
    const box = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, select: document.getElementById('template').getBoundingClientRect().toJSON(),
      wrap: document.getElementById('laneswrap').getBoundingClientRect().toJSON() }));
    await page.focus('.node[tabindex="0"]'); await page.keyboard.press('ArrowLeft');
    const moved = await page.evaluate(() => document.activeElement.dataset.id);
    await shot(page, 'mobile-375-tree40');
    await c.close();
    assert(box.scroll <= 375, `scrollWidth ${box.scroll}`);
    assert(box.select.left >= 0 && box.wrap.right <= 375, `select ${box.select.left}, lane ${box.wrap.right}`);
    assert(moved === 's38', `← moved focus to ${moved}`);
    return `scrollWidth ${box.scroll}; ← from the head focuses ${moved}`;
  });

  await check('375 px: no horizontal scroll, the bar wraps, the stage fits', async () => {
    const { context: c, page } = await open({ viewport: { width: 375, height: 812 } });
    await ready(page);
    await words(page, 'a red tram in the rain');
    await settled(page);
    const box = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, frame: document.getElementById('frame').getBoundingClientRect().toJSON(),
      prompt: document.getElementById('prompt').getBoundingClientRect().toJSON(), select: document.getElementById('template').getBoundingClientRect().toJSON() }));
    await shot(page, 'mobile-375');
    assert(box.scroll <= 375, `scrollWidth ${box.scroll}`);
    for (const [name, r] of Object.entries({ frame: box.frame, prompt: box.prompt, select: box.select })) assert(r.left >= 0 && r.right <= 375, `${name} off-screen ${r.left}..${r.right}`);
    await c.close();
    return `scrollWidth ${box.scroll}, frame ${Math.round(box.frame.width)} px`;
  });

  await check('375 px: 2 and 4 panels fit, the clip timeline fits and is keyboard-reachable', async () => {
    const { context: c, page } = await open({ viewport: { width: 375, height: 812 } });
    await ready(page);
    await choose(page, 'image_z_image_turbo');
    const fits = [];
    for (const n of [2, 4]) {
      await page.click(`#split button[data-n="${n}"]`);
      await words(page, `a red bicycle ${n}`);
      await page.waitForFunction((n) => panelSet?.srcs?.length === n, n, { timeout: 10000 });
      const box = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, frame: document.getElementById('frame').getBoundingClientRect().toJSON(),
        cells: [...document.querySelectorAll('.cell')].map((e) => e.getBoundingClientRect().toJSON()) }));
      await shot(page, `mobile-375-panels-${n}`);
      assert(box.scroll <= 375 && box.frame.right <= 375 && box.cells.every((r) => r.right <= box.frame.right + 1 && r.bottom <= box.frame.bottom + 1), `${n} panels: ${JSON.stringify(box).slice(0, 200)}`);
      fits.push(`${n}: ${box.cells.length} cells in ${Math.round(box.frame.width)}×${Math.round(box.frame.height)}`);
    }
    await page.click('#split button[data-n="1"]');
    await choose(page, 'video_minimax_h3_multiframe_reference');
    const strip = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, r: document.getElementById('timeline').getBoundingClientRect().toJSON() }));
    await page.click('#prompt');
    const seen = [];
    for (let i = 0; i < 45; i++) { await page.keyboard.press('Tab'); seen.push(await page.evaluate(() => document.activeElement.className)); }
    await shot(page, 'mobile-375-timeline');
    await c.close();
    assert(strip.scroll <= 375 && strip.r.right <= 375, `timeline ${JSON.stringify(strip)}`);
    assert(seen.some((s) => s.includes('card')), 'no timeline card reachable by Tab');
    return `${fits.join('; ')}; timeline ${Math.round(strip.r.width)} px, cards by Tab`;
  });

  await check('reduced motion: no cross-fades or blur', async () => {
    const { context: c, page } = await open({ reducedMotion: 'reduce' });
    await ready(page);
    const style = await page.evaluate(() => [getComputedStyle(document.getElementById('a')).transitionDuration, getComputedStyle(document.getElementById('glow')).display]);
    await c.close();
    assert(style[0].split(',').every((d) => parseFloat(d) === 0) && style[1] === 'none', JSON.stringify(style));
  });

  await check('framed: an opaque-origin sandbox works and exchanges bridge messages', async () => {
    const { context: c, page } = await open({}, 'host.html');
    const frame = page.frameLocator('#module');
    await frame.locator('#status').filter({ hasText: 'Live is off' }).waitFor({ timeout: 15000 });
    assert(await frame.locator('#template').isHidden(), 'the chooser shows inside Saddle');
    await frame.locator('#settings').click();  // Settings holds the module's own dials; Saddle owns the template's settings
    assert(await frame.locator('#speed').isVisible() && await frame.locator('#controls').isHidden(), 'the template\'s settings show inside Saddle');
    await frame.locator('#drawerclose').click();
    const portrait = 'data:image/png;base64,' + readFileSync(`${INPUTS}/portrait.png`).toString('base64');
    await page.evaluate(() => host.post('template', { name: 'image_flux3_klein_distilled_image_edit' }));
    await page.evaluate(() => host.post('live', { on: true }));
    await page.evaluate((src) => host.post('state', { state: { template: 'image_flux3_klein_distilled_image_edit', prompt: 'a yellow raincoat',
      values: {}, sources: [{ rel: 'library/portrait.png', src }] } }), portrait);
    await page.waitForFunction(() => host.got.some((m) => m.type === 'settled'), null, { timeout: 20000 });
    // a Saddle library result by its rel alone: the sidecar fetches it from Saddle; Keep carries it as the picture's parent
    const art = (await (await fetch(`${URL}library`)).json()).items[0];  // the sidecar's library page
    await page.evaluate((rel) => host.post('source', { rel }), art.rel);
    await page.waitForFunction((id) => host.got.some((m) => m.type === 'state' && m.state.sources.some((s) => s.parent === id)), art.id, { timeout: 15000 });
    await page.waitForFunction(() => host.got.filter((m) => m.type === 'settled').length >= 2, null, { timeout: 20000 });
    await page.evaluate(() => host.post('keep'));
    await page.waitForFunction(() => host.got.some((m) => m.type === 'keep'), null, { timeout: 5000 });
    const recipe = await page.evaluate(() => host.got.find((m) => m.type === 'keep').recipe);
    assert(recipe.sources.some((s) => s.parent === art.id && s.rel === art.rel) && recipe.rung && recipe.lineage?.length, `keep recipe ${JSON.stringify(recipe).slice(0, 300)}`);
    await page.evaluate(() => host.post('theme', { accent: '#2bd4bd' }));
    await page.evaluate(() => host.post('busy', { busy: true, ahead: 3 }));
    await frame.locator('#status').filter({ hasText: 'GPU shared · 3 ahead' }).waitFor({ timeout: 5000 });
    const got = await page.evaluate(() => host.got.map((m) => m.type));
    const state = await page.evaluate(() => host.got.filter((m) => m.type === 'state').map((m) => m.state).find((s) => s.sources[0]?.rel === 'library/portrait.png'));
    const settledSnap = await page.evaluate(() => host.got.find((m) => m.type === 'settled').snapshot);
    await shot(page, 'framed-host');
    await c.close();
    assert(state.prompt === 'a yellow raincoat' && state.sources[0]?.rel === 'library/portrait.png', `state ${JSON.stringify(state).slice(0, 200)}`);
    assert(settledSnap.sources[0]?.id && 'parent' in settledSnap, 'settled snapshot lacks sources or parent');
    return [...new Set(got)].join(', ');
  });

  await check('under a path prefix, as ComfyUI serves it: a first visit lands a picture, every request stays under the prefix, a same-origin frame is the page itself', async () => {
    const { context: c, page } = await open({}, 'vorpalfusion/');
    const origin = new globalThis.URL(URL).origin, asked = [];
    c.on('request', (r) => { const u = new globalThis.URL(r.url()); if (u.origin === origin) asked.push(u.pathname); });
    await page.reload();
    await ready(page);
    const starter = await page.inputValue('#prompt');
    assert(starter.length > 10, `no starter prompt on a first visit: "${starter}"`);
    await page.waitForFunction(() => window.gvLog.length, null, { timeout: 15000 });  // the starter renders with nothing typed
    await words(page, 'a red tram under a bridge');
    await settled(page);
    const outside = asked.filter((path) => !path.startsWith('/vorpalfusion/'));
    assert(asked.length > 3 && !outside.length, `requests outside the prefix: ${outside.join(', ')}`);
    await page.goto(URL + 'fake/last');  // a same-origin page holding the page in a frame, as ComfyUI's sidebar tab does
    await page.evaluate(() => { document.body.innerHTML = '<iframe id="side" src="/vorpalfusion/" style="width:420px;height:820px;border:0"></iframe>'; });
    const frame = page.frameLocator('#side');
    await frame.locator('#template option').first().waitFor({ state: 'attached', timeout: 15000 });
    assert(await frame.locator('#template').isVisible(), 'a same-origin frame took the page for a Saddle module (chooser hidden)');
    assert(!/Live is off/.test(await frame.locator('#status').textContent()), 'a same-origin frame waits for a host');
    await shot(page, 'prefix-sidebar');
    await c.close();
    return `${asked.length} requests under /vorpalfusion/; starter "${starter.slice(0, 40)}…"`;
  });
} else {
  // Words age: for each live picture painted while typing, how long ago the last key its words include was
  // pressed (debounce included). Drift: the mean difference (0-255) at 16x16 between the last live picture
  // shown and the settled one.
  var liveMeasures = (p, firstKey, lastKey) => p.evaluate(async ([firstKey, lastKey]) => {
    const sends = window.__sends, keys = window.__keys;
    const ages = window.gvLog.filter((f) => f.t >= firstKey && f.t <= lastKey + 2000 && f.kind === 'final' && f.stage === 'live').map((f) => {
      const i = sends.findIndex((s) => s.v === f.v); if (i < 0) return null;
      const key = keys.filter((k) => k <= sends[i].t).at(-1) ?? sends[i].t;  // the last key its words include
      return f.t - key;
    }).filter((a) => a !== null).sort((a, b) => a - b);
    const pct = (q) => ages.length ? Math.round(ages[Math.min(ages.length - 1, Math.floor(q * ages.length))]) : null;
    const lastLive = [...window.gvLog].reverse().find((f) => f.stage === 'live' && f.kind === 'final');
    const settleFinal = [...window.gvLog].reverse().find((f) => f.stage === 'settle' && f.kind === 'final');
    const srcAt = (t) => [...window.__srcs].reverse().find((s) => s.t <= t + 1)?.src;
    const small = (src) => new Promise((done) => { const i = new Image(); i.onload = () => { const c = Object.assign(document.createElement('canvas'), { width: 16, height: 16 });
      const x = c.getContext('2d'); x.drawImage(i, 0, 0, 16, 16); done(x.getImageData(0, 0, 16, 16).data); }; i.onerror = () => done(null); i.src = src; });
    let drift = null;
    if (lastLive && settleFinal && srcAt(lastLive.t) && srcAt(settleFinal.t)) {
      const [a, b] = await Promise.all([small(srcAt(lastLive.t)), small(srcAt(settleFinal.t))]);
      if (a && b) { let sum = 0, n = 0; for (let k = 0; k < a.length; k += 4) for (let c = 0; c < 3; c++) { sum += Math.abs(a[k + c] - b[k + c]); n++; } drift = +(sum / n).toFixed(1); }
    }
    return { words_age_p50_ms: pct(0.5), words_age_p90_ms: pct(0.9), live_jobs_sent: sends.filter((s) => s.t >= firstKey && s.t <= lastKey).length, settle_drift: drift };
  }, [firstKey, lastKey]);
  if (process.argv.includes('--soak')) {
    // Continuous typing for GV_SOAK_S seconds (default 180) on one template at ~9 characters a second: per
    // 10 s window the pictures shown and whether the status line changed while none came; retained heap
    // after forced GC, before and after.
    const { context, page: p } = await open();
    await ready(p);
    const name = process.env.GV_TEMPLATES || 'image_z_image_turbo';
    await check(`soak: ${name}`, async () => {
      await choose(p, name);
      await p.waitForFunction(() => !/^Loading/.test(document.getElementById('status').textContent), null, { timeout: 90000 });
      const cdp = await context.newCDPSession(p);
      const heap = async () => { await cdp.send('HeapProfiler.collectGarbage'); return +((await cdp.send('Runtime.getHeapUsage')).usedSize / 2 ** 20).toFixed(1); };
      const heap0 = await heap();
      const sentences = ['a koi pond under cherry blossoms, ink painting', 'a vintage camera on a wooden desk, soft light',
        'a lighthouse in a storm, huge waves', 'a cat wearing a tiny crown on a velvet cushion', 'a bowl of ramen with steam, top view',
        'an astronaut reading a newspaper on mars', 'an old tram crossing a bridge in the rain'];
      const t0 = await p.evaluate(() => performance.now()); const seconds = +(process.env.GV_SOAK_S || 180);
      for (let i = 0; (await p.evaluate(() => performance.now())) - t0 < seconds * 1000; i++) {
        await words(p, sentences[i % sentences.length] + (i >= sentences.length ? ` ${i}` : ''), 110);
      }
      const t1 = await p.evaluate(() => performance.now());
      const heap1 = await heap();
      // Every picture the stage showed (gvLog keeps only the last 500).
      const { log, status } = await p.evaluate(() => ({ log: window.__srcs.map((f) => ({ t: f.t, stage: 'live' })), status: window.__status }));
      const windows = [];
      for (let w = t0; w + 10000 <= t1; w += 10000) {
        const pics = log.filter((f) => f.t >= w && f.t < w + 10000 && f.stage === 'live').length;
        const said = status.filter((s) => s.t >= w && s.t < w + 10000).map((s) => s.text);
        windows.push({ pictures: pics, said: [...new Set(said)].slice(0, 3) });
      }
      const silent = windows.filter((x) => x.pictures === 0);
      const gaps = []; let prev = t0;
      for (const f of log.filter((f) => f.t >= t0 && f.t <= t1 && f.stage === 'live')) { gaps.push(f.t - prev); prev = f.t; }
      gaps.sort((a, b) => a - b);
      const m = { seconds, windows: windows.length, zero_frame_windows: silent.length, silent_windows_unexplained: silent.filter((x) => !x.said.length).length,
                  longest_gap_ms: Math.round(gaps.at(-1) || 0), gap_p95_ms: Math.round(gaps[Math.floor(0.95 * gaps.length)] || 0),
                  updates_per_s: +(log.filter((f) => f.t >= t0 && f.t <= t1).length / ((t1 - t0) / 1000)).toFixed(1), heap_mb: [heap0, heap1] };
      writeFileSync(`${OUT}/soak-${name}.json`, JSON.stringify({ ...m, windows_detail: windows }, null, 1));
      return JSON.stringify(m);
    });
    await check('no page errors', async () => { assert(!p.errors.length, p.errors.join('; ')); });
    await context.close();
    await browser.close();
    const failed = results.filter((r) => !r.ok);
    process.exit(failed.length ? 1 : 0);
  }
  // One end-to-end pass on the live endpoint, measured: key to first frame, frames per second while
  // typing (~10 characters a second), last key to settled; per template.
  const measures = {};
  const { context, page: p } = await open();
  await ready(p);
  for (const name of (process.env.GV_TEMPLATES || 'image_z_image_turbo,image_flux3_klein_distilled_t2i').split(',')) {
    await check(`live: ${name}`, async () => {
      await words(p, '');
      const chosen = await p.evaluate(() => performance.now());
      await choose(p, name);
      await p.waitForFunction(() => !/^Loading/.test(document.getElementById('status').textContent) && window.__status.some((s) => /^Loading/.test(s.text)), null, { timeout: 90000 });
      const loaded = await p.evaluate(() => performance.now());
      const text = 'a woman in a red coat walking through neon rain at night';
      await p.click('#prompt');
      const firstKey = await p.evaluate(() => performance.now());
      await p.keyboard.type(text, { delay: 100 });
      const lastKey = await p.evaluate(() => performance.now());
      await settled(p, 90000);
      const done = await p.evaluate(() => performance.now());
      const log = await p.evaluate(() => window.gvLog);
      const typingFrames = log.filter((f) => f.t >= firstKey && f.t <= lastKey && f.kind !== 'final');
      const firstFrame = log.find((f) => f.t > firstKey);
      const m = { load_ms: Math.round(loaded - chosen), key_to_first_frame_ms: Math.round(firstFrame.t - firstKey),
                  frames_per_s_typing: +(typingFrames.length / ((lastKey - firstKey) / 1000)).toFixed(1),
                  pictures_per_s_typing: +(log.filter((f) => f.t >= firstKey && f.t <= lastKey && f.kind === 'final').length / ((lastKey - firstKey) / 1000)).toFixed(2),
                  last_key_to_settled_ms: Math.round(done - lastKey),
                  last_key_to_settle_preview_ms: Math.round((log.filter((f) => f.stage === 'settle' && f.kind === 'frame').at(-1)?.t ?? done) - lastKey),
                  ...await liveMeasures(p, firstKey, lastKey), status: await statusText(p),
                  hud: await p.evaluate(() => `${keyToFrame} / ${lastJob}`) };
      measures[name] = m;
      await shot(p, `live-${name}`);
      return JSON.stringify(m);
    });
  }
  if (process.argv.includes('--ladder')) {
    // Through the page on the live endpoint: Graduate twice from the settle (Final, then 2K by the manifest's path), Keep;
    // then 4 panels, a pick, and its settle. Times from the click to the rung landing in the tree.
    await check('live ladder: settled → Final → 2K, Keep; 4 panels and a pick', async () => {
      const m = {};
      const climb = async (rung) => {
        const t = await p.evaluate(() => performance.now()); const parent = await p.evaluate(() => head);
        await p.click('#grad');
        await p.waitForFunction(([r, id]) => snaps.at(-1).rung === r && snaps.at(-1).parent === id, [rung, parent], { timeout: 300000 });
        m[`${rung}_ms`] = Math.round((await p.evaluate(() => performance.now())) - t);
        m[`${rung}_size`] = await p.evaluate(() => snaps.at(-1).size);
      };
      for (const rung of (process.env.GV_LADDER || 'final,2k').split(',')) await climb(rung);
      m.status_seen = [...new Set(await p.evaluate(() => window.__status.map((s) => s.text.replace(/\d+\/\d+/, 'k/n'))))].slice(-12);
      await shot(p, 'live-ladder-2k');
      await clearStatus(p); await p.click('#keep'); await waitStatus(p, /^Kept · /, 120000); m.kept = await statusText(p);
      await p.click('#split button[data-n="4"]');
      const t0 = await p.evaluate(() => performance.now());
      await words(p, 'a red bicycle against a blue wall, morning light');
      await p.waitForFunction(() => panelSet?.srcs?.length === 4 && panelSet.v === sentV, null, { timeout: 60000 });
      m.panels_after_last_key_ms = Math.round((await p.evaluate(() => performance.now())) - t0);
      await shot(p, 'live-panels-4');
      await p.focus('#hudbtn'); await p.keyboard.press('3');
      const t1 = await p.evaluate(() => performance.now());
      await settled(p, 60000);
      m.pick_to_settled_ms = Math.round((await p.evaluate(() => performance.now())) - t1);
      await p.click('#split button[data-n="1"]');
      measures.ladder = m;
      return JSON.stringify(m);
    });
  }
  if (process.argv.includes('--clips')) {
    // The clip ladder through the page on the live endpoint, per clip template: draft (live) → full clip (the settle) →
    // the next rung (Smooth by the interpolation tool, or Final where the template has an enhancer). Times from the
    // last key; drift between rungs is the mean difference (0-255, 16x16) of the clips' frames at 0.5 s; the status lines seen.
    const frameAt = (p, src) => p.evaluate(async (src) => {
      const v = Object.assign(document.createElement('video'), { muted: true, preload: 'auto', crossOrigin: 'anonymous', src });
      await new Promise((ok, no) => { v.onloadeddata = ok; v.onerror = no; });
      v.currentTime = Math.min(0.5, (v.duration || 1) / 2); await new Promise((ok) => { v.onseeked = ok; });
      const c = Object.assign(document.createElement('canvas'), { width: 16, height: 16 }); const x = c.getContext('2d'); x.drawImage(v, 0, 0, 16, 16);
      return [...x.getImageData(0, 0, 16, 16).data];
    }, src).catch(() => null);
    const diff = (a, b) => { if (!a || !b) return null; let s = 0, n = 0; for (let k = 0; k < a.length; k += 4) for (let c = 0; c < 3; c++) { s += Math.abs(a[k + c] - b[k + c]); n++; } return +(s / n).toFixed(1); };
    for (const name of (process.env.GV_CLIPS || 'video_fastvideo_fasth3_t2v,video_ltx2_5_t2v').split(',')) {
      await check(`live clips: ${name}`, async () => {
        await words(p, ''); await clearStatus(p);
        await choose(p, name);
        await p.waitForFunction(() => !/^Loading/.test(document.getElementById('status').textContent) && window.__status.some((s) => /^Loading/.test(s.text)), null, { timeout: 300000 });
        await p.click('#prompt');
        const t0 = await p.evaluate(() => performance.now());
        await p.keyboard.type('a red kite over a windy beach, waves rolling in', { delay: 100 });
        const lastKey = await p.evaluate(() => performance.now());
        const m = {};
        await p.waitForFunction((t) => window.gvLog.some((f) => f.t > t && f.stage === 'live' && f.kind === 'frame'), lastKey, { timeout: 300000 });
        m.first_preview_ms = Math.round((await p.evaluate((t) => window.gvLog.find((f) => f.t > t && f.stage === 'live' && f.kind === 'frame').t, lastKey)) - lastKey);
        m.cancel_shown = await p.isVisible('#cancel');
        await p.waitForFunction((t) => window.gvLog.some((f) => f.t > t && f.stage === 'live' && f.kind === 'final'), lastKey, { timeout: 300000 });
        m.draft_ms = Math.round((await p.evaluate((t) => window.gvLog.find((f) => f.t > t && f.stage === 'live' && f.kind === 'final').t, lastKey)) - lastKey);
        const draft = await p.evaluate(() => vfront.src);
        await settled(p, 600000);
        m.full_clip_ms = Math.round((await p.evaluate(() => performance.now())) - lastKey);
        const full = await p.evaluate(() => vfront.src);
        m.drift_draft_full = diff(await frameAt(p, draft), await frameAt(p, full));
        m.full_status = await statusText(p);
        const t1 = await p.evaluate(() => performance.now()); const parent = await p.evaluate(() => head);
        const rung = await p.evaluate(() => nextRung(snapOf(head)));
        await p.click('#grad');
        await p.waitForFunction(([r, id]) => snaps.at(-1).rung === r && snaps.at(-1).parent === id, [rung, parent], { timeout: 600000 });
        m[`${rung}_ms`] = Math.round((await p.evaluate(() => performance.now())) - t1);
        await p.waitForTimeout(1500);
        const top = await p.evaluate(() => frames[snaps.at(-1).id]);
        m[`drift_full_${rung}`] = diff(await frameAt(p, full), await frameAt(p, top));
        m.status_seen = [...new Set(await p.evaluate((t) => window.__status.filter((s) => s.t >= t).map((s) => s.text.replace(/\d+\/\d+/, 'k/n')), t0))];
        measures[`clips ${name}`] = m;
        await shot(p, `live-clip-${name}`);
        return JSON.stringify(m);
      });
    }
  }
  writeFileSync(`${OUT}/live-measures.json`, JSON.stringify(measures, null, 1));
  await check('no page errors', async () => { assert(!p.errors.length, p.errors.join('; ')); });
  await context.close();
}

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed; screenshots in ${OUT}`);
process.exit(failed.length ? 1 : 0);
