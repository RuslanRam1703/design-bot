// Image inspection within the existing WebView. No fullscreen or slide navigation.
(() => {
  let active = null;
  function open(source, title, onClose) {
    if (active || !source) return false;
    const dialog = document.createElement('dialog');
    if (typeof dialog.showModal !== 'function') return false;
    dialog.className = 'case-viewer';
    dialog.setAttribute('aria-label', 'Просмотр изображения');
    dialog.innerHTML = `<div class="case-viewer-bar"><button type="button" data-close>← Закрыть фото</button><button type="button" data-zoom aria-pressed="false">Увеличить</button></div><div class="case-viewer-scroll" tabindex="0" aria-label="Изображение: прокрутка"><img alt="" draggable="false" decoding="async" /></div><p class="case-viewer-status" role="status">Загрузка изображения…</p>`;
    const closeButton = dialog.querySelector('[data-close]');
    const zoomButton = dialog.querySelector('[data-zoom]');
    const scroller = dialog.querySelector('.case-viewer-scroll');
    const img = dialog.querySelector('img');
    const status = dialog.querySelector('[role=status]');
    const origin = document.activeElement;
    const scrollX = window.scrollX, scrollY = window.scrollY;
    const savedStyle = document.body.getAttribute('style');
    const telegram = window.Telegram?.WebApp;
    const restoreSwipes = telegram?.isVerticalSwipesEnabled === true;
    let scale = 1;
    const pointers = new Map();
    let tap = null;
    let lastTap = null;
    let lastTouchTime = -Infinity;
    let finished = false;
    function fit() {
      const width = Math.max(1, scroller.clientWidth - 24);
      img.style.width = `${width * scale}px`;
      img.style.marginInline = '12px';
    }
    function close() {
      if (finished) return;
      finished = true;
      window.removeEventListener('resize', fit);
      try { telegram?.offEvent?.('viewportChanged', fit); } catch (_) {}
      if (restoreSwipes) { try { telegram.enableVerticalSwipes(); } catch (_) {} }
      dialog.close();
      dialog.remove();
      if (savedStyle === null) document.body.removeAttribute('style');
      else document.body.setAttribute('style', savedStyle);
      window.scrollTo(scrollX, scrollY);
      active = null;
      if (origin?.isConnected) origin.focus({ preventScroll: true });
      onClose?.();
    }
    closeButton.addEventListener('click', close);
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    function point(event) {
      const rect = scroller.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }
    function zoom(next, anchor, target = anchor) {
      const previous = scale;
      scale = Math.max(1, Math.min(4, next));
      const x = (scroller.scrollLeft + anchor.x - 12) / previous;
      const y = (scroller.scrollTop + anchor.y - 12) / previous;
      fit();
      scroller.scrollTo(x * scale + 12 - target.x, y * scale + 12 - target.y);
      zoomButton.textContent = scale > 1.01 ? 'Уменьшить' : 'Увеличить';
      zoomButton.setAttribute('aria-pressed', String(scale > 1.01));
    }
    function toggle(anchor) { zoom(scale > 1.01 ? 1 : 2, anchor); }
    zoomButton.addEventListener('click', () => {
      if (!zoomButton.disabled) toggle({ x: scroller.clientWidth / 2, y: scroller.clientHeight / 2 });
    });
    scroller.addEventListener('pointerdown', event => {
      if (zoomButton.disabled || (event.pointerType === 'mouse' && event.button !== 0)) return;
      if (event.pointerType !== 'mouse') lastTouchTime = event.timeStamp;
      const p = point(event);
      pointers.set(event.pointerId, p);
      scroller.setPointerCapture(event.pointerId);
      tap = pointers.size === 1 ? { ...p, time: event.timeStamp } : null;
      if (pointers.size > 1) lastTap = null;
    });
    const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
    const middle = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
    scroller.addEventListener('pointermove', event => {
      const previous = pointers.get(event.pointerId);
      if (!previous) return;
      const oldPoints = [...pointers.values()];
      const next = point(event);
      pointers.set(event.pointerId, next);
      if (tap && distance(tap, next) > 8) { tap = null; lastTap = null; }
      if (pointers.size === 1) {
        scroller.scrollTo(scroller.scrollLeft + previous.x - next.x,
                          scroller.scrollTop + previous.y - next.y);
      } else if (pointers.size === 2) {
        const newPoints = [...pointers.values()];
        const before = distance(...oldPoints);
        if (before > 4) zoom(scale * distance(...newPoints) / before,
                            middle(...oldPoints), middle(...newPoints));
      }
    });
    function release(event) {
      if (!pointers.has(event.pointerId)) return;
      if (event.type === 'pointerup' && event.pointerType !== 'mouse' && tap &&
          pointers.size === 1 && event.timeStamp - tap.time < 300) {
        const p = point(event);
        if (distance(tap, p) <= 8) {
          if (lastTap && event.timeStamp - lastTap.time < 350 && distance(lastTap, p) < 35) {
            toggle(p); lastTap = null;
          } else lastTap = { ...p, time: event.timeStamp };
        }
      } else lastTap = null;
      pointers.delete(event.pointerId);
      tap = null;
    }
    scroller.addEventListener('pointerup', release);
    scroller.addEventListener('pointercancel', release);
    scroller.addEventListener('lostpointercapture', release);
    scroller.addEventListener('dblclick', event => {
      if (!zoomButton.disabled && event.timeStamp - lastTouchTime > 500 &&
          (!event.pointerType || event.pointerType === 'mouse')) toggle(point(event));
    });
    zoomButton.disabled = true;
    img.addEventListener('load', () => {
      zoomButton.disabled = false;
      status.textContent = 'Разведите пальцы или нажмите «Увеличить». Перемещайте фото для просмотра.';
    });
    img.addEventListener('error', () => {
      zoomButton.disabled = true;
      img.hidden = true;
      status.textContent = 'Не удалось загрузить фото. Закройте просмотр и попробуйте ещё раз.';
    });
    img.alt = title || 'Изображение проекта';
    document.body.append(dialog);
    document.body.style.cssText += `;position:fixed;top:${-scrollY}px;left:${-scrollX}px;width:100%;overflow:hidden;`;
    try { dialog.showModal(); } catch (_) {
      dialog.remove();
      if (savedStyle === null) document.body.removeAttribute('style');
      else document.body.setAttribute('style', savedStyle);
      window.scrollTo(scrollX, scrollY);
      return false;
    }
    active = { close };
    fit();
    img.src = source;
    closeButton.focus();
    window.addEventListener('resize', fit);
    try { telegram?.onEvent?.('viewportChanged', fit); } catch (_) {}
    if (restoreSwipes) { try { telegram.disableVerticalSwipes(); } catch (_) {} }
    return true;
  }
  window.CaseViewer = Object.freeze({ open, close() { active?.close(); } });
})();
