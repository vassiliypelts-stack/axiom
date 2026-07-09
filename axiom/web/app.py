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

import config
from db import database
from importer.import_2gis import norm_phone, phone_from_link, tg_username

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"
KP_DIR = config.DB_PATH.parent / "kp"   # файлы КП кампаний (data/kp/)
AVATAR_DIR = config.DB_PATH.parent / "avatars"   # аватары агентов

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
        acc_by = {r["status"]: r["c"] for r in conn.execute("SELECT status, COUNT(*) c FROM accounts GROUP BY status")}
        acc = acc_by.get("active", 0)

        def _count(sql: str) -> int:
            try:
                return conn.execute(sql).fetchone()["c"]
            except Exception:  # noqa: BLE001
                return 0

        proxies_alive = _count("SELECT COUNT(*) c FROM proxies WHERE status='alive'")
        chats_cat = _count("SELECT COUNT(*) c FROM chats")
        campaigns_running = _count("SELECT COUNT(*) c FROM campaigns WHERE status='running'")
        campaigns_total = _count("SELECT COUNT(*) c FROM campaigns")
        hits_new = _count("SELECT COUNT(*) c FROM chat_hits WHERE status='new'")
        ai_agents = _count("SELECT COUNT(*) c FROM ai_agents WHERE active=1")
        upcoming = _count("SELECT COUNT(*) c FROM deals WHERE meeting_at >= datetime('now')")
    funnel = [{"key": k, "label": lbl, "count": by_status.get(k, 0)} for k, lbl in FUNNEL]
    return JSONResponse({
        "total": total, "funnel": funnel,
        "messages": {"in": msg_in, "out": msg_out, "total": msg_in + msg_out},
        "meetings": meetings, "agents": acc,
        "accounts": {"active": acc_by.get("active", 0), "warming": acc_by.get("warming", 0),
                     "banned": acc_by.get("banned", 0), "total": sum(acc_by.values())},
        "resources": {"proxies_alive": proxies_alive, "chats": chats_cat, "ai_agents": ai_agents},
        "marketing": {"campaigns_running": campaigns_running, "campaigns_total": campaigns_total,
                      "hits_new": hits_new, "msg_out": msg_out, "msg_in": msg_in},
        "tasks": {"meetings": meetings, "upcoming": upcoming},
    })


@app.get("/api/proxy6/whoami")
def proxy6_whoami() -> JSONResponse:
    """Проверка ключа Proxy6 (PROXY6_API_KEY в .env) — баланс и валюта аккаунта."""
    from channels.proxy6 import Proxy6Error, whoami
    try:
        return JSONResponse({"ok": True, **whoami()})
    except Proxy6Error as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/proxy6/price_bulk")
def proxy6_price_bulk(payload: dict = Body(...)) -> JSONResponse:
    """Сколько СПИШЕТСЯ по факту за выбранные аккаунты — до самой покупки. Проверка
    цены идёт ОДИН раз на всё выбранное количество (Proxy6 считает по count), не по
    странам отдельно — цена одинакова для версии/срока независимо от страны."""
    import phone_geo
    from channels.proxy6 import Proxy6Error, price
    ids = [int(x) for x in (payload.get("ids") or []) if str(x).isdigit()]
    period = int(payload.get("period") or 30)
    version = int(payload.get("version") or 4)
    if not ids:
        return JSONResponse({"error": "ничего не выбрано"}, status_code=400)
    with database.get_conn() as conn:
        qm = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT id, phone, country FROM accounts WHERE id IN ({qm})", ids).fetchall()
    known = [r["id"] for r in rows if r["country"] or phone_geo.detect(r["phone"])]
    skipped = len(ids) - len(known)
    if not known:
        return JSONResponse({"error": "ни у одного выбранного аккаунта не определена страна"}, status_code=400)
    try:
        p = price(count=len(known), period=period, version=version)
    except Proxy6Error as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, **p, "accounts": len(known), "skipped_no_country": skipped})


# ---- Мои агенты (аккаунты) ------------------------------------------------ #
def _days_since(ts: str | None) -> int | None:
    """Сколько дней прошло с даты ts (SQLite datetime, UTC). None — если не распарсили.
    Нужно для колонки «жив N дней» = живучесть аккаунта с момента покупки."""
    if not ts:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(ts).replace("T", " ").split(".")[0])
    except (ValueError, TypeError):
        return None
    return max(0, (datetime.utcnow() - dt).days)


@app.get("/api/accounts")
def accounts_list() -> JSONResponse:
    import phone_geo
    database.init_db()
    with database.get_conn() as conn:
        _seed_accounts(conn)
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        # сколько чатов «держит»/слушает каждый аккаунт (по инвентаризации, joined_by)
        chats_by = {r["aid"]: r["c"] for r in conn.execute(
            "SELECT joined_by aid, COUNT(*) c FROM chats WHERE joined_by IS NOT NULL "
            "AND in_account='yes' GROUP BY joined_by")}
    out = []
    for r in rows:
        d = dict(r)
        d["tg_connected"] = bool(d.pop("tg_session", None))  # секрет наружу не отдаём
        d["chats_count"] = chats_by.get(d["id"], 0)
        # страна: сохранённый ISO2 или определяем по номеру на лету (+ готовая надпись с флагом)
        code = d.get("country") or phone_geo.detect(d.get("phone"))
        d["country_label"] = phone_geo.label(code) if code else ""
        d["days_alive"] = _days_since(d.get("bought_at") or d.get("created_at"))
        out.append(d)
    return JSONResponse(out)


@app.post("/api/accounts")
def accounts_add(payload: dict = Body(...)) -> JSONResponse:
    import phone_geo
    f = {k: (payload.get(k) or None) for k in ("label", "phone", "username", "role", "status", "notes")}
    f["status"] = f["status"] or "warming"
    limit = int(payload.get("daily_limit") or 15)
    if not f["label"] and not f["phone"]:
        return JSONResponse({"error": "нужен хотя бы ярлык или телефон"}, status_code=400)
    country = payload.get("country") or phone_geo.detect(f["phone"])   # страна по коду номера
    with database.get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO accounts (label, phone, username, role, status, daily_limit, notes, country, bought_at) "
                "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                (f["label"], f["phone"], f["username"], f["role"], f["status"], limit, f["notes"], country),
            )
        except Exception as e:
            return JSONResponse({"error": f"возможно, такой телефон уже есть ({e})"}, status_code=400)
        return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/accounts/{acc_id}/delete")
