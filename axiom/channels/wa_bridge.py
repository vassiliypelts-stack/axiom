"""WhatsApp-мост AXIOM (Python-сторона).

Канал WhatsApp держит Node-сервис на Baileys (whatsapp/index.js) — по процессу на
номер. Он умеет только коннект к WhatsApp, привязку по коду и отправку/приём
сообщений. Весь «мозг» (ИИ-агент, книжка, встречи, правила кому отвечать) живёт
здесь и переиспользуется из телеграм-канала, чтобы каналы не разъезжались.

Роуты подключены прямо в веб-пульт (web/app.py → include_router), Node ходит на
http://127.0.0.1:8000. Пароль пульта для /wa/* не спрашивается, но только с
localhost — снаружи эти адреса закрыты (см. app._auth_gate).

Node ↔ Python:
  POST /wa/status              → состояние сокета номера (open/closed/logged_out/…)
  GET  /wa/outbox?account_id=N → следующее первое сообщение кампании для этого номера
  POST /wa/outbox/done         → итог отправки (sent / no_wa / failed)
  POST /wa/incoming            → входящее: записать в книжку, сказать, отвечать ли
  POST /wa/reply               → собрать ответ агента (после паузы «прочитал-печатает»)
  POST /wa/replied             → ответ доставлен — пишем его в книжку

Решение «кому писать первым» принимает channels/campaign_send (кладёт строки в
wa_outbox) — с теми же рабочими часами, лимитами и тест-режимом, что в Telegram.

Отдельный запуск (для отладки на локальной машине):
    python -m channels.wa_bridge --port 8100
"""
from __future__ import annotations

import argparse
import json

from fastapi import APIRouter, FastAPI
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
    _is_dangling,
    campaign_prompt_with_objections,
)
# Единый рендер первого сообщения кампании (спинтакс {a|b} + {name}/{agency}/{decision}
# + человечность) — тот же, что в Telegram, чтобы каналы не разъезжались.
from channels.campaign_send import (
    _parts as _render_parts,
    _greeting as _cs_greeting,
    _decision_phrase,
)

router = APIRouter()

# Минимальный зазор между двумя первыми сообщениями с одного WA-номера. Node и сам
# держит паузу 40-130 с между строками, но после рестарта процесса она обнуляется —
# этот зазор живёт в базе и рестарт его не обходит.
OUTBOX_GAP_MIN = 3


class Incoming(BaseModel):
    jid: str
    phone: str | None = None
    push_name: str | None = None
    text: str
    account_id: int | None = None


class Sent(BaseModel):
    contact_id: int
    jid: str | None = None
    text: str = ""
    cid: int | None = None          # кампания (чтобы зафиксировать campaign_contacts)
    account_id: int | None = None   # с какого аккаунта отправлено


class Status(BaseModel):
    account_id: int
    state: str                      # open | connecting | closed | logged_out | pairing
    me: str | None = None
    detail: str | None = None


class OutboxDone(BaseModel):
    id: int
    result: str                     # sent | no_wa | failed
    jid: str | None = None
    error: str | None = None


class ReplyReq(BaseModel):
    contact_id: int
    account_id: int


class Replied(BaseModel):
    contact_id: int
    account_id: int
    text: str


# ─────────────────────────── состояние номера ───────────────────────────

