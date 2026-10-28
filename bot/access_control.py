"""Owner-managed blocklist for a private commercial bot.

All state stays in the existing leads.json (backup and Redis lease/fencing).
A username-only watch is *not* a reliable identity: it matches the handle
currently presented by Telegram, which may have changed/reassigned.
The first matching interaction is pinned to a numeric ID; watchers then
disappear. No global customer quotas or automatic bans.
"""
import re
from datetime import datetime, timezone

from aiogram import BaseMiddleware

from bot import config, content_store

_USERNAME = re.compile(r"^[a-zA-Z0-9_]{5,32}$")


def normalize_username(value: str) -> str | None:
    handle = (value or "").strip().lstrip("@").lower()
    return handle if _USERNAME.fullmatch(handle) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def view() -> dict:
    try:
        document = await content_store._read("leads.json")
    except FileNotFoundError:
        # Fresh local/test storage: no users or blacklist exist yet.
        # UpstashKeyMissingError remains fatal: never bypass a missing
        # production Redis key after its initialization marker exists.
        return {"blocked_ids": {}, "watch_usernames": {}}
    return document.get("access_control") or {"blocked_ids": {}, "watch_usernames": {}}


async def history_matches(username: str) -> list[dict]:
    """Historical usernames are hints; never proof of current identity."""
    handle = normalize_username(username)
    if not handle:
        return []
    document = await content_store._read("leads.json")
    results = {}
    for lead in document.get("leads", []):
        account = lead.get("telegram") or {}
        if normalize_username(account.get("username") or "") == handle and isinstance(account.get("user_id"), int):
            results[account["user_id"]] = {
                "id": account["user_id"], "first_name": account.get("first_name") or "",
                "username": account.get("username") or "",
            }
    return list(results.values())


async def block_id(actor: int | str, user_id: int, username: str | None = None) -> bool:
    content_store._require_designer(actor)
    if type(user_id) is not int or user_id <= 0 or str(user_id) == config.DESIGNER_CHAT_ID:
        raise ValueError("Нельзя заблокировать этот ID")
    async with content_store._lock("leads.json"):
        document = await content_store._read("leads.json")
        access = document.setdefault("access_control", {"blocked_ids": {}, "watch_usernames": {}})
        if str(user_id) in access["blocked_ids"]:
            return False
        access["blocked_ids"][str(user_id)] = {"at": _now(), "username": normalize_username(username or "")}
        if username:
            access["watch_usernames"].pop(normalize_username(username), None)
        await content_store._write("leads.json", document)
        return True


async def block_username(actor: int | str, username: str) -> bool:
    content_store._require_designer(actor)
    handle = normalize_username(username)
    if not handle:
        raise ValueError("Укажите корректный @username (5–32 символа)")
    async with content_store._lock("leads.json"):
        document = await content_store._read("leads.json")
        access = document.setdefault("access_control", {"blocked_ids": {}, "watch_usernames": {}})
        if handle in access["watch_usernames"]:
            return False
        access["watch_usernames"][handle] = {"at": _now()}
        await content_store._write("leads.json", document)
        return True


async def unblock_id(actor: int | str, user_id: int) -> bool:
    content_store._require_designer(actor)
    async with content_store._lock("leads.json"):
        document = await content_store._read("leads.json")
        access = document.get("access_control") or {}
        blocked = access.get("blocked_ids") or {}
        if str(user_id) not in blocked:
            return False
        del blocked[str(user_id)]
        await content_store._write("leads.json", document)
        return True


async def remove_watch(actor: int | str, username: str) -> bool:
    content_store._require_designer(actor)
    handle = normalize_username(username)
    async with content_store._lock("leads.json"):
        document = await content_store._read("leads.json")
        watched = (document.get("access_control") or {}).get("watch_usernames") or {}
        if not handle or handle not in watched:
            return False
        del watched[handle]
        await content_store._write("leads.json", document)
        return True


async def is_blocked(user_id: int, username: str | None = None) -> bool:
    if str(user_id) == config.DESIGNER_CHAT_ID:
        return False
    access = await view()
    if str(user_id) in access.get("blocked_ids", {}):
        return True
    handle = normalize_username(username or "")
    if not handle or handle not in access.get("watch_usernames", {}):
        return False
    # Resolve watch atomically; a simultaneous update may have removed it.
    async with content_store._lock("leads.json"):
        document = await content_store._read("leads.json")
        access = document.get("access_control") or {}
        watched = access.get("watch_usernames") or {}
        if handle not in watched:
            return str(user_id) in access.get("blocked_ids", {})
        watched.pop(handle)
        blocked = access.setdefault("blocked_ids", {})
        blocked[str(user_id)] = {"at": _now(), "username": handle, "from_watch": True}
        await content_store._write("leads.json", document)
    return True


class AccessMiddleware(BaseMiddleware):
    """Runs before FSM handlers and all Telegram client callbacks."""
    async def __call__(self, handler, event, data):
        sender = getattr(event, "from_user", None)
        if sender and await is_blocked(sender.id, sender.username):
            # Acknowledge callback so Telegram stops the loading spinner.
            if hasattr(event, "answer") and hasattr(event, "data"):
                await event.answer("Доступ к боту ограничен", show_alert=True)
            return None
        return await handler(event, data)
