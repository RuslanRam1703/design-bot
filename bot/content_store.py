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

ASYNC + LOCKING MODEL (P1-1/P2-6/P2-7, production-hardening аудит, второй
design review — см. историю). Все публичные функции — async def:
`_upstash_command()` остаётся ПОЛНОСТЬЮ синхронной (urllib, без изменений в
request/timeout/logging/exception semantics) — единственная async-граница
это `await asyncio.to_thread(_upstash_command, ...)` внутри `_read`/`_write`,
не переписывание самого HTTP-вызова.

Locking — per-key, НЕ единый global lock (отклонён во втором review: он бы
без необходимости сериализовал операции на РАЗНЫХ файлах — например,
четыре параллельных fetch() публичных JSON Mini App при каждом открытии
страницы, см. Block 2A). Ровно один `asyncio.Lock` на каждый DATA_FILENAMES
ключ, три уровня:

  Tier 0 — single-key read-only: БЕЗ лока вообще. Upstash GET одного ключа
    атомарен сам по себе (Redis SET — atomic replace, никогда нет
    "частично записанного" значения) — без follow-up записи никакого
    lost-update нет.
  Tier 1 — single-key read-modify-write: один `async with _lock(filename):`
    на ВСЮ функцию, от первого read до последнего write. Держать лок только
    вокруг _write() недостаточно — гонка двух конкурентных `add_lead()` за
    один и тот же `next_id` требует держать лок от read до write включительно.
  Tier 2 — multi-key (delete_service, content_readiness_summary,
    export_backup_bytes, import_backup_bytes): ВСЕ нужные локи приобретаются
    ДО первого storage I/O, строго в каноническом порядке DATA_FILENAMES
    (см. _LOCK_ORDER) — это стандартный lock-ordering приём против deadlock:
    если каждая multi-key операция берёт локи в одном и том же порядке,
    цикл в графе ожидания невозможен по построению. `_StorageLock` сам
    сортирует переданные имена по этому порядку — порядок в вызове на
    конкретном call site не важен, гарантия обеспечена централизованно.

