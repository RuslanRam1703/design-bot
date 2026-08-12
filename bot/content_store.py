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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot import config

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IMG_PORTFOLIO_DIR = Path(__file__).resolve().parent.parent / "webapp" / "img" / "portfolio"
IMG_ABOUT_DIR = Path(__file__).resolve().parent.parent / "webapp" / "img" / "about"

class NotDesignerError(PermissionError):
    """Попытка вызвать мутирующую функцию content_store не от имени DESIGNER_CHAT_ID."""


def _require_designer(actor_chat_id: int | str) -> None:
    if not config.DESIGNER_CHAT_ID or str(actor_chat_id) != config.DESIGNER_CHAT_ID:
        raise NotDesignerError(f"chat_id={actor_chat_id!r} не совпадает с DESIGNER_CHAT_ID")


# ---- Хранилище data/*.json: локальные файлы, либо Upstash Redis (REST) ----
# На бесплатном Render (и большинстве бесплатных PaaS) файловая система
# эфемерна — любые правки, сделанные во время работы инстанса (заявки,
# правки через /admin), пропадают на следующем restart/redeploy. Если
# заданы UPSTASH_REDIS_REST_URL/TOKEN — каждый файл хранится как один
# JSON-блоб под ключом-именем файла в Redis, переживает любой redeploy.
# Без них — поведение как раньше, обычные локальные файлы (не нужен
# Upstash-аккаунт для локальной разработки).

def _upstash_enabled() -> bool:
    return bool(config.UPSTASH_REDIS_REST_URL and config.UPSTASH_REDIS_REST_TOKEN)


