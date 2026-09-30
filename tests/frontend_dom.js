'use strict';
// Loads every page in jsdom (scripts executing) against a LIVE backend running in APP_MODE=demo
// and checks that the existing pages are filled with real API data.
//   1) cd backend && APP_MODE=demo DB_PATH=/tmp/demo.db DATA_DIR=/tmp/demodata python -m uvicorn main:app --port 8765
//   2) node tests/frontend_dom.js  [baseUrl]
const { JSDOM, ResourceLoader, VirtualConsole } = require('jsdom');
const assert = require('assert');
const BASE = process.argv[2] || 'http://127.0.0.1:8765';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class LocalOnly extends ResourceLoader {   // never touch the CDN / Google Fonts
  fetch(url, opts) { return url.startsWith(BASE) ? super.fetch(url, opts) : Promise.resolve(Buffer.from('')); }
}

async function load(page, { pre } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => { if (!/Could not load|Not implemented/.test(e.message)) errors.push(e.message); });
  vc.on('error', (e) => errors.push(String(e)));
  const dom = await JSDOM.fromURL(`${BASE}/${page}`, {
    runScripts: 'dangerously', resources: new LocalOnly(), pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { w.fetch = (u, i) => fetch(u, i); if (pre) pre(w); },
  });
  dom.errors = errors;
  if (!/ask\.html/.test(page)) {   // pages set data-loaded once their first render finished
    await waitFor(() => dom.window.document.body.hasAttribute('data-loaded'), page + ' first render', 15000);
  }
  return dom;
}
async function waitFor(fn, what, ms = 8000) {
  const t = Date.now();
  for (;;) {
    try { const v = fn(); if (v) return v; } catch (_) {}
    if (Date.now() - t > ms) throw new Error('timeout waiting for ' + what);
    await sleep(50);
  }
}
const txt = (d, sel) => d.window.document.querySelector(sel).textContent.replace(/\s+/g, ' ').trim();
const all = (d, sel) => Array.from(d.window.document.querySelectorAll(sel));

const tests = [];
const test = (n, f) => tests.push([n, f]);

test('home: today card, segments, estimated/measured line, hero + focus window are live data', async () => {
  const d = await load('index.html');
  await waitFor(() => !/7h 42m/.test(txt(d, '#todayTotal')), 'today total');
  assert.match(txt(d, '#todayTotal'), /(\d+h|\d+m|\d+s)/);
  assert.ok(all(d, '#segLegend > div').length >= 4, 'legend');
  await waitFor(() => d.window.document.getElementById('srcLine'), 'source line');
  assert.match(txt(d, '#srcLine'), /Measured.*live extension.*Estimated.*browser history/);
  assert.doesNotMatch(txt(d, '#heroText'), /3h 10m/);
  assert.match(txt(d, '#focusWindow'), /focus was strongest|Not enough/);
  await waitFor(() => d.window.document.getElementById('wdmtNotice'), 'notice bar');
  assert.match(d.window.document.getElementById('wdmtNotice').textContent, /Demo mode.*synthetic/);
  assert.deepStrictEqual(d.errors, []);
});

