'use strict';
// Simulation harness: runs background.js in a vm with a mocked chrome API and a controllable clock.
// Run:  node extension/tests/sim.js
const vm = require('vm');
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8');
const sleep = (ms = 25) => new Promise((r) => setTimeout(r, ms));
const clone = (o) => JSON.parse(JSON.stringify(o));

function makeWorld() {
  const world = {
    storage: {}, clock: Date.parse('2026-09-29T09:00:00Z'), alarms: {},
    browser: { idle: 'active', focused: true, tab: { id: 1, url: 'https://github.com/a/b?tab=readme#x', title: 'Repo', incognito: false, audible: false } },
    server: { down: false, dropResponse: false, received: [], unique: new Map(), duplicates: 0, auth: true },
    listeners: {},
  };
  world.boot = () => {           // a fresh service-worker process sharing persisted storage
    const L = {};
    const ev = (name) => ({ addListener: (fn) => { (L[name] = L[name] || []).push(fn); } });
    const chrome = {
      storage: {
        local: {
          get: async (keys) => {
            if (keys == null) return clone(world.storage);
            const ks = Array.isArray(keys) ? keys : [keys];
            const out = {}; ks.forEach((k) => { if (k in world.storage) out[k] = clone(world.storage[k]); });
            return out;
          },
          set: async (obj) => {
            const changes = {}; Object.keys(obj).forEach((k) => { changes[k] = { newValue: clone(obj[k]) }; world.storage[k] = clone(obj[k]); });
            (L['storage.onChanged'] || []).forEach((fn) => fn(changes, 'local'));
          },
        },
        onChanged: ev('storage.onChanged'),
      },
      alarms: {
        create: (name, info) => { world.alarms[name] = info; },
        get: async (name) => world.alarms[name],
        onAlarm: ev('alarms.onAlarm'),
      },
      idle: { setDetectionInterval() {}, queryState: async () => world.browser.idle, onStateChanged: ev('idle.onStateChanged') },
      windows: { getLastFocused: async () => ({ id: 1, focused: world.browser.focused }), onFocusChanged: ev('windows.onFocusChanged') },
      tabs: {
        query: async () => (world.browser.tab ? [world.browser.tab] : []),
        onActivated: ev('tabs.onActivated'), onUpdated: ev('tabs.onUpdated'), onRemoved: ev('tabs.onRemoved'),
      },
      runtime: { onInstalled: ev('runtime.onInstalled'), onStartup: ev('runtime.onStartup'), onMessage: ev('runtime.onMessage') },
    };
    class FakeDate extends Date {
      constructor(...a) { if (a.length === 0) super(world.clock); else super(...a); }
      static now() { return world.clock; }
    }
    const fetch = async (url, opts) => {
      if (world.server.down) throw new TypeError('Failed to fetch');
      if (!world.server.auth) return { ok: false, status: 401, json: async () => ({}) };
      const body = JSON.parse(opts.body);
      assert.strictEqual(opts.headers['X-API-Token'], 'tok');
      body.activities.forEach((a) => {
        world.server.received.push(a);
        if (world.server.unique.has(a.id)) world.server.duplicates++; else world.server.unique.set(a.id, a);
      });
      if (world.server.dropResponse) throw new TypeError('response lost');
      return { ok: true, status: 200, json: async () => ({ acked: body.activities.map((a) => a.id) }) };
    };
    const ctx = vm.createContext({
      chrome, fetch, Date: FakeDate, console, setTimeout, clearTimeout, URL, Promise, JSON, Math, Set, Map, Intl,
      AbortController, Error, TypeError, String, Array, Object,
      crypto: { randomUUID: () => require('crypto').randomUUID() },
    });
    vm.runInContext(SRC, ctx);
    world.listeners = L;
    return L;
  };
  world.fire = async (name, ...args) => { (world.listeners[name] || []).forEach((fn) => fn(...args)); await sleep(); };
  world.tick = async (ms, heartbeat = true) => {
    world.clock += ms;
    if (heartbeat) await world.fire('alarms.onAlarm', { name: 'wdmt-heartbeat' });
  };
  world.run = async (ms) => { for (let t = 0; t < ms; t += 30000) await world.tick(Math.min(30000, ms - t)); };  // realistic heartbeats
  world.configure = async (extra = {}) => {
    world.storage.cfg = { baseUrl: 'http://localhost:8000', token: 'tok', activated: true, activationTs: 'x', excluded: [], ...extra };
  };
  world.queue = () => (world.storage.queue || []);
  world.open = () => world.storage.open || null;
  return world;
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('starts a session on the active tab, strips query/fragment, heartbeats without duplicating', async () => {
  const w = makeWorld(); await w.configure(); w.boot(); await sleep();
  assert.ok(w.open(), 'open session created');
  assert.strictEqual(w.open().url, 'https://github.com/a/b');
  const id = w.open().id;
  await w.tick(30000); await w.tick(30000);
  assert.strictEqual(w.open().id, id); assert.strictEqual(w.queue().length, 0);
  assert.ok(w.open().lastBeat > w.open().start);
});

test('tab switch closes the previous session with the right duration and uploads it', async () => {
  const w = makeWorld(); await w.configure(); w.boot(); await sleep();
  await w.tick(60000, false);
  w.browser.tab = { id: 2, url: 'https://stackoverflow.com/questions/1', title: 'Q', incognito: false };
  await w.fire('tabs.onActivated', { tabId: 2 });
  await sleep(80);
  assert.strictEqual(w.server.unique.size, 1);
  const a = [...w.server.unique.values()][0];
  assert.strictEqual(a.url, 'https://github.com/a/b');
  assert.strictEqual((Date.parse(a.end) - Date.parse(a.start)) / 1000, 60);
  assert.strictEqual(w.open().url, 'https://stackoverflow.com/questions/1');
});

test('URL change and title change each start a new session; count-prefix in title is ignored', async () => {
  const w = makeWorld(); await w.configure(); w.boot(); await sleep();
  await w.tick(10000, false); w.browser.tab.url = 'https://github.com/a/c'; await w.fire('tabs.onUpdated', 1, { url: 'x' });
  await w.tick(10000, false); w.browser.tab.title = 'New title'; await w.fire('tabs.onUpdated', 1, { title: 'x' });
  await w.tick(10000, false); w.browser.tab.title = '(3) New title'; await w.fire('tabs.onUpdated', 1, { title: 'x' }); // same after normalization
  await w.tick(10000, false); w.browser.focused = false; await w.fire('windows.onFocusChanged'); await sleep(80);
  assert.strictEqual(new Set([...w.server.unique.keys(), ...w.queue().map((q) => q.id)]).size, 3);
});

test('window focus loss and idle stop tracking; idle end is backdated; audible tab survives idle', async () => {
  const w = makeWorld(); await w.configure(); w.boot(); await sleep();
  await w.tick(20000, false); w.browser.focused = false; await w.fire('windows.onFocusChanged'); await sleep(60);
  assert.strictEqual(w.open(), null);
  w.browser.focused = true; await w.fire('windows.onFocusChanged'); assert.ok(w.open());
  await w.run(400000); w.browser.idle = 'idle'; await w.fire('idle.onStateChanged', 'idle'); await sleep(60);
  assert.strictEqual(w.open(), null);
  const last = [...w.server.unique.values()].pop();
  const secs = (Date.parse(last.end) - Date.parse(last.start)) / 1000;
  assert.strictEqual(secs, 400 - 180, 'idle time (180s) removed from the session');
  // audible tab keeps counting while idle
  w.browser.tab.audible = true; await w.fire('idle.onStateChanged', 'idle'); assert.ok(w.open());
});

test('worker killed mid-session and revived after a long gap: dead gap NOT counted', async () => {
  const w = makeWorld(); await w.configure(); w.boot(); await sleep();
  await w.tick(30000); const beat = w.open().lastBeat; const start = w.open().start;
  w.server.down = true;                  // keep it in the queue so we can inspect it
  w.clock += 45 * 60 * 1000;             // laptop slept / worker dead for 45 min, no heartbeats
  w.boot(); await sleep(80);             // brand-new worker process, same storage
  const q = w.queue();
  assert.strictEqual(q.length, 1);
  assert.strictEqual(Date.parse(q[0].end), beat, 'closed at the last heartbeat, not now');
  assert.strictEqual(Date.parse(q[0].end) - start, 30000);
  assert.ok(w.open() && w.open().start >= beat + 45 * 60 * 1000 - 1, 'a fresh session starts now');
});

test('worker revived after a SHORT gap keeps the same open session', async () => {
  const w = makeWorld(); await w.configure(); w.boot(); await sleep();
  const id = w.open().id; w.clock += 20000; w.boot(); await sleep(60);
  assert.strictEqual(w.open().id, id);
});

test('backend unavailable: queue persists, retries with backoff, nothing lost or duplicated', async () => {
  const w = makeWorld(); await w.configure(); w.boot(); await sleep();
  w.server.down = true;
  for (let i = 0; i < 3; i++) {
    await w.tick(15000, false);
    w.browser.tab = { id: 10 + i, url: `https://site${i}.com/p`, title: 'T' + i, incognito: false };
    await w.fire('tabs.onActivated'); await sleep(40);
  }
  assert.strictEqual(w.queue().length, 3);
  assert.ok(w.storage.flushState.fails >= 1 && w.storage.flushState.nextAt > w.clock);
  // Alarm flush respects backoff (no request while waiting)...
  const before = w.server.received.length; await w.fire('alarms.onAlarm', { name: 'wdmt-flush' });
  assert.strictEqual(w.server.received.length, before);
  // ...backend comes back; a forced flush drains everything exactly once.
  w.server.down = false; await w.fire('runtime.onMessage', { type: 'flush' }, {}, () => {}); await sleep(80);
  assert.strictEqual(w.queue().length, 0); assert.strictEqual(w.server.unique.size, 3);
});

test('lost response => resend => server sees the same ids again (idempotent upload key)', async () => {
  const w = makeWorld(); await w.configure(); w.boot(); await sleep();
  await w.tick(15000, false); w.server.dropResponse = true;
  w.browser.tab = { id: 3, url: 'https://x.io/', title: 'X', incognito: false };
  await w.fire('tabs.onActivated'); await sleep(80);
  assert.strictEqual(w.queue().length, 1, 'not removed from queue without an ack');
  w.server.dropResponse = false; await w.fire('runtime.onMessage', { type: 'flush' }, {}, () => {}); await sleep(80);
  assert.strictEqual(w.queue().length, 0); assert.strictEqual(w.server.unique.size, 1);
  assert.strictEqual(w.server.duplicates, 1, 'server received the same client id twice -> it can dedupe');
});

test('excluded domains, incognito and non-http pages are never tracked', async () => {
  const w = makeWorld(); await w.configure({ excluded: ['bank.com'] });
  w.browser.tab = { id: 1, url: 'https://secure.bank.com/acct', title: 'x', incognito: false }; w.boot(); await sleep();
  assert.strictEqual(w.open(), null);
  w.browser.tab = { id: 1, url: 'https://ok.com/', title: 'x', incognito: true }; await w.fire('tabs.onActivated'); assert.strictEqual(w.open(), null);
  w.browser.tab = { id: 1, url: 'chrome://settings', title: 'x', incognito: false }; await w.fire('tabs.onActivated'); assert.strictEqual(w.open(), null);
});

test('not activated => nothing tracked', async () => {
  const w = makeWorld(); await w.configure({ activated: false }); w.boot(); await sleep();
  assert.strictEqual(w.open(), null); assert.strictEqual(w.queue().length, 0);
});

test('sub-2s flicker sessions are dropped; 10s sessions are kept', async () => {
  const w = makeWorld(); w.server.down = true; await w.configure(); w.boot(); await sleep();
  await w.tick(1000, false); w.browser.tab = { id: 2, url: 'https://a.io/', title: 'a', incognito: false }; await w.fire('tabs.onActivated');
  await w.tick(10000, false); w.browser.tab = { id: 3, url: 'https://b.io/', title: 'b', incognito: false }; await w.fire('tabs.onActivated');
  assert.deepStrictEqual(w.queue().map((q) => new URL(q.url).host), ['a.io']);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log('  ok   -', name); }
    catch (e) { failed++; console.log('  FAIL -', name, '\n        ', e.message); }
  }
  console.log(failed ? `\n${failed} FAILED` : `\nall ${tests.length} extension simulations passed`);
  process.exit(failed ? 1 : 0);
})();
