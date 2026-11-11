/* Tiny CDN bootstrap: no dependencies, no storage of Telegram credentials. */
(() => {
  "use strict";
  const rawOrigin = window.DESIGN_ASSISTANT_CONFIG?.apiOrigin || "";
  let apiOrigin = "";
  if (rawOrigin) {
    const parsed = new URL(rawOrigin);
    if (parsed.origin !== rawOrigin || parsed.username || parsed.password ||
        (parsed.protocol !== "https:" && !["localhost", "127.0.0.1"].includes(parsed.hostname))) {
      throw new Error("Invalid API origin");
    }
    apiOrigin = parsed.origin;
  }
  const root = document.getElementById("app");
  const overlay = document.getElementById("boot-screen");
  const status = document.getElementById("boot-status");
  const detail = document.getElementById("boot-detail");
  const retry = document.getElementById("boot-retry");
  const lifetime = new AbortController();
  let activeRequest = null;
  let finished = false;
  let backendReady = !apiOrigin;
  let slowTimer, deadlineTimer;

  function apiUrl(path) {
    if (!path.startsWith("/") || path.startsWith("//")) throw new Error("Invalid API path");
    return apiOrigin + path;
  }
  function delay(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) { reject(new Error("BOOT_ABORTED")); return; }
      const stop = () => { clearTimeout(timer); reject(new Error("BOOT_ABORTED")); };
      const timer = setTimeout(() => { signal.removeEventListener("abort", stop); resolve(); }, ms);
      signal.addEventListener("abort", stop, { once: true });
    });
  }
  function waitVisible(signal) {
    if (!document.hidden) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const cleanup = () => {
        document.removeEventListener("visibilitychange", visible);
        signal.removeEventListener("abort", stop);
      };
      const visible = () => { if (!document.hidden) { cleanup(); resolve(); } };
      const stop = () => { cleanup(); reject(new Error("BOOT_ABORTED")); };
      if (signal.aborted) { stop(); return; }
      document.addEventListener("visibilitychange", visible);
      signal.addEventListener("abort", stop, { once: true });
    });
  }
  async function waitForBackend({ origin = apiOrigin, fetchImpl = fetch, now = Date.now,
      sleep = delay, signal = lifetime.signal, maxWaitMs = 90000 } = {}) {
    const started = now();
    let attempts = 0;
    while (!signal.aborted && now() - started < maxWaitMs) {
      await waitVisible(signal);
      const remaining = maxWaitMs - (now() - started);
      if (remaining <= 0) break;
      if (typeof navigator !== "undefined" && navigator.onLine === false) throw new Error("OFFLINE");
      const request = new AbortController();
      activeRequest = request;
      const cancel = () => request.abort();
      signal.addEventListener("abort", cancel, { once: true });
      const timeout = setTimeout(cancel, Math.min(8000, remaining));
      try {
        // A Render wake-up HTML page may have HTTP 200. Validate the body,
        // never treat any successful status as backend readiness.
        const response = await fetchImpl(origin + "/health", {
          signal: request.signal, cache: "no-store", credentials: "omit",
        });
        if (response.ok && (response.headers.get("Content-Type") || "").startsWith("text/plain") &&
            (await response.text()).trim() === "ok") return;
      } catch (error) {
        if (signal.aborted) throw error;
      } finally {
        clearTimeout(timeout);
        signal.removeEventListener("abort", cancel);
        if (activeRequest === request) activeRequest = null;
      }
      const remainingAfterRequest = maxWaitMs - (now() - started);
      if (remainingAfterRequest <= 0) break;
      await sleep(Math.min(1500 + attempts++ * 750, 5000, remainingAfterRequest), signal);
    }
    throw new Error(signal.aborted ? "BOOT_ABORTED" : "BOOT_TIMEOUT");
  }
  function migrateDraft({ timeoutMs = 1500 } = {}) {
    const key = "designAssistant.briefDraft";
    const marker = "designAssistant.draftMigration.v1";
    try {
      if (!apiOrigin || localStorage.getItem(key) || localStorage.getItem(marker)) return Promise.resolve();
    } catch (_) { return Promise.resolve(); }
    return new Promise(resolve => {
      const frame = document.createElement("iframe");
      frame.hidden = true;
      frame.title = "";
      frame.referrerPolicy = "no-referrer";
      frame.src = apiUrl("/draft-bridge");
      const done = () => {
        clearTimeout(timer);
        window.removeEventListener("message", receive);
        lifetime.signal.removeEventListener("abort", done);
        frame.remove();
        resolve();
      };
      const receive = event => {
        if (event.origin !== apiOrigin || event.source !== frame.contentWindow ||
            event.data?.type !== "design-assistant:draft") return;
        try {
          const value = event.data.value;
          if (typeof value === "string" && value.length <= 65536 && !localStorage.getItem(key)) {
            const draft = JSON.parse(value);
            if (draft && typeof draft === "object" && !Array.isArray(draft)) localStorage.setItem(key, value);
          }
          localStorage.setItem(marker, "1");
        } catch (_) { /* Old copy remains intact if storage is unavailable. */ }
        done();
      };
      frame.addEventListener("load", () => {
        frame.contentWindow.postMessage({ type: "design-assistant:read-draft" }, apiOrigin);
      }, { once: true });
      const timer = setTimeout(done, timeoutMs);
      window.addEventListener("message", receive);
      lifetime.signal.addEventListener("abort", done, { once: true });
      document.body.appendChild(frame);
    });
  }
  function cleanup() {
    clearTimeout(slowTimer);
    clearTimeout(deadlineTimer);
    lifetime.abort();
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("pagehide", onPageHide);
  }
  function fail(message, canRetry = true) {
    if (finished) return;
    cleanup();
    overlay?.classList.add("boot-error");
    if (status) status.textContent = message;
    if (detail) detail.textContent = canRetry ? "Проверьте соединение и попробуйте ещё раз." : "";
    if (retry) retry.hidden = !canRetry;
  }
  function finish() {
    if (finished) return;
    finished = true;
    cleanup();
    if (overlay) overlay.remove();
    document.documentElement.classList.remove("booting", "page-hidden");
    if (root) { root.removeAttribute("aria-hidden"); root.removeAttribute("aria-busy"); }
  }
  function onVisibility() {
    document.documentElement.classList.toggle("page-hidden", document.hidden);
    if (document.hidden) activeRequest?.abort();
  }
  function onPageHide() { cleanup(); }
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", onPageHide, { once: true });
  onVisibility();
  // Browsers may restore a page from bfcache after pagehide aborted startup.
  window.addEventListener("pageshow", event => { if (event.persisted && !finished) window.location.reload(); });
  retry?.addEventListener("click", () => window.location.reload());
  slowTimer = setTimeout(() => {
    if (detail && !finished) detail.textContent = backendReady ? "Подготавливаем содержимое. Ещё немного…" : "Запускаем сервер после перерыва. Это может занять около минуты.";
  }, 8000);
  deadlineTimer = setTimeout(() => fail("Сервер пока не отвечает."), 90000);
  const ready = apiOrigin ? waitForBackend() : Promise.resolve();
  ready.then(() => { backendReady = true; }, () => {});
  // Attach immediately: the SDK may load later than the backend promise.
  ready.catch(error => {
    if (!lifetime.signal.aborted) fail(error.message === "OFFLINE" ? "Нет подключения к интернету." : "Сервер пока не отвечает.");
  });
  window.DesignAssistantBoot = Object.freeze({ apiUrl, ready, finish, fail, waitForBackend, migrateDraft });
})();
