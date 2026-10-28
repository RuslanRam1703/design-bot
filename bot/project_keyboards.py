"""Native, owner-only inline controls for projects created from leads."""
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from bot import project_store


def project_list(items: list[dict], page: int = 0, page_size: int = 8) -> K:
    pages = max(1, (len(items) + page_size - 1) // page_size)
    page = min(max(page, 0), pages - 1)
    rows = []
    for project in items[page * page_size:(page + 1) * page_size]:
        label = "📂 " + project["id"] + " · " + project["title"][:34]
        rows.append([B(text=label[:64], callback_data=f"pj:open:{project['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(B(text="◀️", callback_data=f"pj:page:{page-1}"))
        nav.append(B(text=f"{page+1}/{pages}", callback_data="pj:noop"))
        if page + 1 < pages:
            nav.append(B(text="▶️", callback_data=f"pj:page:{page+1}"))
        rows.append(nav)
    rows.append([B(text="◀️ Админ-меню", callback_data="pj:root")])
    return K(inline_keyboard=rows)


def project_detail(project: dict) -> K:
    rows = [
        [B(text="💰 Стоимость", callback_data="pj:price"),
         B(text="📅 Срок", callback_data="pj:deadline")],
        [B(text="📍 Этап", callback_data="pj:stage"),
         B(text="🔄 Статус", callback_data="pj:status")],
        [B(text="➕ Учесть платёж", callback_data="pj:pay"),
         B(text="🧾 Платежи", callback_data="pj:payments")],
        [B(text="🕘 История изменений", callback_data="pj:history")],
    ]
    rows.append([B(text="◀️ К проектам", callback_data="adminmenu:projects")])
    return K(inline_keyboard=rows)


def choice_keyboard(kind: str, choices: list[tuple[str, str]]) -> K:
    return K(inline_keyboard=[
        *[[B(text=label, callback_data=f"pj:{kind}:{value}")] for value, label in choices],
        [B(text="◀️ Назад к проекту", callback_data="pj:back")],
    ])


def confirm_keyboard(kind: str, *, destructive: bool = False) -> K:
    return K(inline_keyboard=[
        [B(text="✅ Подтвердить" if not destructive else "🗑 Подтвердить удаление", callback_data=f"pj:{kind}:yes")],
        [B(text="◀️ Отмена", callback_data=f"pj:{kind}:no")],
    ])


def cancel_keyboard() -> K:
    return K(inline_keyboard=[[B(text="◀️ Назад к проекту", callback_data="pj:back")]])


def payments_keyboard(project: dict) -> K:
    rows = [
        [B(text=f"↩️ Аннулировать №{p['id']} ({p['amount']} ₽)", callback_data=f"pj:void:{p['id']}")]
        for p in project.get("payments", [])[-12:] if not p.get("voided_at")
    ]
    rows.append([B(text="◀️ К проекту", callback_data="pj:back")])
    return K(inline_keyboard=rows)