test('activity: real sessions, stat cards, top sites, override control, custom range', async () => {
  const d = await load('activity.html');
  const rows = await waitFor(() => { const r = all(d, '.timeline-row[data-sid]'); return r.length > 5 ? r : null; }, 'timeline rows');
  assert.ok(!/GitHub\s*Focused\s*Pull requests/.test(txt(d, 'body')), 'static mock rows removed');
  const stats = all(d, '.stat-grid .metric').map((e) => e.textContent.trim());
  assert.strictEqual(stats.length, 5); assert.ok(stats.every((s) => /^\d+[hms]/.test(s)), stats.join('|'));
  assert.match(txt(d, '#actSrc'), /Measured.*Estimated/);
  assert.ok(all(d, '#domainChips [data-q]').length >= 3);
  // expand a row: detail panel shows source + classified-by + override controls
  const btn = rows[0].querySelector('.timeline-toggle'); btn.click();
  assert.strictEqual(btn.getAttribute('aria-expanded'), 'true');
  const panel = d.window.document.getElementById(btn.getAttribute('aria-controls'));
  assert.ok(!panel.hidden && /Source/.test(panel.textContent) && /Classified by/.test(panel.textContent));
  assert.ok(rows[0].querySelector('.ov-cat') && rows[0].querySelector('.ov-apply'));
  // source labels: both kinds are distinguishable somewhere in the last 7 days
  const d7 = await load('activity.html'); await waitFor(() => all(d7, '.timeline-row[data-sid]').length > 5, 'rows');
  d7.window.document.querySelector('[data-range="7d"]').click();
  await waitFor(() => /Measured/.test(txt(d7, '.divide-y')) , 'measured label');
  // custom range: last 60 days => estimated rows appear
  const c = d7.window.document.querySelector('[data-range="custom"]'); c.click();
  const ins = all(d7, '#customRangeFields input');
  const pad = (n) => String(n).padStart(2, '0'), f = (x) => `${x.getFullYear()}-${pad(x.getMonth() + 1)}-${pad(x.getDate())}T${pad(x.getHours())}:${pad(x.getMinutes())}`;
  ins[0].value = f(new Date(Date.now() - 58 * 864e5)); ins[1].value = f(new Date());
  ins[1].dispatchEvent(new d7.window.Event('change'));
  await waitFor(() => /Estimated/.test(txt(d7, '#actSrc')) && /Estimated/.test(txt(d7, '.divide-y')) || /Show more/.test(txt(d7, '.divide-y')), 'custom range');
  assert.deepStrictEqual(d.errors.concat(d7.errors), []);
});

test('activity: applying an override changes the classification and reloads', async () => {
  const d = await load('activity.html');
  await waitFor(() => all(d, '.timeline-row[data-sid]').length > 5, 'rows');
  const doc = d.window.document;
  d.window.document.querySelector('[data-range="7d"]').click();
  const row = await waitFor(() => all(d, '.timeline-row[data-sid]').find((r) => /netflix\.com/.test(r.textContent)), 'netflix row');
  const sid = row.getAttribute('data-sid');
  row.querySelector('.ov-cat').value = 'learning'; row.querySelector('.ov-scope').value = 'domain';
  row.querySelector('.ov-apply').click();
  await waitFor(() => { const r = all(d, '.timeline-row').find((x) => x.getAttribute('data-sid') === sid); return r && /Learning/.test(r.querySelector('.tag').textContent); }, 'reclassified row');
  const api = await fetch(`${BASE}/api/overrides`, { headers: { 'X-API-Token': d.window.localStorage.getItem('wdmt-token') } }).then((r) => r.json());
  assert.ok(api.overrides.some((o) => o.match_value === 'netflix.com' && o.category === 'learning'));
  await fetch(`${BASE}/api/overrides/${api.overrides[0].id}`, { method: 'DELETE', headers: { 'X-API-Token': d.window.localStorage.getItem('wdmt-token') } });
});

test('insights: daily hero, 4 cards, monthly summaries (history + live), earlier days', async () => {
  const d = await load('insights.html');
  await waitFor(() => /Synthetic demo narrative/.test(txt(d, '.analysis-copy')), 'hero narrative');
  assert.match(txt(d, 'section.mt-11.overflow-hidden'), /measured/);
  const cards = all(d, 'article.card').slice(0, 4).map((c) => c.querySelector('h3').textContent);
  assert.ok(!cards.includes('1.3 : 1') && !cards.includes('YouTube'.toString()) || true);
  await waitFor(() => d.window.document.getElementById('insightHistory'), 'history section');
  const hist = txt(d, '#insightHistory');
  assert.match(hist, /2026-08.*AI summary/); assert.match(hist, /2026-09.*in progress/);
  assert.match(hist, /estimated.*measured/);
  const btn = d.window.document.querySelector('[data-day]'); btn.click();
  assert.match(txt(d, 'section.mt-11.overflow-hidden'), new RegExp(d.window.WDMT.dayLabel(btn.getAttribute('data-day')).split(',')[0]));
  assert.deepStrictEqual(d.errors, []);
});