def accounts_delete(acc_id: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
    return JSONResponse({"ok": True})


@app.post("/api/accounts/bulk")
def accounts_bulk(payload: dict = Body(...)) -> JSONResponse:
    """Массовые операции над выбранными аккаунтами: лимит, статус, прогрев, проверка."""
    ids = []
    for x in (payload.get("ids") or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    action = (payload.get("action") or "").strip()
    if not ids:
        return JSONResponse({"error": "не выбран ни один аккаунт"}, status_code=400)
    qm = ",".join("?" * len(ids))
    if action == "limit":
        limit = int(payload.get("daily_limit") or 0)
        if limit <= 0:
            return JSONResponse({"error": "укажи лимит > 0"}, status_code=400)
        with database.get_conn() as conn:
            conn.execute(f"UPDATE accounts SET daily_limit=? WHERE id IN ({qm})", (limit, *ids))
        return JSONResponse({"ok": True, "updated": len(ids), "daily_limit": limit})
    if action == "status":
        status = (payload.get("status") or "").strip()
        if status not in ("warming", "active", "paused", "banned"):
            return JSONResponse({"error": "плохой статус"}, status_code=400)
        with database.get_conn() as conn:
            conn.execute(f"UPDATE accounts SET status=? WHERE id IN ({qm})", (status, *ids))
        return JSONResponse({"ok": True, "updated": len(ids), "status": status})
    if action == "protect":
        val = 1 if payload.get("protected") else 0
        with database.get_conn() as conn:
            conn.execute(f"UPDATE accounts SET protected=? WHERE id IN ({qm})", (val, *ids))
        return JSONResponse({"ok": True, "updated": len(ids), "protected": val})
    if action == "warmup":
        with database.get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, tg_session, COALESCE(protected,0) protected FROM accounts WHERE id IN ({qm})", ids
            ).fetchall()
            protected = [r["id"] for r in rows if r["protected"]]            # родных не трогаем
            ready = [r["id"] for r in rows if r["tg_session"] and not r["protected"]]
            no_sess = [r["id"] for r in rows if not r["tg_session"] and not r["protected"]]
            if ready:
                rq = ",".join("?" * len(ready))
                conn.execute(
                    f"UPDATE accounts SET status='warming' WHERE id IN ({rq}) "
                    "AND status NOT IN ('active','banned')", ready,
                )
        if ready:
            _spawn("channels.warmup", "--run")
        return JSONResponse({"ok": True, "warming": len(ready), "skipped_no_session": no_sess,
                             "skipped_protected": len(protected)})
    if action == "check":
        for i in ids:
            _spawn("channels.health", "--id", str(i))
        return JSONResponse({"ok": True, "checking": len(ids)})
    if action == "identity":
        bio_style = (payload.get("bio_style") or "").strip()
        with database.get_conn() as conn:
            rows = conn.execute(f"SELECT id FROM accounts WHERE id IN ({qm}) "
                                "AND tg_session IS NOT NULL AND tg_session<>''", ids).fetchall()
        queued = [r["id"] for r in rows]
        skipped = len(ids) - len(queued)
        if queued:
            _spawn("channels.identity", "--ids", ",".join(str(i) for i in queued), "--bio-style", bio_style)
        return JSONResponse({"ok": True, "queued": len(queued), "skipped_no_session": skipped})
    if action == "proxy6_buy":
        import phone_geo
        period = int(payload.get("period") or 30)
        version = int(payload.get("version") or 4)
        with database.get_conn() as conn:
            rows = conn.execute(f"SELECT id, phone, country FROM accounts WHERE id IN ({qm})", ids).fetchall()
        queued = [r["id"] for r in rows if r["country"] or phone_geo.detect(r["phone"])]
        skipped = len(ids) - len(queued)
        if queued:
            _spawn("channels.proxy6_bulk", "--ids", ",".join(str(i) for i in queued),
                  "--period", str(period), "--version", str(version))
        return JSONResponse({"ok": True, "queued": len(queued), "skipped_no_country": skipped})
    if action == "onboard":
        with database.get_conn() as conn:
            rows = conn.execute(f"SELECT id FROM accounts WHERE id IN ({qm}) "
                                "AND tg_session IS NOT NULL AND tg_session<>''", ids).fetchall()
        queued = [r["id"] for r in rows]
        skipped = len(ids) - len(queued)
        if queued:
            _spawn("channels.onboard", "--ids", ",".join(str(i) for i in queued))
        return JSONResponse({"ok": True, "queued": len(queued), "skipped_no_session": skipped})
    if action == "inventory":
        with database.get_conn() as conn:
            rows = conn.execute(f"SELECT id FROM accounts WHERE id IN ({qm}) "
                                "AND tg_session IS NOT NULL AND tg_session<>''", ids).fetchall()
        queued = [r["id"] for r in rows]
        for i in queued:
            _spawn("channels.chat_inventory", "--id", str(i))
        skipped = len(ids) - len(queued)
        return JSONResponse({"ok": True, "queued": len(queued), "skipped_no_session": skipped})
    if action == "proxy":
        proxies = [p.strip() for p in (payload.get("proxies") or []) if p and p.strip()]
        if not proxies:
            return JSONResponse({"error": "нет прокси в списке"}, status_code=400)
        with database.get_conn() as conn:
            for i, aid in enumerate(ids):
                conn.execute("UPDATE accounts SET proxy=? WHERE id=?", (proxies[i % len(proxies)], aid))
        return JSONResponse({"ok": True, "updated": len(ids), "proxies": len(proxies)})
    return JSONResponse({"error": "неизвестное действие"}, status_code=400)


# ---- ИИ-агенты (роль+задача+промпт+аккаунт) ------------------------------- #
@app.get("/api/aiagents")
def aiagents_list() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT ag.*, a.label AS account_label, a.phone AS account_phone "
            "FROM ai_agents ag LEFT JOIN accounts a ON a.id=ag.account_id ORDER BY ag.id"
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.post("/api/aiagents")
def aiagents_save(payload: dict = Body(...)) -> JSONResponse:
    aid = payload.get("id")
    name = (payload.get("name") or "").strip() or None
    task = (payload.get("task") or "").strip() or "other"
    prompt = (payload.get("prompt") or "").strip() or None
    account_id = payload.get("account_id") or None
    active = 1 if payload.get("active", True) else 0
    if not name:
        return JSONResponse({"error": "нужно имя агента"}, status_code=400)
    with database.get_conn() as conn:
        if aid:
            conn.execute(
                "UPDATE ai_agents SET name=?, task=?, prompt=?, account_id=?, active=? WHERE id=?",
                (name, task, prompt, account_id, active, int(aid)),
            )
            new_id = int(aid)
        else:
            cur = conn.execute(
                "INSERT INTO ai_agents (name, task, prompt, account_id, active) VALUES (?,?,?,?,?)",
                (name, task, prompt, account_id, active),
            )
            new_id = cur.lastrowid
    return JSONResponse({"ok": True, "id": new_id})


@app.post("/api/aiagents/{aid}/delete")
def aiagents_delete(aid: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM ai_agents WHERE id=?", (aid,))
    return JSONResponse({"ok": True})


@app.get("/api/account/{acc_id}")
def account_detail(acc_id: int) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    d = dict(row)
    d["tg_connected"] = bool(d.pop("tg_session", None))   # секрет наружу не отдаём
    with database.get_conn() as conn:
        d["chats_count"] = conn.execute(
            "SELECT COUNT(*) c FROM chats WHERE joined_by=? AND in_account='yes'", (acc_id,)
        ).fetchone()["c"]
    return JSONResponse(d)


_ACCOUNT_EDIT_FIELDS = ("label", "phone", "username", "role", "status", "daily_limit",
                        "description", "proxy", "protected", "chats_backup", "kind")


@app.post("/api/account/{acc_id}/update")
def account_update(acc_id: int, payload: dict = Body(...)) -> JSONResponse:
    sets, vals = [], []
    for k in _ACCOUNT_EDIT_FIELDS:
        if k in payload:
            v = payload.get(k)
            if k == "daily_limit":
                v = int(v or 15)
            elif k == "protected":
                v = 1 if v else 0
            else:
                v = (v or None)
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return JSONResponse({"ok": True})
    vals.append(acc_id)
    with database.get_conn() as conn:
        conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)
    return JSONResponse({"ok": True})


@app.post("/api/account/{acc_id}/avatar")
async def account_avatar_upload(acc_id: int, file: UploadFile = File(...)) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT avatar FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if not row:
            return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
        old = row["avatar"] if "avatar" in row.keys() else None
    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "пустой файл"}, status_code=400)
    if len(raw) > 5 * 1024 * 1024:
        return JSONResponse({"error": "картинка больше 5 МБ"}, status_code=400)
    ext = Path(file.filename or "img.png").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        ext = ".png"
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    name = f"a{acc_id}{ext}"
    (AVATAR_DIR / name).write_bytes(raw)
    if old and old != name:
        try:
            (AVATAR_DIR / old).unlink(missing_ok=True)
        except OSError:
            pass
    with database.get_conn() as conn:
        conn.execute("UPDATE accounts SET avatar=? WHERE id=?", (name, acc_id))
    return JSONResponse({"ok": True, "avatar": name})


@app.post("/api/account/{acc_id}/proxy_auto")
def account_proxy_auto(acc_id: int) -> JSONResponse:
    """Выдать аккаунту бесплатный MTProto-прокси из пула (альтернатива платному)."""
    with database.get_conn() as conn:
        acc = conn.execute("SELECT id FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if not acc:
            return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
        p = conn.execute(
            "SELECT server, port, secret FROM proxies WHERE status='alive' "
            "ORDER BY (assigned_to IS NOT NULL), ping_ms LIMIT 1"
        ).fetchone()
        if not p:
            return JSONResponse({"error": "в пуле нет живых прокси — обнови пул в разделе «Прокси»"}, status_code=400)
        link = f"tg://proxy?server={p['server']}&port={p['port']}&secret={p['secret']}"
        conn.execute("UPDATE accounts SET proxy=? WHERE id=?", (link, acc_id))
        conn.execute("UPDATE proxies SET assigned_to=? WHERE server=? AND port=? AND secret=?",
                     (acc_id, p["server"], p["port"], p["secret"]))
    return JSONResponse({"ok": True, "proxy": link})


@app.post("/api/account/{acc_id}/login/start")
async def account_login_start(acc_id: int) -> JSONResponse:
    """Шаг 1 веб-логина: запросить у Telegram код подтверждения."""
    from channels.account_login_web import start_login
    res = await start_login(acc_id)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.post("/api/account/{acc_id}/login/code")
async def account_login_code(acc_id: int, payload: dict = Body(...)) -> JSONResponse:
    """Шаг 2 веб-логина: ввести код (+ пароль 2FA при необходимости)."""
    from channels.account_login_web import submit_code
    res = await submit_code(acc_id, payload.get("code") or "", payload.get("password") or "")
    code = 200 if (res.get("ok") or res.get("need_password")) else 400
    return JSONResponse(res, status_code=code)


@app.post("/api/account/{acc_id}/proxy_find")
def account_proxy_find(acc_id: int) -> JSONResponse:
    """Найти живой бесплатный SOCKS5 и назначить аккаунту (с логом)."""
    res = _run_capture(["channels.proxy_find", "--assign", str(acc_id), "--need", "1", "--max-test", "120"], timeout=240)
    proxy = None
    with database.get_conn() as conn:
        row = conn.execute("SELECT proxy FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if row:
            proxy = row["proxy"]
    return JSONResponse({"ok": res.get("ok"), "output": res.get("output"), "proxy": proxy})


@app.post("/api/account/{acc_id}/warm_now")
def account_warm_now(acc_id: int) -> JSONResponse:
    """Прогреть один аккаунт сейчас и вернуть лог (для проверки из пульта)."""
    res = _run_capture(["channels.warmup", "--id", str(acc_id)], timeout=300)
    return JSONResponse({"ok": res.get("ok"), "output": res.get("output")})


@app.post("/api/account/{acc_id}/inventory")
def account_inventory(acc_id: int) -> JSONResponse:
    """Инвентаризация чатов ЭТОГО аккаунта (его сессия) — заносит группы/каналы в каталог."""
    res = _run_capture(["channels.chat_inventory", "--id", str(acc_id)], timeout=240)
    return JSONResponse({"ok": res.get("ok"), "output": res.get("output")})


# Папка Node-приложения WhatsApp (Baileys). Можно переопределить через env AXIOM_WA_DIR.
import os as _os
WA_DIR = Path(_os.environ.get("AXIOM_WA_DIR", r"C:\Users\vp198\axiom-wa"))
_WA_PROCS: dict = {}   # acc_id -> Popen (держим ссылку, чтобы процесс жил для привязки)


@app.post("/api/account/{acc_id}/wa_login")
def account_wa_login(acc_id: int) -> JSONResponse:
    """Подключить WhatsApp по коду привязки: запускает Node-логин и возвращает 8-значный код.
    Код вводишь на телефоне: WhatsApp → Связанные устройства → Привязать → по номеру телефона."""
    import re
    import shutil
    import subprocess
    import threading
    import time
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT phone FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
    digits = re.sub(r"\D", "", row["phone"] or "")
    if not digits:
        return JSONResponse({"error": "у аккаунта не задан номер телефона"}, status_code=400)
    if not (WA_DIR / "index.js").exists():
        return JSONResponse({"error": f"WhatsApp-модуль не найден в {WA_DIR}. Укажи путь в AXIOM_WA_DIR."},
                            status_code=400)
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    if not Path(node).exists() and not shutil.which("node"):
        return JSONResponse({"error": "Node.js не найден — установи Node или добавь в PATH"}, status_code=400)
    # старый процесс этого аккаунта прибиваем, чтобы не плодить коннекты
    old = _WA_PROCS.pop(acc_id, None)
    if old and old.poll() is None:
        old.terminate()
    proc = subprocess.Popen([node, "index.js", "--auth", digits, "--pair"], cwd=str(WA_DIR),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    _WA_PROCS[acc_id] = proc
    lines: list[str] = []
    code = None

    def _reader():
        for ln in proc.stdout:  # type: ignore
            lines.append(ln)
    t = threading.Thread(target=_reader, daemon=True); t.start()
    deadline = time.time() + 45
    while time.time() < deadline:
        for ln in lines:
            m = re.search(r"КОД:\s*([A-Z0-9\-]{6,12})", ln)
            if m:
                code = m.group(1).strip()
                break
        if code or proc.poll() is not None:
            break
        time.sleep(0.4)
    if code:
        return JSONResponse({"ok": True, "code": code, "phone": digits,
                             "hint": "На телефоне: WhatsApp → Связанные устройства → Привязать устройство → "
                                     "«Привязать по номеру телефона» → введи код. Окно подключения не закрывай."})
    if proc.poll() is None:
        proc.terminate()
    _WA_PROCS.pop(acc_id, None)
    return JSONResponse({"ok": False, "error": "не удалось получить код привязки (см. лог)",
                         "output": "".join(lines[-20:])}, status_code=200)


@app.post("/api/account/{acc_id}/tdesktop")
def account_tdesktop(acc_id: int) -> JSONResponse:
    """Собрать портативный Telegram Desktop для аккаунта (зайти в него руками)."""
    res = _run_capture(["channels.tg_export", "--id", str(acc_id)], timeout=200)
    out = res.get("output") or ""
    info = {}
    try:
        import json as _json
        info = _json.loads(out.strip().split("\n")[-1])
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({"ok": bool(info.get("ok")), "folder": info.get("folder"),
                         "exe": info.get("exe"), "error": info.get("error"), "output": out})


@app.get("/api/account/{acc_id}/chats")
def account_chats(acc_id: int) -> JSONResponse:
    """Чаты, числящиеся за аккаунтом (по инвентаризации) — для резервного списка в карточке."""
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, username, link, kind, members_count, can_write "
            "FROM chats WHERE joined_by=? AND in_account='yes' ORDER BY members_count DESC NULLS LAST, title",
            (acc_id,)).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.post("/api/account/{acc_id}/gen_bio")
def account_gen_bio(acc_id: int) -> JSONResponse:
    """Сгенерировать короткое человеческое bio (ИИ) под роль/легенду аккаунта."""
    from channels.profile_gen import generate_bio
    with database.get_conn() as conn:
        row = conn.execute("SELECT role, label, description FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
    bio = generate_bio(role=row["role"], label=row["label"], description=row["description"])
    return JSONResponse({"ok": True, "bio": bio})


@app.post("/api/account/{acc_id}/profile_setup")
async def account_profile_setup(acc_id: int) -> JSONResponse:
    """Оформить профиль сейчас: аватар + bio (описание) из карточки + приватность
    (спрятать номер, защита от репортов). Приватность применяется даже если карточка
    пустая — спрятать номер полезно любому купленному аккаунту."""
    from telethon.sessions import StringSession
    from channels.telegram import build_client
    from channels.warmup import _setup_profile
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
    acc = dict(row)
    if not acc.get("tg_session"):
        return JSONResponse({"error": "у аккаунта нет сессии — сначала залогинь его (кнопка «Логин»)"}, status_code=400)
    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    try:
        await client.start()
        done = await _setup_profile(client, acc, force=True)   # bio+аватар+приватность
        me = await client.get_me()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"не удалось оформить: {e}"}, status_code=400)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return JSONResponse({"ok": True, "username": me.username or str(me.id), "set": done})


@app.get("/api/account/{acc_id}/inspect")
async def account_inspect(acc_id: int) -> JSONResponse:
    """Инспектор: живой профиль аккаунта (как оформлен, спрятан ли номер) + диалоги."""
    from channels.inspect import inspect
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
    acc = dict(row)
    if not acc.get("tg_session"):
        return JSONResponse({"error": "нет сессии — сначала подключи аккаунт (кнопка «Подключить»)"}, status_code=400)
    try:
        data = await inspect(acc)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"не удалось прочитать аккаунт: {e}"}, status_code=400)
    return JSONResponse(data)


