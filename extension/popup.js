'use strict';
const DEFAULT_CFG = { baseUrl: 'http://localhost:8000', token: '', activated: false, activationTs: null, excluded: [] };
const $ = (id) => document.getElementById(id);
let cfg = { ...DEFAULT_CFG };

async function loadCfg() {
  const { cfg: stored } = await chrome.storage.local.get('cfg');
  cfg = { ...DEFAULT_CFG, ...(stored || {}) };
}
async function saveCfg() { await chrome.storage.local.set({ cfg }); }

async function api(path, opts = {}) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 8000);
  try {
    const res = await fetch(cfg.baseUrl.replace(/\/+$/, '') + path, {
      ...opts, signal: ctl.signal,
      headers: { 'Content-Type': 'application/json', 'X-API-Token': cfg.token },
    });
    if (res.status === 401) throw new Error('Token rejected - check backend/data/api_token.txt');
    if (!res.ok) throw new Error('Backend returned HTTP ' + res.status);
    return await res.json();
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('Backend not responding (is the app running?)');
    if (e instanceof TypeError) throw new Error('Cannot reach the backend (is the app running?)');
    throw e;
  } finally { clearTimeout(t); }
}

function setConn(ok, text) {
  $('connDot').className = 'dot ' + (ok ? 'ok' : 'bad');
  $('conn').textContent = text;
}

async function refresh() {
  $('error').textContent = '';
  $('open').href = cfg.baseUrl;
  const { open, queue = [], flushState } = await chrome.storage.local.get(['open', 'queue', 'flushState']);
  $('track').textContent = open ? 'Tracking now: ' + open.domain : (cfg.activated ? 'Tracking paused (idle / other window)' : '');
  $('queue').textContent = queue.length ? `${queue.length} session(s) waiting to upload` : '';
  if (flushState && flushState.lastError) $('error').textContent = 'Last upload issue: ' + flushState.lastError;
  if (!cfg.token) { setConn(false, 'Enter your API token'); $('activate').disabled = true; return; }
  try {
    const c = await api('/api/extension/config');
    setConn(true, 'Connected to local backend' + (c.app_mode === 'demo' ? ' (DEMO)' : ''));
    $('activate').disabled = false;
    if (c.activated) {
      cfg.activated = true; cfg.activationTs = c.activation_ts;
      $('activate').textContent = 'Tracking active'; $('activate').disabled = true;
      $('act').textContent = 'Live tracking since ' + new Date(c.activation_ts).toLocaleString();
      if (!$('excluded').value && c.excluded_domains) $('excluded').value = c.excluded_domains.join(', ');
      await saveCfg();
    } else {
      $('act').textContent = c.bootstrap_completed ? 'History imported. Ready to activate.' : 'Open the website once to import your history, then activate.';
    }
  } catch (e) { setConn(false, 'Not connected'); $('error').textContent = e.message; $('activate').disabled = true; }
}

$('save').addEventListener('click', async () => {
  cfg.baseUrl = $('baseUrl').value.trim() || DEFAULT_CFG.baseUrl;
  cfg.token = $('token').value.trim();
  await saveCfg();
  await refresh();
});

$('activate').addEventListener('click', async () => {
  try {
    const r = await api('/api/extension/activate', {
      method: 'POST', body: JSON.stringify({ timezone: Intl.DateTimeFormat().resolvedOptions().timeZone }),
    });
    cfg.activated = true; cfg.activationTs = r.activation_ts;
    await saveCfg();
    chrome.runtime.sendMessage({ type: 'refresh' });
    await refresh();
  } catch (e) { $('error').textContent = e.message; }
});

$('saveExcluded').addEventListener('click', async () => {
  const domains = $('excluded').value.split(/[,\n]/).map((s) => s.trim().toLowerCase().replace(/^www\./, '')).filter(Boolean);
  cfg.excluded = domains;
  await saveCfg();                                   // the tracker applies this locally...
  try { await api('/api/extension/excluded-domains', { method: 'POST', body: JSON.stringify({ domains }) }); }  // ...and the backend drops them too
  catch (e) { $('error').textContent = e.message; }
});

$('flush').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ type: 'flush' });
  await refresh();
});

(async () => {
  await loadCfg();
  $('baseUrl').value = cfg.baseUrl;
  $('token').value = cfg.token;
  $('excluded').value = (cfg.excluded || []).join(', ');
  await refresh();
})();