Backup (export/import) держит все 6 локов на всю операцию — это то же
самое, что и global lock, но ТОЛЬКО на время backup, а не всегда: остальные
~45 tier-0/tier-1 функций им не платят.
"""

import asyncio
import io
import json
import logging
import os
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot import config

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IMG_PORTFOLIO_DIR = Path(__file__).resolve().parent.parent / "webapp" / "img" / "portfolio"
IMG_ABOUT_DIR = Path(__file__).resolve().parent.parent / "webapp" / "img" / "about"

class NotDesignerError(PermissionError):
    """Попытка вызвать мутирующую функцию content_store не от имени DESIGNER_CHAT_ID."""


class UpstashKeyMissingError(RuntimeError):
    """DATA_FILENAMES-ключ отсутствует в Upstash, хотя MARKER_KEY уже
    подтверждает, что хранилище было полностью инициализировано раньше —
    значит ключ не "ещё не создан", а ПРОПАЛ (ручная ошибка в Upstash
    console, credentials указывают не на ту базу, потеря данных на стороне
    Redis). Реседить его локальным seed'ом здесь нельзя — это тихо
    потеряло бы production-данные (см. production-hardening аудит, P0-1)."""

    def __init__(self, filename: str):
        super().__init__(f"Upstash-ключ {filename!r} отсутствует, хотя storage уже был инициализирован ранее (marker присутствует)")
        self.filename = filename


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

MARKER_KEY = "design_assistant:storage_initialized"

DATA_FILENAMES = ("portfolio.json", "pricing.json", "faq.json", "about.json", "ui_config.json", "leads.json")

# Канонический порядок приобретения per-key locks — единственный источник
# истины для lock ordering (см. докстринг модуля). ЛЮБАЯ multi-key операция
# обязана брать локи через _lock(...) ниже, не напрямую через _locks[...] —
# _lock() сам сортирует по этому порядку, так что порядок аргументов на
# конкретном call site не может случайно нарушить инвариант.
_LOCK_ORDER: tuple[str, ...] = DATA_FILENAMES
_locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in _LOCK_ORDER}


class _StorageLock:
    """Context manager: приобретает per-key asyncio.Lock для одного или
    нескольких DATA_FILENAMES-ключей строго в каноническом порядке
    _LOCK_ORDER, независимо от порядка, в котором имена переданы вызывающим
    кодом — единственная точка приобретения locks в этом модуле. Держится
    ДО первого storage I/O операции и отпускается только после её полного
    завершения (см. докстринг модуля, Tier 1/Tier 2)."""

    __slots__ = ("_filenames",)

    def __init__(self, *filenames: str):
        self._filenames = tuple(sorted(set(filenames), key=_LOCK_ORDER.index))

    async def __aenter__(self) -> "_StorageLock":
        for name in self._filenames:
            await _locks[name].acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for name in reversed(self._filenames):
            _locks[name].release()
        return False


def _lock(*filenames: str) -> _StorageLock:
    return _StorageLock(*filenames)


def _upstash_enabled() -> bool:
    return bool(config.UPSTASH_REDIS_REST_URL and config.UPSTASH_REDIS_REST_TOKEN)


def _upstash_command(*args: Any) -> Any:
    # Централизованное логирование сбоев (P2, production-hardening аудит):
    # это единственная точка, через которую проходят ВСЕ GET/SET (обычные
    # _read/_write, ensure_storage_initialized, backup) — логируем здесь
    # ОДИН раз на уровне WARNING и пробрасываем исходное исключение без
    # изменений (fail-loud не меняется). Логируем ТОЛЬКО args[0] (команда)
    # и args[1] (ключ/filename) — НИКОГДА args[2] (для SET это полное
    # содержимое файла: для leads.json — реальные контакты/payload
    # клиентов) и никогда сам токен (он есть только в headers запроса,
    # в лог не попадает).
    #
    # Остаётся ПОЛНОСТЬЮ синхронной (P1-1 design review, часть "ASYNC
    # STORAGE LAYER"): единственный async boundary — await asyncio.to_thread
    # вокруг ВЫЗОВА этой функции (см. _read/_write/ensure_storage_initialized
    # ниже), не переписывание её внутренней urllib-реализации. Никакого
    # module-level mutable state здесь нет (request собирается заново на
    # каждый вызов, ничего не переиспользуется между вызовами) — вызывать её
    # из worker-треда asyncio.to_thread безопасно без дополнительных мер.
    command = args[0]
    key = args[1] if len(args) > 1 else None
    req = urllib.request.Request(
        config.UPSTASH_REDIS_REST_URL,
        data=json.dumps(list(args)).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.UPSTASH_REDIS_REST_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        logger.warning("Upstash %s failed: %s", command, key)
        raise
    if body.get("error"):
        logger.warning("Upstash %s failed: %s", command, key)
        raise RuntimeError(f"Upstash error on {command}: {body['error']}")
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


async def _read(filename: str) -> Any:
    if not _upstash_enabled():
        return _read_local(filename)
    raw = await asyncio.to_thread(_upstash_command, "GET", filename)
    if raw is None:
        if await asyncio.to_thread(_upstash_command, "GET", MARKER_KEY) is not None:
            # MARKER_KEY есть — значит хранилище уже было полностью
            # инициализировано раньше (см. ensure_storage_initialized), а
            # значит "ключа нет" здесь означает не "ещё не создан", а
            # ПРОПАЛ. Реседить его локальным seed'ом нельзя — это тихо
            # потеряло бы production-данные (P0-1). Fail loud вместо этого.
            logger.error("Upstash key missing: %s", filename)
            raise UpstashKeyMissingError(filename)
        # MARKER_KEY ещё нет — редкая гонка с ещё не завершившимся
        # ensure_storage_initialized (например, самый первый запрос успел
        # прийти раньше вызова на старте) — это тот же случай первичной
        # инициализации, что и в ensure_storage_initialized, безопасно
        # досеять именно этот файл.
        data = _read_local(filename)
        await asyncio.to_thread(_upstash_command, "SET", filename, json.dumps(data, ensure_ascii=False))
        return data
    return json.loads(raw)


async def _write(filename: str, data: Any) -> None:
    if not _upstash_enabled():
        _write_local(filename, data)
        return
    await asyncio.to_thread(_upstash_command, "SET", filename, json.dumps(data, ensure_ascii=False))


async def read_async(filename: str) -> Any:
    """Публичная точка входа для webserver.py::handle_public_data (Block
    2A) — теперь просто тонкий alias для _read, которая сама уже async и
    сама уже не блокирует event loop (P1-1 миграция). Единственный
    вызывающий код — handle_public_data; больше нигде использовать не
    нужно, публичный async-доступ ко ВСЕМ функциям хранилища теперь есть
    напрямую."""
    return await _read(filename)


async def ensure_storage_initialized() -> None:
    """Вызывается один раз при старте процесса (bot/main.py::main), ДО
    начала обработки апдейтов/HTTP-запросов. Eager batch-сид всех
    DATA_FILENAMES под единым persistent MARKER_KEY — решает гонку, которую
    давал бы ленивый per-file сид (как раньше в _read): разные файлы иначе
    получали бы свой "первый" GET в разные, непредсказуемые моменты (при
    первом реальном обращении к каждому конкретно), и общий marker,
    выставленный по самому первому из них, ошибочно принимал бы ещё не
    читанные файлы за "пропавшие" вместо "ещё не досеянные".

    MARKER_KEY выставляется СТРОГО последним шагом, только если весь цикл
    (проверка всех 6 ключей + досев отсутствующих) прошёл без единой
    ошибки — иначе при сбое (сеть, SET) можно было бы получить marker,
    который лжёт о том, что инициализация завершена, хотя часть файлов так
    и не появилась в Redis.

    Ни один уже существующий в Redis ключ не перезаписывается (SET —
    только если GET вернул None) — на уже работающей production-базе, где
    все 6 ключей реальны, это делает вызов идемпотентным no-op'ом,
    добавляющим только сам marker (безопасная миграция для уже
    задеплоенных инстансов, см. production-hardening аудит, P0-1).

    Без per-key locks: выполняется до site.start()/dp.start_polling(),
    когда конкурентного трафика ещё физически нет — блокировать здесь
    нечего от чего защищать (P1-1 design review)."""
    if not _upstash_enabled():
        return
    logger.info("Storage: startup initialization started")
    try:
        if await asyncio.to_thread(_upstash_command, "GET", MARKER_KEY) is not None:
            logger.info("Storage: marker already present, skipping seed")
            return
        seeded = []
        for filename in DATA_FILENAMES:
            if await asyncio.to_thread(_upstash_command, "GET", filename) is None:
                data = _read_local(filename)
                await asyncio.to_thread(_upstash_command, "SET", filename, json.dumps(data, ensure_ascii=False))
                seeded.append(filename)
        await asyncio.to_thread(_upstash_command, "SET", MARKER_KEY, datetime.now(timezone.utc).isoformat())
    except Exception:
        logger.exception("Upstash initialization failed")
        raise
    logger.info("Storage: initialization completed, seeded=%s", seeded)


# ---- Кейсы портфолио (чтение — без ограничений, пишет только в Mini App/бота) ----

async def list_cases() -> list[dict]:
    return (await _read("portfolio.json"))["cases"]


async def list_portfolio_types() -> list[dict]:
    return (await _read("portfolio.json"))["types"]


async def next_case_id() -> str:
    cases = await list_cases()
    nums = []
    for c in cases:
        if c["id"].startswith("case_"):
            try:
                nums.append(int(c["id"].split("_", 1)[1]))
            except ValueError:
                pass
    return f"case_{max(nums, default=0) + 1}"


async def add_case(actor_chat_id: int | str, *, case_id: str, title: str, type_id: str, cover: str, task: str, related_service: str | None) -> dict:
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
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
        await _write("portfolio.json", data)
        return case


async def update_case(actor_chat_id: int | str, case_id: str, **fields: Any) -> bool:
    """"cover" через это поле — быстрый путь "загрузить новую обложку":
    новое фото ДОБАВЛЯЕТСЯ в галерею (images) и становится cover, но не
    стирает остальные изображения кейса — за полным управлением галереей
    (добавить/удалить/переставить/назначить обложку без загрузки) см.
    add_case_image / remove_case_image / reorder_case_image / set_case_cover."""
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        for c in data["cases"]:
            if c["id"] == case_id:
                c.update(fields)
                if "cover" in fields:
                    images = c.setdefault("images", [])
                    if fields["cover"] not in images:
                        images.append(fields["cover"])
                await _write("portfolio.json", data)
                return True
        return False


def _find_case(data: dict, case_id: str) -> dict | None:
    return next((c for c in data["cases"] if c["id"] == case_id), None)


async def add_case_image(actor_chat_id: int | str, case_id: str, image_path: str, *, set_as_cover: bool = False) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        case = _find_case(data, case_id)
        if case is None:
            return False
        images = case.setdefault("images", [])
        if image_path not in images:
            images.append(image_path)
        if set_as_cover or not case.get("cover"):
            case["cover"] = image_path
        await _write("portfolio.json", data)
        return True


async def remove_case_image(actor_chat_id: int | str, case_id: str, image_path: str) -> bool:
    """Если удаляемое изображение было обложкой — обложка автоматически
    переходит на первое оставшееся; если изображений не осталось вовсе —
    cover становится None (Mini App показывает пустое состояние, не
    сломанную картинку — см. renderCase())."""
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        case = _find_case(data, case_id)
        if case is None or image_path not in case.get("images", []):
            return False
        case["images"] = [i for i in case["images"] if i != image_path]
        if case.get("cover") == image_path:
            case["cover"] = case["images"][0] if case["images"] else None
        await _write("portfolio.json", data)
        return True


async def reorder_case_image(actor_chat_id: int | str, case_id: str, image_path: str, direction: str) -> bool:
    """direction: "up" (раньше в галерее) | "down" (позже)."""
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        case = _find_case(data, case_id)
        if case is None or image_path not in case.get("images", []):
            return False
        images = case["images"]
        i = images.index(image_path)
        j = i - 1 if direction == "up" else i + 1
        if j < 0 or j >= len(images):
            return False
        images[i], images[j] = images[j], images[i]
        await _write("portfolio.json", data)
        return True


async def set_case_cover(actor_chat_id: int | str, case_id: str, image_path: str) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        case = _find_case(data, case_id)
        if case is None or image_path not in case.get("images", []):
            return False
        case["cover"] = image_path
        await _write("portfolio.json", data)
        return True


async def update_case_category(actor_chat_id: int | str, case_id: str, new_type_id: str) -> bool:
    """Меняет категорию существующего кейса — раньше это было возможно
    только при создании. related_service подставляется из дефолта НОВОЙ
    категории только если у кейса он либо не задан, либо совпадал с
    дефолтом СТАРОЙ категории (то есть ранее не был выбран вручную) —
    осознанно выбранная связь с услугой при смене категории не стирается."""
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        case = _find_case(data, case_id)
        if case is None:
            return False
        old_type_id = case.get("type")
        old_default = next((t.get("related_service") for t in data["types"] if t["id"] == old_type_id), None)
        new_default = next((t.get("related_service") for t in data["types"] if t["id"] == new_type_id), None)
        if not case.get("related_service") or case.get("related_service") == old_default:
            case["related_service"] = new_default
        case["type"] = new_type_id
        await _write("portfolio.json", data)
        return True


# ---- Разделы содержимого кейса (sections) ----
# Гибкая структура вместо жёстких task/solution/result — разные типы
# кейсов (лендинг/брендинг/UX-UI/графика) описываются по-разному. Кейсы без
# sections (пока не мигрированы) продолжают рендериться по task/solution/
# result как раньше — см. backward-compatible рендер в webapp/js/app.js.

async def add_case_section(actor_chat_id: int | str, case_id: str, *, section_type: str, title: str, content: str = "", images: list[str] | None = None) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        case = _find_case(data, case_id)
        if case is None:
            return False
        section: dict[str, Any] = {"type": section_type, "title": title}
        if section_type == "gallery":
            section["images"] = images or []
        else:
            section["content"] = content
        case.setdefault("sections", []).append(section)
        await _write("portfolio.json", data)
        return True


async def update_case_section(actor_chat_id: int | str, case_id: str, index: int, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        case = _find_case(data, case_id)
        if case is None:
            return False
        sections = case.get("sections", [])
        if not (0 <= index < len(sections)):
            return False
        sections[index].update(fields)
        await _write("portfolio.json", data)
        return True


async def delete_case_section(actor_chat_id: int | str, case_id: str, index: int) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        case = _find_case(data, case_id)
        if case is None:
            return False
        sections = case.get("sections", [])
        if not (0 <= index < len(sections)):
            return False
        del sections[index]
        await _write("portfolio.json", data)
        return True


async def reorder_case_section(actor_chat_id: int | str, case_id: str, index: int, direction: str) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        case = _find_case(data, case_id)
        if case is None:
            return False
        sections = case.get("sections", [])
        j = index - 1 if direction == "up" else index + 1
        if not (0 <= index < len(sections)) or not (0 <= j < len(sections)):
            return False
        sections[index], sections[j] = sections[j], sections[index]
        await _write("portfolio.json", data)
        return True


async def delete_case(actor_chat_id: int | str, case_id: str) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        before = len(data["cases"])
        data["cases"] = [c for c in data["cases"] if c["id"] != case_id]
        if len(data["cases"]) == before:
            return False
        await _write("portfolio.json", data)
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

async def list_faq() -> list[dict]:
    return (await _read("faq.json"))["faq"]


async def add_faq(actor_chat_id: int | str, question: str, answer: str) -> dict:
    _require_designer(actor_chat_id)
    async with _lock("faq.json"):
        data = await _read("faq.json")
        new_id = max((i["id"] for i in data["faq"]), default=0) + 1
        item = {"id": new_id, "question": question, "type": "static", "answer": answer, "needs_review": False}
        data["faq"].append(item)
        await _write("faq.json", data)
        return item


async def update_faq(actor_chat_id: int | str, faq_id: int, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("faq.json"):
        data = await _read("faq.json")
        for item in data["faq"]:
            if item["id"] == faq_id:
                item.update(fields)
                item["needs_review"] = False
                await _write("faq.json", data)
                return True
        return False


async def delete_faq(actor_chat_id: int | str, faq_id: int) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("faq.json"):
        data = await _read("faq.json")
        before = len(data["faq"])
        data["faq"] = [i for i in data["faq"] if i["id"] != faq_id]
        if len(data["faq"]) == before:
            return False
        await _write("faq.json", data)
        return True


# ---- Обо мне ----

async def get_about() -> dict:
    return await _read("about.json")


async def update_about_field(actor_chat_id: int | str, field: str, value: Any) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("about.json"):
        data = await _read("about.json")
        if field not in data:
            return False
        data[field] = value
        data["needs_review_fields"] = [f for f in data.get("needs_review_fields", []) if f != field]
        await _write("about.json", data)
        return True


async def add_about_experience(actor_chat_id: int | str, *, role: str, company: str, period: str, description: str = "") -> bool:
    _require_designer(actor_chat_id)
    async with _lock("about.json"):
        data = await _read("about.json")
        data.setdefault("experience", []).append({"role": role, "company": company, "period": period, "description": description})
        await _write("about.json", data)
        return True


async def update_about_experience(actor_chat_id: int | str, index: int, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("about.json"):
        data = await _read("about.json")
        entries = data.get("experience", [])
        if not (0 <= index < len(entries)):
            return False
        entries[index].update(fields)
        await _write("about.json", data)
        return True


async def delete_about_experience(actor_chat_id: int | str, index: int) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("about.json"):
        data = await _read("about.json")
        entries = data.get("experience", [])
        if not (0 <= index < len(entries)):
            return False
        del entries[index]
        await _write("about.json", data)
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

async def list_services() -> list[dict]:
    return (await _read("pricing.json"))["services"]


async def get_service(service_id: str) -> dict | None:
    return next((s for s in await list_services() if s["id"] == service_id), None)


async def next_service_id() -> str:
    """Встроенные услуги (LEND, SITE...) имеют смысловые id — для новых,
    добавленных админом, генерируем нейтральный SVC_N, чтобы не гадать с
    транслитерацией названия."""
    data = await _read("pricing.json")
    nums = []
    for s in data["services"]:
        if s["id"].startswith("SVC_"):
            try:
                nums.append(int(s["id"].split("_", 1)[1]))
            except ValueError:
                pass
    return f"SVC_{max(nums, default=0) + 1}"


async def add_service(actor_chat_id: int | str, *, service_id: str, name: str, base_price: float, term_min: float, term_max: float, includes: str) -> dict:
    _require_designer(actor_chat_id)
    async with _lock("pricing.json"):
        data = await _read("pricing.json")
        service = {
            "id": service_id,
            "name": name,
            "base_price": base_price,
            "term_min": term_min,
            "term_max": term_max,
            "includes": includes,
        }
        data["services"].append(service)
        await _write("pricing.json", data)
        return service


async def update_service(actor_chat_id: int | str, service_id: str, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("pricing.json"):
        data = await _read("pricing.json")
        for s in data["services"]:
            if s["id"] == service_id:
                s.update(fields)
                await _write("pricing.json", data)
                return True
        return False


async def delete_service(actor_chat_id: int | str, service_id: str) -> bool:
    """Удаляет услугу вместе с её опциями. Кейсы портфолио и категории
    портфолио, ссылавшиеся на неё через related_service, не удаляются —
    просто теряют автоподстановку услуги (related_service -> None), и там,
    и там: иначе после удаления услуги в data остался бы related_service,
    указывающий на несуществующую услугу — referential integrity.

    Multi-key (Tier 2, P1-1/P2-7 design review): pricing.json И
    portfolio.json — оба лока приобретаются ДО первого чтения, в
    каноническом порядке (portfolio раньше pricing в _LOCK_ORDER), хотя
    сама бизнес-логика по-прежнему обрабатывает pricing.json первым —
    порядок приобретения locks и порядок I/O намеренно разные (см. докстринг
    модуля): конкурентный reader не может увидеть состояние "услуга уже
    удалена из pricing.json, но related_service в portfolio.json ещё не
    обнулён"."""
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json", "pricing.json"):
        data = await _read("pricing.json")
        before = len(data["services"])
        data["services"] = [s for s in data["services"] if s["id"] != service_id]
        if len(data["services"]) == before:
            return False
        data["options"] = [o for o in data["options"] if o["service_id"] != service_id]
        for g in data.get("groups", []):
            g["service_ids"] = [sid for sid in g["service_ids"] if sid != service_id]
        await _write("pricing.json", data)

        portfolio = await _read("portfolio.json")
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
            await _write("portfolio.json", portfolio)
        return True


