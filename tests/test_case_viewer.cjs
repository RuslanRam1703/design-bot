// Lifecycle tests only: real WebView layout/gestures require browser acceptance.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../webapp/js/case-viewer.js'), 'utf8');
function setup({swipes = true, supported = true} = {}) {
  class Element {
    constructor() { this.attrs = {}; this.style = {cssText: ''}; this.events = {}; this.clientWidth = 390; this.scrollLeft = 0; this.scrollTop = 0; }
    setAttribute(k, v) { this.attrs[k] = v; }
    getAttribute(k) { return this.attrs[k] ?? null; }
    removeAttribute(k) { delete this.attrs[k]; }
    addEventListener(k, f) { this.events[k] = f; }
    fire(k) { this.events[k]?.({preventDefault() {}}); }
    append(el) { this.child = el; }
    focus() { this.focused = true; }
    remove() { this.removed = true; }
    scrollTo(x, y) { this.scrollLeft = x; this.scrollTop = y; }
  }
  const body = new Element(), origin = new Element(); origin.isConnected = true;
  body.setAttribute('style', 'color: red');
  const dialogs = [], calls = {disable: 0, enable: 0, closed: 0, scroll: [], listeners: new Set()};
  const document = {body, activeElement: origin, createElement() {
    const el = new Element();
    const parts = Object.fromEntries(['[data-close]', '[data-zoom]', '.case-viewer-scroll', 'img', '[role=status]'].map(k => [k, new Element()]));
    el.querySelector = key => parts[key];
    if (supported) el.showModal = () => { el.open = true; };
    el.close = () => { el.open = false; };
    dialogs.push(el); return el;
  }};
  const window = {scrollX: 0, scrollY: 420,
    scrollTo: (x, y) => calls.scroll.push([x, y]),
    addEventListener: k => calls.listeners.add(k), removeEventListener: k => calls.listeners.delete(k),
    Telegram: {WebApp: {isVerticalSwipesEnabled: swipes,
      disableVerticalSwipes() { calls.disable++; }, enableVerticalSwipes() { calls.enable++; },
      onEvent: k => calls.listeners.add(k), offEvent: k => calls.listeners.delete(k),
    }},
  };
  vm.runInNewContext(source, {window, document});
  return {viewer: window.CaseViewer, body, origin, dialogs, calls,
    open: () => window.CaseViewer.open('https://example.invalid/photo.jpg', 'Test', () => calls.closed++)};
}

test('close restores body style, document scroll, focus and Telegram gestures', () => {
  const s = setup(); s.open(); const d = s.dialogs[0];
  assert.equal(d.open, true); assert.equal(s.calls.disable, 1);
  d.querySelector('[data-close]').fire('click');
  assert.equal(d.removed, true); assert.equal(s.body.getAttribute('style'), 'color: red');
  assert.deepEqual(s.calls.scroll, [[0, 420]]); assert.equal(s.origin.focused, true);
  assert.equal(s.calls.enable, 1); assert.equal(s.calls.closed, 1); assert.equal(s.calls.listeners.size, 0);
  s.viewer.close(); assert.equal(s.calls.closed, 1);
});
test('zoom changes only image width and can be reset without closing', () => {
  const s = setup(); s.open(); const d = s.dialogs[0], img = d.querySelector('img'), zoom = d.querySelector('[data-zoom]');
  assert.equal(zoom.disabled, true); img.fire('load'); assert.equal(zoom.disabled, false);
  assert.equal(img.style.width, '366px'); zoom.fire('click'); assert.equal(img.style.width, '732px');
  assert.equal(zoom.textContent, 'Уменьшить'); zoom.fire('click'); assert.equal(img.style.width, '366px');
  assert.equal(d.open, true);
});
test('Escape cancellation and repeated opening do not accumulate dialogs', () => {
  const s = setup(); s.open(); s.open(); assert.equal(s.dialogs.length, 1);
  s.dialogs[0].fire('cancel'); assert.equal(s.calls.closed, 1);
  s.open(); assert.equal(s.dialogs.length, 2); s.viewer.close(); assert.equal(s.calls.closed, 2);
});
test('previously disabled Telegram swipes are left disabled', () => {
  const s = setup({swipes: false}); s.open(); s.viewer.close();
  assert.equal(s.calls.disable, 0); assert.equal(s.calls.enable, 0);
});
test('failed image leaves a usable close control and explains the failure', () => {
  const s = setup(); s.open(); const d = s.dialogs[0]; d.querySelector('img').fire('error');
  assert.equal(d.querySelector('[data-zoom]').disabled, true);
  assert.match(d.querySelector('[role=status]').textContent, /Не удалось загрузить/);
  d.querySelector('[data-close]').fire('click'); assert.equal(s.calls.closed, 1);
});
test('unsupported dialog API leaves the page untouched', () => {
  const s = setup({supported: false}); s.open();
  assert.equal(s.body.child, undefined); assert.equal(s.calls.disable, 0);
});
