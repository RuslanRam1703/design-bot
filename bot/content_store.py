"""Чтение/запись data/*.json для админ-режима (см. ТЗ, раздел 8).

Данные читаются с диска при каждом вызове и перезаписываются атомарно
(временный файл + os.replace) — правки админа в Telegram сразу видны и
боту, и Mini App, без пересборки и повторного деплоя.

Проверка прав: bot/handlers/admin.py уже гасит доступ на уровне роутера
(router.message.filter/router.callback_query.filter — не пропускает вообще
никого, кроме DESIGNER_CHAT_ID, дальше вызова хендлера). Но это не
единственный рубеж: каждая мутирующая функция здесь ТОЖЕ требует
actor_chat_id и сверяет его с DESIGNER_CHAT_ID сама, независимо от того,
кто и как её вызвал — так что даже при ошибке в фильтре роутера или
будущем добавлении нового пути вызова (например, другого хендлера,
который забудут защитить) запись в данные без прав физически невозможна.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from bot import config

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IMG_PORTFOLIO_DIR = Path(__file__).resolve().parent.parent / "webapp" / "img" / "portfolio"
IMG_ABOUT_DIR = Path(__file__).resolve().parent.parent / "webapp" / "img" / "about"

# Категория портфолио -> услуга калькулятора для автоподстановки в "Хочу
# похожий проект". "graphics" объединяет 3 услуги — однозначно не выбрать,
# оставляем None (see data/pricing.json -> groups).
TYPE_TO_SERVICE = {
    "landing": "LEND",
    "site": "SITE",
    "uxui": "UXUI",
    "logo": "LOGO",
    "branding": "BRAND",
    "social": "SMM",
}


class NotDesignerError(PermissionError):
    """Попытка вызвать мутирующую функцию content_store не от имени DESIGNER_CHAT_ID."""


def _require_designer(actor_chat_id: int | str) -> None:
    if not config.DESIGNER_CHAT_ID or str(actor_chat_id) != config.DESIGNER_CHAT_ID:
        raise NotDesignerError(f"chat_id={actor_chat_id!r} не совпадает с DESIGNER_CHAT_ID")


def _read(filename: str) -> Any:
    with open(DATA_DIR / filename, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(filename: str, data: Any) -> None:
    path = DATA_DIR / filename
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix=f".{filename}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# ---- Кейсы портфолио (чтение — без ограничений, пишет только в Mini App/бота) ----

def list_cases() -> list[dict]:
    return _read("portfolio.json")["cases"]


def list_portfolio_types() -> list[dict]:
    return _read("portfolio.json")["types"]


def next_case_id() -> str:
    cases = list_cases()
    nums = []
    for c in cases:
        if c["id"].startswith("case_"):
            try:
                nums.append(int(c["id"].split("_", 1)[1]))
            except ValueError:
                pass
    return f"case_{max(nums, default=0) + 1}"


def add_case(actor_chat_id: int | str, *, case_id: str, title: str, type_id: str, cover: str, task: str, related_service: str | None) -> dict:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    case = {
        "id": case_id,
        "title": title,
        "type": type_id,
        "cover": cover,
        "images": [cover],
        "task": task,
        "solution": "",
        "result": "",
        "related_service": related_service,
    }
    data["cases"].append(case)
    _write("portfolio.json", data)
    return case


def update_case(actor_chat_id: int | str, case_id: str, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    for c in data["cases"]:
        if c["id"] == case_id:
            c.update(fields)
            if "cover" in fields:
                c["images"] = [fields["cover"]]
            _write("portfolio.json", data)
            return True
    return False


def delete_case(actor_chat_id: int | str, case_id: str) -> bool:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    before = len(data["cases"])
    data["cases"] = [c for c in data["cases"] if c["id"] != case_id]
    if len(data["cases"]) == before:
        return False
    _write("portfolio.json", data)
    return True


async def save_case_photo(actor_chat_id: int | str, bot: Any, file_id: str, case_id: str) -> str:
    _require_designer(actor_chat_id)
    file = await bot.get_file(file_id)
    ext = Path(file.file_path).suffix or ".jpg"
    filename = f"{case_id}{ext}"
    dest = IMG_PORTFOLIO_DIR / filename
    await bot.download_file(file.file_path, destination=dest)
    return f"img/portfolio/{filename}"


# ---- FAQ ----

def list_faq() -> list[dict]:
    return _read("faq.json")["faq"]


def add_faq(actor_chat_id: int | str, question: str, answer: str) -> dict:
    _require_designer(actor_chat_id)
    data = _read("faq.json")
    new_id = max((i["id"] for i in data["faq"]), default=0) + 1
    item = {"id": new_id, "question": question, "type": "static", "answer": answer, "needs_review": False}
    data["faq"].append(item)
    _write("faq.json", data)
    return item


def update_faq(actor_chat_id: int | str, faq_id: int, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    data = _read("faq.json")
    for item in data["faq"]:
        if item["id"] == faq_id:
            item.update(fields)
            item["needs_review"] = False
            _write("faq.json", data)
            return True
    return False


def delete_faq(actor_chat_id: int | str, faq_id: int) -> bool:
    _require_designer(actor_chat_id)
    data = _read("faq.json")
    before = len(data["faq"])
    data["faq"] = [i for i in data["faq"] if i["id"] != faq_id]
    if len(data["faq"]) == before:
        return False
    _write("faq.json", data)
    return True


# ---- Обо мне ----

def update_about_field(actor_chat_id: int | str, field: str, value: Any) -> bool:
    _require_designer(actor_chat_id)
    data = _read("about.json")
    if field not in data:
        return False
    data[field] = value
    data["needs_review_fields"] = [f for f in data.get("needs_review_fields", []) if f != field]
    _write("about.json", data)
    return True


async def save_about_photo(actor_chat_id: int | str, bot: Any, file_id: str) -> str:
    _require_designer(actor_chat_id)
    file = await bot.get_file(file_id)
    ext = Path(file.file_path).suffix or ".jpg"
    filename = f"avatar{ext}"
    dest = IMG_ABOUT_DIR / filename
    await bot.download_file(file.file_path, destination=dest)
    return f"img/about/{filename}"
