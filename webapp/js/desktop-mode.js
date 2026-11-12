/* Optional Telegram window control. No device-based layout or automatic fullscreen. */
(function (root) {
  let bound = false;
  let failed = false;
  function attach(tg, doc) {
    if (!tg || !['tdesktop', 'macos', 'web', 'weba', 'webk'].includes(tg.platform) ||
        !tg.isVersionAtLeast?.('8.0') || typeof tg.requestFullscreen !== 'function') return;
    const header = doc.querySelector('.brand-header');
    if (!header) return;
    const box = doc.createElement('div');
    box.className = 'desktop-window-controls';
    const button = doc.createElement('button');
    button.type = 'button';
    const hint = doc.createElement('p');
    hint.className = 'hint'; hint.setAttribute('role', 'status');
    box.append(button, hint); header.append(box);
    function refresh() {
      const current = doc.querySelector('.desktop-window-controls');
      if (!current) return;
      const btn = current.querySelector('button');
      btn.textContent = tg.isFullscreen ? 'Свернуть экран' : 'На весь экран';
      btn.disabled = failed;
      current.querySelector('p').textContent = failed ? 'Telegram не поддержал разворачивание. Можно продолжить в этом окне.' : '';
    }
    button.addEventListener('click', () => {
      try { tg.isFullscreen ? tg.exitFullscreen() : tg.requestFullscreen(); }
      catch (_) { failed = true; refresh(); }
    });
    if (!bound) {
      tg.onEvent?.('fullscreenChanged', refresh);
      tg.onEvent?.('fullscreenFailed', () => { failed = true; refresh(); });
      bound = true;
    }
    refresh();
  }
  root.DesktopMode = { attach };
})(window);