@router.post("/wa/status")
def wa_status(s: Status) -> JSONResponse:
    """Node сообщает, что с сокетом. open → номер привязан (wa_authed='yes');
    logged_out → привязку сняли с телефона, нужен новый код."""
    with database.get_conn() as conn:
        conn.execute("UPDATE accounts SET wa_state=?, wa_state_at=datetime('now') WHERE id=?",
                     (s.state, s.account_id))
        prev = conn.execute("SELECT wa_authed, label FROM accounts WHERE id=?",
                            (s.account_id,)).fetchone()
        if not prev:
            return JSONResponse({"ok": False, "error": "нет такого аккаунта"})
        who = prev["label"] or f"#{s.account_id}"
        if s.state == "open" and prev["wa_authed"] != "yes":
            conn.execute("UPDATE accounts SET wa_authed='yes' WHERE id=?", (s.account_id,))
            database.add_event(conn, "wa_on", f"🟢 WhatsApp подключён: {who}",
                               f"Номер привязан ({s.me or '?'}), слушаю входящие и беру "
                               f"очередь рассылки.", level="good", account_id=s.account_id)
        elif s.state == "logged_out" and prev["wa_authed"] == "yes":
            conn.execute("UPDATE accounts SET wa_authed='no' WHERE id=?", (s.account_id,))
            # Недоставленное с этого номера возвращаем в очередь кампании: строки
            # снимаем, контакты остаются 'new' и уйдут с другого номера команды.
            conn.execute("DELETE FROM wa_outbox WHERE account_id=? AND status IN "
                         "('pending','sending')", (s.account_id,))
            database.add_event(conn, "wa_off", f"🔴 WhatsApp отвязан: {who}",
                               "Привязку сняли на телефоне (или WhatsApp разлогинил "
                               "устройство). Нужен новый код в «Аккаунтах» → WA.",
                               level="warn", account_id=s.account_id)
    return JSONResponse({"ok": True})


# ─────────────────────────── очередь рассылки ───────────────────────────

@router.get("/wa/outbox")
def wa_outbox(account_id: int) -> JSONResponse:
    """Следующее первое сообщение для этого номера. Строку сразу помечаем 'sending',
    чтобы два опроса подряд не взяли её дважды."""
    with database.get_conn() as conn:
        # Снимаем зависшие 'sending' (процесс упал посреди отправки) — через 15 мин
        # считаем, что не ушло: лучше повтор проверки onWhatsApp, чем вечная пробка.
        conn.execute("UPDATE wa_outbox SET status='pending', taken_at=NULL "
                     "WHERE status='sending' AND taken_at < datetime('now','-15 minutes')")
        rows = conn.execute(
            "SELECT * FROM wa_outbox WHERE account_id=? AND status='pending' "
            "ORDER BY is_test DESC, id", (account_id,)).fetchall()
        last = conn.execute(
            "SELECT MAX(sent_at) t FROM wa_outbox WHERE account_id=? AND status='sent' "
            "AND is_test=0", (account_id,)).fetchone()["t"]
        recent = conn.execute(
            "SELECT 1 FROM wa_outbox WHERE account_id=? AND status='sent' AND is_test=0 "
            "AND sent_at > datetime('now', ?)", (account_id, f"-{OUTBOX_GAP_MIN} minutes")
        ).fetchone() is not None
        for r in rows:
            if not r["is_test"]:
                if recent:
                    return JSONResponse({"item": None, "wait": f"зазор после {last}"})
                camp = conn.execute("SELECT * FROM campaigns WHERE id=?",
                                    (r["campaign_id"],)).fetchone()
                # Кампанию остановили/архивировали после постановки в очередь — не шлём.
                if not camp or camp["status"] not in ("running", "active"):
                    conn.execute("DELETE FROM wa_outbox WHERE id=?", (r["id"],))
                    continue
                if not database.outreach_allowed(camp):
                    return JSONResponse({"item": None, "wait": "вне рабочих часов кампании"})
            conn.execute("UPDATE wa_outbox SET status='sending', taken_at=datetime('now') "
                         "WHERE id=?", (r["id"],))
            return JSONResponse({"item": {
                "id": r["id"], "phone": r["phone"], "contact_id": r["contact_id"],
                "parts": json.loads(r["parts"] or "[]"), "fast": bool(r["is_test"]),
            }})
    return JSONResponse({"item": None})


