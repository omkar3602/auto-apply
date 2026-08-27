(function () {
  const deck = document.getElementById("swipe-deck");
  if (!deck) return;

  const emptyState = document.getElementById("swipe-empty");
  const passBtn = document.getElementById("swipe-pass-btn");
  const applyBtn = document.getElementById("swipe-apply-btn");
  const linkBtn = document.getElementById("swipe-link-btn");

  const DISTANCE_THRESHOLD = 90; // px - release past this and it's a swipe regardless of speed
  const FLICK_DISTANCE_MIN = 24; // px - a fast flick still needs to have moved this much...
  const FLICK_VELOCITY_MIN = 0.55; // ...at this speed (px/ms) to count as a swipe
  const VELOCITY_WINDOW_MS = 120; // only recent samples count toward velocity

  let dragging = null; // { card, startX, startY, dx, dy, samples, rafId }

  function topCard() {
    // Cards mid-flyoff stay in the DOM briefly for their exit animation -
    // exclude them so the next swipe/button press acts on the next card
    // right away instead of stalling on one that's already decided.
    return deck.querySelector(".swipe-card:not([data-decided])");
  }

  function refreshControls() {
    const card = topCard();
    if (!card) {
      emptyState.hidden = false;
      linkBtn.hidden = true;
      passBtn.disabled = true;
      applyBtn.disabled = true;
      return;
    }
    emptyState.hidden = true;
    linkBtn.hidden = false;
    linkBtn.href = card.dataset.jobUrl;
    passBtn.disabled = false;
    applyBtn.disabled = false;
  }

  function send(url) {
    fetch(url, { method: "POST" }).catch((err) => console.error("swipe action failed", err));
  }

  function setStampStrength(card, dx) {
    const pass = card.querySelector(".swipe-stamp-pass");
    const apply = card.querySelector(".swipe-stamp-apply");
    const strength = Math.min(Math.abs(dx) / DISTANCE_THRESHOLD, 1);
    if (pass) pass.style.opacity = dx < 0 ? strength : 0;
    if (apply) apply.style.opacity = dx > 0 ? strength : 0;
  }

  // Swiping is optimistic: the card flies off and the deck advances right
  // away. Pass is synchronous server-side; Apply just enqueues the job for
  // the tsenta worker (see automation/tasks.py) - either way there's
  // nothing worth blocking the UI on here.
  function decide(card, direction, opts) {
    if (card.dataset.decided) return;
    card.dataset.decided = "1";
    const jobId = card.dataset.jobId;
    const dy = (opts && opts.dy) || 0;
    const speed = (opts && opts.speed) || 0.9; // px/ms - a flick keeps its own momentum

    card.classList.add("swipe-card-flying");
    // Promote the next card to full size right away instead of waiting for
    // this one's exit animation to finish - makes rapid swiping feel
    // continuous instead of stepping in 300ms beats.
    const next = topCard();
    if (next) next.classList.add("swipe-card-promoted");
    refreshControls();
    card.style.transition = `transform ${Math.max(0.2, Math.min(0.45, 220 / (speed * 1000)))}s cubic-bezier(.2,.6,.3,1), opacity 0.3s ease-out`;
    const flyX = (direction === "right" ? 1 : -1) * (window.innerWidth * 1.2);
    card.style.transform = `translate(${flyX}px, ${dy}px) rotate(${direction === "right" ? 26 : -26}deg)`;
    card.style.opacity = "0";
    setStampStrength(card, direction === "right" ? DISTANCE_THRESHOLD * 2 : -DISTANCE_THRESHOLD * 2);

    send(direction === "right" ? `/jobs/${jobId}/apply` : `/jobs/${jobId}/pass`);

    card.addEventListener(
      "transitionend",
      () => {
        card.remove();
        refreshControls();
      },
      { once: true }
    );
    // Fallback in case transitionend never fires (e.g. tab backgrounded mid-animation).
    setTimeout(() => {
      if (card.isConnected) {
        card.remove();
        refreshControls();
      }
    }, 500);
  }

  function resetCard(card) {
    card.style.transition = "transform 0.28s cubic-bezier(.2,.6,.3,1)";
    card.style.transform = "";
    setStampStrength(card, 0);
  }

  function onPointerDown(e) {
    const card = topCard();
    if (!card || card.dataset.decided) return;
    if (e.target.closest(".swipe-body")) return; // let the description scroll instead of dragging
    if (e.button !== undefined && e.button !== 0) return;

    dragging = {
      card,
      startX: e.clientX,
      startY: e.clientY,
      dx: 0,
      dy: 0,
      samples: [{ t: performance.now(), x: e.clientX }],
      rafId: null,
    };
    if (card.setPointerCapture) card.setPointerCapture(e.pointerId);
    card.style.transition = "none";
    card.classList.add("swipe-card-dragging");
  }

  function applyDragTransform() {
    if (!dragging) return;
    const { card, dx, dy } = dragging;
    card.style.transform = `translate(${dx}px, ${dy}px) rotate(${dx / 14}deg)`;
    setStampStrength(card, dx);
    dragging.rafId = null;
  }

  function onPointerMove(e) {
    if (!dragging) return;
    dragging.dx = e.clientX - dragging.startX;
    dragging.dy = e.clientY - dragging.startY;

    const now = performance.now();
    dragging.samples.push({ t: now, x: e.clientX });
    while (dragging.samples.length > 2 && now - dragging.samples[0].t > VELOCITY_WINDOW_MS) {
      dragging.samples.shift();
    }

    if (dragging.rafId == null) {
      dragging.rafId = requestAnimationFrame(applyDragTransform);
    }
  }

  function releaseVelocity() {
    const samples = dragging.samples;
    const first = samples[0];
    const last = samples[samples.length - 1];
    const dt = last.t - first.t;
    if (dt <= 0) return 0;
    return (last.x - first.x) / dt; // px/ms, signed
  }

  function onPointerUp() {
    if (!dragging) return;
    if (dragging.rafId != null) cancelAnimationFrame(dragging.rafId);
    const { card, dx, dy } = dragging;
    card.classList.remove("swipe-card-dragging");

    const velocity = releaseVelocity();
    const isFlick = Math.abs(dx) >= FLICK_DISTANCE_MIN && Math.abs(velocity) >= FLICK_VELOCITY_MIN;
    const isDrag = Math.abs(dx) > DISTANCE_THRESHOLD;

    if (isDrag || isFlick) {
      const direction = dx > 0 ? "right" : "left";
      const speed = Math.max(Math.abs(velocity), DISTANCE_THRESHOLD / 300);
      decide(card, direction, { dy, speed });
    } else {
      resetCard(card);
    }
    dragging = null;
  }

  deck.addEventListener("pointerdown", onPointerDown);
  window.addEventListener("pointermove", onPointerMove, { passive: true });
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("pointercancel", onPointerUp);

  passBtn.addEventListener("click", () => {
    const card = topCard();
    if (card) decide(card, "left", { speed: 1.4 });
  });
  applyBtn.addEventListener("click", () => {
    const card = topCard();
    if (card) decide(card, "right", { speed: 1.4 });
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    if (document.activeElement && ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    const card = topCard();
    if (!card) return;
    decide(card, e.key === "ArrowRight" ? "right" : "left", { speed: 1.4 });
  });

  refreshControls();
})();
