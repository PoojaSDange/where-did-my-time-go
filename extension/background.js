'use strict';
/*
 * Where Did My Time Go? - live tracker (Manifest V3 service worker, vanilla JS).
 *
 * MV3 rules honoured here:
 *  - The worker can be killed after ~30s idle, so NOTHING important lives only in memory.
 *    The open session is persisted in chrome.storage.local on every state change ("open").
 *  - chrome.alarms drive the heartbeat (bumps open.lastBeat) and the upload flush.
 *  - On every wake-up, if the gap since the last heartbeat is large the session is closed AT THE
 *    LAST HEARTBEAT: the dead gap is never counted as activity.
 *  - The upload queue is in chrome.storage.local; uploads use client-generated ids, so retrying
 *    is idempotent on the server (no duplicates).
 *  - All event listeners are registered synchronously at top level.
 *  - The extension never waits for any LLM: it only talks to the local backend.
 */

const DEFAULT_CFG = { baseUrl: 'http://localhost:8000', token: '', activated: false, activationTs: null, excluded: [] };
const HEARTBEAT_ALARM = 'wdmt-heartbeat';
const FLUSH_ALARM = 'wdmt-flush';
const MAX_RESTORE_GAP_MS = 120000;   // larger gap since last heartbeat => worker/PC was asleep/dead
const MIN_SESSION_MS = 2000;         // sub-2s flickers (tab cycling, loading titles) are noise
const IDLE_SECONDS = 180;            // no input for 3 min => idle (audible tabs still count)
const MAX_BATCH = 50;
const FETCH_TIMEOUT_MS = 10000;

chrome.idle.setDetectionInterval(IDLE_SECONDS);

// ---------------------------------------------------------------- serialization helpers
// `serial` orders all open-session mutations; `qserial` orders short queue read-modify-writes.
let chain = Promise.resolve();
function serial(fn) {
  const run = chain.then(() => fn());
  chain = run.catch((e) => console.error('[wdmt]', e));
  return run;
}
let qchain = Promise.resolve();
function qserial(fn) {
  const run = qchain.then(() => fn());
  qchain = run.catch((e) => console.error('[wdmt:queue]', e));
  return run;
}

const getLocal = (keys) => chrome.storage.local.get(keys);
const setLocal = (obj) => chrome.storage.local.set(obj);

// ---------------------------------------------------------------- URL / title helpers
function cleanUrl(raw) {
  try {
    const u = new URL(raw);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    const domain = u.hostname.toLowerCase().replace(/^www\./, '');
    if (!domain) return null;
    let path = u.pathname.replace(/\/{2,}/g, '/');
    if (path.length > 1 && path.endsWith('/')) path = path.slice(0, -1);
    return { url: `${u.protocol}//${domain}${path}`, domain };   // query string + fragment dropped
  } catch (_) { return null; }
}
function normTitle(t) {
  return String(t || '').replace(/\s+/g, ' ').trim().replace(/^[(\[]\d{1,4}\+?[)\]]\s*/, '').slice(0, 300);
}
function isExcluded(domain, excluded) {
  return (excluded || []).some((e) => e && (domain === e || domain.endsWith('.' + e)));
}
function sameTarget(open, t) { return open.url === t.url && open.title === t.title; }

// ---------------------------------------------------------------- what should be tracked right now?
async function currentTarget(cfg) {
  const idleState = await chrome.idle.queryState(IDLE_SECONDS);
  let win = null;
  try { win = await chrome.windows.getLastFocused({ populate: false }); } catch (_) { /* no window */ }
  if (!win || !win.focused) return { stop: 'blur' };                       // window focus lost
  const tabs = await chrome.tabs.query({ active: true, windowId: win.id });
  const tab = tabs[0];
  if (!tab || !tab.url || tab.incognito) return { stop: 'none' };          // never track incognito
  if (idleState !== 'active' && !tab.audible) return { stop: idleState };  // idle/locked (unless playing audio)
  const c = cleanUrl(tab.url);
  if (!c || isExcluded(c.domain, cfg.excluded)) return { stop: 'ignored' };
  return { url: c.url, domain: c.domain, title: normTitle(tab.title), tabId: tab.id };
}

// ---------------------------------------------------------------- open-session lifecycle
async function closeOpen(open, endMs) {
  const end = Math.max(open.start, endMs);
  await qserial(async () => {
    const { queue = [] } = await getLocal('queue');
    if (end - open.start >= MIN_SESSION_MS) {
      queue.push({
        id: open.id, start: new Date(open.start).toISOString(), end: new Date(end).toISOString(),
        url: open.url, title: open.title,
      });
    }
    await setLocal({ queue, open: null });         // one atomic write: queue gets it, open cleared
  });
  flushSoon();
}

