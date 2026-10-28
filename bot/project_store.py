"""Owner-only project ledger persisted alongside leads under leads.json.

No migration, new Redis key, or change to the public My Leads API.
All writes use the existing per-key distributed _lock and fenced _write.
Projects are created from customer leads. Previously stored project records
remain intact; payments stay append-only (void, do not erase).
Payments are MANUAL RECORDS, never proof of a bank transaction.
"""
from datetime import date, datetime, timezone

from bot import content_store

STATUSES = {
    "PLANNED": "Согласование",
    "IN_PROGRESS": "В работе",
    "REVIEW": "На согласовании",
    "ON_HOLD": "Приостановлен",
    "COMPLETED": "Завершён",
    "CANCELLED": "Отменён",
}
STAGES = ("Подготовка", "Дизайн", "Согласование", "Передача")
MAX_RUB = 100_000_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(actor):
    content_store._require_designer(actor)


async def _read_document() -> dict:
    try:
        return await content_store._read("leads.json")
    except FileNotFoundError:
        return {"leads": []}


def _history(project: dict, kind: str, detail: str) -> None:
    project.setdefault("history", []).append({
        "at": _now(), "kind": kind, "detail": detail,
    })
    project["updated_at"] = _now()


def _new_project(project_id: str, lead_id: int, title: str, client: str) -> dict:
    stamp = _now()
    result = {
        "id": project_id, "lead_id": lead_id,
        "title": title[:160], "client": client[:120],
        "status": "PLANNED", "stage_index": 0,
        "stages": list(STAGES), "price_rub": None, "deadline": None,
        "payments": [], "history": [], "created_at": stamp, "updated_at": stamp,
    }
    _history(result, "created", "Создан из заявки")
    return result


def total_paid(project: dict) -> int:
    return sum(p["amount"] for p in project.get("payments", []) if not p.get("voided_at"))


async def list_projects(actor) -> list[dict]:
    _require(actor)
    projects = (await _read_document()).get("projects", [])
    return sorted(projects, key=lambda p: (p["updated_at"], p["id"]), reverse=True)


async def get_project(actor, project_id: str) -> dict | None:
    _require(actor)
    return next((p for p in (await _read_document()).get("projects", []) if p["id"] == project_id), None)


async def create_from_lead(actor, lead_id: int) -> dict:
    _require(actor)
    async with content_store._lock("leads.json"):
        doc = await _read_document()
        lead = next((item for item in doc["leads"] if item["id"] == lead_id), None)
        if lead is None:
            raise ValueError("Заявка не найдена")
        if lead.get("status") == "CANCELLED":
            raise ValueError("Отменённую заявку нельзя перевести в проект")
        projects = doc.setdefault("projects", [])
        prior = next((p for p in projects if p.get("lead_id") == lead_id), None)
        if prior:
            return prior
        project = _new_project(
            f"P{lead_id}", lead_id,
            lead.get("payload", {}).get("service_name") or "Проект для оценки",
            lead.get("telegram", {}).get("first_name") or "Клиент",
        )
        projects.append(project)
        await content_store._write("leads.json", doc)
        return project


async def mutate(actor, project_id: str, operation: str, value=None) -> dict:
    """Validate every change and persist it atomically with history."""
    _require(actor)
    async with content_store._lock("leads.json"):
        doc = await _read_document()
        project = next((p for p in doc.get("projects", []) if p["id"] == project_id), None)
        if not project:
            raise ValueError("Проект не найден")
        if operation == "status":
            if value not in STATUSES:
                raise ValueError("Неизвестный статус")
            if project["status"] == value:
                return project
            project["status"] = value
            detail = STATUSES[value]
        elif operation == "stage":
            if type(value) is not int or not 0 <= value < len(project["stages"]):
                raise ValueError("Неизвестный этап")
            if project["stage_index"] == value:
                return project
            project["stage_index"] = value
            detail = project["stages"][value]
        elif operation == "price":
            if type(value) is not int or not 1 <= value <= MAX_RUB:
                raise ValueError("Стоимость должна быть от 1 до 100 млн ₽")
            if value < total_paid(project):
                raise ValueError("Стоимость меньше уже полученных платежей")
            if project["price_rub"] == value:
                return project
            project["price_rub"] = value
            detail = f"Согласованная стоимость {value} ₽"
        elif operation == "deadline":
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("Неверная дата")
                try:
                    d = date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError("Неверная дата, используйте ГГГГ-ММ-ДД") from exc
                if d.isoformat() != value:
                    raise ValueError("Неверный формат даты")
            if project["deadline"] == value:
                return project
            project["deadline"] = value
            detail = f"Срок: {value or 'не указан'}"
        elif operation == "pay":
            if type(value) is not int or not 1 <= value <= MAX_RUB:
                raise ValueError("Платёж должен быть положительным целым числом")
            if project["price_rub"] is None:
                raise ValueError("Сначала укажите согласованную стоимость")
            if total_paid(project) + value > project["price_rub"]:
                raise ValueError("Платёж превышает неоплаченный остаток")
            payments = project.setdefault("payments", [])
            payment_id = max((p["id"] for p in payments), default=0) + 1
            payments.append({"id": payment_id, "amount": value, "recorded_at": _now(), "voided_at": None})
            detail = f"Поступление №{payment_id}: {value} ₽ (ручной учёт)"
        elif operation == "void":
            if type(value) is not int:
                raise ValueError("Неверный номер платежа")
            payment = next((p for p in project.get("payments", []) if p["id"] == value), None)
            if payment is None or payment.get("voided_at"):
                raise ValueError("Платёж не найден или уже отменён")
            payment["voided_at"] = _now()
            detail = f"Аннулирован платёж №{value}: {payment['amount']} ₽"
        else:
            raise ValueError("Неизвестное действие")
        _history(project, operation, detail)
        await content_store._write("leads.json", doc)
        return project
