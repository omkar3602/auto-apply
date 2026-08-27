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
  }
})();
