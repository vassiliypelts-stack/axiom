"""WhatsApp-мост AXIOM (Python-сторона).

Канал WhatsApp держит Node-сервис на Baileys (whatsapp/index.js) — он умеет только
коннект к WhatsApp, QR-логин и отправку/приём сообщений. Весь «мозг» (ИИ-агент,
книжка, встречи) живёт здесь, в Python, и переиспользуется из телеграм-канала —
логика 1-в-1, чтобы каналы не разъезжались.

Node ↔ Python общаются по HTTP:
  GET  /wa/outreach?limit=N  → кого и чем писать первым (status='new', есть телефон)
  POST /wa/sent              → Node отчитался, что отправил первое сообщение
  POST /wa/incoming          → входящее от лида → ответ ИИ-агента (+ запись в книжку)

Запуск:
    python -m channels.wa_bridge            # на 127.0.0.1:8100
    python -m channels.wa_bridge --port 8100
"""
from __future__ import annotations

import argparse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import config
from agent.agent import generate_reply
from db import database
from integrations import meetings
# Переиспользуем чистые хелперы телеграм-канала (без Telethon-специфики):
from channels.telegram import (
    _contact_dict,
    _default_slots,
    _first_message_parts,
    _history_for_agent,
)
# Единый рендер первого сообщения кампании (спинтакс {a|b} + {name}/{agency}/{decision}
# + человечность) — тот же, что в Telegram, чтобы каналы не разъезжались.
from channels.campaign_send import (
    _parts as _render_parts,
    _greeting as _cs_greeting,
    _decision_phrase,
)

app = FastAPI(title="AXIOM WhatsApp bridge")


class Incoming(BaseModel):
    jid: str
    phone: str | None = None
    push_name: str | None = None
    text: str


class Sent(BaseModel):
    contact_id: int
    jid: str | None = None
    text: str = ""
    cid: int | None = None          # кампания (чтобы зафиксировать campaign_contacts)
    account_id: int | None = None   # с какого аккаунта отправлено


@app.get("/wa/campaign_outreach")
def wa_campaign_outreach(cid: int, limit: int = 10) -> JSONResponse:
    """Кому слать ПЕРВОЕ сообщение кампании по WhatsApp + готовые части сообщения.
    Берёт аудиторию кампании (тег + есть WhatsApp), ещё не охваченных в WA (wa_jid пуст)."""
    with database.get_conn() as conn:
        camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not camp:
            return JSONResponse({"contacts": []})
        camp = dict(camp)
        tag = camp.get("audience_tag")
        where = ("has_wa IN ('yes','unknown') AND phone IS NOT NULL AND phone<>'' "
                 "AND (wa_jid IS NULL OR wa_jid='')")
        params: list = []
        if tag:
            where += " AND tags LIKE ?"
            params.append(f"%{tag}%")
        rows = conn.execute(f"SELECT * FROM contacts WHERE {where} ORDER BY id LIMIT ?", (*params, limit)).fetchall()
    tmpl = camp.get("message_template") or ""
    out = []
    for r in rows:
        ag = (r["agency"] if "agency" in r.keys() and r["agency"] else None) or r["name"] or ""
        parts = _render_parts(tmpl, _cs_greeting(r), ag, _decision_phrase(r))
        out.append({"contact_id": r["id"], "phone": r["phone"], "parts": parts})
    return JSONResponse({"contacts": out, "cid": cid, "account_id": camp.get("account_id")})


class Mark(BaseModel):
    contact_id: int
    has_wa: str            # 'yes' | 'no'
    jid: str | None = None


@app.get("/wa/to_check")
def to_check(limit: int = 100) -> JSONResponse:
    """Контакты с телефоном, по которым ещё не проверено наличие WhatsApp."""
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, phone FROM contacts "
            "WHERE phone IS NOT NULL AND phone <> '' "
            "AND (has_wa IS NULL OR has_wa = 'unknown') AND wa_jid IS NULL "
            "ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    return JSONResponse({"contacts": [{"contact_id": r["id"], "phone": r["phone"]} for r in rows]})


@app.post("/wa/mark")
def mark(m: Mark) -> JSONResponse:
    """Результат проверки номера в WhatsApp: has_wa + (если есть) wa_jid."""
    with database.get_conn() as conn:
        if m.has_wa == "yes" and m.jid:
            conn.execute(
                "UPDATE contacts SET has_wa='yes', wa_jid=?, checked_at=datetime('now'), "
                "updated_at=datetime('now') WHERE id=?",
                (m.jid, m.contact_id),
            )
        else:
            conn.execute(
                "UPDATE contacts SET has_wa=?, checked_at=datetime('now'), "
                "updated_at=datetime('now') WHERE id=?",
                (m.has_wa, m.contact_id),
            )
    return JSONResponse({"ok": True})


