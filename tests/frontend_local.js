'use strict';
// LOCAL-mode flow: nothing is imported until the user picks a profile; only that profile's history appears.
const { JSDOM, ResourceLoader, VirtualConsole } = require('jsdom');
const assert = require('assert');
const BASE = process.argv[2], TOKEN = process.env.WDMT_TOKEN;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
class LocalOnly extends ResourceLoader { fetch(u, o) { return u.startsWith(BASE) ? super.fetch(u, o) : Promise.resolve(Buffer.from('')); } }
const api = (p, o = {}) => fetch(BASE + p, { ...o, headers: { 'X-API-Token': TOKEN, 'Content-Type': 'application/json' } }).then((r) => r.json());
async function waitFor(fn, what, ms = 10000) {
  const t = Date.now();
  for (;;) { try { const v = fn(); if (v) return v; } catch (_) {} if (Date.now() - t > ms) throw new Error('timeout: ' + what); await sleep(50); }
}
async function open(page) {
  const vc = new VirtualConsole(); const errors = [];
  vc.on('jsdomError', (e) => { if (!/Could not load|Not implemented/.test(e.message)) errors.push(e.message); });
  const dom = await JSDOM.fromURL(`${BASE}/${page}`, { runScripts: 'dangerously', resources: new LocalOnly(), pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { w.fetch = (u, i) => fetch(u, i); w.localStorage.setItem('wdmt-token', TOKEN); } });
  dom.errors = errors; return dom;
}
const txt = (d, s) => d.window.document.querySelector(s).textContent.replace(/\s+/g, ' ').trim();

(async () => {
  let failed = 0;
  const t = async (name, fn) => { try { await fn(); console.log('  ok   -', name); } catch (e) { failed++; console.log('  FAIL -', name, '\n        ', e.message.split('\n')[0]); } };
  let dom;

  await t('first run: profile chooser appears and NOTHING is imported yet', async () => {
    dom = await open('index.html');
    const m = await waitFor(() => dom.window.document.getElementById('wdmtChooser'), 'chooser');
    assert.match(m.textContent, /Which Chrome profile should I import/);
    assert.match(m.textContent, /Work · work@example\.com/); assert.match(m.textContent, /Personal · me@example\.com/);
    assert.match(m.textContent, /Profile 2/);
    assert.strictEqual(m.querySelectorAll('input[type=radio]').length, 2);
    assert.strictEqual(m.querySelectorAll('input[type=radio]:checked').length, 0, 'no profile pre-selected');
    await sleep(600);
    const st = await api('/api/bootstrap/status');
    assert.ok(!st.running && st.status !== 'running' && st.status !== 'completed', JSON.stringify(st));
    assert.strictEqual((await api('/api/dashboard?range=all')).totals.total, 0);
    assert.strictEqual((await api('/api/status')).history_source, null);
  });

  await t('clicking import without choosing shows a hint and still imports nothing', async () => {
    dom.window.document.getElementById('wdmtImport').click();
    assert.match(txt(dom, '#wdmtPickMsg'), /Select a profile first/);
    assert.strictEqual((await api('/api/bootstrap/status')).status === 'running', false);
  });

  await t('choosing "Work" imports ONLY that profile and closes the chooser', async () => {
    const doc = dom.window.document;
    doc.querySelector('#wdmtChooser input[type=radio][value*="Profile 2"]').checked = true;
    doc.getElementById('wdmtImport').click();
    await waitFor(() => !doc.getElementById('wdmtChooser'), 'chooser closes');
    await waitFor(async () => false, 'noop', 1).catch(() => {});
    let st; for (let i = 0; i < 100; i++) { st = await api('/api/bootstrap/status'); if (st.completed || st.status === 'failed') break; await sleep(100); }
    assert.strictEqual(st.status, 'completed', JSON.stringify(st));
    const s = await api('/api/status');
    assert.match(s.history_source.label, /Work \(work@example\.com\)/);
    const a = await api('/api/activity?range=all&limit=500');
    const domains = new Set(a.sessions.map((x) => x.domain));
    assert.ok(domains.has('github.com') && !domains.has('default-only.io'), [...domains].join(','));
    assert.ok(a.sessions.every((x) => x.source === 'history_estimated'));
  });

  await t('after import: choice is remembered (no chooser on reload) and activation hint is shown', async () => {
    dom.window.close();
    const d2 = await open('index.html');
    await waitFor(() => d2.window.document.getElementById('wdmtNotice') && /Activate tracking/.test(d2.window.document.getElementById('wdmtNotice').textContent), 'activation hint');
    assert.ok(!d2.window.document.getElementById('wdmtChooser'));
    assert.match(txt(d2, '#wdmtNotice'), /estimated/);
    assert.deepStrictEqual(d2.errors, []);
    d2.window.close();
  });

  console.log(failed ? `\n${failed} FAILED` : '\nall local-mode chooser tests passed');
  process.exit(failed ? 1 : 0);
})();
