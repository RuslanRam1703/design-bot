// JavaScript UX regression tests run without Telegram or a browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../webapp/js/app.js'), 'utf8');
function appWithDraft(saved = null, document = { addEventListener() {} }) {
  const storage = { getItem: () => saved, setItem() {}, removeItem() {} };
  const sandbox = {
    window: { Telegram: { WebApp: { initDataUnsafe: { user: { id: 31, first_name: 'Клиент', username: 'client' } } } } },
    document,
    localStorage: storage,
    console,
    setTimeout,
    clearTimeout,
  };
  vm.createContext(sandbox);
  const stripped = source.replace(/\ninit\(\);\s*$/, '\n');
  assert.notEqual(stripped, source, 'must skip only the final Mini App bootstrap');
  vm.runInContext(stripped + '\nglobalThis.__ui = { state, restoreBriefDraft, renderBrief, renderMyLeadDetail, renderMyLeads, renderPortfolio, attachPortfolioEvents, renderTabBar };', sandbox);
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
    console, setTimeout, clearTimeout, AbortSignal,
  };
  vm.createContext(context);
  const sourceWithoutBootstrap = source.replace(/\ninit\(\);\s*$/, '\n');
  vm.runInContext(sourceWithoutBootstrap + '\nglobalThis.__initApp = init;', context);
  await context.__initApp();
  assert.equal(closed, 1);
  assert.match(element.textContent, /Доступ к этому приложению ограничен/);
});


test('my lead detail renders saved incoming messages without injecting HTML', () => {
  const { renderMyLeadDetail } = appWithDraft();
  const html = renderMyLeadDetail({ id: 7, status: 'NEW', created_at: '2026-11-11T01:00:00',
    payload: {}, client_messages: [{ kind: 'text', text: '<script>alert(1)</script>', received_at: '2026-11-11' }] });
  assert.match(html, /Ваши сообщения \(1\)/);
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;\/script&gt;'));
  assert.ok(!html.includes('<script>'));
});


test('my leads loading failures have a direct retry action', () => {
  const { state, renderMyLeads } = appWithDraft();
  state.myLeads.status = 'error';
  assert.match(renderMyLeads(), /id="my-leads-retry"/);
});


test('portfolio presentation preserves filtering and escapes project labels', () => {
  const { state, renderPortfolio } = appWithDraft();
  state.portfolio = {types: [{id: 'web', label: '<b>Web'}], cases: [
    {id: 'one', type: 'web', title: '<script>One', featured: true},
    {id: 'two', type: 'brand', title: 'Two'},
  ]};
  state.uiConfig = {menu: {brief: false}};
  state.filter = 'web';
  const html = renderPortfolio();
  assert.match(html, /1 проект/);
  assert.match(html, /data-open-case="one"/);
  assert.doesNotMatch(html, /data-open-case="two"|data-preview-case/);
  assert.doesNotMatch(html, /id="portfolio-order"/);
  assert.ok(html.includes('&lt;script&gt;One'));
  assert.ok(html.includes('&lt;b&gt;Web'));
});

test('thumbnail taps update the preview without navigating or reordering cards', () => {
  function element(properties = {}) {
    return {
      ...properties,
      addEventListener(event, listener) { this[event] = listener; },
      setAttribute(name, value) { this[name] = value; },
    };
  }
  const preview = element({ getBoundingClientRect: () => ({ top: 10 }) });
  const first = element({ dataset: { previewCase: 'one' } });
  const second = element({ dataset: { previewCase: 'two' } });
  const document = {
    addEventListener() {},
    getElementById: id => id === 'portfolio-preview' ? preview : null,
    querySelectorAll: selector => selector === '[data-preview-case]' ? [first, second] : [],
  };
  const { state, renderPortfolio, attachPortfolioEvents } = appWithDraft(null, document);
  state.screen = 'portfolio';
  state.portfolio = {types: [], cases: [
    {id: 'one', type: 'web', title: 'One', featured: true},
    {id: 'two', type: 'brand', title: 'Two'},
  ]};
  assert.match(renderPortfolio(), /data-open-case="one"/);
  attachPortfolioEvents();
  second.click();
  assert.equal(state.screen, 'portfolio');
  assert.equal(state.history.length, 0);
  assert.match(preview.innerHTML, /data-open-case="two"/);
  assert.equal(first['aria-pressed'], 'false');
  assert.equal(second['aria-pressed'], 'true');
  assert.match(renderPortfolio(), /data-preview-case="one"[\s\S]*data-preview-case="two"/);
  state.filter = 'web';
  assert.match(renderPortfolio(), /data-open-case="one"/);
});

test('brand navigation retains settings and exposes the current page accessibly', () => {
  const { state, renderTabBar } = appWithDraft();
  state.uiConfig = {menu: {about: false}};
  state.screen = 'brief';
  const html = renderTabBar();
  assert.doesNotMatch(html, /data-tab="about"/);
  assert.match(html, /data-tab="brief" aria-current="page"/);
  assert.match(html, /aria-label="Разделы приложения"/);
  assert.match(html, /<svg/);
});