# ---- Опции услуг ----

async def list_options(service_id: str) -> list[dict]:
    return [o for o in (await _read("pricing.json"))["options"] if o["service_id"] == service_id]


async def next_option_id(service_id: str) -> str:
    data = await _read("pricing.json")
    nums = []
    prefix = f"{service_id}_"
    for o in data["options"]:
        if o["id"].startswith(prefix):
            try:
                nums.append(int(o["id"][len(prefix):]))
            except ValueError:
                pass
    return f"{prefix}{max(nums, default=0) + 1}"


async def add_option(actor_chat_id: int | str, *, option_id: str, service_id: str, name: str, price: float, days: float, multipliable: bool) -> dict:
    _require_designer(actor_chat_id)
    async with _lock("pricing.json"):
        data = await _read("pricing.json")
        option = {
            "service_id": service_id,
            "id": option_id,
            "name": name,
            "price": price,
            "days": days,
            "multipliable": multipliable,
        }
        data["options"].append(option)
        await _write("pricing.json", data)
        return option


async def update_option(actor_chat_id: int | str, option_id: str, **fields: Any) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("pricing.json"):
        data = await _read("pricing.json")
        for o in data["options"]:
            if o["id"] == option_id:
                o.update(fields)
                await _write("pricing.json", data)
                return True
        return False


