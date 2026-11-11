// Experimental image inspection. No fullscreen, custom pinch or swipe navigation.
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
    let enlarged = false;
    let finished = false;
    function fit() {
      const width = Math.max(1, scroller.clientWidth - 24);
      img.style.width = `${width * (enlarged ? 2 : 1)}px`;
      img.style.marginInline = enlarged ? '12px' : 'auto';
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
    zoomButton.addEventListener('click', () => {
      enlarged = !enlarged;
      zoomButton.textContent = enlarged ? 'Уменьшить' : 'Увеличить';
      zoomButton.setAttribute('aria-pressed', String(enlarged));
      status.textContent = enlarged ? 'Прокручивайте фото, чтобы рассмотреть детали' : 'Масштаб по ширине окна';
      const x = scroller.scrollLeft, y = scroller.scrollTop;
      fit();
      scroller.scrollTo(x * (enlarged ? 2 : .5), y * (enlarged ? 2 : .5));
    });
    zoomButton.disabled = true;
    img.addEventListener('load', () => {
      zoomButton.disabled = false;
      status.textContent = 'Масштаб по ширине окна';
    });
    img.addEventListener('error', () => {
      zoomButton.disabled = true;
      img.hidden = true;
      status.textContent = 'Не удалось загрузить фото. Закройте просмотр и попробуйте ещё раз.';
    });
    img.alt = title || 'Изображение проекта';
    document.body.append(dialog);
    document.body.style.cssText += `;position:fixed;top:${-scrollY}px;left:${-scrollX}px;width:100%;overflow:hidden;`;
    dialog.showModal();
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