@app.get("/api/account/{acc_id}/dialog_messages")
async def account_dialog_messages(acc_id: int, peer: int) -> JSONResponse:
    """Сообщения выбранного диалога аккаунта (peer — id из /inspect). Read-only."""
    from channels.inspect import dialog_messages
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
    acc = dict(row)
    if not acc.get("tg_session"):
        return JSONResponse({"error": "нет сессии"}, status_code=400)
    try:
        data = await dialog_messages(acc, peer)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"не удалось прочитать переписку: {e}"}, status_code=400)
    return JSONResponse(data)


@app.get("/api/account/{acc_id}/avatar")
def account_avatar(acc_id: int):
    with database.get_conn() as conn:
        row = conn.execute("SELECT avatar FROM accounts WHERE id=?", (acc_id,)).fetchone()
    name = row["avatar"] if row and "avatar" in row.keys() else None
    if not name or not (AVATAR_DIR / name).exists():
        return JSONResponse({"error": "нет аватара"}, status_code=404)
    return FileResponse(AVATAR_DIR / name)


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


# ---- Пул бесплатных MTProto-прокси --------------------------------------- #
@app.get("/api/proxies")
def proxies_list() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT p.*, a.label AS acc_label FROM proxies p "
            "LEFT JOIN accounts a ON a.id=p.assigned_to "
            "ORDER BY (p.status='alive') DESC, p.ping_ms IS NULL, p.ping_ms"
        ).fetchall()
        alive = conn.execute("SELECT COUNT(*) c FROM proxies WHERE status='alive'").fetchone()["c"]
    return JSONResponse({"alive": alive, "items": [dict(r) for r in rows]})


@app.post("/api/proxies/refresh")
def proxies_refresh() -> JSONResponse:
    """Собрать свежие прокси из каналов, проверить пингом, раздать аккаунтам."""
    res = _run_capture(["channels.proxy_pool", "--refresh"], timeout=240)
    return JSONResponse({"ok": res.get("ok"), "output": res.get("output")})


@app.get("/api/proxies/auto")
def proxies_auto_get() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        return JSONResponse({
            "auto": database.get_setting(conn, "proxy_auto", "off") == "on",
            "interval_h": int(database.get_setting(conn, "proxy_interval_min", "360")) // 60,
            "last_run": database.get_setting(conn, "proxy_last_run", None),
        })


@app.post("/api/proxies/auto")
def proxies_auto_set(payload: dict = Body(...)) -> JSONResponse:
    auto = "on" if payload.get("auto") else "off"
    interval_h = max(1, int(payload.get("interval_h") or 6))
    with database.get_conn() as conn:
        database.set_setting(conn, "proxy_auto", auto)
        database.set_setting(conn, "proxy_interval_min", str(interval_h * 60))
    return JSONResponse({"ok": True, "auto": auto == "on", "interval_h": interval_h})


LOG_DIR = config.DB_PATH.parent / "logs"


def _log_run(name: str, result) -> None:
    """Пишет вывод фонового запуска в файл-лог (data/logs/<name>.log) — чтобы
    не гадать по немой консоли, что реально произошло на автомате."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = (result.stdout or "") + (result.stderr or "")
        with open(LOG_DIR / f"{name}.log", "a", encoding="utf-8") as f:
            f.write(f"\n===== {ts} (код выхода {result.returncode}) =====\n{out}\n")
    except Exception as e:  # noqa: BLE001
        print(f"[log {name}] не удалось записать лог: {e}")


def _proxy_scheduler() -> None:
    """Фоновый планировщик: периодически обновляет пул прокси, если включено."""
    import os
    import subprocess
    import sys
    import time
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"   # иначе дочерний процесс падает на любом эмодзи/→ в print()
    while True:
        try:
            with database.get_conn() as conn:
                auto = database.get_setting(conn, "proxy_auto", "off")
                interval_min = int(database.get_setting(conn, "proxy_interval_min", "360"))
                last = database.get_setting(conn, "proxy_last_run_ts", "0")
            if auto == "on" and (time.time() - float(last or 0)) >= interval_min * 60:
                with database.get_conn() as conn:
                    database.set_setting(conn, "proxy_last_run_ts", str(time.time()))
                    database.set_setting(conn, "proxy_last_run", __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"))
                res = subprocess.run([sys.executable, "-m", "channels.proxy_pool", "--refresh"],
                                     cwd=str(BASE_DIR.parent), timeout=300, env=env,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
                _log_run("proxy_scheduler", res)
        except Exception as e:  # noqa: BLE001
            print(f"[proxy scheduler] {e}")
        # --- авто-прогрев (одна ступень по расписанию) ---
        try:
            with database.get_conn() as conn:
                wauto = database.get_setting(conn, "warm_auto", "off")
                wint = int(database.get_setting(conn, "warm_interval_min", "1440"))
                wlast = database.get_setting(conn, "warm_last_run_ts", "0")
            if wauto == "on" and (time.time() - float(wlast or 0)) >= wint * 60:
                with database.get_conn() as conn:
                    database.set_setting(conn, "warm_last_run_ts", str(time.time()))
                    database.set_setting(conn, "warm_last_run", __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"))
                res = subprocess.run([sys.executable, "-m", "channels.warmup", "--run"],
                                     cwd=str(BASE_DIR.parent), timeout=1800, env=env,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
                _log_run("warmup_scheduler", res)
        except Exception as e:  # noqa: BLE001
            print(f"[warmup scheduler] {e}")
        time.sleep(60)


@app.on_event("startup")
def _start_scheduler() -> None:
    import threading
    database.init_db()
    threading.Thread(target=_proxy_scheduler, daemon=True).start()
    # многоаккаунтный слушатель входящих: держит подключёнными все боевые/прогреваемые
    # аккаунты и пишет ответы клиентов в «Диалоги» (авто-ответ — только с активных).
    try:
        from channels.listener import start_in_thread
        start_in_thread()
    except Exception as e:  # noqa: BLE001
        print(f"[listener] не удалось запустить слушатель: {e}")


@app.get("/api/listener/status")
def listener_status() -> JSONResponse:
    """Статус слушателя входящих: сколько аккаунтов слушается, кто не подключился."""
    try:
        from channels import listener
        accs = []
        for aid, info in sorted(listener.STATUS.get("accounts", {}).items()):
            accs.append({"id": aid, "label": info.get("label"),
                         "ok": info.get("ok"), "err": info.get("err")})
        with database.get_conn() as conn:
            auto_reply = database.get_setting(conn, "tg_auto_reply", "on") == "on"
        return JSONResponse({"started": listener.STATUS.get("started"),
                             "listening": sum(1 for a in accs if a["ok"]),
                             "accounts": accs, "auto_reply": auto_reply})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=200)


@app.post("/api/listener/auto_reply")
def listener_auto_reply(payload: dict = Body(...)) -> JSONResponse:
    """Тумблер авто-ответа ИИ-агентом (глобально). Слушание/запись ответов работает
    всегда; этот флаг только про то, отвечать ли автоматически с активных аккаунтов."""
    on = "on" if payload.get("auto_reply") else "off"
    with database.get_conn() as conn:
        database.set_setting(conn, "tg_auto_reply", on)
    return JSONResponse({"ok": True, "auto_reply": on == "on"})


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


@app.get("/api/gcal")
def gcal_events() -> JSONResponse:
    """События из личного Google-календаря (показываем рядом со встречами AXIOM).
    connected=False → файл доступа не подключён (см. README по Google Calendar)."""
    from integrations import calendar as gcal
    if not gcal.enabled():
        return JSONResponse({"connected": False, "reason": "no_credentials"})
    evs = gcal.list_events()
    if evs is None:
        return JSONResponse({"connected": False, "reason": "auth_error"})
    return JSONResponse({"connected": True, "events": evs})


@app.get("/api/notifications")
def notifications() -> JSONResponse:
    """Лента событий для колокольчика: входящие ответы + ближайшие встречи."""
    database.init_db()
    with database.get_conn() as conn:
        msgs = conn.execute(
            "SELECT m.id, m.text, m.ts, m.contact_id, "
            "COALESCE(c.person_name, c.name) AS who "
            "FROM messages m JOIN contacts c ON c.id = m.contact_id "
            "WHERE m.direction='in' ORDER BY m.id DESC LIMIT 25"
        ).fetchall()
        meets = conn.execute(
            "SELECT d.id, d.meeting_at, d.contact_id, COALESCE(c.person_name, c.name) AS who "
            "FROM deals d JOIN contacts c ON c.id = d.contact_id "
            "WHERE d.meeting_at IS NOT NULL AND d.meeting_at >= datetime('now','-1 day') "
            "ORDER BY d.meeting_at LIMIT 25"
        ).fetchall()
        evs = conn.execute(
            "SELECT id, type, level, title, text, contact_id, campaign_id, ts "
            "FROM events ORDER BY id DESC LIMIT 40"
        ).fetchall()
    items = [{"type": "msg", "text": m["text"], "who": m["who"], "ts": m["ts"],
              "contact_id": m["contact_id"]} for m in msgs]
    items += [{"type": "meeting", "text": "назначена встреча", "who": m["who"],
               "ts": m["meeting_at"], "contact_id": m["contact_id"]} for m in meets]
    items += [{"type": "event", "event_type": e["type"], "level": e["level"],
               "title": e["title"], "text": e["text"], "contact_id": e["contact_id"],
               "campaign_id": e["campaign_id"], "ts": e["ts"]} for e in evs]
    items.sort(key=lambda x: x["ts"] or "", reverse=True)
    return JSONResponse({"items": items})


# ---- CRM / Контакты ------------------------------------------------------- #
@app.get("/api/contacts")
def contacts() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.person_name, c.person_role, c.username, c.phone, c.wa_phone,
                   c.city, c.agency, c.tags, c.notes, c.status, c.has_tg, c.has_wa,
                   c.preferred_channel, c.pipeline_id, c.company_id, c.updated_at,
                   c.specialization, c.hook, c.enriched_at, c.source, c.created_at,
                   co.name AS company_name,
                   (SELECT COUNT(*) FROM messages m WHERE m.contact_id = c.id) AS msg_count,
                   (SELECT MAX(ts) FROM messages m WHERE m.contact_id = c.id) AS last_ts
            FROM contacts c
            LEFT JOIN companies co ON co.id = c.company_id
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
        comp = None
        if row["company_id"]:
            comp = conn.execute("SELECT id, name FROM companies WHERE id=?", (row["company_id"],)).fetchone()
    d = dict(row); d["tags"] = _split_tags(d.get("tags"))
    d["company_name"] = comp["name"] if comp else None
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


@app.post("/api/contacts/bulk-tag")
def bulk_tag(payload: dict = Body(...)) -> JSONResponse:
    """Добавить тег списку контактов (выбор аудитории кампании из CRM)."""
    ids = payload.get("ids") or []
    tag = (payload.get("tag") or "").strip()
    if not ids or not tag:
        return JSONResponse({"error": "нужны ids и tag"}, status_code=400)
    updated = 0
    with database.get_conn() as conn:
        for cid in ids:
            row = conn.execute("SELECT tags FROM contacts WHERE id = ?", (cid,)).fetchone()
            if not row:
                continue
            cur = [t.strip() for t in (row["tags"] or "").split(",") if t.strip()]
            if tag not in cur:
                cur.append(tag)
            conn.execute(
                "UPDATE contacts SET tags = ?, updated_at = datetime('now') WHERE id = ?",
                (",".join(cur), cid),
            )
            updated += 1
    return JSONResponse({"ok": True, "updated": updated, "tag": tag})


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


# ---- Компании (юрлица) ---------------------------------------------------- #
_COMPANY_FIELDS = ("name", "company_type", "city", "phone", "site", "email", "vk",
                   "address", "inn", "ogrn", "founders", "tags", "notes", "status")


@app.get("/api/companies")
def companies_list(q: str | None = None, city: str | None = None) -> JSONResponse:
    database.init_db()
    where, params = "1=1", []
    if q:
        where += " AND (co.name LIKE ? OR co.inn LIKE ? OR co.phone LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if city:
        where += " AND co.city = ?"
        params.append(city)
    with database.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT co.*,
                   (SELECT COUNT(*) FROM contacts c WHERE c.company_id=co.id) AS contacts_count,
                   (SELECT COUNT(*) FROM deals d WHERE d.company_id=co.id) AS deals_count
                FROM companies co WHERE {where} ORDER BY co.name""",
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["tags"] = _split_tags(d.get("tags")); out.append(d)
    return JSONResponse(out)


