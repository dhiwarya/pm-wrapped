/* Applies the remembered theme before the page paints (R-V9). A per-viewer convenience only. */
(function () {
  try {
    var t = localStorage.getItem("pmd-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage may be blocked: follow the system setting */ }
})();