def _upstash_command(*args: Any) -> Any:
    req = urllib.request.Request(
        config.UPSTASH_REDIS_REST_URL,
        data=json.dumps(list(args)).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.UPSTASH_REDIS_REST_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("error"):
        raise RuntimeError(f"Upstash error on {args[0]}: {body['error']}")
    return body.get("result")


def _read_local(filename: str) -> Any:
    with open(DATA_DIR / filename, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_local(filename: str, data: Any) -> None:
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


def _read(filename: str) -> Any:
    if not _upstash_enabled():
        return _read_local(filename)
    raw = _upstash_command("GET", filename)
    if raw is None:
        # Первый запуск с этим Redis (ключа ещё нет) — сеем стартовыми
        # данными из репозитория и сразу сохраняем в Redis, чтобы дальше
        # читать/писать только оттуда.
        data = _read_local(filename)
        _upstash_command("SET", filename, json.dumps(data, ensure_ascii=False))
        return data
    return json.loads(raw)


def _write(filename: str, data: Any) -> None:
    if not _upstash_enabled():
        _write_local(filename, data)
        return
    _upstash_command("SET", filename, json.dumps(data, ensure_ascii=False))


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
    """"cover" через это поле — быстрый путь "загрузить новую обложку":
    новое фото ДОБАВЛЯЕТСЯ в галерею (images) и становится cover, но не
    стирает остальные изображения кейса — за полным управлением галереей
    (добавить/удалить/переставить/назначить обложку без загрузки) см.
    add_case_image / remove_case_image / reorder_case_image / set_case_cover."""
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    for c in data["cases"]:
        if c["id"] == case_id:
            c.update(fields)
            if "cover" in fields:
                images = c.setdefault("images", [])
                if fields["cover"] not in images:
                    images.append(fields["cover"])
            _write("portfolio.json", data)
            return True
    return False


def _find_case(data: dict, case_id: str) -> dict | None:
    return next((c for c in data["cases"] if c["id"] == case_id), None)


def add_case_image(actor_chat_id: int | str, case_id: str, image_path: str, *, set_as_cover: bool = False) -> bool:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    case = _find_case(data, case_id)
    if case is None:
        return False
    images = case.setdefault("images", [])
    if image_path not in images:
        images.append(image_path)
    if set_as_cover or not case.get("cover"):
        case["cover"] = image_path
    _write("portfolio.json", data)
    return True


def remove_case_image(actor_chat_id: int | str, case_id: str, image_path: str) -> bool:
    """Если удаляемое изображение было обложкой — обложка автоматически
    переходит на первое оставшееся; если изображений не осталось вовсе —
    cover становится None (Mini App показывает пустое состояние, не
    сломанную картинку — см. renderCase())."""
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    case = _find_case(data, case_id)
    if case is None or image_path not in case.get("images", []):
        return False
    case["images"] = [i for i in case["images"] if i != image_path]
    if case.get("cover") == image_path:
        case["cover"] = case["images"][0] if case["images"] else None
    _write("portfolio.json", data)
    return True


def reorder_case_image(actor_chat_id: int | str, case_id: str, image_path: str, direction: str) -> bool:
    """direction: "up" (раньше в галерее) | "down" (позже)."""
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    case = _find_case(data, case_id)
    if case is None or image_path not in case.get("images", []):
        return False
    images = case["images"]
    i = images.index(image_path)
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(images):
        return False
    images[i], images[j] = images[j], images[i]
    _write("portfolio.json", data)
    return True


def set_case_cover(actor_chat_id: int | str, case_id: str, image_path: str) -> bool:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    case = _find_case(data, case_id)
    if case is None or image_path not in case.get("images", []):
        return False
    case["cover"] = image_path
    _write("portfolio.json", data)
    return True


def update_case_category(actor_chat_id: int | str, case_id: str, new_type_id: str) -> bool:
    """Меняет категорию существующего кейса — раньше это было возможно
    только при создании. related_service подставляется из дефолта НОВОЙ
    категории только если у кейса он либо не задан, либо совпадал с
    дефолтом СТАРОЙ категории (то есть ранее не был выбран вручную) —
    осознанно выбранная связь с услугой при смене категории не стирается."""
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    case = _find_case(data, case_id)
    if case is None:
        return False
    old_type_id = case.get("type")
    old_default = next((t.get("related_service") for t in data["types"] if t["id"] == old_type_id), None)
    new_default = next((t.get("related_service") for t in data["types"] if t["id"] == new_type_id), None)
    if not case.get("related_service") or case.get("related_service") == old_default:
        case["related_service"] = new_default
    case["type"] = new_type_id
    _write("portfolio.json", data)
    return True


# ---- Разделы содержимого кейса (sections) ----
# Гибкая структура вместо жёстких task/solution/result — разные типы
# кейсов (лендинг/брендинг/UX-UI/графика) описываются по-разному. Кейсы без
# sections (пока не мигрированы) продолжают рендериться по task/solution/
# result как раньше — см. backward-compatible рендер в webapp/js/app.js.

def add_case_section(actor_chat_id: int | str, case_id: str, *, section_type: str, title: str, content: str = "", images: list[str] | None = None) -> bool:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    case = _find_case(data, case_id)
    if case is None:
        return False
    section: dict[str, Any] = {"type": section_type, "title": title}
    if section_type == "gallery":
        section["images"] = images or []
    else:
        section["content"] = content
    case.setdefault("sections", []).append(section)
    _write("portfolio.json", data)
    return True


def update_case_section(actor_chat_id: int | str, case_id: str, index: int, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    case = _find_case(data, case_id)
    if case is None:
        return False
    sections = case.get("sections", [])
    if not (0 <= index < len(sections)):
        return False
    sections[index].update(fields)
    _write("portfolio.json", data)
    return True


def delete_case_section(actor_chat_id: int | str, case_id: str, index: int) -> bool:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    case = _find_case(data, case_id)
    if case is None:
        return False
    sections = case.get("sections", [])
    if not (0 <= index < len(sections)):
        return False
    del sections[index]
    _write("portfolio.json", data)
    return True


def reorder_case_section(actor_chat_id: int | str, case_id: str, index: int, direction: str) -> bool:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    case = _find_case(data, case_id)
    if case is None:
        return False
    sections = case.get("sections", [])
    j = index - 1 if direction == "up" else index + 1
    if not (0 <= index < len(sections)) or not (0 <= j < len(sections)):
        return False
    sections[index], sections[j] = sections[j], sections[index]
    _write("portfolio.json", data)
    return True


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

def get_about() -> dict:
    return _read("about.json")


def update_about_field(actor_chat_id: int | str, field: str, value: Any) -> bool:
    _require_designer(actor_chat_id)
    data = _read("about.json")
    if field not in data:
        return False
    data[field] = value
    data["needs_review_fields"] = [f for f in data.get("needs_review_fields", []) if f != field]
    _write("about.json", data)
    return True


def add_about_experience(actor_chat_id: int | str, *, role: str, company: str, period: str, description: str = "") -> bool:
    _require_designer(actor_chat_id)
    data = _read("about.json")
    data.setdefault("experience", []).append({"role": role, "company": company, "period": period, "description": description})
    _write("about.json", data)
    return True


def update_about_experience(actor_chat_id: int | str, index: int, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    data = _read("about.json")
    entries = data.get("experience", [])
    if not (0 <= index < len(entries)):
        return False
    entries[index].update(fields)
    _write("about.json", data)
    return True


def delete_about_experience(actor_chat_id: int | str, index: int) -> bool:
    _require_designer(actor_chat_id)
    data = _read("about.json")
    entries = data.get("experience", [])
    if not (0 <= index < len(entries)):
        return False
    del entries[index]
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


# ---- Услуги калькулятора ----

def list_services() -> list[dict]:
    return _read("pricing.json")["services"]


def get_service(service_id: str) -> dict | None:
    return next((s for s in list_services() if s["id"] == service_id), None)


def next_service_id() -> str:
    """Встроенные услуги (LEND, SITE...) имеют смысловые id — для новых,
    добавленных админом, генерируем нейтральный SVC_N, чтобы не гадать с
    транслитерацией названия."""
    data = _read("pricing.json")
    nums = []
    for s in data["services"]:
        if s["id"].startswith("SVC_"):
            try:
                nums.append(int(s["id"].split("_", 1)[1]))
            except ValueError:
                pass
    return f"SVC_{max(nums, default=0) + 1}"


def add_service(actor_chat_id: int | str, *, service_id: str, name: str, base_price: float, term_min: float, term_max: float, includes: str) -> dict:
    _require_designer(actor_chat_id)
    data = _read("pricing.json")
    service = {
        "id": service_id,
        "name": name,
        "base_price": base_price,
        "term_min": term_min,
        "term_max": term_max,
        "includes": includes,
    }
    data["services"].append(service)
    _write("pricing.json", data)
    return service


def update_service(actor_chat_id: int | str, service_id: str, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    data = _read("pricing.json")
    for s in data["services"]:
        if s["id"] == service_id:
            s.update(fields)
            _write("pricing.json", data)
            return True
    return False


def delete_service(actor_chat_id: int | str, service_id: str) -> bool:
    """Удаляет услугу вместе с её опциями. Кейсы портфолио и категории
    портфолио, ссылавшиеся на неё через related_service, не удаляются —
    просто теряют автоподстановку услуги (related_service -> None), и там,
    и там: иначе после удаления услуги в data остался бы related_service,
    указывающий на несуществующую услугу — referential integrity."""
    _require_designer(actor_chat_id)
    data = _read("pricing.json")
    before = len(data["services"])
    data["services"] = [s for s in data["services"] if s["id"] != service_id]
    if len(data["services"]) == before:
        return False
    data["options"] = [o for o in data["options"] if o["service_id"] != service_id]
    for g in data.get("groups", []):
        g["service_ids"] = [sid for sid in g["service_ids"] if sid != service_id]
    _write("pricing.json", data)

    portfolio = _read("portfolio.json")
    changed = False
    for c in portfolio["cases"]:
        if c.get("related_service") == service_id:
            c["related_service"] = None
            changed = True
    for t in portfolio["types"]:
        if t.get("related_service") == service_id:
            t["related_service"] = None
            changed = True
    if changed:
        _write("portfolio.json", portfolio)
    return True


# ---- Опции услуг ----

def list_options(service_id: str) -> list[dict]:
    return [o for o in _read("pricing.json")["options"] if o["service_id"] == service_id]


def next_option_id(service_id: str) -> str:
    data = _read("pricing.json")
    nums = []
    prefix = f"{service_id}_"
    for o in data["options"]:
        if o["id"].startswith(prefix):
            try:
                nums.append(int(o["id"][len(prefix):]))
            except ValueError:
                pass
    return f"{prefix}{max(nums, default=0) + 1}"


def add_option(actor_chat_id: int | str, *, option_id: str, service_id: str, name: str, price: float, days: float, multipliable: bool) -> dict:
    _require_designer(actor_chat_id)
    data = _read("pricing.json")
    option = {
        "service_id": service_id,
        "id": option_id,
        "name": name,
        "price": price,
        "days": days,
        "multipliable": multipliable,
    }
    data["options"].append(option)
    _write("pricing.json", data)
    return option


def update_option(actor_chat_id: int | str, option_id: str, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    data = _read("pricing.json")
    for o in data["options"]:
        if o["id"] == option_id:
            o.update(fields)
            _write("pricing.json", data)
            return True
    return False


def delete_option(actor_chat_id: int | str, option_id: str) -> bool:
    _require_designer(actor_chat_id)
    data = _read("pricing.json")
    before = len(data["options"])
    data["options"] = [o for o in data["options"] if o["id"] != option_id]
    if len(data["options"]) == before:
        return False
    _write("pricing.json", data)
    return True


# ---- Коэффициенты и округление вилки ----

def get_pricing_rules() -> dict:
    data = _read("pricing.json")
    return {"coefficients": data["coefficients"], "rounding": data["rounding"]}


def update_coefficient(actor_chat_id: int | str, key: str, multiplier: float) -> bool:
    _require_designer(actor_chat_id)
    data = _read("pricing.json")
    if key not in data["coefficients"]:
        return False
    data["coefficients"][key]["multiplier"] = multiplier
    _write("pricing.json", data)
    return True


def update_rounding(actor_chat_id: int | str, field: str, value: float) -> bool:
    _require_designer(actor_chat_id)
    data = _read("pricing.json")
    if field not in data["rounding"]:
        return False
    data["rounding"][field] = value
    _write("pricing.json", data)
    return True


# ---- Категории портфолио ----

def default_related_service_for_type(type_id: str) -> str | None:
    """Дефолтная услуга для новых кейсов в категории — читается из
    data/portfolio.json -> types[].related_service (задаётся в /admin ->
    Категории портфолио -> Похожая услуга). Раньше бралась из захардкоженного
    словаря TYPE_TO_SERVICE и не работала для категорий, добавленных через
    /admin -> Категории портфолио -> Добавить, у которых просто не было
    записи в этом словаре."""
    for t in list_portfolio_types():
        if t["id"] == type_id:
            return t.get("related_service")
    return None


def update_portfolio_type_related_service(actor_chat_id: int | str, type_id: str, related_service: str | None) -> bool:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    for t in data["types"]:
        if t["id"] == type_id:
            t["related_service"] = related_service
            _write("portfolio.json", data)
            return True
    return False


def next_portfolio_type_id() -> str:
    data = _read("portfolio.json")
    nums = []
    for t in data["types"]:
        if t["id"].startswith("cat_"):
            try:
                nums.append(int(t["id"].split("_", 1)[1]))
            except ValueError:
                pass
    return f"cat_{max(nums, default=0) + 1}"


def add_portfolio_type(actor_chat_id: int | str, *, type_id: str, label: str) -> dict:
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    type_entry = {"id": type_id, "label": label, "related_service": None}
    data["types"].append(type_entry)
    _write("portfolio.json", data)
    return type_entry


def rename_portfolio_type(actor_chat_id: int | str, type_id: str, new_label: str) -> bool:
    """Меняем только подпись, не id — иначе "type" во всех кейсах, которые
    уже используют эту категорию, перестанет на неё указывать."""
    _require_designer(actor_chat_id)
    data = _read("portfolio.json")
    for t in data["types"]:
        if t["id"] == type_id:
            t["label"] = new_label
            _write("portfolio.json", data)
            return True
    return False


def count_cases_with_type(type_id: str) -> int:
    return sum(1 for c in list_cases() if c["type"] == type_id)


def delete_portfolio_type(actor_chat_id: int | str, type_id: str) -> bool:
    """Отказывает, если категория ещё используется хоть одним кейсом —
    вызывающий код (admin.py) должен сначала проверить count_cases_with_type
    и показать дизайнеру понятное сообщение, а не просто получить False."""
    _require_designer(actor_chat_id)
    if count_cases_with_type(type_id) > 0:
        return False
    data = _read("portfolio.json")
    before = len(data["types"])
    data["types"] = [t for t in data["types"] if t["id"] != type_id]
    if len(data["types"]) == before:
        return False
    _write("portfolio.json", data)
    return True


# ---- Видимость экранов (меню бота / таб-бар Mini App) ----

def get_ui_config() -> dict:
    return _read("ui_config.json")


def set_menu_item_enabled(actor_chat_id: int | str, key: str, enabled: bool) -> bool:
    _require_designer(actor_chat_id)
    data = _read("ui_config.json")
    if key not in data["menu"]:
        return False
    data["menu"][key] = enabled
    _write("ui_config.json", data)
    return True


# ---- Готовность контента к показу реальным клиентам ----

def content_readiness_summary() -> dict:
    """Сводка незавершённого клиент-facing контента — кейсы с обложкой-
    заглушкой, незаполненные поля "Обо мне", вопросы FAQ без финального
    ответа. Используется в /admin (см. handlers/admin.py), чтобы дизайнер
    не пропустил, что часть контента ещё не готова к показу клиентам —
    сами данные (needs_review / needs_review_fields / путь заглушки) уже
    существовали, но нигде не были собраны в одну сводку."""
    placeholder_cases = sum(1 for c in list_cases() if "placeholder" in (c.get("cover") or ""))
    about_pending = len(get_about().get("needs_review_fields", []))
    faq_pending = sum(1 for i in list_faq() if i.get("needs_review"))
    return {
        "placeholder_cases": placeholder_cases,
        "about_pending_fields": about_pending,
        "faq_pending": faq_pending,
    }


# ---- Заявки (leads) ----
# Простое JSON-хранилище — не CRM и не БД: заявки по-прежнему в первую
# очередь приходят дизайнеру сообщением в чат (bot/lead.py), это хранилище
# добавляет только отдельный список/статусы в /admin поверх того же потока,
# без нового канала передачи данных.

LEAD_STATUSES = ("NEW", "VIEWED", "IN_PROGRESS", "WAITING_CLIENT", "DONE", "CANCELLED")


def _read_leads() -> list[dict]:
    try:
        return _read("leads.json")["leads"]
    except FileNotFoundError:
        return []


def _write_leads(leads: list[dict]) -> None:
    _write("leads.json", {"leads": leads})


def add_lead(payload: dict, telegram: dict, calc_summary: dict | None = None, draft_id: str | None = None) -> dict:
    """Вызывается из bot/handlers/webapp.py при получении submit_brief —
    не требует _require_designer, потому что это действие клиента, не
    дизайнера. draft_id (необязательный, из localStorage-черновика Mini App)
    используется для upsert: повторная отправка того же черновика
    (например, через "Дополнить информацию") обновляет существующую заявку
    вместо создания дубликата — см. Part 7 ТЗ."""
    leads = _read_leads()
    if draft_id:
        existing = next((l for l in leads if l.get("draft_id") == draft_id), None)
        if existing is not None:
            existing.update(
                payload=payload,
                telegram=telegram,
                calc_summary=calc_summary,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            _write_leads(leads)
            return existing

    next_id = max((l["id"] for l in leads), default=0) + 1
    lead = {
        "id": next_id,
        "draft_id": draft_id,
        "status": "NEW",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": None,
        "telegram": telegram,
        "payload": payload,
        "calc_summary": calc_summary,
    }
    leads.append(lead)
    _write_leads(leads)
    return lead


def list_leads(status: str | None = None) -> list[dict]:
    leads = _read_leads()
    if status and status != "ALL":
        leads = [l for l in leads if l.get("status") == status]
    return sorted(leads, key=lambda l: l["id"], reverse=True)


def get_lead(lead_id: int) -> dict | None:
    return next((l for l in _read_leads() if l["id"] == lead_id), None)


def update_lead_status(actor_chat_id: int | str, lead_id: int, status: str) -> bool:
    _require_designer(actor_chat_id)
    if status not in LEAD_STATUSES:
        return False
    leads = _read_leads()
    lead = next((l for l in leads if l["id"] == lead_id), None)
    if lead is None:
        return False
    lead["status"] = status
    lead["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_leads(leads)
    return True