async def delete_option(actor_chat_id: int | str, option_id: str) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("pricing.json"):
        data = await _read("pricing.json")
        before = len(data["options"])
        data["options"] = [o for o in data["options"] if o["id"] != option_id]
        if len(data["options"]) == before:
            return False
        await _write("pricing.json", data)
        return True


# ---- Коэффициенты и округление вилки ----

async def get_pricing_rules() -> dict:
    data = await _read("pricing.json")
    return {"coefficients": data["coefficients"], "rounding": data["rounding"]}


async def update_coefficient(actor_chat_id: int | str, key: str, multiplier: float) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("pricing.json"):
        data = await _read("pricing.json")
        if key not in data["coefficients"]:
            return False
        data["coefficients"][key]["multiplier"] = multiplier
        await _write("pricing.json", data)
        return True


async def update_rounding(actor_chat_id: int | str, field: str, value: float) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("pricing.json"):
        data = await _read("pricing.json")
        if field not in data["rounding"]:
            return False
        data["rounding"][field] = value
        await _write("pricing.json", data)
        return True


# ---- Категории портфолио ----

async def default_related_service_for_type(type_id: str) -> str | None:
    """Дефолтная услуга для новых кейсов в категории — читается из
    data/portfolio.json -> types[].related_service (задаётся в /admin ->
    Категории портфолио -> Похожая услуга). Раньше бралась из захардкоженного
    словаря TYPE_TO_SERVICE и не работала для категорий, добавленных через
    /admin -> Категории портфолио -> Добавить, у которых просто не было
    записи в этом словаре."""
    for t in await list_portfolio_types():
        if t["id"] == type_id:
            return t.get("related_service")
    return None


async def update_portfolio_type_related_service(actor_chat_id: int | str, type_id: str, related_service: str | None) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        for t in data["types"]:
            if t["id"] == type_id:
                t["related_service"] = related_service
                await _write("portfolio.json", data)
                return True
        return False


async def next_portfolio_type_id() -> str:
    data = await _read("portfolio.json")
    nums = []
    for t in data["types"]:
        if t["id"].startswith("cat_"):
            try:
                nums.append(int(t["id"].split("_", 1)[1]))
            except ValueError:
                pass
    return f"cat_{max(nums, default=0) + 1}"


async def add_portfolio_type(actor_chat_id: int | str, *, type_id: str, label: str) -> dict:
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        type_entry = {"id": type_id, "label": label, "related_service": None}
        data["types"].append(type_entry)
        await _write("portfolio.json", data)
        return type_entry


async def rename_portfolio_type(actor_chat_id: int | str, type_id: str, new_label: str) -> bool:
    """Меняем только подпись, не id — иначе "type" во всех кейсах, которые
    уже используют эту категорию, перестанет на неё указывать."""
    _require_designer(actor_chat_id)
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        for t in data["types"]:
            if t["id"] == type_id:
                t["label"] = new_label
                await _write("portfolio.json", data)
                return True
        return False


async def count_cases_with_type(type_id: str) -> int:
    return sum(1 for c in await list_cases() if c["type"] == type_id)


async def delete_portfolio_type(actor_chat_id: int | str, type_id: str) -> bool:
    """Отказывает, если категория ещё используется хоть одним кейсом —
    вызывающий код (admin.py) должен сначала проверить count_cases_with_type
    и показать дизайнеру понятное сообщение, а не просто получить False.

    count_cases_with_type — tier-0 read, вызывается ДО приобретения
    portfolio.json lock; сама запись ниже повторно проверяет актуальность
    (before/after длины списка) на СВЕЖЕ прочитанных внутри лока данных —
    тот же defensive-паттерн, что был и до этого рефакторинга (admin-действия
    и так сериализованы SimpleEventIsolation per-chat, см. P1-1 design
    review — второй параллельный delete/add одной и той же категории с
    одного и того же DESIGNER_CHAT_ID физически невозможен)."""
    _require_designer(actor_chat_id)
    if await count_cases_with_type(type_id) > 0:
        return False
    async with _lock("portfolio.json"):
        data = await _read("portfolio.json")
        before = len(data["types"])
        data["types"] = [t for t in data["types"] if t["id"] != type_id]
        if len(data["types"]) == before:
            return False
        await _write("portfolio.json", data)
        return True


# ---- Видимость экранов (меню бота / таб-бар Mini App) ----

async def get_ui_config() -> dict:
    return await _read("ui_config.json")


async def set_menu_item_enabled(actor_chat_id: int | str, key: str, enabled: bool) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("ui_config.json"):
        data = await _read("ui_config.json")
        if key not in data["menu"]:
            return False
        data["menu"][key] = enabled
        await _write("ui_config.json", data)
        return True


# ---- Готовность контента к показу реальным клиентам ----