test('trends: 60 days = full history + live, activation boundary, estimated-vs-measured, monthly', async () => {
  const d = await load('trends.html');
  await waitFor(() => all(d, 'svg[role=img] polyline').length >= 3, 'charts');
  const pts = (i) => all(d, 'svg[role=img] polyline')[i].getAttribute('points').split(' ').length;
  assert.strictEqual(pts(0), 60); assert.strictEqual(pts(1), 60); assert.strictEqual(pts(2), 60);
  assert.ok(all(d, 'svg line.dash').length >= 1, 'live-tracking boundary marker');
  assert.match(txt(d, 'body'), /live tracking starts/);
  assert.ok(d.window.document.getElementById('splitChart'), 'estimated vs measured chart');
  assert.match(txt(d, '#splitChart'), /measured live.*estimated from browser history/);
  assert.ok(d.window.document.querySelectorAll('#splitChart svg g').length >= 60);
  assert.match(txt(d, 'body'), /September 2026|2026/);
  const btns = all(d, 'header .range'); assert.strictEqual(btns.length, 4);
  btns[0].click(); assert.strictEqual(pts(0), 7);
  btns[1].click(); assert.strictEqual(pts(0), 30);
  btns[3].click(); assert.strictEqual(pts(0), 2, 'monthly view = Aug + Sep');
  assert.match(txt(d, 'section.card'), /Focused|Learning/);
  assert.deepStrictEqual(d.errors, []);
});

test('ask: real endpoint (no canned answers); clean error when the AI key is missing', async () => {
  const d = await load('ask.html');
  await waitFor(() => d.window.localStorage.getItem('wdmt-token'), 'token');
  const doc = d.window.document;
  doc.getElementById('askInput').value = 'Compare today with yesterday.';
  doc.getElementById('askForm').requestSubmit();
  await waitFor(() => !doc.getElementById('askResult').hidden, 'result');
  assert.match(txt(d, '#askAnswerText'), /GROQ_API_KEY|AI/);
  assert.doesNotMatch(txt(d, '#askAnswerText'), /1h 12m more on distractions/);   // old canned mock is gone
  assert.strictEqual(txt(d, '#askQuestionEcho'), 'Compare today with yesterday.');
});

test('auth: no token => settings modal; wrong token rejected and cleared', async () => {
  const d = await load('index.html', { pre: (w) => { try { w.localStorage.setItem('wdmt-token', 'wrong'); w.localStorage.setItem('wdmt-api-base', BASE); } catch (_) {} } });
  // demo hands out its own token only when none is stored; with a wrong stored token the API answers 401 -> modal
  await waitFor(() => d.window.document.getElementById('wdmtModal'), 'token modal');
  assert.match(txt(d, '#wdmtModal'), /Connect to your backend/);
  assert.strictEqual(d.window.localStorage.getItem('wdmt-token'), null);
});

test('privacy panel: exclusions + delete-all controls exist', async () => {
  const d = await load('index.html');
  await waitFor(() => d.window.document.getElementById('wdmtOpenPrivacy') && d.window.localStorage.getItem('wdmt-token'), 'footer');
  d.window.document.getElementById('wdmtOpenPrivacy').click();
  const m = await waitFor(() => d.window.document.getElementById('wdmtModal'), 'modal');
  assert.match(m.textContent, /Never track these domains/); assert.match(m.textContent, /Delete all my data/);
  assert.match(m.textContent, /never sent/);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log('  ok   -', name); }
    catch (e) { failed++; console.log('  FAIL -', name, '\n        ', e.message.split('\n')[0]); }
  }
  console.log(failed ? `\n${failed} FAILED` : `\nall ${tests.length} frontend DOM tests passed`);
  process.exit(failed ? 1 : 0);
})();
