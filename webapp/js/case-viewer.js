// Image inspection within the existing WebView. Centered gallery with bounded zoom; no fullscreen.
(() => {
  let active = null;
  function open(source, title, onClose, startIndex = 0, onChange) {
    const sources = (Array.isArray(source) ? source : [source]).filter(s => typeof s === 'string' && s);
    if (active || !sources.length) return false;
    let index = Math.max(0, Math.min(sources.length - 1, startIndex));
    const dialog = document.createElement('dialog');
    if (typeof dialog.showModal !== 'function') return false;
    dialog.className = 'case-viewer';
    dialog.setAttribute('aria-label', 'Просмотр изображения');
    dialog.innerHTML = `<div class="case-viewer-bar"><button type="button" data-close>← Закрыть фото</button><button type="button" data-zoom aria-pressed="false">Увеличить</button></div><div class="case-viewer-scroll" tabindex="0" aria-label="Изображение: прокрутка"><img alt="" draggable="false" decoding="async" /></div><div class="case-viewer-nav"><button type="button" data-prev aria-label="Предыдущее фото">←</button><span data-count aria-live="polite"></span><button type="button" data-next aria-label="Следующее фото">→</button></div><p class="case-viewer-status" role="status">Загрузка изображения…</p>`;
    const closeButton = dialog.querySelector('[data-close]');
    const zoomButton = dialog.querySelector('[data-zoom]');
    const scroller = dialog.querySelector('.case-viewer-scroll');
    const img = dialog.querySelector('img');
    const status = dialog.querySelector('[role=status]');
    const prev = dialog.querySelector('[data-prev]'), next = dialog.querySelector('[data-next]');
    const count = dialog.querySelector('[data-count]');
    dialog.querySelector('.case-viewer-nav').hidden = sources.length < 2;
    const origin = document.activeElement;
    const scrollX = window.scrollX, scrollY = window.scrollY;
    const savedStyle = document.body.getAttribute('style');
    const telegram = window.Telegram?.WebApp;
    const restoreSwipes = telegram?.isVerticalSwipesEnabled === true;
    let scale = 1;
    const pointers = new Map();
    let tap = null;
    let swipe = null;
    let offset = { x: 12, y: 12 };
    let lastTap = null;
    let lastTouchTime = -Infinity;
    let finished = false;
    let inputMode = window.matchMedia?.('(hover: hover) and (pointer: fine)').matches ? 'mouse' : 'touch';
    function hint(mode = inputMode) {
      inputMode = mode;
      if (zoomButton.disabled) return;
      status.textContent = mode === 'mouse'
        ? (scale > 1.01
          ? 'Перетаскивайте фото мышью или прокручивайте. Ctrl + колёсико — масштаб. Двойной щелчок — вписать.'
          : 'Двойной щелчок или Ctrl + колёсико — увеличить.' + (sources.length > 1 ? ' ← → — сменить фото.' : ''))
        : (scale > 1.01 ? 'Перемещайте фото пальцем. Сведите пальцы или нажмите дважды, чтобы уменьшить.'
          : (sources.length > 1 ? 'Листайте свайпом. ' : '') + 'Для увеличения разведите пальцы или нажмите дважды.');
      scroller.style.cursor = scale > 1.01 ? 'grab' : 'zoom-in';
    }
    function fit() {
      const availableWidth = Math.max(1, scroller.clientWidth - 24);
      const availableHeight = Math.max(1, scroller.clientHeight - 24);
      const ratio = img.naturalWidth && img.naturalHeight
        ? img.naturalHeight / img.naturalWidth : availableHeight / availableWidth;
      const width = Math.min(availableWidth, availableHeight / ratio) * scale;
      offset = { x: Math.max(12, (scroller.clientWidth - width) / 2),
                 y: Math.max(12, (scroller.clientHeight - width * ratio) / 2) };
      img.style.width = `${width}px`;
      img.style.margin = `${offset.y}px ${offset.x}px 12px`;
    }
    function select(nextIndex) {
      if (nextIndex < 0 || nextIndex >= sources.length) return;
      index = nextIndex; scale = 1; pointers.clear(); tap = swipe = lastTap = null;
      zoomButton.disabled = true; img.hidden = true;
      zoomButton.textContent = 'Увеличить'; zoomButton.setAttribute('aria-pressed', 'false');
      status.textContent = 'Загрузка изображения…';
      count.textContent = `${index + 1} / ${sources.length}`;
      prev.disabled = index === 0; next.disabled = index === sources.length - 1;
      img.alt = `${title || 'Изображение проекта'} — ${index + 1} из ${sources.length}`;
      img.src = sources[index];
      scroller.scrollTo(0, 0);
      onChange?.(index);
    }
    prev.addEventListener('click', () => select(index - 1));
    next.addEventListener('click', () => select(index + 1));
    dialog.addEventListener('keydown', event => {
      // Preserve browser/system shortcuts (including accessibility page zoom).
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const center = { x: scroller.clientWidth / 2, y: scroller.clientHeight / 2 };
      if (['+', '=', '-', '0'].includes(event.key) && !zoomButton.disabled) {
        event.preventDefault();
        zoom(event.key === '0' ? 1 : scale * (event.key === '-' ? 1 / 1.25 : 1.25), center);
        hint('mouse');
      } else if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
        if (scale > 1.01) {
          event.preventDefault();
          scroller.scrollTo(scroller.scrollLeft + (event.key === 'ArrowLeft' ? -80 : event.key === 'ArrowRight' ? 80 : 0),
                            scroller.scrollTop + (event.key === 'ArrowUp' ? -80 : event.key === 'ArrowDown' ? 80 : 0));
        } else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
          event.preventDefault(); select(index + (event.key === 'ArrowLeft' ? -1 : 1));
        }
      }
    });

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
      const x = (scroller.scrollLeft + anchor.x - offset.x) / previous;
      const y = (scroller.scrollTop + anchor.y - offset.y) / previous;
      fit();
      scroller.scrollTo(x * scale + offset.x - target.x, y * scale + offset.y - target.y);
      zoomButton.textContent = scale > 1.01 ? 'Уменьшить' : 'Увеличить';
      zoomButton.setAttribute('aria-pressed', String(scale > 1.01));
      hint();
    }
    function toggle(anchor) { zoom(scale > 1.01 ? 1 : 2, anchor); }
    zoomButton.addEventListener('click', () => {
      if (!zoomButton.disabled) toggle({ x: scroller.clientWidth / 2, y: scroller.clientHeight / 2 });
    });
    // Plain wheel/two-finger scrolling keeps native panning of long images.
    // Trackpad pinch is delivered as Ctrl+wheel by Chromium/Firefox WebViews.
    scroller.addEventListener('wheel', event => {
      if (!event.ctrlKey || !event.cancelable) return;
      event.preventDefault();
      if (zoomButton.disabled || pointers.size) return;
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? scroller.clientHeight : 1;
      const delta = Math.max(-120, Math.min(120, event.deltaY * unit));
      if (!Number.isFinite(delta)) return;
      zoom(scale * Math.exp(-delta * 0.005), point(event));
      hint('mouse');
    }, { passive: false });
    scroller.addEventListener('pointerdown', event => {
      if (zoomButton.disabled || (event.pointerType === 'mouse' && event.button !== 0)) return;
      if (event.pointerType !== 'mouse') lastTouchTime = event.timeStamp;
      hint(event.pointerType === 'mouse' ? 'mouse' : 'touch');
      if (scale > 1.01) scroller.style.cursor = 'grabbing';
      const p = point(event);
      pointers.set(event.pointerId, p);
      scroller.setPointerCapture(event.pointerId);
      tap = pointers.size === 1 ? { ...p, time: event.timeStamp } : null;
      swipe = pointers.size === 1 && scale <= 1.01 ? { ...p, time: event.timeStamp } : null;
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
      if (event.type === 'pointerup' && swipe && pointers.size === 1 && scale <= 1.01) {
        const p = point(event), dx = p.x - swipe.x, dy = p.y - swipe.y;
        if (Math.abs(dx) >= 55 && Math.abs(dx) > Math.abs(dy) * 1.5 && event.timeStamp - swipe.time < 900) {
          select(index + (dx < 0 ? 1 : -1));
          pointers.clear(); tap = swipe = lastTap = null;
          return;
        }
      }
      swipe = null;
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
      if (!pointers.size) hint();
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
      zoomButton.disabled = false; img.hidden = false; fit();
      hint();
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
    select(index);
    closeButton.focus();
    window.addEventListener('resize', fit);
    try { telegram?.onEvent?.('viewportChanged', fit); } catch (_) {}
    if (restoreSwipes) { try { telegram.disableVerticalSwipes(); } catch (_) {} }
    return true;
  }
  window.CaseViewer = Object.freeze({ open, close() { active?.close(); } });
})();
