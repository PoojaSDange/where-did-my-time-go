/* ==========================================================================
   Where Did My Time Go? — shared behaviour
   No framework: each page is plain HTML, this file just wires up the
   interactive bits (theme toggle, range pills, timeline rows). Data comes from assets/api.js + assets/pages.js.
   ========================================================================== */
 
(function themeToggle() {
  var KEY = "wdmt-theme";
  var root = document.documentElement;
 
  document.querySelectorAll(".theme-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var isDark = root.classList.toggle("dark");
      try { localStorage.setItem(KEY, isDark ? "dark" : "light"); } catch (e) {}
    });
  });
})();
 
(function rangeSelector() {
  var group = document.querySelector(".range-scroll");
  if (!group) return;
 
  var customFields = document.getElementById("customRangeFields");
 
  group.querySelectorAll(".range").forEach(function (btn) {
    btn.addEventListener("click", function () {
      group.querySelectorAll(".range").forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
 
      if (customFields) {
        customFields.hidden = btn.dataset.range !== "custom";
      }
    });
  });
})();
 
(function timelineToggles() {
  document.querySelectorAll(".timeline-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var expanded = btn.getAttribute("aria-expanded") === "true";
      var panel = document.getElementById(btn.getAttribute("aria-controls"));
      btn.setAttribute("aria-expanded", String(!expanded));
      if (panel) panel.hidden = expanded;
    });
  });
})();
 
/* The Ask AI page is wired to the real supervisor agent in assets/pages.js. */
 
(function headerDate() {
  // The date chip in the top bar was static text in every page; show the real current date instead.
  var el = document.querySelector("span.wide-only");
  if (!el) return;
  var svg = el.querySelector("svg");
  el.textContent = "";
  if (svg) el.appendChild(svg);
  el.appendChild(document.createTextNode(
    " " + new Date().toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" })));
})();
 