async def content_readiness_summary() -> dict:
    """Сводка незавершённого клиент-facing контента — кейсы с обложкой-
    заглушкой, незаполненные поля "Обо мне", вопросы FAQ без финального
    ответа. Используется в /admin (см. handlers/admin.py), чтобы дизайнер
    не пропустил, что часть контента ещё не готова к показу клиентам —
    сами данные (needs_review / needs_review_fields / путь заглушки) уже
    существовали, но нигде не были собраны в одну сводку.

    Multi-key read-only (Tier 2, P1-1/P2-7 design review): portfolio.json +
    about.json + faq.json — держим все три лока на время чтения, чтобы не
    получить сводку, посчитанную по смеси до- и после-restore состояния
    разных файлов, если backup restore случится ровно между тремя read."""
    async with _lock("portfolio.json", "about.json", "faq.json"):
        placeholder_cases = sum(1 for c in await list_cases() if "placeholder" in (c.get("cover") or ""))
        about_pending = len((await get_about()).get("needs_review_fields", []))
        faq_pending = sum(1 for i in await list_faq() if i.get("needs_review"))
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

# "ACTIVE" — НЕ business-статус (не входит в LEAD_STATUSES, никогда не
# пишется в lead["status"], в LEAD_STATUSES-валидацию update_lead_status не
# участвует) — это чисто технический alias для list_leads(status=...),
# означающий "не завершено" (см. UX-аудит "Заявки как рабочая очередь",
# default view /admin -> Заявки). Единственное место, которое его понимает
# — сам list_leads() ниже.
ACTIVE_LEAD_STATUSES = ("NEW", "VIEWED", "IN_PROGRESS", "WAITING_CLIENT")


class LeadNotFoundError(Exception):
    """supplement/материал для несуществующего lead_id."""


class NotLeadOwnerError(Exception):
    """lead_id существует, но telegram.user_id (из validate_init_data) не
    совпадает с telegram.user_id заявки — попытка дополнить чужую заявку."""


async def _read_leads() -> list[dict]:
    try:
        return (await _read("leads.json"))["leads"]
    except FileNotFoundError:
        return []


async def _write_leads(leads: list[dict]) -> None:
    await _write("leads.json", {"leads": leads})


def _clear_other_awaiting(leads: list[dict], user_id: int, keep_lead_id: int) -> None:
    """Гарантирует, что в любой момент у ОДНОГО клиента максимум одна
    заявка помечена awaiting_tz_file=True. Раньше find_lead_awaiting_file
    выбирал "самую свежую" среди нескольких ожидающих заявок (max по id) —
    если у клиента параллельно две заявки ждут файл, входящий документ мог
    уйти не туда (см. аудит). Вместо угадывания в момент получения файла —
    не допускаем самого состояния неоднозначности: постановка новой заявки
    в ожидание файла всегда снимает ожидание со всех остальных заявок
    этого же клиента.

    Синхронная (P1-1 design review): чистая мутация уже прочитанного в
    памяти списка leads, никакого I/O — вызывается изнутри уже
    приобретённого leads.json lock в add_lead/add_lead_supplement, не берёт
    лок сама (не re-entrant-проблема, т.к. вообще не лочит)."""
    for l in leads:
        if l["id"] != keep_lead_id and l.get("telegram", {}).get("user_id") == user_id and l.get("awaiting_tz_file"):
            l["awaiting_tz_file"] = False
            l["awaiting_tz_file_source"] = None


async def add_lead(payload: dict, telegram: dict, calc_summary: dict | None = None, draft_id: str | None = None) -> dict:
    """Вызывается и из bot/webserver.py::handle_create_lead (основной путь,
    authenticated HTTP), и из bot/handlers/webapp.py::handle_webapp_data
    (legacy sendData() — оставлен как fallback) — не требует
    _require_designer, потому что это действие клиента, не дизайнера.
    draft_id (необязательный, из localStorage-черновика Mini App)
    используется для upsert: повторная отправка ТОГО ЖЕ ещё не изменённого
    черновика (например, из-за дублирующегося клика — см. аудит про
    submit idempotency) обновляет существующую заявку вместо создания
    дубликата — НЕ путать с supplement-режимом (add_lead_supplement ниже):
    draft_id живёt в localStorage клиента ДО первого успешного submit,
    supplement адресуется по lead_id уже ПОСЛЕ того, как заявка создана и
    localStorage-черновик очищен.

    Возвращаемый dict — это КОПИЯ сохранённой заявки с добавленным
    служебным ключом "created" (True — новая заявка, False — обновление
    существующей по draft_id). Ключ добавлен только в возвращаемое
    значение и не персистится (см. handle_create_lead — уведомлять
    владельца нужно только при реальном создании, не при каждом
    повторном/дублирующемся submit одного и того же черновика).

    awaiting_tz_file — persistent (переживает restart/redeploy через тот же
    Upstash-слой, что и вся заявка) замена FSM-состоянию
    BriefStates.awaiting_tz_file: раньше "жду файл от этого клиента"
    хранилось только в памяти процесса и терялось при рестарте; теперь это
    поле самой заявки, проверяется в handle_tz_file по telegram.user_id —
    см. find_lead_awaiting_file/mark_tz_file_received/record_lead_material
    ниже. awaiting_tz_file_source запоминает, каким действием клиент начал
    ждать файл ("new" — при создании заявки, "supplement" — из дополнения),
    чтобы materials[].source был точным, а не предположением.

    Tier 1 (P1-1 design review): весь read-modify-write под ОДНИМ
    приобретением leads.json lock, от _read_leads() до _write_leads()
    включительно — без этого два конкурентных add_lead() с РАЗНЫМИ
    draft_id могли бы оба вычислить один и тот же next_id из одного и того
    же устаревшего списка и потерять одну из двух заявок (lost update)."""
    async with _lock("leads.json"):
        leads = await _read_leads()
        awaiting_tz_file = bool(payload.get("attach_tz"))
        if draft_id:
            existing = next((l for l in leads if l.get("draft_id") == draft_id), None)
            if existing is not None:
                # draft_id — обычная строка из localStorage клиента, ничем не
                # подписана. Без этой проверки чужой (совпавший или подобранный)
                # draft_id тихо перезаписал бы payload/identity уже существующей
                # заявки (см. production-аудит, P1-2) — тот же класс ошибки, что
                # add_lead_supplement ниже уже ловит по lead_id; здесь то же самое
                # для draft_id-апсерта. Ничего не меняем и не пишем при коллизии.
                if existing.get("telegram", {}).get("user_id") != telegram.get("user_id"):
                    raise NotLeadOwnerError(existing["id"])
                existing.update(
                    payload=payload,
                    telegram=telegram,
                    calc_summary=calc_summary,
                    awaiting_tz_file=awaiting_tz_file,
                    awaiting_tz_file_source=("new" if awaiting_tz_file else None),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                if awaiting_tz_file:
                    _clear_other_awaiting(leads, telegram["user_id"], existing["id"])
                await _write_leads(leads)
                return {**existing, "created": False}

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
            "awaiting_tz_file": awaiting_tz_file,
            "awaiting_tz_file_source": "new" if awaiting_tz_file else None,
            "supplements": [],
            "materials": [],
        }
        leads.append(lead)
        if awaiting_tz_file:
            _clear_other_awaiting(leads, telegram["user_id"], lead["id"])
        await _write_leads(leads)
        return {**lead, "created": True}


