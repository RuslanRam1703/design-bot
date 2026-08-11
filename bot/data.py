import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load(filename: str) -> Any:
    with open(DATA_DIR / filename, "r", encoding="utf-8") as f:
        return json.load(f)


def load_pricing() -> dict:
    return _load("pricing.json")


def load_faq() -> dict:
    return _load("faq.json")


def load_portfolio() -> dict:
    return _load("portfolio.json")


def load_about() -> dict:
    return _load("about.json")


def load_ui_config() -> dict:
    return _load("ui_config.json")


def get_service(pricing: dict, service_id: str) -> dict | None:
    for s in pricing["services"]:
        if s["id"] == service_id:
            return s
    return None


def get_options_for_service(pricing: dict, service_id: str) -> list[dict]:
    return [o for o in pricing["options"] if o["service_id"] == service_id]
