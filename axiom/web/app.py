"""Веб-морда AXIOM — пульт оператора.

Разделы (левое меню): Дашборд, Мои агенты (аккаунты), CRM (база + воронка),
Досье, Кампании, Чаты. Поверх той же SQLite-книжки, что и Telegram-адаптер.

Запуск:

    python -m web.app                 # http://127.0.0.1:8000
    python -m web.app --port 9000
"""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path

from fastapi import FastAPI, Body, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse

from db import database
from importer.import_2gis import norm_phone, phone_from_link, tg_username

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"

FUNNEL = [
    ("new", "Новые"), ("messaged", "Написано"), ("in_dialog", "В диалоге"),
    ("meeting_set", "Встреча назначена"), ("met", "Встреча прошла"), ("won", "Сделка"),
    ("nurture", "Прогрев"), ("lost", "Потеряны"), ("stop", "Стоп"),
]
FUNNEL_KEYS = [k for k, _ in FUNNEL]

app = FastAPI(title="AXIOM Dashboard")


def _split_tags(raw: str | None) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _seed_accounts(conn) -> None:
    """Если аккаунтов нет — заводим текущий залогиненный, чтобы было видно «кто есть кто»."""
    n = conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
    if n == 0:
        conn.execute(
            "INSERT INTO accounts (label, phone, username, role, status, daily_limit, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            ("Основной", "+79288520610", "iivairf", "sdr", "active", 15, "Текущая залогиненная сессия"),
        )


# --------------------------------------------------------------------------- #
@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


# ---- Дашборд -------------------------------------------------------------- #
@app.get("/api/stats")
def stats() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        by_status = {r["status"]: r["c"] for r in conn.execute("SELECT status, COUNT(*) c FROM contacts GROUP BY status")}
        total = conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]
        msg_in = conn.execute("SELECT COUNT(*) c FROM messages WHERE direction='in'").fetchone()["c"]
        msg_out = conn.execute("SELECT COUNT(*) c FROM messages WHERE direction='out'").fetchone()["c"]
        meetings = conn.execute("SELECT COUNT(*) c FROM deals WHERE meeting_at IS NOT NULL").fetchone()["c"]
        acc = conn.execute("SELECT COUNT(*) c FROM accounts WHERE status='active'").fetchone()["c"]
    funnel = [{"key": k, "label": lbl, "count": by_status.get(k, 0)} for k, lbl in FUNNEL]
    return JSONResponse({
        "total": total, "funnel": funnel,
        "messages": {"in": msg_in, "out": msg_out, "total": msg_in + msg_out},
        "meetings": meetings, "agents": acc,
    })


# ---- Мои агенты (аккаунты) ------------------------------------------------ #
@app.get("/api/accounts")
def accounts_list() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        _seed_accounts(conn)
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tg_connected"] = bool(d.pop("tg_session", None))  # секрет наружу не отдаём
        out.append(d)
    return JSONResponse(out)


@app.post("/api/accounts")
def accounts_add(payload: dict = Body(...)) -> JSONResponse:
    f = {k: (payload.get(k) or None) for k in ("label", "phone", "username", "role", "status", "notes")}
    f["status"] = f["status"] or "warming"
    limit = int(payload.get("daily_limit") or 15)
    if not f["label"] and not f["phone"]:
        return JSONResponse({"error": "нужен хотя бы ярлык или телефон"}, status_code=400)
    with database.get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO accounts (label, phone, username, role, status, daily_limit, notes) VALUES (?,?,?,?,?,?,?)",
                (f["label"], f["phone"], f["username"], f["role"], f["status"], limit, f["notes"]),
            )
        except Exception as e:
            return JSONResponse({"error": f"возможно, такой телефон уже есть ({e})"}, status_code=400)
        return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/accounts/{acc_id}/delete")
