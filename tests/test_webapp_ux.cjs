// JavaScript UX regression tests run without Telegram or a browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../webapp/js/app.js'), 'utf8');
function appWithDraft(saved = null) {
  const storage = { getItem: () => saved, setItem() {}, removeItem() {} };
  const sandbox = {
    window: { Telegram: { WebApp: { initDataUnsafe: { user: { id: 31, first_name: 'Клиент', username: 'client' } } } } },
    document: { addEventListener() {} },
    localStorage: storage,
    console,
    setTimeout,
    clearTimeout,
  };
  vm.createContext(sandbox);
  const stripped = source.replace(/\ninit\(\);\s*$/, '\n');
  assert.notEqual(stripped, source, 'must skip only the final Mini App bootstrap');
  vm.runInContext(stripped + '\nglobalThis.__ui = { state, restoreBriefDraft, renderBrief };', sandbox);
  return sandbox.__ui;
}

test('saved legacy complexity flag is removed before an order can be rendered', () => {
  const old = JSON.stringify({ step: 3, serviceId: 'landing', complex: true, calc: { service_id: 'landing', complex: true } });
  const { state, restoreBriefDraft } = appWithDraft(old);
  assert.equal(restoreBriefDraft(), true);
  assert.equal(state.brief.complex, false);
  assert.equal(state.brief.calc.complex, false);
});

test('what-is-ready step is optional without adding a new stage', () => {
  const { state, renderBrief } = appWithDraft();
  state.brief.step = 3;
  state.brief.have = [];
  const html = renderBrief();
  assert.match(html, /Необязательный шаг/);
  assert.match(html, /id="brief-next">Пропустить/);
  assert.doesNotMatch(html, /id="brief-next" disabled/);
});

test('Telegram contact autofill is optional and keeps editable fields', () => {
  const { state, renderBrief } = appWithDraft();
  state.brief.step = 6;
  const html = renderBrief();
  assert.match(html, /id="use-telegram-contact"/);
  assert.match(html, /id="contact-name"/);
  assert.match(html, /id="contact-value"/);
});

test('final review escapes user fields and makes pricing provisional', () => {
  const { state, renderBrief } = appWithDraft();
  Object.assign(state.brief, {
    step: 7, serviceName: 'Сайт', task: '<b> & текст',
    deadline: 'month', budget: '20-40', name: 'Имя',
    contactValue: '@client', tzMode: 'none',
  });
  const html = renderBrief();
  assert.match(html, /Проверка перед отправкой/);
  assert.match(html, /&lt;b&gt; &amp; текст/);
  assert.doesNotMatch(html, /<b> & текст/);
  assert.match(html, /не обязательство оплатить заказ/);
});

test('customer cannot choose professional complexity in either public surface', () => {
  assert.doesNotMatch(source, /id="complex-toggle"/);
  assert.doesNotMatch(source, /id="order-complex-toggle"/);
  assert.match(source, /complex: false/);
});

test('blocked Telegram account closes Mini App before content is rendered', async () => {
  let closed = 0;
  const element = { textContent: '' };
  const fakeTG = {
    initData: 'signed-data',
    ready() {}, expand() {}, themeParams: {}, colorScheme: 'light',
    onEvent() {}, close() { closed += 1; },
  };
  const context = {
    window: { Telegram: { WebApp: fakeTG } },
    document: {
      addEventListener() {},
      documentElement: { style: { setProperty() {} }, dataset: {} },
      getElementById() { return element; },
    },
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    fetch: async () => ({ status: 403, ok: false }),
    console, setTimeout, clearTimeout,
  };
  vm.createContext(context);
  const sourceWithoutBootstrap = source.replace(/\ninit\(\);\s*$/, '\n');
  vm.runInContext(sourceWithoutBootstrap + '\nglobalThis.__initApp = init;', context);
  await context.__initApp();
  assert.equal(closed, 1);
  assert.match(element.textContent, /Доступ к этому приложению ограничен/);
});
