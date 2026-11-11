"""Shared validation for HTTP and legacy Telegram lead submissions."""

def valid_new_lead_payload(payload: object) -> bool:
    """Reject malformed external input before persisting or formatting it."""
    if not isinstance(payload, dict):
        return False
    if payload.get("mode", "new") != "new":
        return False
    # A valid signed initData must not permit empty leads using new draft IDs.
    if not any(isinstance(payload.get(k), str) and payload[k].strip()
               for k in ("service_name", "task_description")):
        return False
    text_fields = (
        "service_id", "service_name", "task_description", "deadline",
        "budget", "contact", "source", "source_case_id",
        "source_case_title", "draft_id",
    )
    for key in text_fields:
        value = payload.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 5000):
            return False
    if "attach_tz" in payload and not isinstance(payload["attach_tz"], bool):
        return False
    have = payload.get("have", [])
    if not isinstance(have, list) or len(have) > 30 or any(
        not isinstance(value, str) or len(value) > 200 for value in have
    ):
        return False
    tz = payload.get("tz_details")
    if tz is not None and (
        not isinstance(tz, dict) or
        any(not isinstance(value, str) or len(value) > 5000 for value in tz.values())
    ):
        return False
    calc = payload.get("calc")
    if calc is None:
        return True
    if not isinstance(calc, dict) or not isinstance(calc.get("service_id"), str):
        return False
    if len(calc["service_id"]) > 150:
        return False
    selected = calc.get("options", [])
    if not isinstance(selected, list) or len(selected) > 100:
        return False
    for item in selected:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return False
        qty = item.get("qty", 1)
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= 100:
            return False
    for name in ("urgent", "complex"):
        if name in calc and not isinstance(calc[name], bool):
            return False
    return True