async def add_lead_supplement(lead_id: int, telegram: dict, fields: dict, wants_file: bool = False) -> tuple[dict, int]:
    """Дополнение к уже существующей заявке — НЕ трогает lead["payload"]
    (исходные ответы Order Builder остаются как есть), только добавляет
    append-only запись в lead["supplements"]. lead_id — единственный и
    достаточный идентификатор режима дополнения: в отличие от draft_id
    (который существует только в localStorage клиента ДО первого submit),
    lead_id уже известен клиенту из "Мои заявки" и однозначно указывает на
    конкретную, уже существующую заявку.

    Владение проверяется здесь, а не только на уровне HTTP-хендлера — тот
    же telegram.user_id, что уже сохранён на заявке (сам он туда попал из
    validate_init_data при создании), должен совпадать с текущим
    провалидированным user_id. LeadNotFoundError/NotLeadOwnerError — на
    хендлере превращаются в 404 (унифицированный ответ, см. production-
    аудит P2-11), тело чужой заявки клиенту не видно.

    Tier 1: весь read-modify-write под одним leads.json lock."""
    async with _lock("leads.json"):
        leads = await _read_leads()
        lead = next((l for l in leads if l["id"] == lead_id), None)
        if lead is None:
            raise LeadNotFoundError(lead_id)
        if lead.get("telegram", {}).get("user_id") != telegram.get("user_id"):
            raise NotLeadOwnerError(lead_id)

        supplements = lead.setdefault("supplements", [])
        next_supplement_id = max((s["id"] for s in supplements), default=0) + 1
        supplements.append({
            "id": next_supplement_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "fields": fields,
        })
        lead["updated_at"] = datetime.now(timezone.utc).isoformat()
        if wants_file:
            lead["awaiting_tz_file"] = True
            lead["awaiting_tz_file_source"] = "supplement"
            _clear_other_awaiting(leads, telegram["user_id"], lead_id)
        await _write_leads(leads)
        return lead, next_supplement_id


async def record_lead_material(lead_id: int, file_id: str, file_unique_id: str, kind: str, source: str) -> bool:
    """Сохраняет метаданные присланного клиентом файла (document/photo) НА
    заявке — раньше файл только пересылался владельцу через message.forward()
    и нигде не сохранялся, связь "файл ↔ заявка" существовала лишь на
    момент исполнения handle_tz_file и тут же терялась (см. аудит). Сам
    файл по-прежнему не скачивается и не хранится нами — только Telegram
    file_id/file_unique_id, этого достаточно, чтобы позже получить файл
    заново через Bot API (getFile), без Google Drive/S3 на этом этапе."""
    async with _lock("leads.json"):
        leads = await _read_leads()
        lead = next((l for l in leads if l["id"] == lead_id), None)
        if lead is None:
            return False
        materials = lead.setdefault("materials", [])
        materials.append({
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "kind": kind,
            "source": source,
            "received_at": datetime.now(timezone.utc).isoformat(),
        })
        lead["awaiting_tz_file"] = False
        lead["awaiting_tz_file_source"] = None
        lead["updated_at"] = datetime.now(timezone.utc).isoformat()
        await _write_leads(leads)
        return True


async def find_lead_awaiting_file(user_id: int) -> dict | None:
    """Для handle_tz_file (bot/handlers/webapp.py) — связывает присланный в
    чат документ/фото с правильной заявкой ТОЛЬКО по проверенному
    Telegram user_id (см. message.from_user.id — всегда надёжен, не
    initData, но тот же принцип "не доверять чужому id"). add_lead/
    add_lead_supplement поддерживают инвариант "максимум одна заявка этого
    клиента одновременно ждёт файл" (см. _clear_other_awaiting) — поэтому
    здесь больше НЕТ угадывания "самой свежей" среди нескольких кандидатов
    (см. аудит): при соблюдённом инварианте кандидат всего один, max()
    остаётся только защитой на случай уже существующих в хранилище данных,
    сохранённых до этого исправления.

    Tier 0 — pure read, без lock (см. докстринг модуля)."""
    leads = [
        l for l in await _read_leads()
        if l.get("telegram", {}).get("user_id") == user_id and l.get("awaiting_tz_file")
    ]
    return max(leads, key=lambda l: l["id"], default=None)


async def mark_tz_file_received(lead_id: int) -> bool:
    """Снять ожидание файла БЕЗ записи материала — используется только для
    "Отменить" (bot/handlers/start.py::cmd_cancel). Получение реального
    файла идёт через record_lead_material (выше), которая сама снимает
    awaiting_tz_file — эта функция для него не нужна."""
    async with _lock("leads.json"):
        leads = await _read_leads()
        lead = next((l for l in leads if l["id"] == lead_id), None)
        if lead is None:
            return False
        lead["awaiting_tz_file"] = False
        lead["awaiting_tz_file_source"] = None
        lead["updated_at"] = datetime.now(timezone.utc).isoformat()
        await _write_leads(leads)
        return True


async def list_leads(status: str | None = None) -> list[dict]:
    """Для /admin -> Заявки. status="ACTIVE" — см. ACTIVE_LEAD_STATUSES
    выше (не персистентный статус, чисто фильтр по НЕ-завершённым).

    Сортировка — updated_at DESC, фолбэк created_at, тай-брейк id DESC:
    тот же принцип, что уже используется в list_leads_by_user() ниже (для
    клиентского "Мои заявки") — недавняя активность (смена статуса,
    supplement, материал, ответ дизайнера, теперь и просто открытие
    карточки владельцем) должна поднимать заявку наверх рабочей очереди,
    а не оставлять её на месте по порядку создания (см. UX-аудит "Заявки
    как рабочая очередь").

    Tier 0 — pure read, без lock."""
    leads = await _read_leads()
    if status == "ACTIVE":
        leads = [l for l in leads if l.get("status") in ACTIVE_LEAD_STATUSES]
    elif status and status != "ALL":
        leads = [l for l in leads if l.get("status") == status]
    return sorted(leads, key=lambda l: (l.get("updated_at") or l["created_at"], l["id"]), reverse=True)


async def list_leads_by_user(user_id: int) -> list[dict]:
    """Для клиентского "Мои заявки" — user_id должен быть уже проверен через
    bot.telegram_auth.validate_init_data ДО вызова этой функции, здесь
    доверие к нему не проверяется повторно (это ответственность вызывающего
    HTTP-хендлера, не хранилища).

    Сортировка: updated_at DESC, при равенстве — id DESC (см. UX-аудит про
    "Мои заявки" — заявка с недавней активностью дизайнера — статус,
    supplement, owner_message, материал — должна подниматься выше, а не
    оставаться на месте по порядку создания). updated_at — None у ещё ни
    разу не менявшихся заявок (см. add_lead) — в этом случае единственная
    известная точка активности это сам момент создания, поэтому используем
    created_at как фолбэк. ISO 8601-строки (везде datetime.now(timezone.utc)
    .isoformat(), один и тот же формат) сравниваются лексикографически
    корректно — парсинг в datetime не нужен. list_leads() (для /admin)
    сортировку не меняет — это отдельная функция, здесь не затронута.

    Tier 0 — pure read, без lock."""
    leads = [l for l in await _read_leads() if l.get("telegram", {}).get("user_id") == user_id]
    return sorted(leads, key=lambda l: (l.get("updated_at") or l["created_at"], l["id"]), reverse=True)


async def get_lead(lead_id: int) -> dict | None:
    return next((l for l in await _read_leads() if l["id"] == lead_id), None)


