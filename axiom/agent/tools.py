"""H3 — реестр инструментов агента по категориям («78 tools» в духе ЛидКадр Дениса).

Идея: вместо 4 захардкоженных tool'ов (как в NeuroAgents) — расширяемый каталог
функций, сгруппированный по категориям (коммуникация / CRM / встречи / платежи /
эскалация / база знаний). Агент на tool-use сам выбирает нужный и вызывает; мы
исполняем через `dispatch`. Это ФУНДАМЕНТ: здесь реестр + рабочие обработчики
CRM/эскалации (пишут в нашу БД). Подключение в живой диалог (agent.agent.generate_reply)
— отдельным шагом, чтобы не трогать работающего агента раньше времени.

Каждый инструмент: {name, category, description, input_schema, handler(args, ctx)->dict}.
ctx — словарь с contact_id и пр. (что нужно обработчику).

Использование:
    from agent import tools
    schemas = tools.anthropic_tools(["crm", "escalation"])   # для API (tools=...)
    result  = tools.dispatch("crm_set_stage", {"stage": "won"}, {"contact_id": 5})
"""
from __future__ import annotations

from typing import Any, Callable

from db import database

# Категории (порядок = как показывать в UI). Расширяется свободно.
CATEGORIES = ["communication", "crm", "meetings", "payments", "escalation", "knowledge"]

_REGISTRY: dict[str, dict] = {}


def tool(name: str, category: str, description: str, input_schema: dict):
    """Декоратор регистрации инструмента в каталоге."""
    def deco(fn: Callable[[dict, dict], dict]):
        _REGISTRY[name] = {
            "name": name, "category": category, "description": description,
            "input_schema": input_schema, "handler": fn,
        }
        return fn
    return deco


# --------------------------------------------------------------- CRM (рабочие) #
@tool("crm_set_stage", "crm", "Перевести лида на стадию воронки (new/in_dialog/meeting_set/met/won/lost/nurture/stop).",
      {"type": "object", "properties": {"stage": {"type": "string"}}, "required": ["stage"]})
def _crm_set_stage(args: dict, ctx: dict) -> dict:
    cid = ctx.get("contact_id")
    if not cid:
        return {"ok": False, "error": "нет contact_id"}
    with database.get_conn() as conn:
        database.set_status(conn, cid, args["stage"])
    return {"ok": True, "stage": args["stage"]}


@tool("crm_add_tag", "crm", "Добавить тег лиду (накопительно, не затирает старые).",
      {"type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"]})
def _crm_add_tag(args: dict, ctx: dict) -> dict:
    cid = ctx.get("contact_id")
    if not cid:
        return {"ok": False, "error": "нет contact_id"}
    with database.get_conn() as conn:
        row = conn.execute("SELECT tags FROM contacts WHERE id=?", (cid,)).fetchone()
        old = (row["tags"] if row else "") or ""
        tag = args["tag"].strip()
        if tag and tag not in old:
            new = f"{old}, {tag}" if old else tag
            conn.execute("UPDATE contacts SET tags=?, updated_at=datetime('now') WHERE id=?", (new, cid))
    return {"ok": True, "tag": args.get("tag")}


@tool("crm_set_score", "crm", "Проставить AI-скоринг лида 0..1 (насколько горячий/целевой).",
      {"type": "object", "properties": {"score": {"type": "number"}}, "required": ["score"]})
def _crm_set_score(args: dict, ctx: dict) -> dict:
    cid = ctx.get("contact_id")
    if not cid:
        return {"ok": False, "error": "нет contact_id"}
    score = max(0.0, min(1.0, float(args["score"])))
    with database.get_conn() as conn:
        conn.execute("UPDATE contacts SET score=?, updated_at=datetime('now') WHERE id=?", (score, cid))
    return {"ok": True, "score": score}


# --------------------------------------------------------- эскалация (рабочая) #
@tool("escalate_to_human", "escalation", "Позвать живого человека (оператора) — когда нужен человек/деньги/нестандарт.",
      {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]})
def _escalate(args: dict, ctx: dict) -> dict:
    cid = ctx.get("contact_id")
    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO events (type, level, title, text, contact_id) VALUES ('info','warn','Нужен человек',?,?)",
            (args.get("reason", ""), cid),
        )
    return {"ok": True, "escalated": True}


# ---------------------------------------------- остальное: схемы-заглушки (TODO) #
# Зарегистрированы для каталога/UI и подключения позже. handler пока возвращает
# {"ok": False, "todo": ...} — реальное исполнение делается в канале (коммуникация)
# или требует интеграций (платежи). Это честный «скелет 78 tools».
def _stub(name: str):
    def fn(args: dict, ctx: dict) -> dict:
        return {"ok": False, "todo": f"{name}: исполнение ещё не подключено"}
    return fn


_STUBS = [
    ("send_photo", "communication", "Отправить фото/картинку товара."),
    ("send_document", "communication", "Отправить документ/КП файлом."),
    ("typing_action", "communication", "Показать «печатает…» (человекоподобность)."),
    ("send_contact_card", "communication", "Отправить визитку-контакт."),
    ("book_meeting", "meetings", "Записать встречу (Zoom/слот) в календарь."),
    ("propose_slots", "meetings", "Предложить свободные слоты времени."),
    ("create_invoice", "payments", "Выставить счёт/ссылку на оплату (Stars/CloudPayments)."),
    ("get_product_info", "knowledge", "Достать карточку продукта/ответ из базы знаний."),
]
for _n, _c, _d in _STUBS:
    _REGISTRY.setdefault(_n, {
        "name": _n, "category": _c, "description": _d,
        "input_schema": {"type": "object", "properties": {}},
        "handler": _stub(_n),
    })


# ------------------------------------------------------------------ публичное #
def anthropic_tools(categories: list[str] | None = None) -> list[dict]:
    """Список схем инструментов для Anthropic API (параметр tools=...).
    categories=None → все; иначе только из указанных категорий."""
    out = []
    for t in _REGISTRY.values():
        if categories and t["category"] not in categories:
            continue
        out.append({"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]})
    return out


def dispatch(name: str, args: dict, ctx: dict) -> dict:
    """Исполнить инструмент по имени. Неизвестный → {'ok':False}."""
    t = _REGISTRY.get(name)
    if not t:
        return {"ok": False, "error": f"нет инструмента {name}"}
    try:
        return t["handler"](args or {}, ctx or {})
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": str(ex)}


def catalog() -> dict[str, list[dict]]:
    """Каталог инструментов по категориям (для UI «инструменты агента»)."""
    by: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for t in _REGISTRY.values():
        impl = t["handler"].__name__ != "fn"  # эвристика: stub'ы зовутся 'fn'
        by.setdefault(t["category"], []).append(
            {"name": t["name"], "description": t["description"], "ready": impl})
    return by


def count() -> tuple[int, int]:
    """(всего инструментов, из них рабочих)."""
    total = len(_REGISTRY)
    ready = sum(1 for t in _REGISTRY.values() if t["handler"].__name__ != "fn")
    return total, ready
