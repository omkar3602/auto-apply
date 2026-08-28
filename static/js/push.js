(function () {
  const btn = document.getElementById("notif-btn");
  if (!btn) return;

  // Same gate base.html uses before registering the service worker at all -
  // there's nothing to subscribe to on localhost or without SW/Push support.
  const isLocal = ["localhost", "127.0.0.1"].includes(location.hostname);
  if (isLocal || !("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    return;
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(base64);
    const output = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i);
    return output;
  }

  function setState(subscribed) {
    btn.textContent = subscribed ? "🔔" : "🔕";
    btn.title = subscribed ? "Daily apply reminders on - click to turn off" : "Enable daily apply reminders";
    btn.dataset.subscribed = subscribed ? "1" : "0";
  }

  function refreshState() {
    navigator.serviceWorker.ready
      .then((reg) => reg.pushManager.getSubscription())
      .then((sub) => {
        setState(!!sub);
        btn.hidden = false;
      })
      .catch(() => {});
  }

  function subscribe() {
    navigator.serviceWorker.ready
      .then((reg) =>
        fetch("/push/vapid-public-key")
          .then((r) => r.json())
          .then(({ key }) =>
            reg.pushManager.subscribe({
              userVisibleOnly: true,
              applicationServerKey: urlBase64ToUint8Array(key),
            })
          )
      )
      .then((sub) =>
        fetch("/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(sub.toJSON()),
        })
      )
      .then(() => setState(true))
      .catch((err) => {
        console.error("push subscribe failed", err);
        alert("Couldn't enable reminders: " + (err && err.message ? err.message : err));
      });
  }

  function unsubscribe() {
    navigator.serviceWorker.ready
      .then((reg) => reg.pushManager.getSubscription())
      .then((sub) => {
        if (!sub) return;
        const endpoint = sub.endpoint;
        return sub
          .unsubscribe()
          .then(() =>
            fetch("/push/unsubscribe", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ endpoint }),
            })
          );
      })
      .then(() => setState(false))
      .catch((err) => console.error("push unsubscribe failed", err));
  }

  btn.addEventListener("click", () => {
    if (btn.dataset.subscribed === "1") {
      unsubscribe();
      return;
    }
    if (Notification.permission === "denied") {
      alert("Notifications are blocked for this app in your browser/OS settings.");
      return;
    }
    Notification.requestPermission().then((perm) => {
      if (perm === "granted") subscribe();
    });
  });

  refreshState();
})();