@app.get("/api/company/{cid}")
def company_detail(cid: int) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        contacts = conn.execute(
            "SELECT id, name, person_name, person_role, phone, username, status, has_tg, has_wa "
            "FROM contacts WHERE company_id=? ORDER BY id", (cid,)
        ).fetchall()
        deals = conn.execute(
            "SELECT id, title, stage, product, amount, pipeline_id FROM deals WHERE company_id=? ORDER BY id DESC",
            (cid,)
        ).fetchall()
    d = dict(row); d["tags"] = _split_tags(d.get("tags"))
    d["contacts"] = [dict(c) for c in contacts]
    d["deals"] = [dict(x) for x in deals]
    return JSONResponse(d)


@app.post("/api/companies")
def company_create(payload: dict = Body(...)) -> JSONResponse:
    f = {k: (payload.get(k) or None) for k in _COMPANY_FIELDS}
    if not f["name"]:
        return JSONResponse({"error": "нужно название компании"}, status_code=400)
    f["company_type"] = f["company_type"] or "ООО"
    f["status"] = f["status"] or "active"
    cols = ",".join(_COMPANY_FIELDS)
    ph = ",".join("?" for _ in _COMPANY_FIELDS)
    with database.get_conn() as conn:
        cur = conn.execute(f"INSERT INTO companies ({cols}) VALUES ({ph})",
                           [f[k] for k in _COMPANY_FIELDS])
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/company/{cid}/update")
def company_update(cid: int, payload: dict = Body(...)) -> JSONResponse:
    sets, vals = [], []
    for k in _COMPANY_FIELDS:
        if k in payload:
            sets.append(f"{k}=?"); vals.append(payload.get(k) or None)
    if not sets:
        return JSONResponse({"ok": True})
    vals.append(cid)
    with database.get_conn() as conn:
        conn.execute(f"UPDATE companies SET {', '.join(sets)} WHERE id=?", vals)
    return JSONResponse({"ok": True})


@app.post("/api/company/{cid}/delete")
def company_delete(cid: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("UPDATE contacts SET company_id=NULL WHERE company_id=?", (cid,))
        conn.execute("UPDATE deals SET company_id=NULL WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM companies WHERE id=?", (cid,))
    return JSONResponse({"ok": True})


# ---- Контакты (физлица): создание/правка ---------------------------------- #
_CONTACT_EDIT_FIELDS = ("name", "person_name", "person_role", "phone", "username",
                        "wa_phone", "city", "company_id", "specialization", "tags",
                        "notes", "agent_context", "preferred_channel")


@app.post("/api/contacts/create")
def contact_create(payload: dict = Body(...)) -> JSONResponse:
    name = (payload.get("person_name") or payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "нужно имя контакта"}, status_code=400)
    f = {k: (payload.get(k) or None) for k in _CONTACT_EDIT_FIELDS}
    f["name"] = f["name"] or name
    cols = ["source", *(_CONTACT_EDIT_FIELDS)]
    ph = ",".join("?" for _ in cols)
    with database.get_conn() as conn:
        cur = conn.execute(f"INSERT INTO contacts ({','.join(cols)}) VALUES ({ph})",
                           ["manual", *[f[k] for k in _CONTACT_EDIT_FIELDS]])
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/contact/{contact_id}/update")
def contact_update(contact_id: int, payload: dict = Body(...)) -> JSONResponse:
    sets, vals = [], []
    for k in _CONTACT_EDIT_FIELDS:
        if k in payload:
            v = payload.get(k)
            if k == "company_id":
                v = v or None
            else:
                v = v if (v is not None and v != "") else None
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return JSONResponse({"ok": True})
    vals.append(contact_id)
    with database.get_conn() as conn:
        conn.execute(f"UPDATE contacts SET {', '.join(sets)}, updated_at=datetime('now') WHERE id=?", vals)
    return JSONResponse({"ok": True})