async def update_lead_status(actor_chat_id: int | str, lead_id: int, status: str) -> bool:
    _require_designer(actor_chat_id)
    if status not in LEAD_STATUSES:
        return False
    async with _lock("leads.json"):
        leads = await _read_leads()
        lead = next((l for l in leads if l["id"] == lead_id), None)
        if lead is None:
            return False
        lead["status"] = status
        lead["updated_at"] = datetime.now(timezone.utc).isoformat()
        await _write_leads(leads)
        return True


async def add_owner_message(actor_chat_id: int | str, lead_id: int, text: str, delivery_status: str) -> dict | None:
    """Ответ дизайнера клиенту (bot/handlers/admin.py::lead_reply_send) —
    append-only, тот же паттерн, что и add_lead_supplement/record_lead_material
    выше: не трогает payload/supplements/materials, отдельный независимый
    поток на lead. delivery_status ("sent"/"failed") приходит от вызывающего
    кода уже готовым — на момент вызова этой функции попытка отправки через
    Bot API уже сделана, здесь только сохраняем факт и её результат, чтобы
    ответ не терялся из истории заявки даже если Telegram-доставка не удалась
    (клиент заблокировал бота и т.п.)."""
    _require_designer(actor_chat_id)
    async with _lock("leads.json"):
        leads = await _read_leads()
        lead = next((l for l in leads if l["id"] == lead_id), None)
        if lead is None:
            return None
        messages = lead.setdefault("owner_messages", [])
        next_id = max((m["id"] for m in messages), default=0) + 1
        messages.append({
            "id": next_id,
            "text": text,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "delivery_status": delivery_status,
        })
        lead["updated_at"] = datetime.now(timezone.utc).isoformat()
        await _write_leads(leads)
        return lead


async def delete_lead(actor_chat_id: int | str, lead_id: int) -> bool:
    _require_designer(actor_chat_id)
    async with _lock("leads.json"):
        leads = await _read_leads()
        before = len(leads)
        leads = [l for l in leads if l["id"] != lead_id]
        if len(leads) == before:
            return False
        await _write_leads(leads)
        return True


# ---- Бэкап (экспорт/восстановление через .zip в Telegram) ----
# На бесплатном Render нет персистентного диска (см. render.yaml/README) —
# без внешнего сервиса единственный бесплатный способ пережить redeploy:
# дизайнер вручную выгружает .zip себе в Telegram (Telegram сам сохраняет
# присланный документ у получателя) и загружает его обратно после деплоя.
# Покрывает и data/*.json, и загруженные фото — то, что Upstash-режим
# выше не покрывает вовсе.

# Минимальная structural-валидация бэкапа (P1-1, production-hardening
# аудит) — только то, что остальной код content_store уже безусловно
# предполагает (прямая индексация data["key"] без .get, см. list_cases/
# list_faq/list_services/_read_leads/get_ui_config и т.п. выше). Намеренно
# НЕ бизнес-валидация — лишняя строгость сделала бы старый легитимный
# бэкап "несовместимым". about.json нигде не индексируется по
# фиксированному обязательному ключу (только произвольный field через
# data.get(...)/data[field]) — для него достаточно проверки "это object".
_REQUIRED_SHAPE: dict[str, dict[str, type]] = {
    "portfolio.json": {"cases": list, "types": list},
    "pricing.json": {"services": list, "options": list, "coefficients": dict, "rounding": dict},
    "faq.json": {"faq": list},
    "ui_config.json": {"menu": dict},
    "leads.json": {"leads": list},
}

_MISSING = object()  # sentinel для snapshot: ключа не было и до restore (P0-1) — откатывать нечего


class BackupExportError(RuntimeError):
    """export_backup_bytes не смог прочитать один или несколько
    DATA_FILENAMES (обычно UpstashKeyMissingError после P0-1) — экспорт НЕ
    формируется частично: явная ошибка лучше, чем ZIP без, например,
    leads.json, который могли бы принять за полноценный бэкап
    (P1-1, production-hardening аудит)."""

    def __init__(self, missing_filenames: list[str]):
        self.missing_filenames = missing_filenames
        super().__init__(f"Резервная копия невозможна: отсутствуют данные {', '.join(missing_filenames)}")


class BackupValidationError(ValueError):
    """Post-parse валидация (JSON parse ИЛИ minimal shape) не прошла для
    файла из архива restore — поднимается ДО единой записи (Phase 0).
    Означает, что ВЕСЬ restore отменяется целиком (all-or-nothing) —
    тихий skip одного битого файла с восстановлением остальных больше не
    допускается (P1-1, production-hardening аудит)."""

    def __init__(self, filename: str, reason: str, found_filenames: list[str]):
        super().__init__(f"{filename}: {reason}")
        self.filename = filename
        self.reason = reason
        self.found_filenames = found_filenames


class BackupSnapshotError(RuntimeError):
    """Phase 1 (снапшот текущих значений перед записью) во время restore
    упал НЕ из-за UpstashKeyMissingError (та сохраняет свою существующую
    _MISSING-семантику ниже) — а из-за какой-то другой ошибки чтения (сеть,
    Upstash-side error-ответ, повреждённый JSON уже хранящегося значения).
    Restore отменяется целиком ДО первой записи — Phase 2 не начинается
    вовсе (P2-6, production-hardening аудит, второй design review)."""

    def __init__(self, filename: str):
        self.filename = filename
        super().__init__(f"Резервная копия не восстановлена: не удалось прочитать текущее значение {filename!r} перед записью (снапшот) — изменения не применены")


class BackupRestoreFailedError(RuntimeError):
    """Запись JSON-файла (Phase 2) во время restore упала уже ПОСЛЕ
    успешной Phase 0 (валидация) и Phase 1 (снапшот) — попытка отката уже
    записанных файлов к снапшоту сделана; rollback_failed непустой значит,
    что и сам откат не полностью удался, и часть production-данных могла
    остаться в промежуточном состоянии — нужна ручная проверка
    (P1-1, production-hardening аудит)."""

    def __init__(self, failed_filename: str, rolled_back: list[str], rollback_failed: list[str]):
        self.failed_filename = failed_filename
        self.rolled_back = rolled_back
        self.rollback_failed = rollback_failed
        if rollback_failed:
            msg = f"restore прерван на {failed_filename!r}, ОТКАТ НЕ УДАЛСЯ для: {rollback_failed}"
        else:
            msg = f"restore прерван на {failed_filename!r}, откат выполнен успешно для: {rolled_back}"
        super().__init__(msg)


@dataclass
class BackupImportResult:
    restored_json: list[str]
    missing_json: list[str]
    restored_images: list[str]
    failed_images: list[str]


def _validate_shape(filename: str, data: Any) -> str | None:
    """Возвращает текст причины, если структура не проходит минимальную
    проверку, иначе None. См. _REQUIRED_SHAPE — намеренно не проверяет
    ничего сверх уже существующих допущений остального кода."""
    if not isinstance(data, dict):
        return "верхний уровень должен быть object (dict)"
    for key, expected_type in _REQUIRED_SHAPE.get(filename, {}).items():
        if key not in data:
            return f"отсутствует обязательный ключ {key!r}"
        if not isinstance(data[key], expected_type):
            return f"ключ {key!r} должен быть {expected_type.__name__}"
    return None


