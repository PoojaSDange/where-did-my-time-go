/* ==========================================================================
   Where Did My Time Go? - API layer (no framework).
   - API base URL is configurable (default: the origin serving this page, else http://localhost:8000)
   - every call sends the shared API token (entered once, kept in this browser's localStorage)
   - shows first-run import progress, and the privacy / data controls
   ========================================================================== */
(function () {
  "use strict";
  var LS_TOKEN = "wdmt-token", LS_BASE = "wdmt-api-base";
  var W = (window.WDMT = { mode: "local", config: null });

  function ls(k, v) {
    try {
      if (v === undefined) return localStorage.getItem(k);
      if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v);
    } catch (e) {}
    return null;
  }

  W.base = function () {
    var b = ls(LS_BASE) || (location.protocol.indexOf("http") === 0 ? location.origin : "http://localhost:8000");
    return b.replace(/\/+$/, "");
  };
  W.token = function () { return ls(LS_TOKEN) || ""; };

  /* ------------------------------------------------------------ helpers */
  W.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  W.fmt = function (sec) {               // 7h 42m / 42m / 25s
    sec = Math.round(sec || 0);
    if (sec < 60) return sec + "s";
    var m = Math.floor(sec / 60);
    if (m < 60) return m + "m";
    var h = Math.floor(m / 60), mm = m % 60;
    return mm ? h + "h " + (mm < 10 ? "0" : "") + mm + "m" : h + "h";
  };
  W.hour12 = function (h) {
    var ap = h >= 12 ? "PM" : "AM", x = h % 12 === 0 ? 12 : h % 12;
    return x + ":00 " + ap;
  };
  W.CAT = {
    focused_work: "Focused work", learning: "Learning", research: "Research", communication: "Communication",
    social_media: "Social media", entertainment: "Entertainment", shopping: "Shopping", news: "News",
    productivity: "Productivity", personal: "Personal", creative: "Creative", break: "Break", ambiguous: "Ambiguous",
  };
  W.GROUPS = {
    focus: { label: "Focused", tag: "focus", color: "#b8df69" },
    learn: { label: "Learning", tag: "learn", color: "#c7baff" },
    break: { label: "Break", tag: "break", color: "#ffcb91" },
    distract: { label: "Distraction", tag: "distract", color: "#f0b6c3" },
    other: { label: "Other", tag: "", color: "#dcded4" },
    unclassified: { label: "Unclassified", tag: "", color: "#c4c6bc" },
  };
  W.groupOf = function (s) {             // mirrors backend models.ui_group_for
    if (s.status !== "classified") return "unclassified";
    if (s.is_wasted) return "distract";
    if (["focused_work", "productivity", "creative"].indexOf(s.category) >= 0) return "focus";
    if (["learning", "research"].indexOf(s.category) >= 0) return "learn";
    if (s.category === "break") return "break";
    return "other";
  };
  W.shortDate = function (iso, tz) {
    try {
      return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: tz });
    } catch (e) { return iso; }
  };
  W.dayLabel = function (day) {          // 'YYYY-MM-DD' -> 'Sep 24, 2026'
    return new Date(day + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  };
  W.$ = function (sel, root) { return (root || document).querySelector(sel); };
  W.$$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  /* ------------------------------------------------------------ requests */
  function detailOf(body, status) {
    if (body && typeof body.detail === "string") return body.detail;
    if (body && Array.isArray(body.detail) && body.detail[0]) return body.detail[0].msg || "Invalid request";
    return "Request failed (HTTP " + status + ")";
  }

  W.api = function (path, opts) {
    opts = opts || {};
    var headers = { "X-API-Token": W.token() };
    var init = { method: opts.method || "GET", headers: headers };
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(W.base() + path, init).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (res.status === 401) {
          ls(LS_TOKEN, null);
          W.openSettings(true);
          throw new Error("API token missing or invalid");
        }
        if (!res.ok) throw new Error(detailOf(body, res.status));
        return body;
      });
    }, function () {
      throw new Error("Cannot reach the local backend at " + W.base() + ". Is it running?");
    });
  };

  /* ------------------------------------------------------------ boot */
  W.ready = fetch(W.base() + "/api/public/config")
    .then(function (r) { return r.json(); })
    .then(function (cfg) {
      W.config = cfg;
      W.mode = cfg.app_mode || "local";
      if (cfg.demo_token && !W.token()) ls(LS_TOKEN, cfg.demo_token);   // demo data is synthetic
      if (!W.token()) W.openSettings(true);
      return cfg;
    })
    .catch(function () {
      W.config = { app_mode: "local" };
      if (!W.token()) W.openSettings(true);
      return W.config;
    });

  /* ------------------------------------------------------------ notice bar (first-run import etc.) */
  var bar = null;
  function notice(html, kind) {
    var main = document.querySelector("main");
    if (!main) return;
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "wdmtNotice";
      bar.setAttribute("aria-live", "polite");
      bar.className = "mb-8 flex flex-wrap items-center gap-3 rounded-2xl bg-[var(--faint)] px-5 py-3 text-sm";
      main.insertBefore(bar, main.firstChild);
    }
    if (!html) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.innerHTML = '<span class="h-2 w-2 shrink-0 rounded-full ' + (kind === "warn" ? "" : "pulse ") +
      'bg-[var(--lime-deep)]" style="' + (kind === "warn" ? "background:#d9506c" : "") + '"></span><span>' + html + "</span>";
  }
  W.notice = notice;

  var STAGE = { read: "Reading your browser history", classify: "Classifying pages", daily: "Building daily summaries",
                monthly: "Writing monthly summaries" };

  function pollBootstrap() {
    W.api("/api/bootstrap/status").then(function (b) {
      if (b.completed) {
        notice(null);
        document.dispatchEvent(new CustomEvent("wdmt:refresh"));
        return W.api("/api/status").then(showActivationHint);
      }
      if (b.status === "failed") {
        notice("History import failed: " + W.esc(b.error || "unknown error") +
          ' <button type="button" id="wdmtRetry" class="underline">Retry</button> · ' +
          '<button type="button" id="wdmtPick" class="underline">Choose a different profile</button>', "warn");
        var r = document.getElementById("wdmtRetry");
        if (r) r.onclick = function () { startImport(); };
        var pk = document.getElementById("wdmtPick");
        if (pk) pk.onclick = function () { W.chooseProfile(); };
        return;
      }
      var pct = b.total ? " (" + Math.min(100, Math.round((100 * (b.done || 0)) / b.total)) + "%)" : "";
      notice("Importing your last two months of history: " + W.esc(STAGE[b.stage] || "starting") + pct + "…");
      setTimeout(pollBootstrap, 2000);
    }).catch(function () { setTimeout(pollBootstrap, 4000); });
  }

  function showActivationHint(st) {
    if (st.app_mode === "demo") {
      notice("Demo mode: every number here is <b>synthetic</b> - no real browsing data. " +
        "The real product is local-first and runs on your own PC.");
      return;
    }
    if (!st.activated) {
      notice("History imported. To start <b>live measurement</b>, open the extension popup and click " +
        "<b>Activate tracking</b>. Until then all time shown is <b>estimated</b> from your browser history.");
    } else {
      notice(null);
    }
  }

  function startImport(profileId) {
    return W.api("/api/bootstrap/start", { method: "POST", body: profileId ? { profile_id: profileId } : {} })
      .then(function (r) {
        if (r.needs_profile) return W.chooseProfile();
        pollBootstrap();
      });
  }

  /* First run: NOTHING is imported until the user explicitly picks a browser profile. */
  W.chooseProfile = function () {
    if (document.getElementById("wdmtChooser")) return;
    W.api("/api/history/profiles").then(function (r) {
      var m = document.createElement("div");
      m.id = "wdmtChooser";
      m.setAttribute("role", "dialog");
      m.setAttribute("aria-modal", "true");
      m.style.cssText = "position:fixed;inset:0;z-index:100;display:grid;place-items:center;background:rgba(20,21,19,.55);padding:16px;overflow:auto";
      var rows = r.profiles.map(function (p, i) {
        var when = new Date(p.last_used * 1000).toLocaleString();
        var mb = Math.max(0.1, p.size_bytes / 1048576).toFixed(1);
        var who = W.esc(p.name) + (p.email ? " · " + W.esc(p.email) : "");
        return '<label class="flex cursor-pointer items-start gap-3 rounded-xl border border-[var(--line)] p-4">' +
          '<input type="radio" name="wdmtProfile" value="' + W.esc(p.id) + '" class="mt-1"' + (r.history_source && r.history_source.id === p.id ? " checked" : "") + ">" +
          '<span class="text-sm"><b>' + who + "</b><br><span class=\"text-xs text-[var(--muted)]\">" +
          W.esc(p.browser) + " · folder “" + W.esc(p.profile_dir) + "” · history last updated " + W.esc(when) + " · " + mb + " MB</span></span></label>";
      }).join("");
      var body = r.profiles.length
        ? '<div class="mt-5 grid gap-3" id="wdmtProfileList">' + rows + "</div>" +
          '<p id="wdmtPickMsg" class="mt-3 text-xs text-[var(--muted)]"></p>' +
          '<div class="mt-4 flex gap-3"><button type="button" id="wdmtImport" class="btn primary">Import this profile</button>' +
          '<button type="button" id="wdmtLater" class="btn">Not now</button></div>'
        : '<p class="mt-5 text-sm leading-6">No browser profiles were found on this computer.</p>' +
          '<p class="mt-2 text-xs leading-5 text-[var(--muted)]">Open <code>chrome://version</code>, copy <b>Profile Path</b>, add <code>\\History</code> and set it as <code>CHROME_HISTORY_PATH</code> in <code>backend/.env</code>, then restart the backend.</p>' +
          '<div class="mt-4"><button type="button" id="wdmtLater" class="btn">Close</button></div>';
      m.innerHTML = '<div class="card p-7" style="width:min(600px,100%);max-height:92vh;overflow:auto;background:var(--paper)">' +
        '<p class="display text-[26px]">Which Chrome profile should I import?</p>' +
        '<p class="mt-3 text-sm leading-6 text-[var(--muted)]">Pick the profile whose browsing you want analysed (the one you use for work/study). ' +
        "I read a <b>private copy</b> of that profile's history file for the last ~2 months; your browser is never modified and nothing is imported until you click. " +
        "See <i>Privacy &amp; data</i> for exactly what the AI receives.</p>" + body + "</div>";
      document.body.appendChild(m);
      var later = document.getElementById("wdmtLater");
      if (later) later.onclick = function () {
        m.remove();
        notice('No profile chosen yet, so nothing has been imported. <button type="button" id="wdmtChooseAgain" class="underline">Choose a profile</button>');
        var again = document.getElementById("wdmtChooseAgain");
        if (again) again.onclick = function () { W.chooseProfile(); };
      };
      var go = document.getElementById("wdmtImport");
      if (go) go.onclick = function () {
        var sel = m.querySelector('input[name="wdmtProfile"]:checked');
        if (!sel) { document.getElementById("wdmtPickMsg").textContent = "Select a profile first."; return; }
        go.disabled = true;
        startImport(sel.value).then(function () { m.remove(); }).catch(function (e) {
          go.disabled = false;
          document.getElementById("wdmtPickMsg").textContent = e.message;
        });
      };
    }).catch(function (e) { notice(W.esc(e.message), "warn"); });
  };

  W.ready.then(function () {
    if (!W.token()) return;
    W.api("/api/status").then(function (st) {
      if (st.app_mode === "demo") return showActivationHint(st);
      if (!st.bootstrap.completed) {
        // a remembered choice (or an explicit CHROME_HISTORY_PATH) resumes; otherwise the user must choose
        if (st.history_source || st.history_forced) return startImport();
        return W.chooseProfile();
      }
      showActivationHint(st);
    }).catch(function (e) { notice(W.esc(e.message), "warn"); });
  });

  /* ------------------------------------------------------------ footer + settings / privacy modal */
  function footer() {
    var main = document.querySelector("main");
    if (!main || document.getElementById("wdmtFooter")) return;
    var p = document.createElement("p");
    p.id = "wdmtFooter";
    p.className = "mt-14 border-t border-[var(--line)] pt-6 text-xs text-[var(--muted)]";
    p.innerHTML = 'Local-first: your browsing data stays on this computer. ' +
      '<button type="button" id="wdmtOpenPrivacy" class="underline underline-offset-4">Privacy &amp; data</button>';
    main.appendChild(p);
    document.getElementById("wdmtOpenPrivacy").onclick = function () { W.openSettings(false); };
  }
  document.addEventListener("DOMContentLoaded", footer);
  if (document.readyState !== "loading") footer();

  W.openSettings = function (needToken) {
    if (document.getElementById("wdmtModal")) return;
    var m = document.createElement("div");
    m.id = "wdmtModal";
    m.setAttribute("role", "dialog");
    m.setAttribute("aria-modal", "true");
    m.style.cssText = "position:fixed;inset:0;z-index:100;display:grid;place-items:center;background:rgba(20,21,19,.55);padding:16px;overflow:auto";
    var demo = W.mode === "demo";
    m.innerHTML =
      '<div class="card p-7" style="width:min(560px,100%);max-height:92vh;overflow:auto;background:var(--paper)">' +
      '<div class="flex items-start justify-between"><p class="display text-[26px]">' + (needToken ? "Connect to your backend" : "Privacy &amp; data") + "</p>" +
      (needToken ? "" : '<button type="button" id="wdmtClose" class="text-xl leading-none" aria-label="Close">&times;</button>') + "</div>" +
      '<p class="mt-3 text-sm leading-6 text-[var(--muted)]">' +
      (needToken ? "Paste the API token printed in the terminal on first run (also saved in <code>backend/data/api_token.txt</code>). It is stored only in this browser." :
        "Everything is stored in a local SQLite file. Classification sends only compact, redacted data to Gemini (history import) and Groq (live/analysis/Ask): domain, a path shape, and a redacted title. Query strings, full URLs and sensitive-domain titles are never sent.") + "</p>" +
      '<label class="mt-5 block text-xs font-semibold">Backend URL<input id="wdmtBase" class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] p-2 text-sm" value="' + W.esc(W.base()) + '"></label>' +
      '<label class="mt-4 block text-xs font-semibold">API token<input id="wdmtTok" type="password" autocomplete="off" class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] p-2 text-sm" value="' + W.esc(W.token()) + '"></label>' +
      '<div class="mt-4"><button type="button" id="wdmtSaveConn" class="btn primary">Save connection</button></div>' +
      (needToken ? "" :
        '<hr class="my-6 border-[var(--line)]">' +
        '<label class="block text-xs font-semibold">Never track these domains (comma separated)<textarea id="wdmtExcl" rows="2" class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] p-2 text-sm"></textarea></label>' +
        '<label class="mt-4 block text-xs font-semibold">Extra sensitive domains (stored with a generic category, never sent to an AI)<textarea id="wdmtSens" rows="2" class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] p-2 text-sm"></textarea></label>' +
        '<div class="mt-3"><button type="button" id="wdmtSaveLists" class="btn">Save lists</button> <span id="wdmtListMsg" class="text-xs text-[var(--muted)]"></span></div>' +
        '<hr class="my-6 border-[var(--line)]">' +
        '<p class="text-sm font-semibold">Delete all my data</p>' +
        '<p class="mt-1 text-xs leading-5 text-[var(--muted)]">Wipes every session, summary, cache and override from the local database' + (demo ? " (the demo re-seeds its synthetic data)" : "") + ". Type DELETE to confirm.</p>" +
        '<div class="mt-3 flex gap-2"><input id="wdmtDelConfirm" class="w-28 rounded-lg border border-[var(--line)] bg-[var(--paper)] p-2 text-sm" placeholder="DELETE"><button type="button" id="wdmtDelete" class="btn">Delete everything</button></div>' +
        '<p id="wdmtDelMsg" class="mt-2 text-xs text-[var(--muted)]"></p>') +
      "</div>";
    document.body.appendChild(m);

    var close = document.getElementById("wdmtClose");
    if (close) close.onclick = function () { m.remove(); };
    document.getElementById("wdmtSaveConn").onclick = function () {
      ls(LS_BASE, document.getElementById("wdmtBase").value.trim() || null);
      ls(LS_TOKEN, document.getElementById("wdmtTok").value.trim() || null);
      location.reload();
    };
    if (needToken) return;

    W.api("/api/settings").then(function (s) {
      document.getElementById("wdmtExcl").value = (s.excluded_domains || []).join(", ");
      document.getElementById("wdmtSens").value = (s.sensitive_domains_extra || []).join(", ");
    }).catch(function () {});
    function list(id) { return document.getElementById(id).value.split(/[,\n]/).map(function (x) { return x.trim(); }).filter(Boolean); }
    document.getElementById("wdmtSaveLists").onclick = function () {
      Promise.all([
        W.api("/api/settings/excluded-domains", { method: "POST", body: { domains: list("wdmtExcl") } }),
        W.api("/api/settings/sensitive-domains", { method: "POST", body: { domains: list("wdmtSens") } }),
      ]).then(function () { document.getElementById("wdmtListMsg").textContent = "Saved (applies to new activity)."; })
        .catch(function (e) { document.getElementById("wdmtListMsg").textContent = e.message; });
    };
    document.getElementById("wdmtDelete").onclick = function () {
      var msg = document.getElementById("wdmtDelMsg");
      W.api("/api/data/delete", { method: "POST", body: { confirm: document.getElementById("wdmtDelConfirm").value } })
        .then(function () { msg.textContent = "All data deleted."; setTimeout(function () { location.reload(); }, 800); })
        .catch(function (e) { msg.textContent = e.message; });
    };
  };
})();