# ---- Сделки (воронка Битрикс) --------------------------------------------- #
@app.get("/api/deals")
def deals_list(pipeline_id: int | None = None) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        pid = pipeline_id or database.get_default_pipeline_id(conn)
        rows = conn.execute(
            """SELECT d.*, co.name AS company_name, c.person_name, c.name AS contact_name,
                      c.username, c.phone
               FROM deals d
               LEFT JOIN companies co ON co.id=d.company_id
               LEFT JOIN contacts c ON c.id=d.contact_id
               WHERE (d.pipeline_id=? OR (d.pipeline_id IS NULL AND ?=?))
               ORDER BY d.updated_at DESC, d.id DESC""",
            (pid, pid, database.get_default_pipeline_id(conn)),
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/deal/{did}")
def deal_detail(did: int) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute(
            """SELECT d.*, co.name AS company_name, c.person_name, c.name AS contact_name
               FROM deals d LEFT JOIN companies co ON co.id=d.company_id
               LEFT JOIN contacts c ON c.id=d.contact_id WHERE d.id=?""",
            (did,),
        ).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(dict(row))


@app.post("/api/deals")
def deal_create(payload: dict = Body(...)) -> JSONResponse:
    title = (payload.get("title") or "").strip()
    contact_id = payload.get("contact_id") or None
    company_id = payload.get("company_id") or None
    with database.get_conn() as conn:
        if not title:
            if company_id:
                r = conn.execute("SELECT name FROM companies WHERE id=?", (company_id,)).fetchone()
                title = (r["name"] if r else None) or "Новая сделка"
            else:
                title = "Новая сделка"
        pid = payload.get("pipeline_id") or database.get_default_pipeline_id(conn)
        cur = conn.execute(
            "INSERT INTO deals (contact_id, company_id, pipeline_id, stage, title, product, amount, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?, datetime('now'), datetime('now'))",
            (contact_id, company_id, pid, payload.get("stage") or "new", title,
             payload.get("product") or None, payload.get("amount") or None),
        )
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.get("/api/leads")
def leads_list() -> JSONResponse:
    """Лиды на квалификацию: контакты в диалоге/ответившие, у кого ещё НЕТ сделки."""
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.person_name, c.person_role, c.username, c.phone,
                   c.city, c.agency, c.tags, c.status, c.source, c.hook, c.specialization,
                   (SELECT COUNT(*) FROM messages m WHERE m.contact_id=c.id AND m.direction='in') AS in_cnt,
                   (SELECT text FROM messages m WHERE m.contact_id=c.id AND m.direction='in' ORDER BY m.id DESC LIMIT 1) AS last_in,
                   (SELECT MAX(ts) FROM messages m WHERE m.contact_id=c.id) AS last_ts
            FROM contacts c
            WHERE c.status IN ('messaged','in_dialog','meeting_set')
              AND NOT EXISTS (SELECT 1 FROM deals d WHERE d.contact_id=c.id)
            ORDER BY (in_cnt>0) DESC, last_ts DESC, c.id DESC
            """
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["tags"] = _split_tags(d.get("tags")); out.append(d)
    return JSONResponse(out)


@app.post("/api/contact/{contact_id}/to_deal")
def contact_to_deal(contact_id: int, payload: dict = Body(default={})) -> JSONResponse:
    """Квалифицировал → конвертирую контакт в сделку и веду по воронке."""
    database.init_db()
    with database.get_conn() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if not c:
            return JSONResponse({"error": "контакт не найден"}, status_code=404)
        ex = conn.execute("SELECT id FROM deals WHERE contact_id=? ORDER BY id DESC LIMIT 1", (contact_id,)).fetchone()
        if ex:
            return JSONResponse({"ok": True, "deal_id": ex["id"], "existing": True})
        title = (payload.get("title") or c["person_name"] or c["name"] or c["agency"] or f"Лид #{contact_id}").strip()
        pid = (payload or {}).get("pipeline_id") or database.get_default_pipeline_id(conn)
        cur = conn.execute(
            "INSERT INTO deals (contact_id, company_id, pipeline_id, stage, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?, datetime('now'), datetime('now'))",
            (contact_id, c["company_id"] if "company_id" in c.keys() else None, pid, "new", title),
        )
        # помечаем контакт как квалифицированный и двигаем по воронке
        database.set_status(conn, contact_id, "in_dialog")
        cur_tags = [t.strip() for t in (c["tags"] or "").split(",") if t.strip()]
        if "квал ✓" not in cur_tags:
            cur_tags.append("квал ✓")
            conn.execute("UPDATE contacts SET tags=? WHERE id=?", (",".join(cur_tags), contact_id))
        database.add_event(conn, "lead", f"✅ Квалифицирован → сделка: {title}",
                           "лид прошёл квалификацию, заведена сделка", level="good", contact_id=contact_id)
    return JSONResponse({"ok": True, "deal_id": cur.lastrowid})


@app.post("/api/deal/{did}/move")
def deal_move(did: int, payload: dict = Body(...)) -> JSONResponse:
    stage = payload.get("stage")
    pid = payload.get("pipeline_id", "keep")
    with database.get_conn() as conn:
        if pid != "keep":
            conn.execute("UPDATE deals SET pipeline_id=?, updated_at=datetime('now') WHERE id=?",
                         (pid or None, did))
        if stage:
            conn.execute("UPDATE deals SET stage=?, updated_at=datetime('now') WHERE id=?", (stage, did))
            # синхронизируем статус привязанного контакта (для дашборда/прогресса)
            row = conn.execute("SELECT contact_id FROM deals WHERE id=?", (did,)).fetchone()
            if row and row["contact_id"]:
                conn.execute("UPDATE contacts SET status=?, updated_at=datetime('now') WHERE id=?",
                             (stage, row["contact_id"]))
    return JSONResponse({"ok": True})


@app.post("/api/deal/{did}/update")
def deal_update(did: int, payload: dict = Body(...)) -> JSONResponse:
    fields = ("title", "product", "amount", "stage", "company_id", "contact_id", "pipeline_id", "notes")
    sets, vals = [], []
    for k in fields:
        if k in payload:
            sets.append(f"{k}=?"); vals.append(payload.get(k) or None)
    if not sets:
        return JSONResponse({"ok": True})
    sets.append("updated_at=datetime('now')")
    vals.append(did)
    with database.get_conn() as conn:
        conn.execute(f"UPDATE deals SET {', '.join(sets)} WHERE id=?", vals)
    return JSONResponse({"ok": True})


@app.post("/api/deal/{did}/delete")
def deal_delete(did: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM deals WHERE id=?", (did,))
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


# ---- Каталог чатов (Волна C, фаза 1: анализ + админы) --------------------- #
@app.get("/api/chatcat")
def chatcat_list() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM chat_admins a WHERE a.chat_id=c.id) AS admins_count "
            "FROM chats c ORDER BY c.members_count DESC, c.id DESC"
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/chatcat/{chat_id}")
def chatcat_detail(chat_id: int) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        admins = conn.execute(
            "SELECT id, tg_user_id, username, name FROM chat_admins WHERE chat_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()
    d = dict(row); d["admins"] = [dict(a) for a in admins]
    return JSONResponse(d)


@app.post("/api/chatcat")
def chatcat_create(payload: dict = Body(...)) -> JSONResponse:
    target = (payload.get("target") or payload.get("username") or "").strip()
    if not target:
        return JSONResponse({"error": "укажи @username или ссылку чата"}, status_code=400)
    username = target.lstrip("@") if not target.startswith("http") and "t.me/" not in target else None
    link = target if (target.startswith("http") or "t.me/" in target) else None
    with database.get_conn() as conn:
        if username:
            ex = conn.execute("SELECT id FROM chats WHERE username=?", (username,)).fetchone()
            if ex:
                return JSONResponse({"ok": True, "id": ex["id"], "existing": True})
        cur = conn.execute(
            "INSERT INTO chats (title, username, link, topic, status) VALUES (?,?,?,?, 'new')",
            (payload.get("title") or username or link, username, link, payload.get("topic") or None),
        )
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/chatcat/{chat_id}/update")
def chatcat_update(chat_id: int, payload: dict = Body(...)) -> JSONResponse:
    sets, vals = [], []
    for k in ("title", "topic", "city", "notes", "status", "link", "can_write", "favorite"):
        if k in payload:
            v = (1 if payload.get(k) else 0) if k == "favorite" else (payload.get(k) or None)
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return JSONResponse({"ok": True})
    vals.append(chat_id)
    with database.get_conn() as conn:
        conn.execute(f"UPDATE chats SET {', '.join(sets)} WHERE id=?", vals)
    return JSONResponse({"ok": True})


@app.post("/api/chatcat/inventory")
def chatcat_inventory() -> JSONResponse:
    """Инвентаризация: занести чаты личного аккаунта в каталог (только чтение)."""
    res = _run_capture(["channels.chat_inventory"], timeout=240)
    return JSONResponse({"ok": res.get("ok"), "output": res.get("output")})


@app.post("/api/chatcat/{chat_id}/delete")
def chatcat_delete(chat_id: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM chat_admins WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
    return JSONResponse({"ok": True})


@app.post("/api/chatcat/{chat_id}/scan")
def chatcat_scan(chat_id: int) -> JSONResponse:
    """Анализ чата (чтение): участники + активность + админы. Без вступления."""
    with database.get_conn() as conn:
        row = conn.execute("SELECT username, link FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "чат не найден"}, status_code=404)
    target = ("@" + row["username"]) if row["username"] else (row["link"] or "")
    if not target:
        return JSONResponse({"error": "у чата нет @username/ссылки для анализа"}, status_code=400)
    res = _run_capture(["channels.chat_scan", "--target", target, "--id", str(chat_id)], timeout=180)
    return JSONResponse({"ok": res.get("ok"), "output": res.get("output")})


@app.post("/api/chatcat/import")
def chatcat_import(payload: dict = Body(...)) -> JSONResponse:
    """Импорт списка чатов из текста (по одной ссылке/username в строке)."""
    try:
        database.init_db()  # инициализируй БД перед импортом
        text = (payload.get("text") or "").strip()
        city = (payload.get("city") or "").strip() or None
        topic = (payload.get("topic") or "").strip() or None

        if not text:
            return JSONResponse({"error": "укажи список ссылок или username'ов"}, status_code=400)

        lines = [line.strip() for line in text.split("\n") if line.strip()]
        added, skipped, errors = 0, 0, []

        with database.get_conn() as conn:
            for line in lines:
                try:
                    target = line.strip()
                    if not target:
                        continue

                    username = None
                    link = None
                    title = target

                    if target.startswith("@"):
                        username = target.lstrip("@").split("?")[0].split("/")[0]
                        title = username
                    elif target.startswith("http") or "t.me/" in target:
                        link = target
                        if "t.me/" in target:
                            parts = target.split("t.me/")
                            if len(parts) > 1:
                                extracted = parts[1].split("?")[0].split("/")[0].strip()
                                if extracted and not extracted.startswith("-"):
                                    username = extracted
                        title = username or link
                    else:
                        username = target.split("?")[0].split("/")[0].strip()
                        title = username

                    if not username and not link:
                        skipped += 1
                        continue

                    ex = None
                    if username:
                        ex = conn.execute("SELECT id FROM chats WHERE username=?", (username,)).fetchone()
                    if not ex and link:
                        ex = conn.execute("SELECT id FROM chats WHERE link=?", (link,)).fetchone()

                    if ex:
                        skipped += 1
                        continue

                    conn.execute(
                        "INSERT INTO chats (title, username, link, city, topic, status) VALUES (?,?,?,?,?,'new')",
                        (title, username, link, city, topic),
                    )
                    added += 1
                except Exception as e:
                    if "UNIQUE constraint failed" in str(e):
                        skipped += 1
                    else:
                        errors.append(f"{line}: {str(e)[:80]}")

        return JSONResponse({
            "ok": True,
            "added": added,
            "skipped": skipped,
            "errors": errors[:10],  # только первые 10 ошибок
            "total": added + skipped + len(errors),
        })
    except Exception as e:
        import traceback
        return JSONResponse({
            "ok": False,
            "error": f"Ошибка импорта: {str(e)}",
            "debug": traceback.format_exc()[:500],
        }, status_code=500)


# ---- Ниши и прослушка чатов по ключам (лиды по нишам) --------------------- #
@app.get("/api/niches")
def niches_list() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM niches ORDER BY id").fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.post("/api/niches")
def niche_create(payload: dict = Body(...)) -> JSONResponse:
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "нужно название ниши"}, status_code=400)
    with database.get_conn() as conn:
        cur = conn.execute("INSERT INTO niches (name, keywords, active) VALUES (?,?,1)",
                           (name, payload.get("keywords") or ""))
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/niche/{nid}/update")
def niche_update(nid: int, payload: dict = Body(...)) -> JSONResponse:
    sets, vals = [], []
    for k in ("name", "keywords", "active"):
        if k in payload:
            v = int(bool(payload[k])) if k == "active" else (payload.get(k) or "")
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return JSONResponse({"ok": True})
    vals.append(nid)
    with database.get_conn() as conn:
        conn.execute(f"UPDATE niches SET {', '.join(sets)} WHERE id=?", vals)
    return JSONResponse({"ok": True})


@app.post("/api/niche/{nid}/delete")
def niche_delete(nid: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM niches WHERE id=?", (nid,))
    return JSONResponse({"ok": True})


@app.post("/api/niche/{nid}/enrich")
def niche_enrich(nid: int) -> JSONResponse:
    """Обогатить ключевые слова ниши через Claude Haiku (генерирует новые ключи)."""
    if not config.ANTHROPIC_API_KEY:
        return JSONResponse({"error": "нет ANTHROPIC_API_KEY в .env"}, status_code=400)

    with database.get_conn() as conn:
        niche = conn.execute("SELECT * FROM niches WHERE id=?", (nid,)).fetchone()
        if not niche:
            return JSONResponse({"error": "ниша не найдена"}, status_code=404)

    import anthropic
    client = anthropic.Anthropic()

    current_keys = (niche["keywords"] or "").split(",")
    current_keys = [k.strip() for k in current_keys if k.strip()]

    prompt = f"""Ты эксперт по B2B лидогенерации. Текущие ключевые слова для ниши "{niche['name']}":
{', '.join(current_keys) if current_keys else '(пусто)'}

Сгенерируй 10-15 НОВЫХ релевантных ключевых слов/фраз для поиска лидов в этой нише.
Ключи — реальные поисковые запросы, которые ищут люди в чатах.

Ответ: просто список через запятую, без нумерации."""

    try:
        resp = client.messages.create(
            model=config.MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        new_keys_raw = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        new_keys = [k.strip() for k in new_keys_raw.split(",") if k.strip()][:15]

        all_keys = set(current_keys + new_keys)
        updated = ", ".join(sorted(all_keys))

        with database.get_conn() as conn:
            conn.execute("UPDATE niches SET keywords=? WHERE id=?", (updated, nid))

        return JSONResponse({
            "ok": True,
            "niche_id": nid,
            "new_keys": new_keys,
            "total": len(all_keys),
            "keywords": updated,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/hits")
def hits_list(status: str = "new") -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT h.*, n.name AS niche_name, c.username AS chat_username, c.link AS chat_link "
            "FROM chat_hits h "
            "LEFT JOIN niches n ON n.id=h.niche_id "
            "LEFT JOIN chats c ON c.id=h.chat_id "
            "WHERE h.status=? ORDER BY h.id DESC LIMIT 500", (status,)
        ).fetchall()
        counts = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM chat_hits GROUP BY status")}
    items = []
    for r in rows:
        d = dict(r)
        # ссылка прямо на сообщение в чате (перейти, увидеть контекст, продолжить переписку)
        if d.get("chat_username") and d.get("source_msg_id"):
            d["msg_link"] = f"https://t.me/{d['chat_username']}/{d['source_msg_id']}"
        else:
            d["msg_link"] = d.get("chat_link") or None    # приватный чат — хотя бы ссылка на сам чат
        items.append(d)
    return JSONResponse({"items": items, "counts": counts})


@app.post("/api/hit/{hid}/lead")
def hit_to_lead(hid: int) -> JSONResponse:
    """Занести находку в CRM как контакт (лид). Помечает hit как lead."""
    with database.get_conn() as conn:
        h = conn.execute("SELECT * FROM chat_hits WHERE id=?", (hid,)).fetchone()
        if not h:
            return JSONResponse({"error": "не найдено"}, status_code=404)
        niche = conn.execute("SELECT name FROM niches WHERE id=?", (h["niche_id"],)).fetchone()
        tag = f"Ниша: {niche['name']}" if niche else (f"Ключ: {h['keyword']}")
        note = f"[{h['chat_title']}] «{h['keyword']}»: {h['text']}"
        cid = database.upsert_contact(
            conn, source="tg_keyword", username=h["username"], tg_user_id=h["tg_user_id"],
            name=h["name"], tags=tag, notes=note,
        )
        conn.execute("UPDATE contacts SET has_tg='yes' WHERE id=?", (cid,))
        conn.execute("UPDATE chat_hits SET status='lead', contact_id=? WHERE id=?", (cid, hid))
    return JSONResponse({"ok": True, "contact_id": cid})


@app.post("/api/hit/{hid}/ignore")
def hit_ignore(hid: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("UPDATE chat_hits SET status='ignored' WHERE id=?", (hid,))
    return JSONResponse({"ok": True})


@app.get("/api/keywords/status")
def keywords_status() -> JSONResponse:
    """Прозрачность слушателя: кто слушает, по скольким чатам, сколько ниш активно."""
    database.init_db()
    with database.get_conn() as conn:
        niches = conn.execute("SELECT COUNT(*) c FROM niches WHERE active=1").fetchone()["c"]
        chats = conn.execute("SELECT COUNT(*) c FROM chats WHERE (username IS NOT NULL AND username<>'') "
                             "OR in_account='yes'").fetchone()["c"]
        fav = conn.execute("SELECT COUNT(*) c FROM chats WHERE COALESCE(favorite,0)=1").fetchone()["c"]
        sample = [dict(r) for r in conn.execute(
            "SELECT title, username FROM chats WHERE (username IS NOT NULL AND username<>'') "
            "OR in_account='yes' ORDER BY COALESCE(favorite,0) DESC, id LIMIT 12")]
    return JSONResponse({"account": "основной аккаунт (.env)" if config.TG_STRING_SESSION else "основной (.env)",
                         "chats": chats, "favorite": fav, "niches": niches, "sample": sample})


@app.post("/api/keywords/run")
def keywords_run(payload: dict = Body(default={})) -> JSONResponse:
    """Прослушать чаты каталога по ключам активных ниш (поллинг, на обзор)."""
    limit = int((payload or {}).get("limit") or 300)
    args = ["channels.chat_keywords", "--limit", str(limit)]
    if (payload or {}).get("favorites"):
        args.append("--favorites")    # слушать только ⭐ избранные
    res = _run_capture(args, timeout=300)
    return JSONResponse({"ok": res.get("ok"), "output": res.get("output")})


# ---- Визард запуска кампании: копайлот + загрузка телефонов ЦА ------------- #
@app.post("/api/copilot")
def copilot(payload: dict = Body(...)) -> JSONResponse:
    """Подсказка от Claude по шагу визарда (Haiku, дёшево)."""
    if not config.ANTHROPIC_API_KEY:
        return JSONResponse({"error": "нет ANTHROPIC_API_KEY в .env"}, status_code=400)
    step = (payload.get("step") or "").strip()
    context = payload.get("context") or ""
    try:
        from agent.copilot import suggest
        text = suggest(step, context)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "text": text})


@app.post("/api/import/phones")
def import_phones(payload: dict = Body(...)) -> JSONResponse:
    """Загрузка списка телефонов ЦА (вставка текстом). Создаёт контакты для проверки мессенджеров."""
    raw = payload.get("text") or ""
    tag = (payload.get("tag") or "Телефоны ЦА").strip()
    source = (payload.get("source") or "phones").strip() or "phones"
    import re as _re
    nums = set()
    # выдёргиваем телефоноподобные последовательности (с пробелами/скобками/дефисами внутри)
    for cand in _re.findall(r"\+?[\d][\d\s\-()]{8,}\d", raw):
        p = norm_phone(cand)
        if p:
            nums.add(p)
    if not nums:
        return JSONResponse({"error": "не нашёл валидных номеров"}, status_code=400)
    added = 0
    with database.get_conn() as conn:
        for p in nums:
            database.upsert_contact(conn, source=source, phone=p, name=p, tags=tag)
            added += 1
        total = conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]
    return JSONResponse({"ok": True, "imported": added, "total": total})


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


def _parse_2gis(text: str, tag: str, source: str = "2gis") -> tuple[int, int]:
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
                conn, source=source, phone=phone, username=username, name=name,
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
async def import_2gis(file: UploadFile = File(...), tag: str = Form("Агентства недвижимости"),
                      source: str = Form("2gis")) -> JSONResponse:
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
        added, skipped = _parse_2gis(text, tag.strip(), (source or "2gis").strip() or "2gis")
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
                   (SELECT COUNT(*) FROM messages m WHERE m.contact_id = c.id AND m.direction='in') AS in_cnt,
                   (SELECT text FROM messages m WHERE m.contact_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_text,
                   (SELECT direction FROM messages m WHERE m.contact_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_dir,
                   (SELECT MAX(ts) FROM messages m WHERE m.contact_id = c.id) AS last_ts,
                   (SELECT a.label FROM campaign_contacts cc JOIN accounts a ON a.id=cc.account_id
                      WHERE cc.contact_id=c.id AND cc.account_id IS NOT NULL ORDER BY cc.rowid DESC LIMIT 1) AS account_label,
                   (SELECT cc.account_id FROM campaign_contacts cc
                      WHERE cc.contact_id=c.id AND cc.account_id IS NOT NULL ORDER BY cc.rowid DESC LIMIT 1) AS account_id
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
def _sync_campaign_accounts(conn, cid: int, account_ids, account_limits: dict | None = None) -> None:
    """Полная пересборка команды кампании (какие агенты её работают + лимит/день
    на КАЖДЫЙ — сколько эта кампания вправе слать именно с него в сутки).
    account_limits: {account_id (int|str): daily_limit}. Пусто у аккаунта — падает
    обратно на его общий daily_limit (см. COALESCE в campaign_send._team)."""
    account_limits = account_limits or {}
    conn.execute("DELETE FROM campaign_accounts WHERE campaign_id=?", (cid,))
    for aid in (account_ids or []):
        try:
            aid_i = int(aid)
        except (TypeError, ValueError):
            continue
        lim = account_limits.get(aid_i) if aid_i in account_limits else account_limits.get(str(aid_i))
        try:
            lim = int(lim) if lim not in (None, "") else None
        except (TypeError, ValueError):
            lim = None
        conn.execute(
            "INSERT OR IGNORE INTO campaign_accounts (campaign_id, account_id, daily_limit) VALUES (?,?,?)",
            (cid, aid_i, lim),
        )


def _channel_clause(channel: str | None) -> str:
    """SQL-условие «контакт достижим хотя бы по одному из выбранных каналов».
    channel может быть 'telegram', 'whatsapp' или 'telegram,whatsapp'."""
    chans = [c.strip() for c in (channel or "").split(",") if c.strip()]
    conds = []
    if "telegram" in chans:
        conds.append("has_tg IN ('yes','unknown')")
    if "whatsapp" in chans:
        conds.append("has_wa IN ('yes','unknown')")
    return "(" + " OR ".join(conds) + ")" if conds else ""


def _audience_count(conn, tag, channel) -> int:
    where = "status='new' AND (username IS NOT NULL OR phone IS NOT NULL)"
    params = []
    cc = _channel_clause(channel)
    if cc:
        where += " AND " + cc
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
    team_rows = conn.execute(
        "SELECT account_id, daily_limit FROM campaign_accounts WHERE campaign_id=?", (d["id"],)).fetchall()
    d["accounts"] = [r["account_id"] for r in team_rows]
    d["account_limits"] = {str(r["account_id"]): r["daily_limit"] for r in team_rows if r["daily_limit"] is not None}
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
    account_limits = payload.get("account_limits") or {}
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (name, product, audience_tag, channel, account_id, daily_limit, "
            "message_template, agent_prompt, kp_text, project_id, status) VALUES (?,?,?,?,?,?,?,?,?,?, 'draft')",
            (f["name"], f["product"], f["audience_tag"], f["channel"], account_id, daily_limit,
             f["message_template"], f["agent_prompt"], f["kp_text"], project_id),
        )
        _sync_campaign_accounts(conn, cur.lastrowid, account_ids, account_limits)
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
    account_limits = payload.get("account_limits") or {}
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
            _sync_campaign_accounts(conn, cid, account_ids, account_limits)
    return JSONResponse({"ok": True, "id": cid})


@app.post("/api/campaign/{cid}/test_contacts")
def campaign_test_contacts(cid: int, payload: dict = Body(...)) -> JSONResponse:
    """Свои номера/юзернеймы для теста ЭТОЙ кампании — без отдельной тестовой кампании.
    Помечает is_test=1 (см. channels/campaign_send._audience: тестовые всегда идут первыми
    в очереди), сбрасывает статус в 'new' (даже если контакт уже был раньше) и добавляет
    тег аудитории кампании — иначе не попадут в фильтр _audience по audience_tag."""
    raw = (payload.get("text") or "").strip()
    if not raw:
        return JSONResponse({"error": "пусто — введи хотя бы один номер или @username"}, status_code=400)
    with database.get_conn() as conn:
        camp = conn.execute("SELECT audience_tag FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not camp:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        tag = (camp["audience_tag"] or "").strip()
        added = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("@"):
                uname = line.lstrip("@")
                row_id = database.upsert_contact(conn, source="test", username=uname, name=line, tags=tag)
            else:
                p = norm_phone(line)
                if not p:
                    continue
                row_id = database.upsert_contact(conn, source="test", phone=p, name=p, tags=tag)
            conn.execute("UPDATE contacts SET is_test=1, status='new' WHERE id=?", (row_id,))
            added += 1
    if not added:
        return JSONResponse({"error": "не нашёл ни валидного номера, ни @username"}, status_code=400)
    return JSONResponse({"ok": True, "added": added})


@app.get("/api/campaign/{cid}/preview")
def campaign_preview(cid: int) -> JSONResponse:
    """Показать РЕАЛЬНЫЙ текст, который уйдёт каждому получателю — без единой отправки
    в Telegram. Рендерит тот же шаблон (обращение по ФИО, синонимизация {a|b|c}), что и
    боевая рассылка, на тестовых (is_test=1) и на первых из обычной аудитории контактах."""
    from channels.campaign_send import _parts, _greeting, _decision_phrase
    database.init_db()
    with database.get_conn() as conn:
        camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not camp:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        camp = dict(camp)
        tag = (camp.get("audience_tag") or "").strip()
        where = "status='new' AND (username IS NOT NULL OR phone IS NOT NULL)"
        params: list = []
        if tag:
            where += " AND tags LIKE ?"
            params.append(f"%{tag}%")
        rows = conn.execute(
            f"SELECT * FROM contacts WHERE {where} "
            f"ORDER BY COALESCE(is_test,0) DESC, id LIMIT 10", params,
        ).fetchall()
    out = []
    for r in rows:
        name = _greeting(r)
        parts = _parts(camp.get("message_template"), name, r["agency"] or r["name"], _decision_phrase(r))
        out.append({
            "contact_id": r["id"], "handle": ("@" + r["username"]) if r["username"] else (r["phone"] or "—"),
            "greeting": name or "(без имени — обращение будет пустым)",
            "is_test": bool(r["is_test"]) if "is_test" in r.keys() else False,
            "parts": parts,
        })
    return JSONResponse({"campaign": camp.get("name"), "count": len(out), "items": out})


def _safe_kp_name(cid: int, filename: str) -> str:
    """Имя файла КП: c{cid}_<очищенное имя>. Без путей и спецсимволов."""
    base = Path(filename or "kp").name
    base = "".join(ch for ch in base if ch.isalnum() or ch in "._- ").strip() or "kp.pdf"
    return f"c{cid}_{base}"


@app.post("/api/campaign/{cid}/kp")
async def campaign_kp_upload(cid: int, file: UploadFile = File(...)) -> JSONResponse:
    """Прикрепить файл КП к кампании. Агент будет отправлять его файлом в диалоге."""
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT id, kp_file FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        old = row["kp_file"] if "kp_file" in row.keys() else None
    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "пустой файл"}, status_code=400)
    if len(raw) > 20 * 1024 * 1024:
        return JSONResponse({"error": "файл больше 20 МБ"}, status_code=400)
    KP_DIR.mkdir(parents=True, exist_ok=True)
    name = _safe_kp_name(cid, file.filename)
    (KP_DIR / name).write_bytes(raw)
    if old and old != name:
        try:
            (KP_DIR / old).unlink(missing_ok=True)
        except OSError:
            pass
    with database.get_conn() as conn:
        conn.execute("UPDATE campaigns SET kp_file=? WHERE id=?", (name, cid))
    return JSONResponse({"ok": True, "kp_file": name, "size": len(raw)})


@app.get("/api/campaign/{cid}/kp")
def campaign_kp_download(cid: int):
    """Скачать/посмотреть прикреплённое КП (для проверки оператором)."""
    with database.get_conn() as conn:
        row = conn.execute("SELECT kp_file FROM campaigns WHERE id=?", (cid,)).fetchone()
    name = row["kp_file"] if row and "kp_file" in row.keys() else None
    if not name or not (KP_DIR / name).exists():
        return JSONResponse({"error": "КП не приложено"}, status_code=404)
    return FileResponse(KP_DIR / name, filename=name)


@app.post("/api/campaign/{cid}/kp/delete")
def campaign_kp_delete(cid: int) -> JSONResponse:
    with database.get_conn() as conn:
        row = conn.execute("SELECT kp_file FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        name = row["kp_file"] if "kp_file" in row.keys() else None
        conn.execute("UPDATE campaigns SET kp_file=NULL WHERE id=?", (cid,))
    if name:
        try:
            (KP_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass
    return JSONResponse({"ok": True})


# ---- Несколько КП в кампании (под типы ЦА; агент сам выбирает уместное) ---- #
@app.get("/api/campaign/{cid}/kps")
def campaign_kps_list(cid: int) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, campaign_id, name, when_to_use, kp_text, kp_file "
            "FROM campaign_kps WHERE campaign_id=? ORDER BY id", (cid,),
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.post("/api/campaign/{cid}/kps")
def campaign_kps_save(cid: int, payload: dict = Body(...)) -> JSONResponse:
    """Создать или обновить КП в наборе кампании (name, when_to_use, kp_text)."""
    kp_id = payload.get("id")
    name = (payload.get("name") or "").strip() or None
    when_to_use = (payload.get("when_to_use") or "").strip() or None
    kp_text = (payload.get("kp_text") or "").strip() or None
    with database.get_conn() as conn:
        if kp_id:
            conn.execute(
                "UPDATE campaign_kps SET name=?, when_to_use=?, kp_text=? WHERE id=? AND campaign_id=?",
                (name, when_to_use, kp_text, int(kp_id), cid),
            )
            new_id = int(kp_id)
        else:
            cur = conn.execute(
                "INSERT INTO campaign_kps (campaign_id, name, when_to_use, kp_text) VALUES (?,?,?,?)",
                (cid, name, when_to_use, kp_text),
            )
            new_id = cur.lastrowid
    return JSONResponse({"ok": True, "id": new_id})


@app.post("/api/campaign/{cid}/kps/{kp_id}/file")
async def campaign_kps_file(cid: int, kp_id: int, file: UploadFile = File(...)) -> JSONResponse:
    with database.get_conn() as conn:
        row = conn.execute("SELECT id, kp_file FROM campaign_kps WHERE id=? AND campaign_id=?", (kp_id, cid)).fetchone()
        if not row:
            return JSONResponse({"error": "КП не найдено"}, status_code=404)
        old = row["kp_file"]
    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "пустой файл"}, status_code=400)
    if len(raw) > 20 * 1024 * 1024:
        return JSONResponse({"error": "файл больше 20 МБ"}, status_code=400)
    KP_DIR.mkdir(parents=True, exist_ok=True)
    name = _safe_kp_name(cid, f"kp{kp_id}_{file.filename}")
    (KP_DIR / name).write_bytes(raw)
    if old and old != name:
        try:
            (KP_DIR / old).unlink(missing_ok=True)
        except OSError:
            pass
    with database.get_conn() as conn:
        conn.execute("UPDATE campaign_kps SET kp_file=? WHERE id=?", (name, kp_id))
    return JSONResponse({"ok": True, "kp_file": name})


@app.get("/api/campaign/{cid}/kps/{kp_id}/file")
def campaign_kps_file_get(cid: int, kp_id: int):
    with database.get_conn() as conn:
        row = conn.execute("SELECT kp_file FROM campaign_kps WHERE id=? AND campaign_id=?", (kp_id, cid)).fetchone()
    name = row["kp_file"] if row else None
    if not name or not (KP_DIR / name).exists():
        return JSONResponse({"error": "файл не приложен"}, status_code=404)
    return FileResponse(KP_DIR / name, filename=name)


@app.post("/api/campaign/{cid}/kps/{kp_id}/delete")
def campaign_kps_delete(cid: int, kp_id: int) -> JSONResponse:
    with database.get_conn() as conn:
        row = conn.execute("SELECT kp_file FROM campaign_kps WHERE id=? AND campaign_id=?", (kp_id, cid)).fetchone()
        name = row["kp_file"] if row else None
        conn.execute("DELETE FROM campaign_kps WHERE id=? AND campaign_id=?", (kp_id, cid))
    if name:
        try:
            (KP_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass
    return JSONResponse({"ok": True})


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
        d["kps"] = [dict(r) for r in conn.execute(
            "SELECT id, name, when_to_use, kp_text, kp_file FROM campaign_kps WHERE campaign_id=? ORDER BY id",
            (cid,),
        ).fetchall()]
    return JSONResponse(d)


@app.get("/api/campaign/{cid}/preflight")
def campaign_preflight(cid: int) -> JSONResponse:
    """Пред-полётная проверка: что готово/мешает запуску кампании."""
    database.init_db()
    with database.get_conn() as conn:
        camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not camp:
            return JSONResponse({"error": "not found"}, status_code=404)
        camp = dict(camp)
        team = [dict(t) for t in conn.execute(
            "SELECT a.*, COALESCE(ca.daily_limit, a.daily_limit) AS cap "
            "FROM accounts a JOIN campaign_accounts ca ON ca.account_id=a.id "
            "WHERE ca.campaign_id=?", (cid,)).fetchall()]
        aud = _audience_count(conn, camp.get("audience_tag"), camp.get("channel"))
        kps = conn.execute("SELECT COUNT(*) c FROM campaign_kps WHERE campaign_id=?", (cid,)).fetchone()["c"]
        has_main = bool(config.TG_STRING_SESSION)

    connected = [t for t in team if t.get("tg_session")]
    no_proxy = [t for t in connected if not (t.get("proxy") or "").strip()]
    banned = [t for t in team if t.get("status") == "banned"]
    usable = [t for t in connected if t.get("status") != "banned"]

    checks = []
    def add(ok, level, text):
        checks.append({"ok": ok, "level": level, "text": text})

    add(bool((camp.get("channel") or "").strip()), "fail", "Канал выбран" if camp.get("channel") else "Не выбран канал (Telegram/WhatsApp)")
    add(bool((camp.get("message_template") or "").strip()), "fail",
        "Первое сообщение заполнено" if (camp.get("message_template") or "").strip() else "Пустое первое сообщение — нечего слать")
    add(aud > 0, "fail", f"Аудитория: {aud} контактов" if aud > 0 else "Аудитория пуста (проверь тег и канал)")

    if team:
        add(True, "ok", f"Команда отправителей: {len(team)} акк.")
        add(len(usable) > 0, "fail",
            f"Подключены (TG✓), не в бане: {len(usable)} из {len(team)}" if usable
            else "Ни один отправитель не подключён/живой — подключи и прогрей")
        add(len(no_proxy) == 0, "warn",
            "У всех отправителей есть прокси" if not no_proxy
            else f"Без прокси: {len(no_proxy)} акк. — из РФ не подключатся (вставь SOCKS5)")
        if banned:
            add(False, "warn", f"В бане: {len(banned)} акк. — выведи из кампании")
        # реальная суммарная ёмкость: только живые не-забаненные, с их персональным (или общим) лимитом
        usable_ids = {t["id"] for t in usable}
        total_cap = sum(int(t.get("cap") or 15) for t in team if t["id"] in usable_ids)
        add(True, "info", f"Суммарно готовы слать до {total_cap}/день на всю команду (при текущих лимитах)")
    else:
        add(has_main, "warn",
            "Команда не назначена — пойдёт с основного аккаунта (.env)" if has_main
            else "Нет ни команды, ни основного аккаунта (.env) — слать нечем")

    add(bool((camp.get("kp_text") or "").strip()) or bool(camp.get("kp_file")) or kps > 0, "info",
        (f"КП: {kps} под типы" if kps else "КП задано") if (kps or camp.get("kp_text") or camp.get("kp_file"))
        else "КП не задано (не критично — агент пришлёт позже, если добавишь)")

    ready = all(c["ok"] for c in checks if c["level"] == "fail")
    return JSONResponse({"ready": ready, "checks": checks, "audience": aud, "team": len(team)})


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
        cc = _channel_clause(channel)
        if cc:
            where += " AND " + cc
        if tag:
            where += " AND tags LIKE ?"
            params.append(f"%{tag}%")
        pend = conn.execute(
            f"SELECT id,name,username,phone,person_name,status,COALESCE(is_test,0) is_test "
            f"FROM contacts WHERE {where} ORDER BY COALESCE(is_test,0) DESC, id LIMIT 500",
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
            "is_test": bool(r["is_test"]),
        })
    return JSONResponse({"account": acc, "account_name": acc_name,
                         "sent_count": len(sent_ids), "total": len(rows), "rows": rows})


_ECON_FIELDS = ("goal_start", "result_note", "cost_proxy", "cost_accounts", "cost_ai",
                "cost_other", "revenue_per_deal", "manager_salary", "manager_leads")
_ENGAGED = ("in_dialog", "meeting_set", "met", "won")


@app.get("/api/campaign/{cid}/econ")
def campaign_econ(cid: int) -> JSONResponse:
    """Экономика кампании: цели, расходы, стоимость лида, ROI, робот vs человек."""
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        row = dict(row)
        reached = conn.execute("SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchone()["c"]
        qmarks = ",".join("?" for _ in _ENGAGED)
        leads = conn.execute(
            f"SELECT COUNT(DISTINCT cc.contact_id) c FROM campaign_contacts cc "
            f"JOIN contacts ct ON ct.id=cc.contact_id WHERE cc.campaign_id=? AND ct.status IN ({qmarks})",
            (cid, *_ENGAGED),
        ).fetchone()["c"]
        deals = conn.execute(
            "SELECT COUNT(DISTINCT cc.contact_id) c FROM campaign_contacts cc "
            "JOIN contacts ct ON ct.id=cc.contact_id WHERE cc.campaign_id=? AND ct.status='won'",
            (cid,),
        ).fetchone()["c"]

    def num(k):
        v = row.get(k)
        return float(v) if v not in (None, "") else 0.0

    total_cost = num("cost_proxy") + num("cost_accounts") + num("cost_ai") + num("cost_other")
    rev = deals * num("revenue_per_deal")
    cost_per_lead = round(total_cost / leads) if leads else None
    cost_per_deal = round(total_cost / deals) if deals else None
    roi = round((rev - total_cost) / total_cost * 100) if total_cost else None
    # робот vs человек
    human_cpl = round(num("manager_salary") / num("manager_leads")) if num("manager_leads") else None
    econ = {k: row.get(k) for k in _ECON_FIELDS}
    return JSONResponse({
        "econ": econ,
        "metrics": {
            "reached": reached, "leads": leads, "deals": deals,
            "total_cost": round(total_cost), "revenue": round(rev),
            "cost_per_lead": cost_per_lead, "cost_per_deal": cost_per_deal, "roi": roi,
            "human_cost_per_lead": human_cpl,
            "saving_vs_human": (round((human_cpl - cost_per_lead)) if (human_cpl and cost_per_lead) else None),
        },
    })


@app.post("/api/campaign/{cid}/econ")
def campaign_econ_save(cid: int, payload: dict = Body(...)) -> JSONResponse:
    sets, vals = [], []
    for k in _ECON_FIELDS:
        if k in payload:
            v = payload.get(k)
            if k not in ("goal_start", "result_note"):
                v = float(v) if v not in (None, "") else None
            else:
                v = v or None
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return JSONResponse({"ok": True})
    vals.append(cid)
    with database.get_conn() as conn:
        conn.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id=?", vals)
    return JSONResponse({"ok": True})


@app.get("/api/campaign/{cid}/team")
def campaign_team(cid: int) -> JSONResponse:
    """Команда кампании с прогрессом прогрева (для панели прогрева)."""
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT a.id, a.label, a.phone, a.username, a.status, a.warm_stage, "
            "(a.tg_session IS NOT NULL AND a.tg_session<>'') AS tg_connected, a.proxy "
            "FROM accounts a JOIN campaign_accounts ca ON ca.account_id=a.id "
            "WHERE ca.campaign_id=? ORDER BY a.id", (cid,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["ready_stage"] = 14; d["tg_connected"] = bool(d["tg_connected"]); out.append(d)
    return JSONResponse({"team": out})


@app.post("/api/campaign/{cid}/warmup")
def campaign_warmup(cid: int, payload: dict = Body(default={})) -> JSONResponse:
    """Запустить прогрев (одна ступень). Греет аккаунты в статусе 'warming'
    (взаимная переписка + каналы + якоря). Безопасный человекоподобный темп."""
    with database.get_conn() as conn:
        warming = conn.execute(
            "SELECT COUNT(*) c FROM accounts a JOIN campaign_accounts ca ON ca.account_id=a.id "
            "WHERE ca.campaign_id=? AND a.status='warming' AND a.tg_session IS NOT NULL AND a.tg_session<>''",
            (cid,),
        ).fetchone()["c"]
    if not warming:
        return JSONResponse({"error": "в команде нет аккаунтов в статусе «прогрев» с авторизованной сессией"}, status_code=400)
    _spawn("channels.warmup", "--run")
    return JSONResponse({"ok": True, "warming": warming})


@app.get("/api/warmup/settings")
def warmup_settings_get() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        return JSONResponse({
            "auto": database.get_setting(conn, "warm_auto", "off") == "on",
            "interval_h": int(database.get_setting(conn, "warm_interval_min", "1440")) // 60,
            "ca_mix": database.get_setting(conn, "warm_ca_mix", "off") == "on",
            "last_run": database.get_setting(conn, "warm_last_run", None),
        })


@app.post("/api/warmup/run_now")
def warmup_run_now() -> JSONResponse:
    """Прогреть СЕЙЧАС всех аккаунтов в статусе «прогрев» с сессией (фоновый процесс)."""
    with database.get_conn() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE status='warming' "
            "AND tg_session IS NOT NULL AND tg_session <> ''"
        ).fetchone()["c"]
    if not cnt:
        return JSONResponse({"error": "нет аккаунтов в статусе «прогрев» с авторизованной сессией"}, status_code=400)
    _spawn("channels.warmup", "--run")
    return JSONResponse({"ok": True, "warming": cnt})


@app.post("/api/warmup/settings")
def warmup_settings_set(payload: dict = Body(...)) -> JSONResponse:
    with database.get_conn() as conn:
        if "auto" in payload:
            database.set_setting(conn, "warm_auto", "on" if payload.get("auto") else "off")
        if "interval_h" in payload:
            database.set_setting(conn, "warm_interval_min", str(max(1, int(payload.get("interval_h") or 24)) * 60))
        if "ca_mix" in payload:
            database.set_setting(conn, "warm_ca_mix", "on" if payload.get("ca_mix") else "off")
    return JSONResponse({"ok": True})


@app.post("/api/campaign/{cid}/launch")
def campaign_launch(cid: int, payload: dict = Body(...)) -> JSONResponse:
    import subprocess
    import sys
    limit = int(payload.get("limit") or 3)
    force = bool(payload.get("force"))
    with database.get_conn() as conn:
        row = conn.execute("SELECT message_template FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        if not (row["message_template"] or "").strip():
            return JSONResponse({"error": "сначала заполни текст первого сообщения"}, status_code=400)
        # Защита от повторного запуска: если запускали < 10 мин назад — просим подтверждение.
        recent = conn.execute(
            "SELECT ts FROM events WHERE campaign_id=? AND type='campaign_start' "
            "AND ts >= datetime('now','-10 minutes') ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        if recent and not force:
            return JSONResponse({"needs_confirm": True,
                                 "warn": "Кампанию уже запускали недавно — рассылка ещё идёт в фоне. "
                                         "Повторный запуск задвоит сообщения и повысит риск флуд-лимита. "
                                         "Точно запустить ещё раз?"})
        nm = conn.execute("SELECT name FROM campaigns WHERE id=?", (cid,)).fetchone()
        conn.execute("UPDATE campaigns SET status='running' WHERE id=?", (cid,))
        database.add_event(conn, "campaign_start", f"▶ Старт кампании «{(nm['name'] if nm else cid)}»",
                           f"запуск рассылки до {limit} контактов", level="good", campaign_id=cid)
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
