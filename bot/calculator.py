"""Серверный (авторитетный) пересчёт стоимости — та же логика, что в webapp/js/calculator.js.

Пересчитываем на сервере, а не доверяем цифрам, присланным из Mini App, чтобы
уведомление дизайнеру всегда содержало корректный расчёт даже при ручном
вмешательстве в данные на клиенте.
"""

import math
from dataclasses import dataclass, field

from bot.data import get_options_for_service, get_service


@dataclass
class CalcResult:
    service_id: str
    service_name: str
    exact_price: float
    price_from: int
    price_to: int
    term_from: float
    term_to: float
    urgent: bool
    complex_: bool
    selected_options: list[dict] = field(default_factory=list)
    valid: bool = True
    error: str | None = None


def calculate(pricing: dict, service_id: str, selected: list[dict], urgent: bool, complex_: bool) -> CalcResult:
    """selected: [{"id": "LEND_2", "qty": 2}, ...] — qty игнорируется для не-множащихся опций (считается как 1)."""
    service = get_service(pricing, service_id)
    if service is None:
        return CalcResult(
            service_id=service_id, service_name="", exact_price=0, price_from=0, price_to=0,
            term_from=0, term_to=0, urgent=urgent, complex_=complex_, valid=False,
            error=f"Неизвестная услуга: {service_id}",
        )

    available = {o["id"]: o for o in get_options_for_service(pricing, service_id)}

    options_price_sum = 0.0
    options_days_sum = 0.0
    resolved_options = []
    for item in selected:
        opt = available.get(item.get("id"))
        if opt is None:
            continue
        qty = item.get("qty", 1) if opt.get("multipliable") else 1
        qty = max(1, int(qty))
        options_price_sum += opt["price"] * qty
        options_days_sum += opt["days"] * qty
        resolved_options.append({"id": opt["id"], "name": opt["name"], "qty": qty, "price": opt["price"], "days": opt["days"]})

    coefficients = pricing["coefficients"]
    urgent_mult = coefficients["urgent"]["multiplier"] if urgent else 1
    complex_mult = coefficients["complex"]["multiplier"] if complex_ else 1

    exact_price = (service["base_price"] + options_price_sum) * urgent_mult * complex_mult

    rounding = pricing["rounding"]
    round_to = rounding["round_to"]
    price_from = math.floor(exact_price * rounding["price_from_factor"] / round_to) * round_to
    price_to = math.ceil(exact_price * rounding["price_to_factor"] / round_to) * round_to

    term_from = service["term_min"] + options_days_sum
    term_to = service["term_max"] + options_days_sum

    return CalcResult(
        service_id=service_id,
        service_name=service["name"],
        exact_price=exact_price,
        price_from=int(price_from),
        price_to=int(price_to),
        term_from=term_from,
        term_to=term_to,
        urgent=urgent,
        complex_=complex_,
        selected_options=resolved_options,
    )
