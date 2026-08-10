// Логика расчёта — зеркало bot/calculator.py. Держите оба файла в синхронизации,
// если меняете формулу (см. data/pricing.json -> rounding/coefficients).

function calcServiceOptions(pricing, serviceId) {
  return pricing.options.filter((o) => o.service_id === serviceId);
}

// selectedOptions: { [optionId]: qty }
function calculatePrice(pricing, serviceId, selectedOptions, urgent, complex_) {
  const service = pricing.services.find((s) => s.id === serviceId);
  if (!service) return null;

  const options = calcServiceOptions(pricing, serviceId);
  let optionsPriceSum = 0;
  let optionsDaysSum = 0;
  const resolved = [];

  for (const opt of options) {
    const rawQty = selectedOptions[opt.id];
    if (!rawQty) continue;
    const qty = opt.multipliable ? Math.max(1, rawQty) : 1;
    optionsPriceSum += opt.price * qty;
    optionsDaysSum += opt.days * qty;
    resolved.push({ id: opt.id, name: opt.name, qty, price: opt.price, days: opt.days });
  }

  const urgentMult = urgent ? pricing.coefficients.urgent.multiplier : 1;
  const complexMult = complex_ ? pricing.coefficients.complex.multiplier : 1;

  const exact = (service.base_price + optionsPriceSum) * urgentMult * complexMult;

  const r = pricing.rounding;
  const priceFrom = Math.floor((exact * r.price_from_factor) / r.round_to) * r.round_to;
  const priceTo = Math.ceil((exact * r.price_to_factor) / r.round_to) * r.round_to;

  const termFrom = service.term_min + optionsDaysSum;
  const termTo = service.term_max + optionsDaysSum;

  return {
    service,
    exact,
    priceFrom,
    priceTo,
    termFrom,
    termTo,
    options: resolved,
  };
}

function formatMoney(n) {
  return n.toLocaleString("ru-RU") + " ₽";
}

function formatDays(n) {
  return Number.isInteger(n) ? String(n) : String(n).replace(".", ",");
}