def _write_image_atomic(dest: Path, content: bytes) -> None:
    """Тот же паттерн, что и _write_local для JSON (temp-файл + os.replace)
    — обрыв записи не оставляет старое фото заменённым наполовину
    испорченными байтами: старый файл не трогается, пока новый полностью
    не готов (P1-1, production-hardening аудит). Локальный диск, не сеть —
    остаётся синхронной (P1-1 design review, "internal helpers... могут
    остаться sync, если они не делают network I/O напрямую"), вызывается
    вне locks (см. import_backup_bytes Phase 3)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, dest)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


async def export_backup_bytes() -> bytes:
    """Multi-key (Tier 2): держит все 6 locks ТОЛЬКО на время чтения всех
    DATA_FILENAMES в память (см. P2-7 design review) — сборка самого .zip
    (CPU/диск, не Upstash) намеренно происходит уже ПОСЛЕ освобождения
    locks: данные уже полностью и консистентно собраны в `collected`,
    удерживать locks дольше не даёт никакой дополнительной гарантии, только
    без нужды продлевает окно блокировки для остальных операций."""
    missing: list[str] = []
    collected: dict[str, Any] = {}
    async with _lock(*DATA_FILENAMES):
        for name in DATA_FILENAMES:
            try:
                collected[name] = await _read(name)
            except (FileNotFoundError, UpstashKeyMissingError):
                missing.append(name)
    if missing:
        raise BackupExportError(missing)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in collected.items():
            zf.writestr(f"data/{name}", json.dumps(data, ensure_ascii=False, indent=2))
        for img_dir, prefix in ((IMG_PORTFOLIO_DIR, "img/portfolio"), (IMG_ABOUT_DIR, "img/about")):
            if not img_dir.exists():
                continue
            for f in sorted(img_dir.iterdir()):
                if f.is_file():
                    zf.write(f, f"{prefix}/{f.name}")
    return buf.getvalue()


async def import_backup_bytes(actor_chat_id: int | str, zip_bytes: bytes) -> BackupImportResult:
    """Восстанавливает data/*.json и фото из .zip, созданного export_backup_bytes.

    Phase 0 — архив читается и полностью валидируется (parse + minimal
    shape) ДО единой записи; любая ошибка отменяет ВЕСЬ restore. Никакого
    storage I/O, никаких locks.
    Phase 1 — снапшот текущих значений файлов, которые будут перезаписаны
    (тоже только чтение). UpstashKeyMissingError -> _MISSING (нечего
    откатывать, как и раньше). Любая ДРУГАЯ ошибка чтения -> BackupSnapshotError,
    restore отменяется целиком, Phase 2 не начинается (P2-6).
    Phase 2 — сама запись JSON; при ошибке уже записанные файлы
    откатываются к снапшоту (best-effort, без тяжёлой transaction-
    инфраструктуры — Upstash REST не поддерживает multi-key транзакции).
    Phase 1 и Phase 2 выполняются ПОД ОДНИМ приобретением ВСЕХ 6 locks
    (Tier 2, P2-7 design review) — единственный способ гарантировать, что
    ни один content_store reader/writer внутри процесса не увидит restore
    "наполовину": другой вызов либо выполняется целиком до этого restore,
    либо целиком после.
    Phase 3 — изображения, ПОСЛЕ освобождения locks: локальные файлы, не
    часть content_store/Upstash-консистентности, между собой не
    транзакционны (по решению аудита — не изображать транзакционность там,
    где её нет), ошибка одного файла не трогает остальные и не откатывает
    уже восстановленный JSON.

    Имена файлов в архиве берутся только по basename (без пути) и данные —
    только по белому списку DATA_FILENAMES, чтобы вредоносный/повреждённый
    .zip не мог записать что-то за пределами ожидаемых файлов (zip-slip)."""
    _require_designer(actor_chat_id)

    # ---- Phase 0a: прочитать и классифицировать архив целиком — без
    # парсинга JSON и без единой записи.
    raw_json: dict[str, bytes] = {}
    image_entries: list[tuple[str, Path, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if name.startswith("data/") and name.endswith(".json"):
                filename = name[len("data/"):]
                if filename not in DATA_FILENAMES:
                    continue
                raw_json[filename] = zf.read(info)
            elif name.startswith("img/portfolio/"):
                image_entries.append((name, IMG_PORTFOLIO_DIR / Path(name).name, zf.read(info)))
            elif name.startswith("img/about/"):
                image_entries.append((name, IMG_ABOUT_DIR / Path(name).name, zf.read(info)))
            # прочие записи в архиве — вне whitelist'а, безопасно игнорируются

    found_json_filenames = list(raw_json.keys())

    # ---- Phase 0b: parse + shape-валидация ВСЕХ найденных JSON — любая
    # ошибка отменяет ВЕСЬ restore целиком, ни одной записи ещё не было.
    json_plan: dict[str, Any] = {}
    for filename, content in raw_json.items():
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise BackupValidationError(filename, f"повреждённый JSON ({e})", found_json_filenames) from e
        shape_error = _validate_shape(filename, data)
        if shape_error:
            raise BackupValidationError(filename, shape_error, found_json_filenames)
        json_plan[filename] = data

    missing_json = [f for f in DATA_FILENAMES if f not in json_plan]

    # ---- Phase 1 + Phase 2 (+ rollback) — под всеми 6 locks, приобретёнными
    # ДО первого storage I/O (см. докстринг функции выше).
    async with _lock(*DATA_FILENAMES):
        # ---- Phase 1: снапшот
        snapshot: dict[str, Any] = {}
        for filename in json_plan:
            try:
                snapshot[filename] = await _read(filename)
            except UpstashKeyMissingError:
                snapshot[filename] = _MISSING
            except Exception as e:
                logger.exception("Storage: backup snapshot failed for %s — restore aborted before any write", filename)
                raise BackupSnapshotError(filename) from e

        # ---- Phase 2: запись JSON. Ошибка -> откат уже записанных к снапшоту.
        written: list[str] = []
        failed_filename: str | None = None
        write_error: Exception | None = None
        for filename, data in json_plan.items():
            try:
                await _write(filename, data)
                written.append(filename)
            except Exception as e:
                failed_filename = filename
                write_error = e
                break

        if write_error is not None:
            rolled_back: list[str] = []
            rollback_failed: list[str] = []
            for filename in written:
                if snapshot.get(filename) is _MISSING:
                    continue
                try:
                    await _write(filename, snapshot[filename])
                    rolled_back.append(filename)
                except Exception:
                    logger.exception("Storage: backup rollback FAILED for %s — требуется ручная проверка данных", filename)
                    rollback_failed.append(filename)
            if rollback_failed:
                logger.error("Storage: backup restore aborted on %s, ROLLBACK INCOMPLETE for: %s", failed_filename, rollback_failed)
            else:
                logger.error("Storage: backup restore aborted on %s, rollback succeeded for: %s", failed_filename, rolled_back)
            raise BackupRestoreFailedError(failed_filename, rolled_back, rollback_failed) from write_error

    # ---- Phase 3: изображения — ПОСЛЕ освобождения locks, только после
    # успешного завершения Phase 2 целиком.
    restored_images: list[str] = []
    failed_images: list[str] = []
    for name, dest, content in image_entries:
        try:
            _write_image_atomic(dest, content)
            restored_images.append(name)
        except Exception:
            logger.exception("Storage: backup image restore failed for %s (JSON restore already succeeded)", name)
            failed_images.append(name)

    return BackupImportResult(
        restored_json=list(json_plan.keys()),
        missing_json=missing_json,
        restored_images=restored_images,
        failed_images=failed_images,
    )
