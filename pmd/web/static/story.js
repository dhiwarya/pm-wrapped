(function () {
  "use strict";
  const dialog = document.querySelector("[data-story]");
  if (!dialog) return;
  const cards = Array.from(dialog.querySelectorAll("[data-story-card]"));
  const bars = Array.from(dialog.querySelectorAll("[data-story-bar] i"));
  const count = dialog.querySelector("[data-story-count]");
  const pauseButton = dialog.querySelector("[data-story-pause]");
  const previous = dialog.querySelector("[data-story-prev]");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  const duration = 6000;
  let index = Number(dialog.dataset.slide);
  let elapsed = 0;
  let last = 0;
  let paused = false;
  let pointer = null;
  let submitting = false;
  let frame;

  function show(next) {
    if (next >= cards.length) { window.location.assign("/"); return; }
    index = Math.max(0, next);
    elapsed = 0;
    cards.forEach((card, i) => { card.hidden = i !== index; });
    bars.forEach((bar, i) => { bar.style.width = i < index || (reduce.matches && i === index) ? "100%" : "0%"; });
    count.textContent = `${index + 1} of ${cards.length}`;
    previous.disabled = index === 0;
  }

  function pause() {
    paused = !paused;
    pauseButton.textContent = paused ? "Resume" : "Pause";
    pauseButton.setAttribute("aria-pressed", paused);
  }

  function tick(now) {
    if (!dialog.open) return;
    if (last && !paused && !pointer && !submitting && !document.hidden) {
      elapsed += now - last;
      if (elapsed >= duration) show(index + 1);
      else bars[index].style.width = `${elapsed / duration * 100}%`;
    }
    last = now;
    if (!reduce.matches) frame = window.requestAnimationFrame(tick);
  }

  function motion() {
    window.cancelAnimationFrame(frame);
    pauseButton.hidden = reduce.matches;
    last = 0;
    show(index);
    if (!reduce.matches) frame = window.requestAnimationFrame(tick);
  }

  dialog.addEventListener("keydown", event => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      show(index + (event.key === "ArrowRight" ? 1 : -1));
    } else if (event.code === "Space" && !reduce.matches && !event.target.closest("button,a")) {
      event.preventDefault();
      pause();
    }
  });
  dialog.addEventListener("submit", () => { submitting = true; });
  dialog.addEventListener("cancel", event => { event.preventDefault(); window.location.assign("/"); });
  pauseButton.addEventListener("click", pause);
  previous.addEventListener("click", () => show(index - 1));
  dialog.querySelectorAll("[data-story-next]").forEach(button => button.addEventListener("click", () => show(index + 1)));
  cards.forEach(card => {
    card.addEventListener("pointerdown", event => {
      if (event.target.closest("button,a") || !event.isPrimary) return;
      pointer = { id: event.pointerId, x: event.clientX, y: event.clientY, at: performance.now() };
      card.setPointerCapture(event.pointerId);
    });
    card.addEventListener("pointerup", event => {
      if (!pointer || pointer.id !== event.pointerId) return;
      const dx = event.clientX - pointer.x;
      const dy = event.clientY - pointer.y;
      const held = performance.now() - pointer.at > 350;
      pointer = null;
      if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)) show(index + (dx < 0 ? 1 : -1));
      else if (!held && Math.abs(dx) < 10 && Math.abs(dy) < 10) {
        const box = card.getBoundingClientRect();
        show(index + (event.clientX >= box.left + box.width / 2 ? 1 : -1));
      }
    });
    card.addEventListener("pointercancel", () => { pointer = null; });
  });
  document.addEventListener("visibilitychange", () => { last = 0; });
  reduce.addEventListener("change", motion);
  dialog.showModal();
  motion();
})();
