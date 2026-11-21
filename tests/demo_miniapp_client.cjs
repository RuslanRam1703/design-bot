const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function run({ origin, initData }) {
  const root = { dataset: {}, innerHTML: '' };
  const context = vm.createContext({
    window: { Telegram: { WebApp: { initData, BackButton: { hide() {} } } }, scrollTo() {} },
    document: {
      addEventListener() {}, querySelectorAll() { return []; },
      getElementById(id) { return id === 'app' ? root : null; },
    },
    localStorage: { setItem() {}, removeItem() {} },
    fetch: (url, options) => fetch(new URL(url, origin), options),
    AbortSignal,
  });
  const script = name => fs.readFileSync(path.join(__dirname, '../webapp/js', name), 'utf8');
  vm.runInContext(script('calculator.js'), context);
  vm.runInContext(script('app.js').replace(/\ninit\(\);\s*$/, ''), context);
  const get = key => fetch(new URL('/data/' + key + '.json', origin), {
    headers: { 'X-Telegram-Init-Data': initData },
  }).then(response => {
    assert.equal(response.status, 200);
    return response.json();
  });
  context.seed = { portfolio: await get('portfolio'), pricing: await get('pricing') };
  const result = await vm.runInContext(`
    (async () => {
      Object.assign(state, seed);
      state.screen = "portfolio";
      render();
      const project = state.portfolio.cases.find(item => item.id === "case_1");
      const service = state.pricing.services.find(item => item.id === project.related_service);
      const options = project.order_template.options.map(option => ({ id: option.option_id, qty: option.quantity }));
      const estimate = calculatePrice(state.pricing, service.id,
        Object.fromEntries(options.map(option => [option.id, option.qty])), true, false);
      Object.assign(state.brief, {
        serviceId: service.id, serviceName: service.name, task: "Демо-заявка на сайт кофейни",
        name: "Демо", contactValue: "@demo", have: ["references"],
        tzMode: "file", deadline: "asap", budget: "40-70",
        source: "case", sourceCaseId: project.id, sourceCaseTitle: project.title,
        calc: { service_id: service.id, options, urgent: true, complex: false },
        draftId: "demo-smoke-brief",
      });
      await submitBrief();
      if (!state.lastLeadResult) throw Error(state.briefSubmitError);
      return { result: state.lastLeadResult, estimate };
    })()
  `, context);
  assert.match(root.innerHTML, /Заявка/);
  assert.deepEqual(JSON.parse(JSON.stringify(result.result.price_range)), {
    from: result.estimate.priceFrom, to: result.estimate.priceTo,
  });
  return result;
}

const config = JSON.parse(fs.readFileSync(0, 'utf8'));
run(config).then(result => process.stdout.write(JSON.stringify(result))).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