def accounts_delete(acc_id: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
    return JSONResponse({"ok": True})


@app.post("/api/accounts/{acc_id}/proxy")
def accounts_set_proxy(acc_id: int, payload: dict = Body(...)) -> JSONResponse:
    proxy = (payload.get("proxy") or "").strip() or None
    with database.get_conn() as conn:
        conn.execute("UPDATE accounts SET proxy=? WHERE id=?", (proxy, acc_id))
    return JSONResponse({"ok": True, "proxy": proxy})


@app.post("/api/accounts/proxy_all")
def accounts_proxy_all(payload: dict = Body(...)) -> JSONResponse:
    proxy = (payload.get("proxy") or "").strip() or None
    with database.get_conn() as conn:
        conn.execute("UPDATE accounts SET proxy=?", (proxy,))
    return JSONResponse({"ok": True})


@app.post("/api/health")
def accounts_health() -> JSONResponse:
    """Проверка всех аккаунтов через @SpamBot (фоном). Результат — в spam_status карточек."""
    _spawn("channels.health")
    return JSONResponse({"ok": True})


# ---- Календарь (встречи / КЭВ) -------------------------------------------- #
@app.get("/api/meetings")
def meetings_list() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT d.meeting_at, d.zoom_link, d.stage, d.notes, "
            "c.id AS cid, c.name, c.username, c.phone "
            "FROM deals d JOIN contacts c ON c.id = d.contact_id "
            "WHERE d.meeting_at IS NOT NULL ORDER BY d.meeting_at"
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


# ---- CRM / Контакты ------------------------------------------------------- #
@app.get("/api/contacts")
def contacts() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.username, c.phone, c.wa_phone, c.city, c.agency, c.tags, c.notes,
                   c.status, c.has_tg, c.has_wa, c.preferred_channel, c.pipeline_id, c.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.contact_id = c.id) AS msg_count,
                   (SELECT MAX(ts) FROM messages m WHERE m.contact_id = c.id) AS last_ts
            FROM contacts c
            ORDER BY (last_ts IS NULL), last_ts DESC, c.id DESC
            """
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["tags"] = _split_tags(d.get("tags")); out.append(d)
    return JSONResponse(out)


@app.get("/api/contact/{contact_id}")
def contact_detail(contact_id: int) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        history = [dict(m) for m in database.get_history(conn, contact_id)]
        deal = conn.execute("SELECT * FROM deals WHERE contact_id = ? ORDER BY id DESC LIMIT 1", (contact_id,)).fetchone()
    d = dict(row); d["tags"] = _split_tags(d.get("tags"))
    d["history"] = history; d["deal"] = dict(deal) if deal else None
    return JSONResponse(d)


@app.post("/api/contact/{contact_id}/tags")
def set_tags(contact_id: int, payload: dict = Body(...)) -> JSONResponse:
    tags = payload.get("tags", [])
    if isinstance(tags, list):
        tags = ",".join(t.strip() for t in tags if t.strip())
    with database.get_conn() as conn:
        conn.execute("UPDATE contacts SET tags = ?, updated_at = datetime('now') WHERE id = ?", (tags, contact_id))
    return JSONResponse({"ok": True, "tags": _split_tags(tags)})


@app.post("/api/contact/{contact_id}/status")
def set_status(contact_id: int, payload: dict = Body(...)) -> JSONResponse:
    status = payload.get("status", "")
    if status not in FUNNEL_KEYS:
        return JSONResponse({"error": "bad status"}, status_code=400)
    with database.get_conn() as conn:
        database.set_status(conn, contact_id, status)
    return JSONResponse({"ok": True, "status": status})


# ---- Воронки (как в Битрикс) ---------------------------------------------- #
@app.get("/api/pipelines")
def pipelines_list() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM pipelines ORDER BY is_default DESC, id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["stages"] = json.loads(d.get("stages") or "[]")
            except (TypeError, ValueError):
                d["stages"] = []
            d["count"] = conn.execute(
                "SELECT COUNT(*) c FROM contacts WHERE pipeline_id=? OR (pipeline_id IS NULL AND ?=1)",
                (r["id"], 1 if r["is_default"] else 0),
            ).fetchone()["c"]
            out.append(d)
    return JSONResponse(out)


@app.post("/api/pipelines")
def pipelines_create(payload: dict = Body(...)) -> JSONResponse:
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "нужно название воронки"}, status_code=400)
    stages = payload.get("stages")
    if not stages:
        # дефолтный набор стадий продаж
        stages = [
            {"key": "new", "label": "Новые"}, {"key": "messaged", "label": "Написано"},
            {"key": "in_dialog", "label": "В диалоге"}, {"key": "meeting_set", "label": "Встреча назначена"},
            {"key": "won", "label": "Сделка"}, {"key": "lost", "label": "Отказ"},
        ]
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO pipelines (name, product, project_id, stages) VALUES (?,?,?,?)",
            (name, payload.get("product") or None, payload.get("project_id") or None,
             json.dumps(stages, ensure_ascii=False)),
        )
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/pipeline/{pid}/delete")
def pipelines_delete(pid: int) -> JSONResponse:
    with database.get_conn() as conn:
        row = conn.execute("SELECT is_default FROM pipelines WHERE id=?", (pid,)).fetchone()
        if row and row["is_default"]:
            return JSONResponse({"error": "нельзя удалить основную воронку"}, status_code=400)
        conn.execute("DELETE FROM pipelines WHERE id=?", (pid,))
        conn.execute("UPDATE contacts SET pipeline_id=NULL WHERE pipeline_id=?", (pid,))
    return JSONResponse({"ok": True})


@app.post("/api/contact/{contact_id}/move")
def contact_move(contact_id: int, payload: dict = Body(...)) -> JSONResponse:
    """Перемещение лида: смена стадии и/или воронки (продукта)."""
    stage = payload.get("stage")
    pid = payload.get("pipeline_id", "keep")
    with database.get_conn() as conn:
        if pid != "keep":
            conn.execute("UPDATE contacts SET pipeline_id=?, updated_at=datetime('now') WHERE id=?",
                         (pid or None, contact_id))
        if stage:
            conn.execute("UPDATE contacts SET status=?, updated_at=datetime('now') WHERE id=?",
                         (stage, contact_id))
    return JSONResponse({"ok": True})


def _spawn(*args: str) -> None:
    import subprocess
    import sys
    subprocess.Popen([sys.executable, "-m", *args], cwd=str(BASE_DIR.parent))


@app.post("/api/contact/{contact_id}/enrich")
def enrich_one(contact_id: int) -> JSONResponse:
    _spawn("agent.enrich", "--id", str(contact_id))
    return JSONResponse({"ok": True})


@app.post("/api/enrich")
def enrich_batch(payload: dict = Body(...)) -> JSONResponse:
    tag = (payload.get("tag") or "").strip()
    limit = int(payload.get("limit") or 20)
    args = ["agent.enrich", "--limit", str(limit)]
    if tag:
        args += ["--tag", tag]
    _spawn(*args)
    return JSONResponse({"ok": True, "limit": limit})


# ---- Парсинг Telegram (поиск групп / парсер / инвайты) -------------------- #
def _run_capture(args: list[str], timeout: int = 240) -> dict:
    """Запускает модуль и ВОЗВРАЩАЕТ его вывод (для веба, в отличие от _spawn)."""
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(
            [sys.executable, "-m", *args], cwd=str(BASE_DIR.parent),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return {"ok": False, "output": partial + f"\n[таймаут {timeout}с — уменьши scan/limit]"}
    out = (r.stdout or "")
    if (r.stderr or "").strip():
        out += "\n[stderr]\n" + r.stderr
    return {"ok": r.returncode == 0, "output": out.strip() or "(пусто)"}


@app.post("/api/parse/run")
def parse_run(payload: dict = Body(...)) -> JSONResponse:
    target = (payload.get("target") or "").strip()
    mode = payload.get("mode") or "search"
    if not target:
        return JSONResponse({"error": "укажи @чат/ссылку или поисковый запрос"}, status_code=400)
    args = ["channels.tg_parser", "--target", target, "--mode", mode]
    if payload.get("save"):
        args.append("--save")
    if mode == "members":
        args += ["--limit", str(int(payload.get("limit") or 500))]
    elif mode in ("active", "all"):
        args += ["--scan", str(int(payload.get("scan") or 2000)), "--top", str(int(payload.get("top") or 50))]
    elif mode == "search":
        args += ["--limit", str(int(payload.get("limit") or 30))]
    return JSONResponse(_run_capture(args))


@app.post("/api/parse/invites")
def parse_invites(payload: dict = Body(...)) -> JSONResponse:
    target = (payload.get("target") or "").strip()
    dialogs = bool(payload.get("dialogs"))
    if not target and not dialogs:
        return JSONResponse({"error": "укажи @чат или включи «все диалоги»"}, status_code=400)
    args = ["channels.tg_invites"]
    if target:
        args += ["--target", target, "--limit", str(int(payload.get("limit") or 3000))]
    if dialogs:
        args += ["--dialogs", "--per", str(int(payload.get("per") or 800))]
    return JSONResponse(_run_capture(args, timeout=300))


# ---- Импорт 2ГИС (по заголовкам — ловит любую выгрузку) ------------------- #
def _find_cols(header: list[str]) -> dict:
    """Сопоставляет колонки по названию заголовка. Возвращает {role: [индексы]}."""
    idx = {"name": [], "desc": [], "address": [], "city": [], "phone": [], "email": [],
           "web": [], "vk": [], "wa": [], "tg": []}
    for i, h in enumerate(header):
        h = (h or "").strip().lower()
        if h == "наименование": idx["name"].append(i)
        elif h == "описание": idx["desc"].append(i)
        elif h == "адрес": idx["address"].append(i)
        elif h == "город": idx["city"].append(i)
        elif h.startswith("телефон"): idx["phone"].append(i)
        elif h == "e-mail": idx["email"].append(i)
        elif h.startswith("веб-сайт"): idx["web"].append(i)
        elif h == "вконтакте": idx["vk"].append(i)
        elif h.startswith("whatsapp"): idx["wa"].append(i)
        elif h.startswith("telegram"): idx["tg"].append(i)
    return idx


def _parse_2gis(text: str, tag: str) -> tuple[int, int]:
    reader = csv.reader(io.StringIO(text), delimiter=";")
    rows = list(reader)
    if not rows:
        return 0, 0
    cols = _find_cols(rows[0])
    if not cols["name"]:
        raise ValueError("не нашёл колонку «Наименование» — это точно выгрузка 2ГИС?")

    def cell(row, i):
        return row[i].strip() if i < len(row) else ""

    def first(row, key):
        for i in cols[key]:
            v = cell(row, i)
            if v:
                return v
        return ""

    def all_vals(row, key):
        return [cell(row, i) for i in cols[key] if cell(row, i)]

    added = skipped = 0
    database.init_db()
    with database.get_conn() as conn:
        for row in rows[1:]:
            name = first(row, "name")
            if not name:
                skipped += 1
                continue
            phones = all_vals(row, "phone")
            was = all_vals(row, "wa")
            tgs = all_vals(row, "tg")
            phone = next((p for p in (norm_phone(x) for x in phones) if p), None) \
                or next((p for p in (phone_from_link(x) for x in was + tgs) if p), None)
            wa_phone = next((p for p in (phone_from_link(x) for x in was) if p), None)
            username = tg_username(*tgs)
            has_wa = "yes" if was else "unknown"
            has_tg = "yes" if any("t.me/" in t for t in tgs) else "unknown"
            preferred = "telegram" if has_tg == "yes" else ("whatsapp" if has_wa == "yes" else "telegram")
            notes = " | ".join(p for p in [first(row, "email"), first(row, "web"),
                                           first(row, "vk"), first(row, "address")] if p)
            cid = database.upsert_contact(
                conn, source="2gis", phone=phone, username=username, name=name,
                city=first(row, "city") or None, agency=name,
                tags=tag or first(row, "desc") or None, notes=notes or None,
            )
            conn.execute(
                "UPDATE contacts SET wa_phone=COALESCE(?,wa_phone), has_wa=?, has_tg=?, "
                "preferred_channel=?, checked_at=datetime('now') WHERE id=?",
                (wa_phone, has_wa, has_tg, preferred, cid),
            )
            added += 1
    return added, skipped


@app.post("/api/import")
async def import_2gis(file: UploadFile = File(...), tag: str = Form("Агентства недвижимости")) -> JSONResponse:
    raw = await file.read()
    text = None
    for enc in ("cp1251", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return JSONResponse({"error": "не удалось распознать кодировку файла"}, status_code=400)
    try:
        added, skipped = _parse_2gis(text, tag.strip())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    with database.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]
    return JSONResponse({"ok": True, "imported": added, "skipped": skipped, "total": total})


# ---- Чаты ----------------------------------------------------------------- #
@app.get("/api/chats")
def chats() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.username, c.status, c.tags,
                   (SELECT COUNT(*) FROM messages m WHERE m.contact_id = c.id) AS msg_count,
                   (SELECT text FROM messages m WHERE m.contact_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_text,
                   (SELECT MAX(ts) FROM messages m WHERE m.contact_id = c.id) AS last_ts
            FROM contacts c
            WHERE EXISTS (SELECT 1 FROM messages m WHERE m.contact_id = c.id)
            ORDER BY last_ts DESC
            """
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["tags"] = _split_tags(d.get("tags")); out.append(d)
    return JSONResponse(out)


