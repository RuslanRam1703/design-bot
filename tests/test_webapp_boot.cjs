const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../webapp/js/boot.js'), 'utf8');

function emitter(object = {}) {
  const handlers = new Map();
  object.addEventListener = (name, fn) => { if (!handlers.has(name)) handlers.set(name, new Set()); handlers.get(name).add(fn); };
  object.removeEventListener = (name, fn) => handlers.get(name)?.delete(fn);
  object.emit = (name, event = {}) => { for (const fn of [...(handlers.get(name) || [])]) fn(event); };
  return object;
}
function harness(t, apiOrigin = "") {
  const storage = new Map();
  const nodes = Object.fromEntries(['app', 'boot-screen', 'boot-status', 'boot-detail', 'boot-retry'].map(id => [id,
    emitter({ textContent: '', hidden: true, classList: { add() {} }, remove() { this.removed = true; }, removeAttribute() {} })]));
  let frames = 0, afterPost;
  const window = emitter({ DESIGN_ASSISTANT_CONFIG: { apiOrigin }, location: { reload() {} } });
  const document = emitter({ hidden: false, documentElement: { classList: { remove() {}, toggle() {} } },
    getElementById: id => nodes[id], createElement: () => {
      frames++;
      const frame = emitter({ hidden: false, remove() { this.removed = true; } });
      frame.contentWindow = { postMessage: (value, origin) => afterPost?.(value, origin, frame) };
      return frame;
    }, body: { appendChild: frame => frame.emit('load') } });
  const context = { window, document, URL, AbortController, setTimeout, clearTimeout,
    localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
    fetch: async () => response(), console };
  vm.createContext(context);
  // Runtime API config is tested separately; run same-origin boot without automatic polling.
  vm.runInContext(source, context);
  const boot = window.DesignAssistantBoot;
  t.after(() => boot.finish());
  return { boot, window, document, nodes, storage, frames: () => frames, setAfterPost: fn => { afterPost = fn; } };
}
const response = (type = 'text/plain', body = 'ok', status = 200) => ({
  ok: status >= 200 && status < 300, headers: { get: () => type }, text: async () => body,
});

test('Render HTML with status 200 is never treated as readiness; warm-up uses bounded GET only', async t => {
  const { boot } = harness(t);
  let calls = 0, clock = 0;
  await boot.waitForBackend({ origin: 'https://api.example', now: () => clock,
    sleep: async ms => { clock += ms; }, fetchImpl: async (url, options) => {
      assert.equal(url, 'https://api.example/health');
      assert.equal(options.credentials, 'omit');
      assert.equal(options.cache, 'no-store');
      calls++;
      return calls < 3 ? response('text/html', '<html>Render loading</html>') : response();
    } });
  assert.equal(calls, 3);
});

test('timeout ends the retry loop and never retries a form submission', async t => {
  const { boot } = harness(t);
  let clock = 0, calls = 0;
  await assert.rejects(boot.waitForBackend({ origin: 'https://api.example', maxWaitMs: 5000,
    now: () => clock, sleep: async ms => { clock += ms; }, fetchImpl: async () => { calls++; return response('', '', 503); } }), /BOOT_TIMEOUT/);
  assert.equal(clock, 5000);
  assert.equal(calls, 3);
});

test('a hidden page makes no readiness requests until visible', async t => {
  const { boot, document } = harness(t);
  document.hidden = true;
  let calls = 0;
  const pending = boot.waitForBackend({ fetchImpl: async () => { calls++; return response(); } });
  await Promise.resolve();
  assert.equal(calls, 0);
  document.hidden = false;
  document.emit('visibilitychange');
  await pending;
  assert.equal(calls, 1);
});

test('closing the page cancels an active readiness request', async t => {
  const { boot, window } = harness(t);
  const pending = boot.waitForBackend({ fetchImpl: (url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(Error('aborted')));
  }) });
  await Promise.resolve();
  window.emit('pagehide');
  await assert.rejects(pending, /aborted|BOOT_ABORTED/);
});

test('successful rendering removes overlay and failure offers a manual retry', t => {
  const { boot, nodes } = harness(t);
  boot.fail('Network failed');
  assert.equal(nodes['boot-status'].textContent, 'Network failed');
  assert.equal(nodes['boot-retry'].hidden, false);
  boot.finish();
  assert.equal(nodes['boot-screen'].removed, true);
});

test('API URL construction cannot turn a path into an external credential destination', t => {
  const { boot } = harness(t);
  assert.equal(boot.apiUrl('/api/leads'), '/api/leads');
  assert.throws(() => boot.apiUrl('//evil.example'), /Invalid API path/);
  assert.throws(() => boot.apiUrl('https://evil.example'), /Invalid API path/);
});

test('draft migration leaves an existing new-origin draft untouched', async t => {
  const { boot, storage, frames } = harness(t, 'https://api.example');
  storage.set('designAssistant.briefDraft', '{"task":"Existing"}');
  await boot.migrateDraft();
  assert.equal(frames(), 0);
  assert.equal(storage.get('designAssistant.briefDraft'), '{"task":"Existing"}');
});


test('draft handoff accepts only the old origin and exact iframe window', async t => {
  const { boot, storage, window, setAfterPost } = harness(t, 'https://api.example');
  setAfterPost((request, origin, frame) => {
    assert.equal(origin, 'https://api.example');
    window.emit('message', {origin: 'https://evil.example', source: frame.contentWindow,
      data: {type: 'design-assistant:draft', value: '{"task":"Forged"}'}});
    assert.equal(storage.has('designAssistant.briefDraft'), false);
    window.emit('message', {origin, source: {},
      data: {type: 'design-assistant:draft', value: '{"task":"Forged"}'}});
    assert.equal(storage.has('designAssistant.briefDraft'), false);
    window.emit('message', {origin, source: frame.contentWindow,
      data: {type: 'design-assistant:draft', value: '{"task":"Original"}'}});
  });
  await boot.migrateDraft();
  assert.equal(storage.get('designAssistant.briefDraft'), '{"task":"Original"}');
});

test('blocked draft storage or a missing bridge never blocks application startup', async t => {
  const { boot, storage } = harness(t, 'https://api.example');
  await boot.migrateDraft({timeoutMs: 5});
  assert.equal(storage.has('designAssistant.briefDraft'), false);
  assert.equal(storage.has('designAssistant.draftMigration.v1'), false);
});
