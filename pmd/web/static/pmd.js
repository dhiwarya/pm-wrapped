/* PM Dash page behaviour. No framework, no build step (Detailed Design Part 1 §3).
 *
 * Forms: every POST form is sent with fetch and PMD's own header, which the guard
 * requires (Design §9-2). A page on another site cannot add that header.
 */
(function () {
  "use strict";
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function showError(text) {
    var box = document.getElementById("net-error");
    if (!box) { window.alert(text); return; }
    box.querySelector(".wrap").textContent = "✕ " + text;
    box.hidden = false;
    window.scrollTo(0, 0);
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
    e.preventDefault();
    var ask = form.getAttribute("data-confirm");
    if (ask && !window.confirm(ask)) return;
    var data = new FormData(form, e.submitter || undefined);
    var buttons = $$("button", form);
    buttons.forEach(function (b) { b.disabled = true; });
    fetch(form.action, { method: "POST", body: data, headers: { "X-PMD-Request": "1" }, credentials: "same-origin" })
      .then(function (r) {
        if (r.redirected) { window.location.assign(r.url); return; }
        return r.text().then(function (t) {
          buttons.forEach(function (b) { b.disabled = false; });
          showError("Not saved (" + r.status + "): " + t.slice(0, 200));
        });
      })
      .catch(function () {
        buttons.forEach(function (b) { b.disabled = false; });
        showError("PM Dash could not be reached. Nothing was saved.");
      });
  });

  /* dialogs */
  document.addEventListener("click", function (e) {
    var open = e.target.closest("[data-open]");
    if (open) { var d = document.getElementById(open.getAttribute("data-open")); if (d && d.showModal) d.showModal(); return; }
    var close = e.target.closest("[data-close]");
    if (close) { var dlg = close.closest("dialog"); if (dlg) dlg.close(); return; }
    var toggle = e.target.closest("[data-theme-toggle]");
    if (toggle) {
      var root = document.documentElement;
      var cur = root.getAttribute("data-theme") || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
      var next = cur === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("pmd-theme", next); } catch (err) { /* ignore */ }
    }
  });

  /* close an open popover when another opens */
  document.addEventListener("toggle", function (e) {
    var el = e.target;
    if (!(el instanceof HTMLDetailsElement) || !el.classList.contains("pop") || !el.open) return;
    $$("details.pop[open]").forEach(function (d) { if (d !== el) d.open = false; });
  }, true);

  /* hero numbers count up (R-V8) */
  if (!REDUCED) {
    $$("[data-count]").forEach(function (el) {
      var to = parseInt(el.getAttribute("data-count"), 10);
      if (!(to > 0)) return;
      var t0 = null;
      function step(t) {
        if (t0 === null) t0 = t;
        var p = Math.min((t - t0) / 700, 1);
        el.textContent = String(Math.round(to * (1 - Math.pow(1 - p, 3))));
        if (p < 1) window.requestAnimationFrame(step);
      }
      el.textContent = "0";
      window.requestAnimationFrame(step);
    });
  }

  /* drop ?msg= / ?err= from the address bar once shown */
  if (window.history.replaceState && /[?&](msg|err)=/.test(window.location.search)) {
    var params = new URLSearchParams(window.location.search);
    params.delete("msg"); params.delete("err");
    var q = params.toString();
    window.history.replaceState(null, "", window.location.pathname + (q ? "?" + q : "") + window.location.hash);
  }
})();
