/* ==========================================================================
   Page renderers. Each page keeps its existing markup/classes; we only fill in the numbers.
   Estimated (from browser history) and measured (live extension) time are always kept apart.
   ========================================================================== */
(function () {
  "use strict";
  var W = window.WDMT, $ = W.$, $$ = W.$$, esc = W.esc, fmt = W.fmt;
  var page = document.body.getAttribute("data-page");
  var refreshers = [];

  function onError(e) {
    if (window.console) console.error("[wdmt]", e && e.stack ? e.stack : e);
    W.notice(esc(e.message || String(e)), "warn");
  }
  function run(fn) { return W.ready.then(function () { return W.token() ? fn() : null; }).catch(onError); }
  function srcLine(measured, estimated, extra) {
    var parts = [];
    parts.push("<b>Measured</b> " + fmt(measured) + " (live extension)");
    parts.push("<b>Estimated</b> " + fmt(estimated) + " (from browser history)");
    if (extra) parts.push(extra);
    return parts.join(" &nbsp;·&nbsp; ");
  }
  function covLine(cov, unclassified) {
    if (!cov || !cov.sessions) return "";
    var bits = [Math.round(cov.classified_pct) + "% classified"];
    if (cov.pending) bits.push(cov.pending + " pending");
    if (cov.failed) bits.push(cov.failed + " could not be classified");
    if (unclassified) bits.push(fmt(unclassified) + " unclassified");
    return bits.join(", ");
  }
  function ensureLine(id, afterEl, cls) {
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement("p");
      el.id = id;
      el.className = cls || "mt-5 text-xs leading-5 text-[var(--muted)]";
      afterEl.parentNode.insertBefore(el, afterEl.nextSibling);
    }
    return el;
  }

  /* =============================================================== HOME */
  function home() {
    return W.api("/api/dashboard?range=today").then(function (d) {
      var t = d.totals;
      var totalEl = $("#todayTotal");
      totalEl.innerHTML = fmt(t.total) + ' <span class="text-base font-medium text-[var(--muted)]">online</span>';
      $("#todayDate").textContent = W.shortDate(d.range.start, d.range.timezone);

      var bar = $("#segBar"), legend = $("#segLegend");
      var order = ["focus", "learn", "break", "distract", "other", "unclassified"];
      var vals = { focus: t.focus, learn: t.learn, break: t["break"], distract: t.wasted, other: t.other, unclassified: t.unclassified };
      bar.innerHTML = "";
      legend.innerHTML = "";
      order.forEach(function (g) {
        var v = vals[g] || 0, meta = W.GROUPS[g];
        if (v > 0 && t.total) {
          var seg = document.createElement("div");
          seg.className = "seg rounded-[10px] " + meta.tag;
          seg.title = meta.label + ": " + fmt(v);
          seg.style.width = (100 * v / t.total).toFixed(1) + "%";
          if (!meta.tag) seg.style.background = meta.color;
          bar.appendChild(seg);
        }
        if (v > 0 || ["focus", "learn", "break", "distract"].indexOf(g) >= 0) {
          var cell = document.createElement("div");
          cell.innerHTML = '<div class="flex items-center gap-2 text-xs text-[var(--muted)]"><i class="h-2 w-2 rounded-full ' +
            meta.tag + '" style="' + (meta.tag ? "" : "background:" + meta.color) + '"></i>' + meta.label +
            '</div><p class="metric mt-1 text-[17px] font-semibold tracking-[-.04em]">' + fmt(v) + "</p>";
          legend.appendChild(cell);
        }
      });
      if (!t.total) bar.innerHTML = '<div class="w-full self-center px-3 text-xs text-[var(--muted)]">No activity recorded yet today.</div>';
      var line = ensureLine("srcLine", legend, "mt-6 border-t border-[var(--line)] pt-4 text-xs leading-5 text-[var(--muted)]");
      line.innerHTML = srcLine(t.measured, t.estimated, covLine(d.coverage, t.unclassified));

      var status = $("#heroStatus"), text = $("#heroText");
      if (!t.total) {
        status.textContent = "Waiting for activity";
        text.textContent = "Nothing tracked yet today. Once the extension is active, your day appears here.";
      } else {
        status.textContent = "Live snapshot";
        var lf = d.cards.longest_focus;
        text.innerHTML = "You had <b class=\"text-[var(--ink)]\">" + fmt(t.focus) + "</b> of focused activity" +
          (lf ? ". Your longest uninterrupted stretch was <b class=\"text-[var(--ink)]\">" + fmt(lf.seconds) + "</b>." : ".");
      }
      W.api("/api/status").then(function (st) {
        var det = $("#retDetail"), mo = $("#retMonths");
        if (det && st.first_data_day) {
          det.innerHTML = W.dayLabel(st.first_data_day).replace(/, \d{4}$/, "") + ' <span class="float-right">Today</span>';
        }
        if (mo) mo.textContent = st.months.length ? st.months.slice(-4).join(" · ") : "Built as months complete";
      }).catch(function () {});
      var w = d.cards.most_productive_window, fw = $("#focusWindow");
      fw.textContent = w
        ? "Your focus was strongest between " + W.hour12(w.start_hour) + " and " + W.hour12(w.end_hour) + " today."
        : "Not enough focused activity yet today to spot a pattern.";
    });
  }

  /* =============================================================== ACTIVITY */
  var act = { spec: "48h", offset: 0, q: "", category: "", cats: null, sessions: [] };

  function activeRange() {
    var btn = $(".range-scroll .range.active");
    var r = btn ? btn.getAttribute("data-range") : "48h";
    if (r === "custom") {
      var ins = $$("#customRangeFields input");
      if (ins.length === 2 && ins[0].value && ins[1].value) return ins[0].value + ".." + ins[1].value;
      return "48h";
    }
    return r;
  }

  function sessionRow(s, i) {
    var g = W.groupOf(s), meta = W.GROUPS[g];
    var tag = '<span class="tag ' + meta.tag + '"' + (meta.tag ? "" : ' style="background:var(--faint)"') + ">" + meta.label + "</span>";
    var srcTxt = s.source === "extension_measured" ? "Measured" : "Estimated";
    var note = s.is_wasted ? "Flagged distracting · " + srcTxt : srcTxt;
    var catTxt = s.status === "classified" ? (W.CAT[s.category] || s.category) : (s.status === "failed" ? "Could not be classified" : "Pending classification");
    var conf = s.confidence != null && s.status === "classified" ? " · " + Math.round(s.confidence * 100) + "% sure" : "";
    var how = s.classified_by ? ({ rule: "known-site rule", cache: "earlier result", llm: "AI", override: "your correction", sensitive: "sensitive-site rule", prior: "the site's usual category"  }[s.classified_by] || s.classified_by) : "not yet";
    var opts = Object.keys(W.CAT).map(function (c) {
      return '<option value="' + c + '"' + (c === s.category ? " selected" : "") + ">" + W.CAT[c] + "</option>";
    }).join("");
    var path = (s.url || "").replace(/^https?:\/\/[^/]+/, "");
    return '<div class="timeline-row py-5" data-sid="' + esc(s.id) + '">' +
      '<button class="timeline-toggle flex w-full items-start gap-4 text-left" aria-expanded="false" aria-controls="session-' + i + '">' +
      '<div class="mt-1.5"><div class="dot" style="background:' + meta.color + '"></div></div>' +
      '<p class="w-[42px] shrink-0 font-mono text-xs text-[var(--muted)]">' + esc(s.local_start.slice(11)) + "</p>" +
      '<div class="timeline-copy flex-1"><div class="flex items-center gap-2"><p class="font-semibold">' + esc(s.sensitive ? s.domain + " (sensitive)" : s.domain) + "</p>" + tag + "</div>" +
      '<p class="mt-1 text-xs text-[var(--muted)]">' + esc(s.sensitive ? "Title hidden from AI" : (s.title || path || "")) + "</p></div>" +
      '<p class="activity-meta mr-3 text-xs text-[var(--muted)]">' + esc(note) + "</p>" +
      '<p class="metric min-w-[50px] text-right text-sm font-semibold">' + fmt(s.duration) + "</p>" +
      '<span class="mt-0.5 text-[var(--muted)]"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg></span></button>' +
      '<div id="session-' + i + '" hidden class="ml-[83px] mt-4 grid grid-cols-2 gap-3 rounded-xl bg-[var(--faint)] p-4 text-xs sm:grid-cols-4">' +
      '<div><p class="text-[var(--muted)]">Website</p><p class="mt-1 break-all font-semibold">' + esc(s.domain + (s.sensitive ? "" : path)) + "</p></div>" +
      '<div><p class="text-[var(--muted)]">Started</p><p class="mt-1 font-semibold">' + esc(s.local_start.slice(5, 10)) + " · " + esc(s.local_start.slice(11)) + "</p></div>" +
      '<div><p class="text-[var(--muted)]">Category</p><p class="mt-1 font-semibold">' + esc(catTxt + conf) + "</p></div>" +
      '<div><p class="text-[var(--muted)]">Source</p><p class="mt-1 font-semibold">' + (s.source === "extension_measured" ? "Measured live" : "Estimated from history") + "</p></div>" +
      '<div><p class="text-[var(--muted)]">Classified by</p><p class="mt-1 font-semibold">' + esc(how) + "</p></div>" +
      '<div class="col-span-2"><p class="text-[var(--muted)]">Reason</p><p class="mt-1 font-semibold">' + esc(s.reason || "—") + "</p></div>" +
      '<div class="col-span-2 sm:col-span-4 flex flex-wrap items-center gap-2 border-t border-[var(--line)] pt-3">' +
      '<span class="text-[var(--muted)]">Wrong? Correct it:</span>' +
      '<select class="ov-cat rounded-lg border border-[var(--line)] bg-[var(--paper)] p-1.5">' + opts + "</select>" +
      '<select class="ov-scope rounded-lg border border-[var(--line)] bg-[var(--paper)] p-1.5"><option value="signature">this page</option><option value="domain">whole site</option></select>' +
      '<button type="button" class="ov-apply btn" style="padding:6px 12px;font-size:12px">Apply</button><span class="ov-msg text-[var(--muted)]"></span></div>' +
      "</div></div>";
  }

  function bindRows(root) {
    $$(".timeline-toggle", root).forEach(function (btn) {
      btn.addEventListener("click", function () {
        var expanded = btn.getAttribute("aria-expanded") === "true";
        var panel = document.getElementById(btn.getAttribute("aria-controls"));
        btn.setAttribute("aria-expanded", String(!expanded));
        if (panel) panel.hidden = expanded;
      });
    });
    $$(".ov-apply", root).forEach(function (b) {
      b.addEventListener("click", function () {
        var row = b.closest(".timeline-row"), msg = $(".ov-msg", row);
        msg.textContent = "Saving…";
        W.api("/api/overrides", { method: "POST", body: {
          session_id: row.getAttribute("data-sid"), category: $(".ov-cat", row).value, scope: $(".ov-scope", row).value } })
          .then(function (r) { msg.textContent = "Saved - " + r.updated_sessions + " session(s) updated."; loadActivity(true); })
          .catch(function (e) { msg.textContent = e.message; });
      });
    });
  }

  function loadActivity(keepOffset) {
    if (!keepOffset) act.offset = 0;
    act.spec = activeRange();
    var qs = "range=" + encodeURIComponent(act.spec) + "&limit=200&offset=" + act.offset +
      (act.q ? "&q=" + encodeURIComponent(act.q) : "") + (act.category ? "&category=" + act.category : "");
    return W.api("/api/activity?" + qs).then(function (d) {
      var cells = $$(".stat-grid .metric");
      var g = { focus: 0, learn: 0, break: 0 };
      // group totals need the whole range (not just this page): use the dashboard numbers
      return W.api("/api/dashboard?range=" + encodeURIComponent(act.spec)).then(function (dash) {
        var t = dash.totals;
        [t.total, t.focus, t.learn, t.wasted, t["break"]].forEach(function (v, i) { if (cells[i]) cells[i].textContent = fmt(v); });
        var stat = $(".stat-grid");
        var line = ensureLine("actSrc", stat, "mt-4 text-xs leading-5 text-[var(--muted)]");
        line.innerHTML = srcLine(t.measured, t.estimated, covLine(dash.coverage, t.unclassified));

        var title = $$("section p.text-xs.font-semibold.text-\\[var\\(--muted\\)\\]").filter(function (p) { return /^PAST|^LAST|^TODAY|^YESTERDAY|^THIS|^CUSTOM|^ACTIVITY IN/.test(p.textContent); })[0];
        if (title) title.textContent = ("ACTIVITY IN: " + d.range.label).toUpperCase();

        // top websites chips (click to filter)
        var chips = document.getElementById("domainChips");
        var anchor = $("section.mt-11.grid");
        if (!chips) {
          chips = document.createElement("div");
          chips.id = "domainChips";
          chips.className = "mt-6 flex flex-wrap items-center gap-2 text-xs";
          anchor.parentNode.insertBefore(chips, anchor);
        }
        chips.innerHTML = '<span class="font-semibold text-[var(--muted)]">TOP SITES</span>' +
          d.domains.slice(0, 8).map(function (x) {
            return '<button type="button" data-q="' + esc(x.domain) + '" class="rounded-full border border-[var(--line)] px-3 py-1.5 text-[var(--muted)] transition hover:bg-[var(--faint)]">' +
              esc(x.domain) + " · " + fmt(x.seconds) + "</button>";
          }).join("") +
          (act.q ? ' <button type="button" data-q="" class="underline">clear filter “' + esc(act.q) + "”</button>" : "") +
          '<select id="catFilter" class="ml-auto rounded-lg border border-[var(--line)] bg-[var(--paper)] p-1.5"><option value="">All categories</option>' +
          Object.keys(W.CAT).map(function (c) { return '<option value="' + c + '"' + (act.category === c ? " selected" : "") + ">" + W.CAT[c] + "</option>"; }).join("") +
          '<option value="unclassified"' + (act.category === "unclassified" ? " selected" : "") + ">Unclassified</option></select>" +
          ' <button type="button" id="reclassBtn" class="rounded-full border border-[var(--line)] px-3 py-1.5 text-[var(--muted)] transition hover:bg-[var(--faint)]">Re-classify ambiguous</button><span id="reclassMsg" class="text-[var(--muted)]"></span>';        $$("[data-q]", chips).forEach(function (b) { b.onclick = function () { act.q = b.getAttribute("data-q"); loadActivity(); }; });
        $("#reclassBtn", chips).onclick = function () {
          if (!confirm("Re-run the AI on sessions it marked ambiguous? Only compact, redacted page info is sent (see Privacy & data).")) return;
          var msg = $("#reclassMsg", chips);
          msg.textContent = "Starting…";
          W.api("/api/reclassify-ambiguous", { method: "POST", body: {} })
            .then(function (r) { msg.textContent = r.reset + " session(s) queued. Reload in a minute."; })
            .catch(function (e) { msg.textContent = e.message; });
        };
        
        $("#catFilter", chips).onchange = function () { act.category = this.value; loadActivity(); };

        var list = $("section.mt-11.grid > .divide-y");
        if (!d.sessions.length) {
          list.innerHTML = '<p class="py-10 text-sm text-[var(--muted)]">No activity in this range' + (act.q || act.category ? " matching your filter" : "") + ".</p>";
          return;
        }
        var html = d.sessions.map(function (s, i) { return sessionRow(s, act.offset + i); }).join("");
        var more = d.total_sessions > act.offset + d.sessions.length;
        list.innerHTML = html + (more ? '<div class="py-5"><button type="button" id="moreSessions" class="btn">Show more (' +
          (d.total_sessions - act.offset - d.sessions.length) + " left)</button></div>" : "");
        bindRows(list);
        var mb = document.getElementById("moreSessions");
        if (mb) mb.onclick = function () {
          act.offset += d.sessions.length;
          W.api("/api/activity?" + qs.replace(/offset=\d+/, "offset=" + act.offset)).then(function (n) {
            mb.parentNode.remove();
            var tmp = document.createElement("div");
            tmp.innerHTML = n.sessions.map(function (s, i) { return sessionRow(s, act.offset + i); }).join("");
            while (tmp.firstChild) list.appendChild(tmp.firstChild);
            bindRows(list);
          }).catch(onError);
        };
      });
    });
  }

  function activityPage() {
    $$(".range-scroll .range").forEach(function (b) {
      b.addEventListener("click", function () { setTimeout(function () { run(function () { return loadActivity(); }); }, 0); });
    });
    $$("#customRangeFields input").forEach(function (i) { i.addEventListener("change", function () { run(function () { return loadActivity(); }); }); });
    // custom inputs default to the last 48 hours in the user's local time
    var ins = $$("#customRangeFields input");
    if (ins.length === 2) {
      var pad = function (n) { return (n < 10 ? "0" : "") + n; };
      var f = function (d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()); };
      ins[1].value = f(new Date()); ins[0].value = f(new Date(Date.now() - 48 * 3600 * 1000));
    }
    return loadActivity();
  }

  /* =============================================================== INSIGHTS */
  function pickHero(daily) {
    for (var i = 0; i < daily.length; i++) if (daily[i].ai_analysis && daily[i].ai_analysis.summary) return daily[i];
    return daily[0] || null;
  }

  function showDay(d) {
    var hero = $("section.mt-11.overflow-hidden");
    var dateEl = $("span.text-xs.text-\\[\\#bfc1b9\\]", hero), copy = $(".analysis-copy", hero);
    var meta = $$(".border-t span", hero);
    if (!d) {
      dateEl.textContent = "";
      copy.textContent = "No days to analyse yet. Import your history or activate the extension.";
      meta.forEach(function (m) { m.textContent = ""; });
      return;
    }
    var kind = d.measured_seconds && d.estimated_seconds ? "measured + estimated" : (d.measured_seconds ? "measured" : "estimated from history");
    dateEl.textContent = W.dayLabel(d.day) + " · " + kind;
    var ai = d.ai_analysis;
    if (ai && ai.summary) {
      copy.textContent = ai.summary;
    } else if (d.analysis_status === "deterministic_only") {
      copy.textContent = "This day comes from browser history only (estimated), so it has numbers but no AI narrative: " +
        fmt(d.total_seconds) + " tracked, " + fmt(d.wasted_seconds) + " flagged distracting.";
    } else {
      var why = { in_progress: "The day isn't over yet - it is analysed once, after it ends.",
        waiting: "The AI narrative is waiting (unclassified sessions or the daily AI budget). Numbers are already available.",
        partial: "The AI narrative is partly done and will finish when budget allows.", no_data: "No activity this day." }[d.analysis_status] || "";
      copy.textContent = why + " So far: " + fmt(d.total_seconds) + " tracked, " + fmt(d.wasted_seconds) + " flagged distracting.";
    }
    if (meta[0]) meta[0].textContent = "Confidence: " + ((ai && ai.confidence) || "n/a");
    if (meta[1]) meta[1].textContent = "Based on " + ((d.coverage && d.coverage.sessions) || 0) + " sessions · " + Math.round((d.coverage && d.coverage.classified_pct) || 0) + "% classified";
    var nudge = $("section.mt-12 p.max-w-\\[620px\\]");
    if (nudge) nudge.textContent = (ai && ai.suggestion) || "Once a day has been analysed, one small, specific suggestion appears here.";
  }

  function insightsPage() {
    return W.api("/api/insights?days=30").then(function (d) {
      var hero = pickHero(d.daily);
      showDay(hero);

      var c = d.cards, cards = $$("article.card");
      var setCard = function (i, title, text) {
        if (!cards[i]) return;
        $("h3", cards[i]).textContent = title;
        $("p.text-sm", cards[i]).textContent = text;
      };
      var w = c.most_productive_window;
      setCard(0, w ? W.hour12(w.start_hour).replace(":00", "") + " — " + W.hour12(w.end_hour).replace(":00", "") : "Not enough data",
        w ? "Your clearest focus window, with " + fmt(w.seconds) + " of productive time (" + d.cards_range + ")." : "Needs a few days of classified activity.");
      var bd = c.biggest_distraction;
      setCard(1, bd ? bd.domain : "None flagged", bd ? fmt(bd.seconds) + " flagged as distracting (" + d.cards_range + ")." : "No time was flagged as distracting in " + d.cards_range + ".");
      var lf = c.longest_focus;
      setCard(2, lf ? fmt(lf.seconds) : "—", lf ? "Uninterrupted productive stretch, mostly on " + lf.domain + "." : "No focused stretch found yet.");
      setCard(3, c.learning_to_wasted_ratio != null ? c.learning_to_wasted_ratio + " : 1" : (c.learning_seconds ? "No waste" : "—"),
        c.learning_to_wasted_ratio != null ? "Learning and research time versus flagged distraction (" + d.cards_range + ")." :
        (c.learning_seconds ? fmt(c.learning_seconds) + " of learning and nothing flagged as distracting." : "No learning time in this range."));

      // earlier days + months (history AND live)
      var main = $("main"), sec = document.getElementById("insightHistory");
      if (!sec) {
        sec = document.createElement("section");
        sec.id = "insightHistory";
        sec.className = "mt-14 border-t border-[var(--line)] pt-9";
        main.insertBefore(sec, document.getElementById("wdmtFooter"));
      }
      var days = d.daily.slice(0, 14).map(function (x) {
        var ai = x.ai_analysis && x.ai_analysis.summary;
        var kind = x.measured_seconds ? (x.estimated_seconds ? "measured + estimated" : "measured") : "estimated";
        return '<button type="button" data-day="' + x.day + '" class="card w-full p-4 text-left transition hover:opacity-90">' +
          '<div class="flex items-center justify-between"><p class="text-sm font-semibold">' + W.dayLabel(x.day) + '</p><span class="text-xs text-[var(--muted)]">' + kind + " · " + fmt(x.total_seconds) + "</span></div>" +
          '<p class="mt-1 text-xs leading-5 text-[var(--muted)]">' + esc(ai || ({ deterministic_only: "Numbers only (history day).", waiting: "AI narrative waiting.", in_progress: "In progress.", partial: "AI narrative partly done." }[x.analysis_status] || "")) + "</p></button>";
      }).join("");
      var months = d.monthly.map(function (m) {
        var ai = m.ai_analysis && m.ai_analysis.summary;
        var st = { in_progress: "in progress", waiting: "narrative waiting", complete: "AI summary" }[m.analysis_status] || m.analysis_status;
        return '<article class="card p-5"><div class="flex items-center justify-between"><p class="text-sm font-semibold">' + esc(m.month) + '</p><span class="text-xs text-[var(--muted)]">' + esc(st) + "</span></div>" +
          '<p class="mt-2 text-xs text-[var(--muted)]"><b>' + fmt(m.total_seconds) + "</b> total · " + fmt(m.estimated_seconds) + " estimated · " + fmt(m.measured_seconds) + " measured · " + fmt(m.wasted_seconds) + " flagged</p>" +
          (ai ? '<p class="mt-3 text-sm leading-6">' + esc(ai) + "</p>" : "") + "</article>";
      }).join("");
      sec.innerHTML = '<p class="text-sm font-semibold">Monthly summaries</p><p class="mt-1 text-sm text-[var(--muted)]">History months are estimates written once during setup; live months combine both, kept separate.</p>' +
        '<div class="mt-5 grid gap-4 sm:grid-cols-2">' + (months || '<p class="text-sm text-[var(--muted)]">No months yet.</p>') + "</div>" +
        '<p class="mt-10 text-sm font-semibold">Earlier days</p><p class="mt-1 text-sm text-[var(--muted)]">Click a day to read it above.</p>' +
        '<div class="mt-5 grid gap-3 sm:grid-cols-2">' + (days || '<p class="text-sm text-[var(--muted)]">No days yet.</p>') + "</div>";
      $$("[data-day]", sec).forEach(function (b) {
        b.onclick = function () {
          var day = d.daily.filter(function (x) { return x.day === b.getAttribute("data-day"); })[0];
          showDay(day);
          window.scrollTo({ top: 0, behavior: "smooth" });
        };
      });
    });
  }

  /* =============================================================== TRENDS */
  var tr = { days: 60, mode: "daily", data: null, month: null };

  function drawSeries(svg, points, labels, opts) {
    var W_ = 620, top = 30, bottom = 165;
    $$("polyline,circle,text,line.dash,rect.est", svg).forEach(function (n) { n.remove(); });
    var max = Math.max.apply(null, points.concat([1]));
    var y = function (v) { return (bottom - (v / max) * (bottom - top)).toFixed(2); };
    var n = points.length, x = function (i) { return n === 1 ? W_ / 2 : (i * W_ / (n - 1)).toFixed(2); };
    var ns = "http://www.w3.org/2000/svg";
    var mk = function (tag, attrs, txt) {
      var e = document.createElementNS(ns, tag);
      Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
      if (txt != null) e.textContent = txt;
      svg.appendChild(e);
      return e;
    };
    if (opts.boundaryIndex != null && opts.boundaryIndex > 0 && opts.boundaryIndex < n) {
      var bx = x(opts.boundaryIndex);
      mk("line", { "class": "dash", x1: bx, x2: bx, y1: 10, y2: 165, stroke: "var(--muted)", "stroke-dasharray": "3 4", "stroke-width": 1 });
      mk("text", { x: bx, y: 12, "text-anchor": "start", fill: "var(--muted)", "font-size": 10 }, " live tracking starts →");
    }
    mk("polyline", { "class": opts.cls, points: points.map(function (v, i) { return x(i) + "," + y(v); }).join(" "), fill: "none" });
    var r = n > 35 ? 1.8 : 3.6;
    points.forEach(function (v, i) {
      var c = mk("circle", { cx: x(i), cy: y(v), r: r, fill: opts.dot });
      c.appendChild(document.createElementNS(ns, "title")).textContent = labels[i] + ": " + fmt(v * 3600);
    });
    var step = Math.max(1, Math.ceil(n / 8));
    labels.forEach(function (l, i) {
      if (i % step === 0 || i === n - 1) mk("text", { x: x(i), y: 190, "text-anchor": i === 0 ? "start" : (i === n - 1 ? "end" : "middle"), fill: "var(--muted)", "font-size": 11 }, l);
    });
    mk("text", { x: 0, y: 24, fill: "var(--muted)", "font-size": 10 }, "max " + max.toFixed(1) + "h");
  }

  function shortDay(day) {
    return new Date(day + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  function renderTrends() {
    var d = tr.data, isMonthly = tr.mode === "monthly";
    var rows = isMonthly ? d.monthly : d.daily.slice(-tr.days);
    var labels = rows.map(function (r) { return isMonthly ? r.month : shortDay(r.day); });
    var h = function (s) { return s / 3600; };
    var focus = rows.map(function (r) { return h(isMonthly ? (r.categories.focused_work || 0) + (r.categories.productivity || 0) + (r.categories.creative || 0) : r.focus); });
    var learn = rows.map(function (r) { return h(isMonthly ? (r.categories.learning || 0) + (r.categories.research || 0) : r.learning); });
    var wasted = rows.map(function (r) { return h(isMonthly ? r.wasted : r.wasted); });
    var svgs = $$("svg[role=img]");
    var bi = null;
    if (!isMonthly && d.activation_day) bi = rows.findIndex(function (r) { return r.day >= d.activation_day; });
    var cfg = [[focus, "chart-path", "var(--ink)"], [learn, "soft-path", "var(--lime-deep)"], [wasted, "chart-path", "var(--ink)"]];
    cfg.forEach(function (c, i) {
      if (svgs[i]) drawSeries(svgs[i], c[0], labels, { cls: c[1], dot: c[2], boundaryIndex: bi });
    });
    var sub = $$("section > div.mb-5 p.text-xs");
    var unit = isMonthly ? "Monthly" : "Daily";
    ["focused hours", "learning hours", "time flagged as distraction"].forEach(function (t, i) { if (sub[i]) sub[i].textContent = unit + " " + t; });

    // distribution over the selected window
    var tot = { focus: 0, learn: 0, break: 0, distract: 0, other: 0 }, all = 0;
    rows.forEach(function (r) {
      var c = isMonthly ? r.categories : r.categories;
      var f = (c.focused_work || 0) + (c.productivity || 0) + (c.creative || 0), l = (c.learning || 0) + (c.research || 0), b = c["break"] || 0;
      var wst = r.wasted || 0, total = Object.keys(c).reduce(function (a, k) { return a + c[k]; }, 0);
      tot.focus += f; tot.learn += l; tot["break"] += b; tot.distract += wst; tot.other += Math.max(0, total - f - l - b - wst); all += total;
    });
    var card = $$("section.card").filter(function (s) { return /Activity distribution/.test(s.textContent); })[0];
    if (card) {
      var bar = $(".flex.h-8", card), grid = $(".grid.grid-cols-2", card);
      var keys = ["focus", "learn", "break", "distract", "other"];
      bar.innerHTML = keys.map(function (k) { return tot[k] ? '<div title="' + W.GROUPS[k].label + '" style="width:' + (100 * tot[k] / (all || 1)).toFixed(1) + "%;background:" + W.GROUPS[k].color + '"></div>' : ""; }).join("");
      grid.innerHTML = keys.map(function (k) {
        return '<div class="flex items-center gap-2 text-sm"><i class="h-2 w-2 rounded-full" style="background:' + W.GROUPS[k].color + '"></i><span class="text-[var(--muted)]">' + W.GROUPS[k].label + '</span><b class="ml-auto mr-5">' + fmt(tot[k]) + "</b></div>";
      }).join("");
      var lbl = $$("p", card)[1];
      if (lbl) lbl.textContent = "How " + (isMonthly ? "all months" : "the last " + tr.days + " days") + " were composed";
    }
    renderSplit(rows, labels, isMonthly);
    renderMonth();
  }

  function renderSplit(rows, labels, isMonthly) {
    var grid = $(".trend-grid"), sec = document.getElementById("splitChart");
    if (!sec) {
      sec = document.createElement("section");
      sec.id = "splitChart";
      sec.className = "col-span-2 max-md:col-span-1";
      grid.appendChild(sec);
    }
    var maxv = Math.max.apply(null, rows.map(function (r) { return (isMonthly ? r.total : r.total); }).concat([1]));
    var n = rows.length, bw = 620 / Math.max(n, 1), gap = Math.min(3, bw * 0.25);
    var bars = rows.map(function (r, i) {
      var est = isMonthly ? r.estimated : r.estimated, mea = isMonthly ? r.measured : r.measured;
      var hE = 150 * est / maxv, hM = 150 * mea / maxv, x = (i * bw + gap / 2).toFixed(2), w = Math.max(1, bw - gap).toFixed(2);
      return '<g><title>' + esc(labels[i]) + ": measured " + fmt(mea) + ", estimated " + fmt(est) + "</title>" +
        '<rect x="' + x + '" y="' + (160 - hM - hE).toFixed(1) + '" width="' + w + '" height="' + hE.toFixed(1) + '" fill="#c4c6bc"/>' +
        '<rect x="' + x + '" y="' + (160 - hM).toFixed(1) + '" width="' + w + '" height="' + hM.toFixed(1) + '" fill="var(--lime-deep)"/></g>';
    }).join("");
    sec.innerHTML = '<div class="mb-5"><p class="text-sm font-semibold">Estimated vs measured</p><p class="mt-1 text-xs text-[var(--muted)]">' +
      '<i class="mr-1 inline-block h-2 w-2 rounded-full" style="background:var(--lime-deep)"></i>measured live by the extension &nbsp; ' +
      '<i class="mr-1 inline-block h-2 w-2 rounded-full" style="background:#c4c6bc"></i>estimated from browser history (approximate)</p></div>' +
      '<svg class="h-[170px] w-full overflow-visible" viewBox="0 0 620 170" preserveAspectRatio="none" role="img" aria-label="Estimated versus measured time">' + bars + "</svg>";
  }

  function renderMonth() {
    var d = tr.data, months = d.monthly.filter(function (m) { return m.total > 0; });
    var sec = $$("section.rounded-\\[28px\\]").filter(function (s) { return /MONTHLY SUMMARY/.test(s.textContent); })[0];
    if (!sec) return;
    if (!months.length) { $("h2", sec).textContent = "No months yet"; return; }
    if (!tr.month) tr.month = months[months.length - 1].month;
    var m = months.filter(function (x) { return x.month === tr.month; })[0] || months[months.length - 1];
    $("h2", sec).textContent = new Date(m.month + "-15T12:00:00").toLocaleDateString("en-US", { month: "long", year: "numeric" });
    $(".metric", sec).innerHTML = fmt(m.total) + ' <span class="text-sm font-medium text-[var(--muted)]">total browsing</span>';
    var c = m.categories, f = (c.focused_work || 0) + (c.productivity || 0) + (c.creative || 0), l = (c.learning || 0) + (c.research || 0);
    var cells = $$(".grid .metric", sec);
    [f, l, m.wasted, c["break"] || 0].forEach(function (v, i) { if (cells[i]) cells[i].textContent = fmt(v); });
    var full = d.monthly.filter(function (x) { return x.month === m.month; })[0];
    var p = $("p.max-w-\\[700px\\]", sec);
    var src = document.getElementById("monthSrc");
    if (!src) {
      src = document.createElement("p");
      src.id = "monthSrc";
      src.className = "mt-3 text-xs text-[var(--muted)]";
      p.parentNode.parentNode.insertBefore(src, p.parentNode.nextSibling);
    }
    src.innerHTML = srcLine(m.measured, m.estimated) + (months.length > 1 ? " &nbsp;·&nbsp; other months: " + months.map(function (x) {
      return x.month === m.month ? "<b>" + x.month + "</b>" : '<button type="button" class="underline" data-month="' + x.month + '">' + x.month + "</button>";
    }).join(" ") : "");
    $$("[data-month]", src).forEach(function (b) { b.onclick = function () { tr.month = b.getAttribute("data-month"); renderMonth(); }; });
    W.api("/api/insights?days=1").then(function (ins) {
      var mm = ins.monthly.filter(function (x) { return x.month === m.month; })[0];
      var ai = mm && mm.ai_analysis && mm.ai_analysis.summary;
      p.textContent = ai || (mm && mm.analysis_status === "in_progress"
        ? "This month is still in progress; its written summary is generated once the month ends. The numbers above update live."
        : "The written summary for this month is not available yet.");
    }).catch(function () {});
  }

  function trendsPage() {
    var group = $$("header .range").length ? $$("header .range")[0].parentNode : null;
    if (group && !$("[data-trend=\"60\"]", group)) {
      var b60 = document.createElement("button");
      b60.className = "range"; b60.textContent = "60 days";
      group.insertBefore(b60, group.children[2]);
    }
    var btns = $$("header .range");
    var map = [{ days: 7, mode: "daily" }, { days: 30, mode: "daily" }, { days: 60, mode: "daily" }, { days: 400, mode: "monthly" }];
    btns.forEach(function (b, i) {
      b.setAttribute("data-trend", String([7, 30, 60, 0][i]));
      b.classList.toggle("active", i === 2);   // default: the full 60 days (history + live)
      b.onclick = function () {
        btns.forEach(function (x) { x.classList.remove("active"); });
        b.classList.add("active");
        tr.days = map[i].days; tr.mode = map[i].mode;
        renderTrends();
      };
    });
    return W.api("/api/trends?days=400").then(function (d) { tr.data = d; tr.days = 60; tr.mode = "daily"; renderTrends(); });
  }

  /* =============================================================== ASK */
  function askPage() {
    var form = $("#askForm"), input = $("#askInput"), loading = $("#askLoading"), result = $("#askResult");
    var history = [];
    $$("[data-prompt]").forEach(function (chip) {
      chip.addEventListener("click", function () { input.value = chip.getAttribute("data-prompt"); form.requestSubmit(); });
    });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var q = input.value.trim();
      if (!q) return;
      result.hidden = true; loading.hidden = false;
      W.ready.then(function () {
        return W.api("/api/ask", { method: "POST", body: { question: q, history: history.slice(-4) } });
      }).then(function (r) {
        history.push({ role: "user", content: q }, { role: "assistant", content: r.answer });
        $("#askQuestionEcho").textContent = q;
        $("#askAnswerText").textContent = r.answer;
        var ev = $("#askEvidence");
        ev.innerHTML = "";
        (r.evidence.length ? r.evidence : ["No supporting rows were needed for this answer."]).forEach(function (line) {
          var row = document.createElement("div");
          row.className = "flex items-center gap-3 rounded-xl bg-[var(--faint)] px-4 py-3 text-sm";
          row.innerHTML = '<i class="h-2 w-2 rounded-full bg-[var(--lime-deep)]"></i>' + esc(line);
          ev.appendChild(row);
        });
        loading.hidden = true; result.hidden = false;
      }).catch(function (err) {
        $("#askQuestionEcho").textContent = q;
        $("#askAnswerText").textContent = err.message;
        $("#askEvidence").innerHTML = "";
        loading.hidden = true; result.hidden = false;
      });
    });
  }

  /* =============================================================== placeholders */
  // The HTML ships with design-time sample numbers. Never show them as if they were yours:
  // blank them the moment the script runs, until real data arrives.
  function blankMock() {
    var set = function (sel, v) { $$(sel).forEach(function (e) { e.textContent = v; }); };
    if (page === "home") {
      set("#todayTotal", "—"); set("#heroText", "Loading…"); set("#heroStatus", "Loading"); set("#focusWindow", "…");
      var sb = $("#segBar"), sl = $("#segLegend"); if (sb) sb.innerHTML = ""; if (sl) sl.innerHTML = "";
    } else if (page === "activity") {
      set(".stat-grid .metric", "—");
      var list = $("section.mt-11.grid > .divide-y");
      if (list) list.innerHTML = '<p class="py-10 text-sm text-[var(--muted)]">Loading your activity…</p>';
    } else if (page === "insights") {
      set(".analysis-copy", "Loading…");
      $$("article.card").forEach(function (c) { var h = $("h3", c), p = $("p.text-sm", c); if (h) h.textContent = "—"; if (p) p.textContent = ""; });
      set("section.mt-12 p.max-w-\\[620px\\]", "");
    } else if (page === "trends") {
      $$("svg[role=img]").forEach(function (svg) { $$("polyline,circle,text", svg).forEach(function (n) { n.remove(); }); });
      var card = $$("section.card").filter(function (x) { return /Activity distribution/.test(x.textContent); })[0];
      if (card) { $(".flex.h-8", card).innerHTML = ""; $(".grid.grid-cols-2", card).innerHTML = ""; }
      var ms = $$("section.rounded-\\[28px\\]").filter(function (x) { return /MONTHLY SUMMARY/.test(x.textContent); })[0];
      if (ms) { $("h2", ms).textContent = "…"; $$(".metric", ms).forEach(function (m) { m.textContent = "—"; }); $("p.max-w-\\[700px\\]", ms).textContent = ""; }
    }
  }

  /* =============================================================== boot */
  var loaders = { home: home, activity: activityPage, insights: insightsPage, trends: trendsPage };
  if (page === "ask") { askPage(); return; }
  var load = loaders[page];
  if (!load) return;
  blankMock();
  var done = function () { document.body.setAttribute("data-loaded", "1"); };
  run(load).then(done, done);
  document.addEventListener("wdmt:refresh", function () { run(load); });
  if (page === "home" || page === "activity") {
    setInterval(function () { if (!document.hidden && !document.getElementById("wdmtModal")) run(page === "activity" ? function () { return loadActivity(true); } : load); }, 60000);
  }
})();