@app.get("/wa/outreach")
def outreach(limit: int = 0) -> JSONResponse:
    """Кого писать первым по WhatsApp: новые контакты с телефоном, ещё не охваченные.
    Соблюдает дневной лимит первых сообщений (антибан)."""
    cap = min(limit or config.DAILY_FIRST_MESSAGES, config.DAILY_FIRST_MESSAGES)
    out: list[dict] = []
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM contacts "
            "WHERE status = 'new' AND phone IS NOT NULL AND wa_jid IS NULL "
            "AND has_wa IN ('yes','unknown') "
            "ORDER BY id LIMIT ?",
            (cap,),
        ).fetchall()
    for r in rows:
        out.append({"contact_id": r["id"], "phone": r["phone"], "parts": _first_message_parts(r)})
    return JSONResponse({"contacts": out})


@app.post("/wa/sent")
def sent(payload: Sent) -> JSONResponse:
    """Node отправил первое сообщение → фиксируем jid, пишем в книжку, двигаем статус."""
    with database.get_conn() as conn:
        if payload.jid:
            database.set_wa_jid(conn, payload.contact_id, payload.jid)
        if payload.text:
            database.add_message(conn, payload.contact_id, "out", payload.text, intent=None)
        database.set_status(conn, payload.contact_id, "messaged")
        if payload.cid:
            conn.execute(
                "INSERT OR IGNORE INTO campaign_contacts (campaign_id, contact_id, account_id) VALUES (?,?,?)",
                (payload.cid, payload.contact_id, payload.account_id),
            )
    return JSONResponse({"ok": True})


@app.post("/wa/incoming")
def incoming(msg: Incoming) -> JSONResponse:
    """Входящее от лида → ответ ИИ-агента. Возвращает части ответа для Node."""
    with database.get_conn() as conn:
        contact = database.find_contact_by_wa(conn, jid=msg.jid, phone=msg.phone)
        if contact is None:
            return JSONResponse({"ignore": True, "reason": "not in book"})
        contact_id = contact["id"]
        if not contact["wa_jid"]:
            database.set_wa_jid(conn, contact_id, msg.jid)
        opener, history = _history_for_agent(database.get_history(conn, contact_id))
        contact_info = _contact_dict(contact)
        camp = database.get_contact_campaign(conn, contact_id)
        campaign_prompt = camp["agent_prompt"] if camp else None
        extra_context = contact["agent_context"] if "agent_context" in contact.keys() else None

    history.append({"role": "user", "content": msg.text})

    try:
        reply = generate_reply(history, _default_slots(), contact_info, opener, campaign_prompt, extra_context)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)

    reply_text = "\n".join(p.strip() for p in reply.reply_parts if p.strip())

    meeting = None
    if reply.meeting_agreed:
        try:
            meeting = meetings.arrange(contact_info, reply.proposed_datetime)
        except Exception as e:  # noqa: BLE001
            print(f"[wa meeting error] contact {contact_id}: {e}")

    with database.get_conn() as conn:
        database.add_message(conn, contact_id, "in", msg.text, intent=reply.intent)
        database.add_message(conn, contact_id, "out", reply_text, intent=None)
        if meeting is not None:
            database.record_meeting(
                conn, contact_id, meeting.meeting_at_iso, reply.notes,
                zoom_link=meeting.zoom_link, calendar_event_id=meeting.calendar_event_id,
            )
        elif reply.intent == "not_interested":
            database.set_status(conn, contact_id, "nurture")
        else:
            database.set_status(conn, contact_id, "in_dialog")

    extra_parts: list[str] = []
    if meeting is not None and meeting.zoom_link:
        extra_parts = [f"закинул ссылку на zoom: {meeting.zoom_link}", "до созвона напомню)"]

    print(f"[wa reply -> {contact_info.get('name', contact_id)}] "
          f"intent={reply.intent} agreed={reply.meeting_agreed}")
    return JSONResponse({
        "reply_parts": reply.reply_parts,
        "extra_parts": extra_parts,
        "intent": reply.intent,
        "meeting_agreed": reply.meeting_agreed,
    })


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM WhatsApp bridge (Python)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8100)
    args = p.parse_args()
    from agent import llm
    if not llm.available(config.AGENT_MODEL):
        print(f"Нет ключа под модель «{config.AGENT_MODEL}» в .env — агент не сможет отвечать.")
        return
    database.init_db()
    import uvicorn
    print(f"AXIOM WhatsApp bridge -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
