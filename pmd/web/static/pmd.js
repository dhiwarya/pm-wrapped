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

  const picks = (set, root) => $$('[data-pick="' + CSS.escape(set) + '"]', root);
  let saving = false;
  let statusTimer;

  function notice(message, error) {
    const box = document.querySelector("dialog[open] :is(.story-feedback,.dialog-feedback)") || document.getElementById("save-status");
    clearTimeout(statusTimer);
    box.classList.toggle("error", error);
    box.textContent = message;
    if (!error) statusTimer = setTimeout(() => { box.textContent = ""; }, 4500);
  }

  function updateSelections() {
    for (const toggle of $$("[data-all]")) {
      const group = picks(toggle.dataset.all, document.getElementById(toggle.dataset.group));
      const checked = group.filter(box => box.checked).length;
      toggle.checked = group.length > 0 && checked === group.length;
      toggle.indeterminate = checked > 0 && checked < group.length;
    }
    for (const form of $$("[data-batch]")) {
      const count = picks(form.dataset.batch).filter(box => box.checked).length;
      form.querySelector("[data-selected-count]").textContent = count + " selected";
      for (const button of $$("button", form)) button.disabled = saving || !count;
    }
  }

  document.addEventListener("change", event => {
    const field = event.target;
    if (field.matches("[data-all]")) {
      picks(field.dataset.all, document.getElementById(field.dataset.group)).forEach(box => { box.checked = field.checked; });
    }
    if (field.matches("[data-pick],[data-all]")) updateSelections();
  });

  document.addEventListener("input", event => {
    const field = event.target;
    if (field.matches('[name="person"],[name^="edit_"]')) {
      const box = field.form?.querySelector("[data-pick]");
      if (box) {
        box.checked = true;
        updateSelections();
      }
    }
  });

  function edits(form) {
    return Object.fromEntries($$('[name^="edit_"]', form)
      .filter(field => field.value !== field.defaultValue)
      .map(field => [field.name, field.value]));
  }

  function batchRows(form) {
    const boxes = picks(form.dataset.batch).filter(box => box.checked);
    if (!boxes.length) throw new Error("Select at least one row.");
    return boxes.map(box => {
      const fields = new FormData(box.form);
      switch (form.dataset.mode) {
        case "proposals":
          return {id: box.value, ...edits(box.form)};
        case "names": {
          const person = fields.get("person").trim() || form.elements.default_person.value.trim();
          if (!person) {
            box.form.elements.person.focus();
            throw new Error("Enter a person for " + fields.get("value") + " or set a default person.");
          }
          return {kind: fields.get("kind"), value: fields.get("value"), person};
        }
        case "health":
          return {id: box.value, project: box.dataset.project, proposal_id: fields.get("proposal_id")};
        case "evidence":
          return {id: box.value, note: fields.get("note"), evidence_url: fields.get("evidence_url")};
        default:
          throw new Error("Unknown selection.");
      }
    });
  }

  function updatePage(fresh, form, success, originalFocus) {
    const story = form.closest("[data-story-card]");
    const current = story || document.querySelector("main");
    const next = story ? fresh.getElementById(story.id) : fresh.querySelector("main");
    if (!next || !fresh.querySelector(".brand")) throw new Error("Could not update this view. Check the saved state before retrying.");

    // Keep drafts in surviving rows, including edits made while the save was in flight.
    const drafts = [];
    for (const field of $$("input[id],textarea[id],select[id]", current)) {
      if (success && field.form === form && !field.matches("[data-pick]")) continue;
      if (field.matches("[data-pick]")) {
        if (field.checked) drafts.push([field.id, "checked", true]);
      } else if (field.tagName === "SELECT") {
        const initial = Array.from(field.options).findLast(option => option.defaultSelected) || field.options[0];
        if (initial && field.value !== initial.value)
          drafts.push([field.id, "value", field.value]);
      } else if (field.value !== field.defaultValue) {
        drafts.push([field.id, "value", field.value]);
      }
    }
    const active = document.activeElement === document.body ? originalFocus : document.activeElement;
    const row = form.closest("li[id],section[id],article[id]");
    const candidates = [row, row?.nextElementSibling, ...$$("li[id],section[id],article[id]", current)];
    const top = document.querySelector(".top").getBoundingClientRect().bottom;
    const anchor = !story && candidates.find(el => {
      if (!el?.id || !fresh.getElementById(el.id)) return false;
      const rect = el.getBoundingClientRect();
      return rect.top < innerHeight && rect.bottom > top && rect.height;
    });
    const oldTop = anchor && anchor.getBoundingClientRect().top;
    const pop = success && form.closest("details.pop");
    if (success) form.closest("dialog:not([data-story])")?.close();
    window.morphdom(current, next, {
      onBeforeElUpdated(from, to) {
        if (from.tagName === "DETAILS") to.open = from.open && from !== pop;
        if (from.tagName === "DIALOG") to.open = from.open;
        if (story && from === story) to.hidden = from.hidden;
      }
    });
    for (const [id, prop, value] of drafts) {
      const field = document.getElementById(id);
      if (field) field[prop] = value;
    }
    if (anchor?.isConnected) window.scrollBy(0, anchor.getBoundingClientRect().top - oldTop);
    if (active && active !== document.body && !active.isConnected) {
      const fallback = (row?.isConnected && row) || (anchor?.isConnected && anchor) || document.querySelector(".vhead h1,main h1");
      if (fallback) { fallback.tabIndex = -1; fallback.focus({preventScroll: true}); }
    }
    document.title = fresh.title;
  }

  document.addEventListener("submit", async function (event) {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
    event.preventDefault();
    const inline = form.hasAttribute("data-inline");
    if (inline && saving) return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    const data = new FormData(form, event.submitter || undefined);
    try {
      if (form.dataset.batch) data.set("items", JSON.stringify(batchRows(form)));
      if (form.matches(".propform") && data.get("action") === "accept" && Object.keys(edits(form)).length)
        data.set("action", "edit");
    } catch (error) { notice(error.message, true); return; }

    const originalFocus = document.activeElement;
    const buttons = inline ? $$("form[data-inline] button[type=submit]:enabled") : $$("button:enabled", form);
    if (inline) saving = true;
    form.setAttribute("aria-busy", "true");
    buttons.forEach(button => { button.disabled = true; });
    let success = false;
    try {
      // A button named "action" hides form.action.
      const response = await fetch(form.getAttribute("action") || location.href, {
        method: "POST", body: data, headers: {"X-PMD-Request": "1"}, credentials: "same-origin"
      });
      if (!response.ok || !response.redirected) throw new Error("Save request failed (" + response.status + "). Check the current state before retrying.");
      if (!inline) { window.location.assign(response.url); return; }
      const params = new URL(response.url).searchParams;
      if (!params.has("msg") && !params.has("err")) throw new Error("No save result received. Check the current state before retrying.");
      const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
      success = !params.has("err");
      updatePage(fresh, form, success, originalFocus);
      notice(params.get(success ? "msg" : "err"), !success);
    } catch (error) {
      success = false;
      const message = error instanceof TypeError && error.message === "Failed to fetch" ? "Connection interrupted. Check the current state before retrying." : error.message;
      if (inline) notice(message, true); else showError(message);
    } finally {
      buttons.forEach(button => { button.disabled = false; });
      form.removeAttribute("aria-busy");
      if (inline) {
        saving = false;
        updateSelections();
        document.dispatchEvent(new CustomEvent("pmd:saved", {detail: {success, form}}));
      }
    }
  });
  updateSelections();
  if (document.querySelector(".names-tile") && window.ResizeObserver) {
    new ResizeObserver(entries => {
      document.documentElement.style.setProperty("--nav-height", entries[0].target.offsetHeight + "px");
    }).observe(document.querySelector(".top"));
  }

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
        if (Number(el.dataset.count) !== to) return;
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