@router.post("/wa/outbox/done")
def wa_outbox_done(d: OutboxDone) -> JSONResponse:
    """Итог отправки. sent → книжка, campaign_contacts, статус 'messaged' — ровно то
    же, что Telegram-рассылка делает после успешной отправки."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM wa_outbox WHERE id=?", (d.id,)).fetchone()
        if not r:
            return JSONResponse({"ok": False, "error": "нет такой строки"})
        cid, contact_id, acc_id = r["campaign_id"], r["contact_id"], r["account_id"]
        if d.result == "sent":
            conn.execute("UPDATE wa_outbox SET status='sent', sent_at=datetime('now'), "
                         "error=NULL WHERE id=?", (d.id,))
            if d.jid:
                database.set_wa_jid(conn, contact_id, d.jid)
            text = "\n".join(json.loads(r["parts"] or "[]"))
            database.add_message(conn, contact_id, "out", text, account_id=acc_id,
                                 channel="whatsapp")
            database.set_status(conn, contact_id, "messaged")
            if cid:
                conn.execute(
                    "INSERT OR IGNORE INTO campaign_contacts (campaign_id, contact_id, account_id) "
                    "VALUES (?,?,?)", (cid, contact_id, acc_id))
                conn.execute("UPDATE contacts SET outreach_campaign_id=? WHERE id=?",
                             (cid, contact_id))
        elif d.result == "no_wa":
            conn.execute("UPDATE wa_outbox SET status='no_wa', error=? WHERE id=?",
                         (d.error, d.id))
            conn.execute("UPDATE contacts SET has_wa='no', checked_at=datetime('now') "
                         "WHERE id=?", (contact_id,))
        else:
            conn.execute("UPDATE wa_outbox SET status='failed', error=? WHERE id=?",
                         ((d.error or "")[:300], d.id))
            database.add_event(conn, "wa_send_fail", "⚠️ WhatsApp: сообщение не ушло",
                               (d.error or "")[:200], level="warn", contact_id=contact_id,
                               campaign_id=cid, account_id=acc_id)
    return JSONResponse({"ok": True})


# ─────────────────────────── входящие и ответы ───────────────────────────

def _our_dialog(conn, contact_id: int, acc_id: int | None) -> bool:
    """Это НАШ разговор: кампания писала этому человеку с этого номера.

    Правило то же, что у Telegram-слушателя (инцидент 08.2026, бот влез в личную
    переписку хозяина): на номере может идти и личная жизнь, а книжка полна людей из
    парсинга. Входящее от них не пишем в «Диалоги» и не отвечаем — это не наше."""
    if not acc_id:
        return False
    if conn.execute("SELECT 1 FROM campaign_contacts WHERE contact_id=? AND account_id=? "
                    "LIMIT 1", (contact_id, acc_id)).fetchone():
        return True
    return conn.execute("SELECT 1 FROM messages WHERE contact_id=? AND account_id=? "
                        "AND direction='out' LIMIT 1", (contact_id, acc_id)).fetchone() is not None


@router.post("/wa/incoming")
def incoming(msg: Incoming) -> JSONResponse:
    """Входящее от человека. Пишем в книжку (если это наш диалог) и говорим Node,
    ждать ли ответа агента. Сам ответ собирается отдельно (/wa/reply) — после паузы,
    чтобы человек успел дописать мысль и агент ответил на всё сразу."""
    from channels.listener import _should_reply
    with database.get_conn() as conn:
        contact = database.find_contact_by_wa(conn, jid=msg.jid, phone=msg.phone)
        if contact is None:
            return JSONResponse({"ignore": True, "reason": "не в книжке"})
        contact_id = contact["id"]
        if not _our_dialog(conn, contact_id, msg.account_id):
            return JSONResponse({"ignore": True, "reason": "не наш диалог (кампания не писала)"})
        if not contact["wa_jid"]:
            database.set_wa_jid(conn, contact_id, msg.jid)
        database.add_message(conn, contact_id, "in", msg.text, account_id=msg.account_id,
                             channel="whatsapp")
        conn.execute("UPDATE messages SET delivered_at=COALESCE(delivered_at, datetime('now')), "
                     "read_at=COALESCE(read_at, datetime('now')) WHERE contact_id=? "
                     "AND direction='out' AND account_id=?", (contact_id, msg.account_id))
        is_test = bool(contact["is_test"]) if "is_test" in contact.keys() else False
    if not _should_reply(msg.account_id, contact_id):
        return JSONResponse({"ignore": True, "recorded": True, "contact_id": contact_id,
                             "reason": "авто-ответ выключен / горячий лид / номер не в команде"})
    return JSONResponse({"reply": True, "contact_id": contact_id, "fast": is_test})


@router.post("/wa/reply")
def reply(req: ReplyReq) -> JSONResponse:
    """Ответ агента на всё, что человек написал с нашей последней реплики."""
    contact_id = req.contact_id
    with database.get_conn() as conn:
        contact = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if contact is None:
            return JSONResponse({"skip": "нет контакта"})
        opener, history = _history_for_agent(database.get_history(conn, contact_id))
        contact_info = _contact_dict(contact)
        camp = database.get_contact_campaign(conn, contact_id)
        campaign_prompt = campaign_prompt_with_objections(conn, camp)
        extra_context = contact["agent_context"] if "agent_context" in contact.keys() else None
        kps = []
        if camp:
            kps = [dict(r) for r in conn.execute(
                "SELECT id, name, when_to_use, kp_text, kp_file FROM campaign_kps "
                "WHERE campaign_id=? ORDER BY id", (camp["id"],)).fetchall()]
            paused = conn.execute(
                "SELECT 1 FROM campaign_paused_contacts WHERE campaign_id=? AND contact_id=?",
                (camp["id"], contact_id)).fetchone() is not None
        else:
            paused = False
    if contact["status"] == "refused":
        return JSONResponse({"skip": "отказ — автоответ выключен"})
    if paused:
        return JSONResponse({"skip": "оператор ведёт сам"})
    if not history or history[-1]["role"] != "user":
        return JSONResponse({"skip": "нечего отвечать"})
    is_test = bool(contact["is_test"]) if "is_test" in contact.keys() else False
    if camp and not is_test and not database.in_work_hours(camp):
        return JSONResponse({"skip": "вне рабочих часов кампании"})

    try:
        r = generate_reply(history, _default_slots(), contact_info, opener, campaign_prompt,
                           extra_context, False, kps, camp["id"] if camp else None)
    except Exception as e:  # noqa: BLE001
        from channels.telegram import _notify_agent_down
        _notify_agent_down(contact_id, e)
        return JSONResponse({"skip": f"агент упал: {e}"})
    if _is_dangling(r.reply_parts):
        return JSONResponse({"skip": "обрубок от модели"})

    parts = [p.strip() for p in r.reply_parts if p and p.strip()]
    # КП текстом (файлы по WhatsApp пока не шлём — Baileys-отправку документа не
    # доделали). Выбор по названию — как в Telegram (_agent_reply).
    if kps and r.kp_choice:
        raw = str(r.kp_choice).lower()
        for k in kps:
            nm = (k.get("name") or "").strip().lower()
            if nm and nm in raw and k.get("kp_text"):
                parts.append(k["kp_text"])

    text_in = history[-1]["content"]
    meeting = None
    if r.meeting_agreed:
        try:
            meeting = meetings.arrange(contact_info, r.proposed_datetime,
                                       camp["id"] if camp else None, r.notes, contact_id,
                                       None, contact["phone"])
        except Exception as e:  # noqa: BLE001
            print(f"[wa meeting error] contact {contact_id}: {e}")

    who = contact_info.get("name") or contact_info.get("person_name") or str(contact_id)
    with database.get_conn() as conn:
        conn.execute("UPDATE messages SET intent=? WHERE id=(SELECT id FROM messages "
                     "WHERE contact_id=? AND direction='in' ORDER BY id DESC LIMIT 1)",
                     (r.intent, contact_id))
        if meeting is not None:
            database.record_meeting(conn, contact_id, meeting.meeting_at_iso, r.notes,
                                    zoom_link=meeting.zoom_link,
                                    calendar_event_id=meeting.calendar_event_id)
            database.add_event(conn, "meeting", f"📅 Встреча назначена (WA): {who}",
                               f"{meeting.meeting_at_iso}", level="good", contact_id=contact_id)
        elif r.intent == "not_interested":
            database.set_status(conn, contact_id, "refused")
            conn.execute("DELETE FROM opener_queue WHERE contact_id=?", (contact_id,))
            database.add_event(conn, "refused", f"🚫 Отказ (WA): {who}",
                               (text_in or "").strip()[:160], level="info",
                               contact_id=contact_id, campaign_id=camp["id"] if camp else None,
                               account_id=req.account_id)
        else:
            database.set_status(conn, contact_id, "in_dialog")
            if r.intent in ("positive", "agreed"):
                database.add_event(conn, "lead", f"🔥 Тёплый лид (WA): {who}",
                                   (text_in or "").strip()[:160], level="good",
                                   contact_id=contact_id)
        if r.hot:
            conn.execute("UPDATE contacts SET hot_since=datetime('now'), "
                         "lead_since=COALESCE(lead_since, datetime('now')) WHERE id=?",
                         (contact_id,))

    # FastAPI гонит синхронный эндпоинт в threadpool — своего event loop тут нет.
    import asyncio
    from channels import notify
    if r.hot:
        asyncio.run(notify.notify_hot(contact_id, text_in, camp["id"] if camp else None))
    if meeting is not None:
        asyncio.run(notify.notify_meeting(contact_id, meeting.meeting_at_iso, r.notes,
                                          meeting.zoom_link, camp["id"] if camp else None))
    print(f"[wa reply -> {who}] intent={r.intent} agreed={r.meeting_agreed}")
    return JSONResponse({"parts": parts, "fast": is_test, "intent": r.intent})


@router.post("/wa/replied")
def replied(p: Replied) -> JSONResponse:
    """Node доставил ответ — теперь он в книжке (раньше нельзя: не ушло — не было)."""
    with database.get_conn() as conn:
        database.add_message(conn, p.contact_id, "out", p.text, account_id=p.account_id,
                             channel="whatsapp")
    return JSONResponse({"ok": True})


# ─────────────── старые ручные режимы Node (--match / --outreach) ───────────────

@router.get("/wa/campaign_outreach")
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
    # Тот же гейт, что в Telegram-рассылке: если в поле первого сообщения лежит
    # промпт, а не письмо, — мост не должен получить его на отправку.
    from channels import opener_lint
    problems = opener_lint.severe(opener_lint.lint(tmpl))
    if problems:
        return JSONResponse({"contacts": [], "cid": cid,
                             "error": opener_lint.blocking_message(problems)}, status_code=400)
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


@router.get("/wa/to_check")
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


@router.post("/wa/mark")
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


@router.get("/wa/outreach")
def outreach(limit: int = 0) -> JSONResponse:
    """Кого писать первым по WhatsApp: новые контакты с телефоном, ещё не охваченные.
    Соблюдает дневной лимит первых сообщений (антибан)."""
    cap = min(limit or config.DAILY_FIRST_MESSAGES, config.DAILY_FIRST_MESSAGES)
    out: list[dict] = []
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM contacts "
            "WHERE status = 'new' AND outreach_campaign_id IS NULL AND phone IS NOT NULL AND wa_jid IS NULL "
            "AND has_wa IN ('yes','unknown') "
            "ORDER BY id LIMIT ?",
            (cap,),
        ).fetchall()
    for r in rows:
        out.append({"contact_id": r["id"], "phone": r["phone"], "parts": _first_message_parts(r)})
    return JSONResponse({"contacts": out})


@router.post("/wa/sent")
def sent(payload: Sent) -> JSONResponse:
    """Node отправил первое сообщение (ручные режимы) → фиксируем jid, книжку, статус."""
    with database.get_conn() as conn:
        if payload.jid:
            database.set_wa_jid(conn, payload.contact_id, payload.jid)
        if payload.text:
            database.add_message(conn, payload.contact_id, "out", payload.text, intent=None,
                                 account_id=payload.account_id, channel="whatsapp")
        database.set_status(conn, payload.contact_id, "messaged")
        if payload.cid:
            conn.execute(
                "INSERT OR IGNORE INTO campaign_contacts (campaign_id, contact_id, account_id) VALUES (?,?,?)",
                (payload.cid, payload.contact_id, payload.account_id),
            )
    return JSONResponse({"ok": True})


# Отдельное приложение — только для запуска моста вне пульта (локальная отладка).
app = FastAPI(title="AXIOM WhatsApp bridge")
app.include_router(router)


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM WhatsApp bridge (Python)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8100)
    args = p.parse_args()
    database.init_db()
    import uvicorn
    print(f"AXIOM WhatsApp bridge -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
