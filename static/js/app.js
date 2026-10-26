(function () {
  const SPINNER_HTML = '<span class="spinner"></span>';

  function post(url) {
    return fetch(url, { method: "POST" }).then((r) => r.json());
  }
  function get(url) {
    return fetch(url).then((r) => r.json());
  }

  function findJobId(el) {
    const holder = el.closest("[data-job-id]");
    return holder ? holder.dataset.jobId : null;
  }

  function setButtonLoading(btn, label) {
    btn.disabled = true;
    btn.innerHTML = SPINNER_HTML + label;
  }
  function setButtonIdle(btn, label) {
    btn.disabled = false;
    btn.textContent = label;
  }

  function cardOverlay(el) {
    const card = el.closest(".card");
    return card ? card.querySelector(".card-loading-overlay") : null;
  }

  function removeCard(el) {
    const card = el.closest(".card");
    if (card) {
      card.classList.add("removing");
      setTimeout(() => card.remove(), 200);
    } else if (window.AUTO_APPLY_DETAIL_REDIRECT) {
      window.location.href = window.AUTO_APPLY_DETAIL_REDIRECT;
    }
  }

  function pollApplyStatus(jobId, button) {
    const overlay = cardOverlay(button);
    const poll = () => {
      get(`/jobs/${jobId}/apply-status`).then((data) => {
        if (data.status === "queued" || data.status === "applying") {
          setTimeout(poll, 3000);
          return;
        }
        if (data.status === "applied") {
          removeCard(button);
          return;
        }
        if (data.status === "apply_failed") {
          if (overlay) overlay.hidden = true;
          setButtonIdle(button, "Retry");
          const card = button.closest(".card") || button.closest(".detail-page");
          if (card && data.status_message) {
            let msg = card.querySelector(".error-msg");
            if (!msg) {
              msg = document.createElement("div");
              msg.className = "error-msg";
              button.closest(".card-footer, .detail-actions").after(msg);
            }
            msg.textContent = data.status_message;
          }
        }
      });
    };
    setTimeout(poll, 3000);
  }

  document.addEventListener("click", function (e) {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const jobId = findJobId(btn);
    if (!jobId) return;

    if (btn.dataset.action === "pass") {
      setButtonLoading(btn, "Passing…");
      post(`/jobs/${jobId}/pass`).then(() => removeCard(btn));
    }

    if (btn.dataset.action === "apply") {
      setButtonLoading(btn, "Queued…");
      const overlay = cardOverlay(btn);
      if (overlay) overlay.hidden = false;
      post(`/jobs/${jobId}/apply`).then(() => pollApplyStatus(jobId, btn));
    }
  });

  // Sync button + status pill + loading overlay (tab page only)
  const syncBtn = document.getElementById("sync-btn");
  if (syncBtn) {
    const pill = document.getElementById("sync-pill");
    const overlay = document.getElementById("sync-overlay");
    const errorEl = document.getElementById("sync-error");
    const source = syncBtn.dataset.source;

    const pollSync = (wasRunning) => {
      get(`/jobs/${source}/sync-status`).then((data) => {
        if (data.state === "running") {
          pill.textContent = "Syncing…";
          setButtonLoading(syncBtn, "Syncing…");
          overlay.hidden = false;
          setTimeout(() => pollSync(true), 2000);
        } else {
          setButtonIdle(syncBtn, "Sync Jobs");
          overlay.hidden = true;
          if (wasRunning) {
            if (data.last_result && data.last_result.error) {
              pill.textContent = "Sync failed";
              errorEl.textContent = data.last_result.error;
            } else {
              window.location.reload();
            }
          } else {
            pill.textContent = "Idle";
          }
        }
      });
    };

    syncBtn.addEventListener("click", function () {
      errorEl.textContent = "";
      setButtonLoading(syncBtn, "Syncing…");
      pill.textContent = "Syncing…";
      overlay.hidden = false;
      post(`/jobs/${source}/sync`).then(() => pollSync(true));
    });

    // In case a sync was already running when the page loaded
    pollSync(false);

    // Chrome throttles/suspends setTimeout in a backgrounded tab, so the
    // poll chain above can stall for as long as the tab stays hidden - if
    // the real sync actually finished while that happened, the overlay is
    // left showing "Syncing…" forever until something re-checks. Re-poll
    // immediately on return instead of waiting for a timer that may not
    // fire on schedule.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && !overlay.hidden) {
        pollSync(true);
      }
    });
  }

  // Gear-icon settings panel (every page, top nav) - keyboard mode and
  // which sources feed swipe mode. Persisted server-side (routes/
  // settings.py) rather than localStorage: this app is used from more
  // than one device (laptop, phone over tailscale), and a preference set
  // on one browser needs to show up on the other.
  const settingsBtn = document.getElementById("settings-btn");
  const settingsPanel = document.getElementById("settings-panel");
  if (settingsBtn && settingsPanel) {
    const keyboardCheckbox = document.getElementById("settings-keyboard-mode");
    const sourceCheckboxes = Array.from(document.querySelectorAll(".settings-source-checkbox"));
    const applyBtn = document.getElementById("settings-apply-btn");

    get("/settings").then((settings) => {
      keyboardCheckbox.checked = !!settings.keyboard_mode;
      // null (no filter saved yet) shows every source checked, matching
      // what the page itself is actually showing right now by default.
      const sources = settings.swipe_sources;
      sourceCheckboxes.forEach((cb) => {
        cb.checked = sources === null || sources.includes(cb.value);
      });
    });

    settingsBtn.addEventListener("click", () => {
      const open = settingsPanel.hidden;
      settingsPanel.hidden = !open;
      settingsBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", (e) => {
      if (settingsPanel.hidden) return;
      if (settingsPanel.contains(e.target) || settingsBtn.contains(e.target)) return;
      settingsPanel.hidden = true;
      settingsBtn.setAttribute("aria-expanded", "false");
    });

    applyBtn.addEventListener("click", () => {
      applyBtn.disabled = true;
      fetch("/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          keyboard_mode: keyboardCheckbox.checked,
          swipe_sources: sourceCheckboxes.filter((cb) => cb.checked).map((cb) => cb.value),
        }),
      }).then(() => window.location.reload());
    });
  }
})();