async function evaluate() {
  const st = await getLocal(['cfg', 'open']);
  const cfg = { ...DEFAULT_CFG, ...(st.cfg || {}) };
  let open = st.open || null;
  const now = Date.now();

  // 1) dead-gap protection (worker was killed / PC slept): close at the last heartbeat.
  if (open && now - open.lastBeat > MAX_RESTORE_GAP_MS) {
    await closeOpen(open, open.lastBeat);
    open = null;
  }

  // 2) not activated / no token => track nothing.
  if (!cfg.activated || !cfg.token) {
    if (open) await closeOpen(open, now);
    return;
  }

  // 3) what is the user doing right now?
  const target = await currentTarget(cfg);
  if (target.stop) {
    if (open) {
      // "idle" is detected after the fact: backdate the end by the idle threshold.
      const end = target.stop === 'idle' ? Math.max(open.start, now - IDLE_SECONDS * 1000) : now;
      await closeOpen(open, end);
    }
    return;
  }

  // 4) same page & title => heartbeat; otherwise close and start a new session.
  if (open && sameTarget(open, target)) {
    open.lastBeat = now;
    await setLocal({ open });
    return;
  }
  if (open) await closeOpen(open, now);
  await setLocal({
    open: { id: crypto.randomUUID(), url: target.url, title: target.title, domain: target.domain,
            tabId: target.tabId, start: now, lastBeat: now },
  });
}

// ---------------------------------------------------------------- upload queue
let flushing = false;
function flushSoon() { flush(false).catch((e) => console.error('[wdmt:flush]', e)); }

async function flush(force) {
  if (flushing) return;
  flushing = true;
  try {
    for (let round = 0; round < 20; round++) {
      const st = await getLocal(['cfg', 'queue', 'flushState']);
      const cfg = { ...DEFAULT_CFG, ...(st.cfg || {}) };
      const queue = st.queue || [];
      const fs = st.flushState || { fails: 0, nextAt: 0 };
      if (!queue.length || !cfg.token || !cfg.activated) return;
      if (!force && Date.now() < fs.nextAt) return;

      const batch = queue.slice(0, MAX_BATCH);
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
      try {
        const res = await fetch(`${cfg.baseUrl}/api/extension/activities`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-API-Token': cfg.token },
          body: JSON.stringify({ activities: batch, tz: Intl.DateTimeFormat().resolvedOptions().timeZone }),
          signal: ctl.signal,
        });
        clearTimeout(timer);
        if (res.status === 401 || res.status === 403) throw new Error(`auth rejected (HTTP ${res.status})`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const acked = new Set(data.acked || batch.map((b) => b.id));
        await qserial(async () => {
          const cur = (await getLocal('queue')).queue || [];   // re-read: new items may have arrived
          await setLocal({ queue: cur.filter((a) => !acked.has(a.id)),
                           flushState: { fails: 0, nextAt: 0, lastOk: Date.now(), lastError: null } });
        });
        if (queue.length <= MAX_BATCH) return;
      } catch (e) {
        clearTimeout(timer);
        const fails = (fs.fails || 0) + 1;                       // backend down / app not running
        const delay = Math.min(300000, 5000 * 2 ** Math.min(fails, 6));
        await setLocal({ flushState: { fails, nextAt: Date.now() + delay, lastOk: fs.lastOk || null,
                                       lastError: String((e && e.message) || e) } });
        return;
      }
    }
  } finally {
    flushing = false;
  }
}

// ---------------------------------------------------------------- alarms
async function ensureAlarms() {
  if (!(await chrome.alarms.get(HEARTBEAT_ALARM))) chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: 0.5 });
  if (!(await chrome.alarms.get(FLUSH_ALARM))) chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 1 });
}

async function init(browserStartup) {
  await ensureAlarms();
  if (browserStartup) {
    // A session left over from the previous browser run ends at its last heartbeat.
    const { open } = await getLocal('open');
    if (open) await closeOpen(open, open.lastBeat);
  }
  await evaluate();
  flushSoon();
}

// ---------------------------------------------------------------- listeners (top level, synchronous)
chrome.runtime.onInstalled.addListener(() => { serial(() => init(false)); });
chrome.runtime.onStartup.addListener(() => { serial(() => init(true)); });
chrome.tabs.onActivated.addListener(() => { serial(evaluate); });
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.url || info.title || info.status === 'complete' || info.audible !== undefined) serial(evaluate);
});
chrome.tabs.onRemoved.addListener(() => { serial(evaluate); });
chrome.windows.onFocusChanged.addListener(() => { serial(evaluate); });
chrome.idle.onStateChanged.addListener(() => { serial(evaluate); });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === HEARTBEAT_ALARM) serial(evaluate);
  else if (alarm.name === FLUSH_ALARM) flushSoon();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.cfg) serial(evaluate);       // popup activated/changed settings
});
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === 'flush') {
    flush(true).then(() => sendResponse({ ok: true })).catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (msg && msg.type === 'refresh') {
    serial(evaluate).then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

// Runs on every worker boot (including after being killed): make sure alarms exist and state is sane.
serial(async () => { await ensureAlarms(); await evaluate(); });