# ---- Проекты (верхний уровень: проект → кампании) ------------------------- #
@app.get("/api/projects")
def projects_list() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["campaigns"] = conn.execute("SELECT COUNT(*) c FROM campaigns WHERE project_id=?", (r["id"],)).fetchone()["c"]
            out.append(d)
    return JSONResponse(out)


@app.post("/api/projects")
def projects_create(payload: dict = Body(...)) -> JSONResponse:
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "нужно название проекта"}, status_code=400)
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, entity, description) VALUES (?,?,?)",
            (name, payload.get("entity") or None, payload.get("description") or None),
        )
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/project/{pid}/update")
def projects_update(pid: int, payload: dict = Body(...)) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET name=?, entity=?, description=?, status=? WHERE id=?",
            (payload.get("name"), payload.get("entity") or None, payload.get("description") or None,
             payload.get("status") or "active", pid),
        )
    return JSONResponse({"ok": True})


@app.post("/api/project/{pid}/delete")
def projects_delete(pid: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.execute("UPDATE campaigns SET project_id=NULL WHERE project_id=?", (pid,))
    return JSONResponse({"ok": True})


# ---- Контекст контакта для агента (досье) --------------------------------- #
@app.post("/api/contact/{contact_id}/context")
def set_agent_context(contact_id: int, payload: dict = Body(...)) -> JSONResponse:
    ctx = (payload.get("agent_context") or "").strip() or None
    with database.get_conn() as conn:
        conn.execute("UPDATE contacts SET agent_context=?, updated_at=datetime('now') WHERE id=?", (ctx, contact_id))
    return JSONResponse({"ok": True})


# ---- Кампании ------------------------------------------------------------- #
def _sync_campaign_accounts(conn, cid: int, account_ids) -> None:
    """Полная пересборка команды кампании (какие агенты её работают)."""
    conn.execute("DELETE FROM campaign_accounts WHERE campaign_id=?", (cid,))
    for aid in (account_ids or []):
        try:
            conn.execute(
                "INSERT OR IGNORE INTO campaign_accounts (campaign_id, account_id) VALUES (?,?)",
                (cid, int(aid)),
            )
        except (TypeError, ValueError):
            continue


def _audience_count(conn, tag, channel) -> int:
    where = "status='new' AND (username IS NOT NULL OR phone IS NOT NULL)"
    params = []
    if channel == "telegram":
        where += " AND has_tg IN ('yes','unknown')"
    elif channel == "whatsapp":
        where += " AND has_wa IN ('yes','unknown')"
    if tag:
        where += " AND tags LIKE ?"
        params.append(f"%{tag}%")
    return conn.execute(f"SELECT COUNT(*) c FROM contacts WHERE {where}", params).fetchone()["c"]


def _camp_row(conn, r) -> dict:
    d = dict(r)
    d["audience"] = _audience_count(conn, d.get("audience_tag"), d.get("channel"))
    d["sent"] = conn.execute(
        "SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=?", (d["id"],)
    ).fetchone()["c"]
    d["accounts"] = [row["account_id"] for row in conn.execute(
        "SELECT account_id FROM campaign_accounts WHERE campaign_id=?", (d["id"],))]
    return d


@app.get("/api/campaigns")
def campaigns_list(project_id: int | None = None) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        if project_id:
            rows = conn.execute("SELECT * FROM campaigns WHERE project_id=? ORDER BY id DESC", (project_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
        out = [_camp_row(conn, r) for r in rows]
    return JSONResponse(out)


_CAMP_FIELDS = ("name", "product", "audience_tag", "channel", "message_template", "agent_prompt", "kp_text")


@app.post("/api/campaigns")
def campaigns_create(payload: dict = Body(...)) -> JSONResponse:
    f = {k: (payload.get(k) or None) for k in _CAMP_FIELDS}
    f["channel"] = f["channel"] or "telegram"
    account_id = payload.get("account_id") or None
    daily_limit = int(payload.get("daily_limit") or 15)
    if not f["name"]:
        return JSONResponse({"error": "нужно название кампании"}, status_code=400)
    project_id = payload.get("project_id") or None
    account_ids = payload.get("account_ids") or []
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (name, product, audience_tag, channel, account_id, daily_limit, "
            "message_template, agent_prompt, kp_text, project_id, status) VALUES (?,?,?,?,?,?,?,?,?,?, 'draft')",
            (f["name"], f["product"], f["audience_tag"], f["channel"], account_id, daily_limit,
             f["message_template"], f["agent_prompt"], f["kp_text"], project_id),
        )
        _sync_campaign_accounts(conn, cur.lastrowid, account_ids)
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/campaign/{cid}/update")
def campaign_update(cid: int, payload: dict = Body(...)) -> JSONResponse:
    f = {k: (payload.get(k) or None) for k in _CAMP_FIELDS}
    f["channel"] = f["channel"] or "telegram"
    account_id = payload.get("account_id") or None
    daily_limit = int(payload.get("daily_limit") or 15)
    if not f["name"]:
        return JSONResponse({"error": "нужно название кампании"}, status_code=400)
    project_id = payload.get("project_id") or None
    account_ids = payload.get("account_ids")
    with database.get_conn() as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        conn.execute(
            "UPDATE campaigns SET name=?, product=?, audience_tag=?, channel=?, account_id=?, "
            "daily_limit=?, message_template=?, agent_prompt=?, kp_text=?, project_id=? WHERE id=?",
            (f["name"], f["product"], f["audience_tag"], f["channel"], account_id, daily_limit,
             f["message_template"], f["agent_prompt"], f["kp_text"], project_id, cid),
        )
        if account_ids is not None:
            _sync_campaign_accounts(conn, cid, account_ids)
    return JSONResponse({"ok": True, "id": cid})


@app.get("/api/campaign/{cid}")
def campaign_detail(cid: int) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        d = _camp_row(conn, row)
        sample = conn.execute(
            "SELECT id, name, username, phone FROM contacts "
            "WHERE status='new' AND (username IS NOT NULL OR phone IS NOT NULL) "
            + ("AND tags LIKE ? " if d.get("audience_tag") else "")
            + "ORDER BY id LIMIT 8",
            ((f"%{d['audience_tag']}%",) if d.get("audience_tag") else ()),
        ).fetchall()
        d["sample"] = [dict(s) for s in sample]
    return JSONResponse(d)


@app.get("/api/campaign/{cid}/progress")
def campaign_progress(cid: int) -> JSONResponse:
    """Таблица прогресса: кому пишем, с какого аккаунта, отправлено ли (✓)."""
    database.init_db()
    with database.get_conn() as conn:
        camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not camp:
            return JSONResponse({"error": "not found"}, status_code=404)
        camp = dict(camp)
        acc = None
        if camp.get("account_id"):
            a = conn.execute("SELECT id,label,phone,username FROM accounts WHERE id=?", (camp["account_id"],)).fetchone()
            acc = dict(a) if a else None
        sent_rows = conn.execute(
            "SELECT cc.contact_id, cc.sent_at, c.name, c.username, c.phone, c.person_name, c.status, "
            "a.label AS acc_label, a.phone AS acc_phone "
            "FROM campaign_contacts cc JOIN contacts c ON c.id=cc.contact_id "
            "LEFT JOIN accounts a ON a.id=cc.account_id "
            "WHERE cc.campaign_id=? ORDER BY cc.sent_at DESC",
            (cid,),
        ).fetchall()
        sent_ids = {r["contact_id"] for r in sent_rows}
        tag = camp.get("audience_tag")
        channel = camp.get("channel")
        where = "status='new' AND (username IS NOT NULL OR phone IS NOT NULL)"
        params: list = []
        if channel == "telegram":
            where += " AND has_tg IN ('yes','unknown')"
        elif channel == "whatsapp":
            where += " AND has_wa IN ('yes','unknown')"
        if tag:
            where += " AND tags LIKE ?"
            params.append(f"%{tag}%")
        pend = conn.execute(
            f"SELECT id,name,username,phone,person_name,status FROM contacts WHERE {where} ORDER BY id LIMIT 500",
            params,
        ).fetchall()

    def handle(r) -> str:
        return ("@" + r["username"]) if r["username"] else (r["phone"] or "—")

    acc_name = (acc and (acc.get("label") or acc.get("phone"))) or "—"
    rows = []
    for r in sent_rows:
        rows.append({
            "id": r["contact_id"], "name": r["person_name"] or r["name"], "handle": handle(r),
            "sent": True, "sent_at": r["sent_at"],
            "account": r["acc_label"] or r["acc_phone"] or acc_name, "status": r["status"],
        })
    for r in pend:
        if r["id"] in sent_ids:
            continue
        rows.append({
            "id": r["id"], "name": r["person_name"] or r["name"], "handle": handle(r),
            "sent": False, "sent_at": None, "account": acc_name, "status": r["status"],
        })
    return JSONResponse({"account": acc, "account_name": acc_name,
                         "sent_count": len(sent_ids), "total": len(rows), "rows": rows})


@app.post("/api/campaign/{cid}/launch")
def campaign_launch(cid: int, payload: dict = Body(...)) -> JSONResponse:
    import subprocess
    import sys
    limit = int(payload.get("limit") or 3)
    with database.get_conn() as conn:
        row = conn.execute("SELECT message_template FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        if not (row["message_template"] or "").strip():
            return JSONResponse({"error": "сначала заполни текст первого сообщения"}, status_code=400)
        conn.execute("UPDATE campaigns SET status='running' WHERE id=?", (cid,))
    # Шлём в отдельном процессе, чтобы не блокировать веб и не конфликтовать с event loop FastAPI.
    subprocess.Popen(
        [sys.executable, "-m", "channels.campaign_send", str(cid), "--limit", str(limit)],
        cwd=str(BASE_DIR.parent),
    )
    return JSONResponse({"ok": True, "launched": limit})


@app.post("/api/campaign/{cid}/delete")
def campaign_delete(cid: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM campaigns WHERE id=?", (cid,))
        conn.execute("DELETE FROM campaign_contacts WHERE campaign_id=?", (cid,))
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM веб-пульт")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    import uvicorn
    print(f"AXIOM dashboard -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
