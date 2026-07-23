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

from fastapi import FastAPI, Body, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse

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

# --------------------------------------------------------------------------- #
#  Вход по паролю (закрытый доступ на сервере).                                #
#  Включается ТОЛЬКО если задана переменная окружения AXIOM_PASSWORD —         #
#  локально (без неё) пульт работает как раньше, без входа. Работает с любого  #
#  IP: привязки к адресу нет. ⚠️ По-настоящему безопасно только под HTTPS      #
#  (иначе пароль идёт по сети открытым текстом) — это следующий шаг.           #
# --------------------------------------------------------------------------- #
import hashlib as _hashlib
import hmac as _hmac
import os as _os_auth

_AUTH_PW = _os_auth.environ.get("AXIOM_PASSWORD", "").strip()
_AUTH_COOKIE = "axiom_auth"
_AUTH_OPEN = {"/login", "/favicon.ico", "/health", "/api/auth/request-code", "/api/auth/verify-code"}


def _auth_token() -> str:
    """Стабильный токен из пароля: меняется при смене пароля (старые входы слетают)."""
    return _hmac.new(_AUTH_PW.encode(), b"axiom-web-v1", _hashlib.sha256).hexdigest()


_LOGIN_HTML = """<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>AXIOM — вход</title>
<style>
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#0b1020;font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#e8ecf5}
.card{background:#141b2f;padding:32px 28px;border-radius:16px;width:320px;
box-shadow:0 20px 60px rgba(0,0,0,.5);border:1px solid #222c46}
h1{font-size:20px;margin:0 0 4px;text-align:center}
p{color:#8b96b3;font-size:13px;margin:0 0 20px;text-align:center}
input{width:100%;box-sizing:border-box;padding:12px 14px;border-radius:10px;
border:1px solid #2a3557;background:#0d1428;color:#fff;font-size:15px;margin-bottom:12px}
button{width:100%;padding:12px;border:0;border-radius:10px;background:#5b6cff;
color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#4a5bef}
button.sec{background:transparent;border:1px solid #2a3557;color:#8b96b3;margin-top:8px}
button.sec:hover{border-color:#5b6cff;color:#e8ecf5}
.err{background:#3a1622;color:#ff9db1;padding:9px 12px;border-radius:8px;
font-size:13px;margin-bottom:12px;text-align:center}
.ok{background:#163a1e;color:#9bffb1;padding:9px 12px;border-radius:8px;font-size:13px;margin-bottom:12px;text-align:center}
.divider{display:flex;align-items:center;gap:12px;margin:16px 0;color:#4a5580;font-size:12px}
.divider:before,.divider:after{content:"";flex:1;height:1px;background:#1e2844}
.tab{display:flex;margin-bottom:18px;border-radius:10px;background:#0d1428;overflow:hidden}
.tab button{flex:1;padding:10px;border:0;background:transparent;color:#8b96b3;font-size:13px;cursor:pointer;border-radius:0}
.tab button.on{background:#5b6cff;color:#fff}
.hide{display:none}
.tg-info{font-size:12px;color:#6a75a0;margin-bottom:16px;text-align:center;line-height:1.5}
</style>
<div class=card>
<div class=tab>
<button id=t1 class=on onclick="switchTab(1)">🔑 Пароль</button>
<button id=t2 onclick="switchTab(2)">✈️ Telegram</button>
</div>

<!-- Пароль -->
<div id=pane1>
<form method=post action=/login>
<!--ERR-->
<input type=password name=password placeholder="Пароль" autofocus required>
<button type=submit>Войти</button>
</form>
</div>

<!-- Telegram -->
<div id=pane2 class=hide>
<p>Авторизация через бота</p>
<div class=tg-info>Напишите <b>/login</b> боту <b>@Jarvisvvp_bot</b>,<br>затем нажмите «Получить код»</div>
<div id=tg-status></div>
<button class=sec onclick="requestTgCode()">📱 Получить код</button>
<div id=tg-code-block class=hide style=margin-top:16px>
<input id=tg-code placeholder="6 цифр из Telegram" maxlength=6 autocomplete=off inputmode=numeric>
<button onclick="verifyTgCode()">Войти</button>
</div>
</div>
</div>

<script>
let tab=1;
function switchTab(n){tab=n;['t1','t2'].forEach((id,i)=>document.getElementById(id).className=i+1===n?'on':'');['pane1','pane2'].forEach((id,i)=>document.getElementById(id).className=i+1===n?'':'hide')}
function status(msg,ok){document.getElementById('tg-status').innerHTML=ok?'<div class=ok>'+msg+'</div>':'<div class=err>'+msg+'</div>'}
async function requestTgCode(){try{let r=await fetch('/api/auth/request-code',{method:'POST'});let d=await r.json();if(d.ok){status('Код отправлен в Telegram!',1);document.getElementById('tg-code-block').className='';document.getElementById('tg-code').focus()}else{status(d.error||'Ошибка',0)}}catch(e){status('Ошибка сети',0)}}
let _checking=0;
async function verifyTgCode(){let code=document.getElementById('tg-code').value.trim();if(code.length!==6){status('Введите 6 цифр',0);return}try{let r=await fetch('/api/auth/verify-code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});let d=await r.json();if(d.ok){document.cookie='axiom_auth='+d.session_id+';path=/;max-age='+(30*24*3600);window.location.href='/'}else{status(d.error||'Неверный код',0)}}catch(e){status('Ошибка сети',0)}}
document.getElementById('tg-code').addEventListener('keydown',e=>{if(e.key==='Enter')verifyTgCode()});
</script>
</html>"""


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    # Если нет пароля И мы на локальной машине — защита выключена
    if not _AUTH_PW:
        host = (request.headers.get("host") or "").split(":")[0]
        if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return await call_next(request)
        # На внешнем IP/домене — Telegram-защита активна всегда
    path = request.url.path
    if path in _AUTH_OPEN:
        return await call_next(request)
    cookie = request.cookies.get(_AUTH_COOKIE, "")
    # Проверка: пароль ИЛИ Telegram-сессия
    pw_ok = _AUTH_PW and _hmac.compare_digest(cookie, _auth_token())
    tg_ok = bool(cookie) and _bot_auth.check_session(cookie)
    if pw_ok or tg_ok:
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "нужен вход в пульт"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def login_page() -> HTMLResponse:
    return HTMLResponse(_LOGIN_HTML.replace("<!--ERR-->", ""))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    pw = (form.get("password") or "").strip()
    if _AUTH_PW and _hmac.compare_digest(pw, _AUTH_PW):
        index_html = (BASE_DIR / "index.html").read_text(encoding="utf-8")
        resp = HTMLResponse(index_html, status_code=200)
        resp.set_cookie(_AUTH_COOKIE, _auth_token(), max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax")
        return resp
    return HTMLResponse(
        _LOGIN_HTML.replace("<!--ERR-->", '<div class=err>Неверный пароль</div>'),
        status_code=401,
    )


# --------------------------------------------------------------------------- #
#  Telegram bot auth (через @Jarvisvvp_bot)                                   #
# --------------------------------------------------------------------------- #
from channels import bot_auth as _bot_auth


@app.post("/api/auth/request-code")
async def tg_auth_request_code(request: Request) -> JSONResponse:
    """Запросить код авторизации через Telegram-бота."""
    result = _bot_auth.request_code()
    return JSONResponse(result)


@app.post("/api/auth/verify-code")
async def tg_auth_verify_code(payload: dict = Body(...)) -> JSONResponse:
    """Проверить код и создать сессию."""
    code = (payload.get("code") or "").strip()
    if len(code) != 6 or not code.isdigit():
        return JSONResponse({"ok": False, "error": "Код — 6 цифр"})
    result = _bot_auth.verify_code(code)
    return JSONResponse(result)


@app.get("/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(_AUTH_COOKIE)
    return resp


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
    # no-cache: пульт — один файл, который часто правится. Без этого браузер держит
    # СТАРЫЙ index.html из кэша и крутит старый скрипт (жалоба: «страница сама
    # перезагружается каждые ~20с» — это был авто-рефреш из давно удалённой версии,
    # живший в кэше). Заставляем браузер каждый раз брать свежую версию.
    return FileResponse(INDEX_HTML, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


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


@app.get("/api/sms/countries")
def sms_countries() -> JSONResponse:
    """hero-sms: баланс + страны с ценой/наличием для tg. READ-ONLY, денег не тратит —
    нужно, чтобы в UI выбрать страну перед регистрацией. Ключ наружу не отдаём."""
    from channels.sms_hero import SmsHeroError, balance, countries
    try:
        bal = balance()
        cs = countries("tg")
        # средняя цена в наличии — прикинуть, на сколько номеров хватит баланса
        avail = [c for c in cs if c["count"] > 0]
        return JSONResponse({"ok": True, "balance": bal, "countries": cs,
                             "available": len(avail)})
    except SmsHeroError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/sms/register")
def sms_register(payload: dict = Body(default={})) -> JSONResponse:
    """Купить номера через hero-sms + опционально прокси через Proxy6 + создать аккаунты.
    ТРАТИТ ДЕНЬГИ: get_number() + proxy6.buy() за каждый номер."""
    from channels.phone_register import buy_and_save
    from channels.sms_hero import SmsHeroError

    country = payload.get("country")
    qty = int(payload.get("qty") or 1)
    label = (payload.get("label") or "").strip()
    proxy_period = int(payload.get("proxy_period") or 0)
    proxy_version = int(payload.get("proxy_version") or 4)

    if not country:
        return JSONResponse({"ok": False, "error": "выбери страну"}, status_code=400)
    if qty < 1 or qty > 10:
        return JSONResponse({"ok": False, "error": "от 1 до 10 номеров за раз"}, status_code=400)
    if proxy_period and proxy_period < 7:
        return JSONResponse({"ok": False, "error": "прокси минимум на 7 дней"}, status_code=400)

    try:
        created = buy_and_save(int(country), qty, label, proxy_period, proxy_version)
    except SmsHeroError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    with_paid = sum(1 for a in created if (a.get("proxy") or "").startswith("socks5://"))
    with_mt = sum(1 for a in created if (a.get("proxy") or "").startswith("tg://"))
    without = len(created) - with_paid - with_mt
    parts = []
    if with_paid:
        parts.append(f"{with_paid} с Proxy6 ({proxy_period} дн)")
    if with_mt:
        parts.append(f"{with_mt} с бесплатным MTProto")
    if without:
        parts.append(f"{without} без прокси")
    proxy_note = " · " + ", ".join(parts) if parts else ""

    return JSONResponse({
        "ok": True,
        "msg": f"Куплено {len(created)} номеров{proxy_note}. Подключи через 🔌 Подключить.",
        "accounts": created,
    })


# --- Авто-регистрация (полный цикл) ---
_AUTO_TASKS: dict = {}   # task_id -> {"done": bool, "result": dict}


@app.post("/api/auto/register")
def auto_register(payload: dict = Body(default={})) -> JSONResponse:
    """Полная авто-регистрация: купить номер → SMS → Telegram → прокси → упаковка.
    Запускается в фоне, возвращает task_id для опроса статуса."""
    import uuid
    import threading

    country = payload.get("country")
    qty = int(payload.get("qty") or 1)
    proxy_period = int(payload.get("proxy_period") or 7)
    proxy_version = int(payload.get("proxy_version") or 4)

    if not country:
        return JSONResponse({"ok": False, "error": "выбери страну"}, status_code=400)
    if not config.TG_API_ID or not config.TG_API_HASH:
        return JSONResponse({"ok": False, "error": "Заполни TG_API_ID и TG_API_HASH в .env"}, status_code=400)

    task_id = str(uuid.uuid4())[:8]
    _AUTO_TASKS[task_id] = {"done": False, "result": {}, "progress": []}

    def _run():
        import asyncio
        from channels.auto_register import register_batch
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(register_batch(
                country, qty, proxy_period, proxy_version,
            ))
            _AUTO_TASKS[task_id] = {
                "done": True,
                "result": {"ok": any(r.get("ok") for r in results),
                           "accounts": results},
                "progress": [s for r in results for s in r.get("steps", [])],
            }
        except Exception as e:
            _AUTO_TASKS[task_id] = {"done": True, "result": {"error": str(e)}}
        finally:
            loop.close()

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "task_id": task_id})


@app.get("/api/auto/status/{task_id}")
def auto_status(task_id: str) -> JSONResponse:
    """Статус задачи авто-регистрации."""
    task = _AUTO_TASKS.get(task_id)
    if not task:
        return JSONResponse({"ok": False, "error": "задача не найдена"}, status_code=404)
    return JSONResponse({
        "done": task["done"],
        "result": task.get("result"),
        "progress": task.get("progress", []),
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


_BIO_STYLE_KEY = "bio_style_history"     # app_settings: JSON-список последних инструкций (новые — первыми)
_BIO_STYLE_KEEP = 10                     # храним с запасом, в диалоге показываем 3


def _bio_style_history() -> list[str]:
    with database.get_conn() as conn:
        raw = database.get_setting(conn, _BIO_STYLE_KEY, "[]")
    try:
        items = json.loads(raw or "[]")
    except (ValueError, TypeError):       # руками покорёженное значение — не роняем пульт из-за истории
        return []
    return [str(x) for x in items if isinstance(x, str) and x.strip()][:_BIO_STYLE_KEEP]


def _bio_style_remember(style: str) -> None:
    """Инструкцию — в начало истории, дубли убираем (повтор темы не должен вытеснять остальные)."""
    style = (style or "").strip()
    if not style:
        return
    items = [style] + [s for s in _bio_style_history() if s.strip().lower() != style.lower()]
    with database.get_conn() as conn:
        database.set_setting(conn, _BIO_STYLE_KEY, json.dumps(items[:_BIO_STYLE_KEEP], ensure_ascii=False))


@app.get("/api/accounts/bio_styles")
def bio_styles() -> JSONResponse:
    """Для диалога «✨ Оформить»: прошлые инструкции + примеры того, что ИИ по ним выдал."""
    database.init_db()
    with database.get_conn() as conn:
        samples = [
            {"label": r["label"], "bio": r["description"]}
            for r in conn.execute(
                "SELECT label, description FROM accounts WHERE description IS NOT NULL "
                "AND TRIM(description)<>'' ORDER BY id DESC LIMIT 5")
        ]
    return JSONResponse({"ok": True, "history": _bio_style_history(), "samples": samples})


@app.post("/api/accounts/bio_variants")
def bio_variants(payload: dict = Body(default={})) -> JSONResponse:
    """Генератор вариантов bio для превью в диалоге «Оформить»: оператор задаёт бриф,
    видит N вариантов, отмечает удачные — они уйдут пулом в упаковку (каждому свой)."""
    from channels.profile_gen import generate_bio_variants
    brief = (payload.get("brief") or "").strip()
    link = (payload.get("link") or "").strip()
    count = max(1, min(int(payload.get("count") or 6), 12))
    gender = (payload.get("gender") or "").strip() or None
    variants = generate_bio_variants(brief, count=count, link=link or None, gender=gender)
    return JSONResponse({"ok": True, "variants": variants})


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
        if status not in ("warming", "active", "paused", "banned", "archived"):
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
        # выбранные оператором в превью варианты bio (пул) — каждому аккаунту достаётся
        # СВОЙ из пула (без повторов пока хватает), чтобы не было одинаковых профилей.
        bios = [b.strip() for b in (payload.get("bios") or []) if isinstance(b, str) and b.strip()]
        with database.get_conn() as conn:
            rows = conn.execute(f"SELECT id FROM accounts WHERE id IN ({qm}) "
                                "AND tg_session IS NOT NULL AND tg_session<>''", ids).fetchall()
            queued = [r["id"] for r in rows]
            # пул кладём в настройку — identity его заберёт и очистит (одноразово)
            database.set_setting(conn, "bio_pool_pending",
                                 json.dumps(bios, ensure_ascii=False) if bios else "")
            # заменить фото на новое из пула лиц: чистим avatar → ensure_avatar подберёт
            # новое (пул лиц в приоритете) и снесёт старые фото в Telegram
            if payload.get("refresh_photo") and queued:
                conn.execute(f"UPDATE accounts SET avatar=NULL WHERE id IN "
                             f"({','.join('?' * len(queued))})", queued)
        skipped = len(ids) - len(queued)
        if queued and bio_style:
            _bio_style_remember(bio_style)
        if queued:
            _spawn("channels.identity", "--ids", ",".join(str(i) for i in queued), "--bio-style", bio_style)
        return JSONResponse({"ok": True, "queued": len(queued), "skipped_no_session": skipped,
                             "bio_pool": len(bios)})
    if action == "protect":
        # 2FA (сразу) + смена номера на свой (если аккаунту 24+ч и 2FA уже стоит) —
        # см. channels/account_protect.py. Деньги за смену номера тратятся только у
        # готовых кандидатов, модуль сам решает, кому ещё рано (fresh-лок Telegram).
        country = payload.get("country")
        country = int(country) if country not in (None, "") else None
        with database.get_conn() as conn:
            rows = conn.execute(
                f"SELECT id FROM accounts WHERE id IN ({qm}) AND session_alive=1 "
                "AND tg_session IS NOT NULL AND tg_session<>'' AND COALESCE(protected,0)=0", ids).fetchall()
        queued = [r["id"] for r in rows]
        skipped = len(ids) - len(queued)
        if queued:
            args = ["channels.account_protect", "--ids", ",".join(str(i) for i in queued)]
            if country is not None:
                args += ["--country", str(country)]
            _spawn(*args)
        return JSONResponse({"ok": True, "queued": len(queued), "skipped_not_eligible": skipped,
                             "phone_change": country is not None})
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
    if action == "proxy_check":
        with database.get_conn() as conn:
            rows = conn.execute(f"SELECT id FROM accounts WHERE id IN ({qm}) "
                                "AND proxy IS NOT NULL AND proxy<>''", ids).fetchall()
        queued = [r["id"] for r in rows]
        skipped = len(ids) - len(queued)
        if queued:
            _spawn("channels.proxy_check", "--ids", ",".join(str(i) for i in queued))
        return JSONResponse({"ok": True, "queued": len(queued), "skipped_no_proxy": skipped})
    if action == "session_check":
        with database.get_conn() as conn:
            rows = conn.execute(f"SELECT id FROM accounts WHERE id IN ({qm}) "
                                "AND tg_session IS NOT NULL AND tg_session<>''", ids).fetchall()
        queued = [r["id"] for r in rows]
        skipped = len(ids) - len(queued)
        if queued:
            _spawn("channels.session_check", "--ids", ",".join(str(i) for i in queued))
        return JSONResponse({"ok": True, "queued": len(queued), "skipped_no_session": skipped})
    if action == "proxy_pool_assign":
        with database.get_conn() as conn:
            rows = conn.execute(
                f"SELECT id FROM accounts WHERE id IN ({qm}) AND COALESCE(protected,0)=0", ids
            ).fetchall()
        queued = [r["id"] for r in rows]
        skipped = len(ids) - len(queued)
        if queued:
            _spawn("channels.proxy_pool", "--refresh", "--ids", ",".join(str(i) for i in queued))
        return JSONResponse({"ok": True, "queued": len(queued), "skipped_protected": skipped})
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


# ---- Оргструктура (отделы + сотрудники, живые/виртуальные) --------------- #
@app.get("/api/org/tree")
def org_tree() -> JSONResponse:
    """Дерево отделов с сотрудниками внутри — для схемы «как в Битрикс»."""
    database.init_db()
    with database.get_conn() as conn:
        depts = conn.execute("SELECT * FROM departments ORDER BY sort_order, id").fetchall()
        members = conn.execute(
            "SELECT m.*, ag.name AS agent_name, ag.task AS agent_task, "
            "acc.label AS account_label, acc.username AS account_username, "
            "acc.session_alive AS account_alive, acc.status AS account_status "
            "FROM org_members m LEFT JOIN ai_agents ag ON ag.id=m.ai_agent_id "
            "LEFT JOIN accounts acc ON acc.id=m.account_id "
            "ORDER BY m.sort_order, m.id"
        ).fetchall()
    by_dept: dict[int, list] = {}
    for r in members:
        d = dict(r)
        if d["kind"] == "agent" and d.get("agent_name"):
            d["name"] = d["name"] or d["agent_name"]
        by_dept.setdefault(d["department_id"], []).append(d)
    tree = []
    for r in depts:
        d = dict(r)
        d["members"] = by_dept.get(d["id"], [])
        tree.append(d)
    return JSONResponse({"departments": tree})


@app.post("/api/org/department")
def org_department_save(payload: dict = Body(...)) -> JSONResponse:
    did = payload.get("id")
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "нужно название отдела"}, status_code=400)
    description = (payload.get("description") or "").strip() or None
    parent_id = payload.get("parent_id") or None
    if did and parent_id and int(parent_id) == int(did):
        return JSONResponse({"error": "отдел не может быть родителем самого себя"}, status_code=400)
    with database.get_conn() as conn:
        if did:
            conn.execute(
                "UPDATE departments SET name=?, description=?, parent_id=? WHERE id=?",
                (name, description, parent_id, int(did)),
            )
            new_id = int(did)
        else:
            cur = conn.execute(
                "INSERT INTO departments (name, description, parent_id) VALUES (?,?,?)",
                (name, description, parent_id),
            )
            new_id = cur.lastrowid
    return JSONResponse({"ok": True, "id": new_id})


@app.post("/api/org/department/{did}/delete")
def org_department_delete(did: int) -> JSONResponse:
    with database.get_conn() as conn:
        n_members = conn.execute(
            "SELECT COUNT(*) c FROM org_members WHERE department_id=?", (did,)
        ).fetchone()["c"]
        n_children = conn.execute(
            "SELECT COUNT(*) c FROM departments WHERE parent_id=?", (did,)
        ).fetchone()["c"]
        if n_members or n_children:
            return JSONResponse(
                {"error": "сначала перенеси сотрудников/подотделы из этого отдела"}, status_code=400
            )
        conn.execute("DELETE FROM departments WHERE id=?", (did,))
    return JSONResponse({"ok": True})


@app.get("/api/org/members")
def org_members_list() -> JSONResponse:
    """Плоский список сотрудников (для вкладки «Сотрудники» — таблицей)."""
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT m.*, d.name AS department_name, ag.name AS agent_name, ag.task AS agent_task, "
            "acc.label AS account_label, acc.username AS account_username, acc.phone AS account_phone, "
            "acc.session_alive AS account_alive, acc.status AS account_status "
            "FROM org_members m LEFT JOIN departments d ON d.id=m.department_id "
            "LEFT JOIN ai_agents ag ON ag.id=m.ai_agent_id "
            "LEFT JOIN accounts acc ON acc.id=m.account_id ORDER BY d.sort_order, m.id"
        ).fetchall()
    items = [dict(r) for r in rows]
    for x in items:
        if x["kind"] == "agent" and x.get("agent_name"):
            x["name"] = x["name"] or x["agent_name"]
    return JSONResponse(items)


@app.post("/api/org/member")
def org_member_save(payload: dict = Body(...)) -> JSONResponse:
    mid = payload.get("id")
    department_id = payload.get("department_id")
    if not department_id:
        return JSONResponse({"error": "нужно выбрать отдел"}, status_code=400)
    kind = payload.get("kind") or "human"
    name = (payload.get("name") or "").strip() or None
    role = (payload.get("role") or "").strip() or None
    phone = (payload.get("phone") or "").strip() or None
    email = (payload.get("email") or "").strip() or None
    ai_agent_id = payload.get("ai_agent_id") or None
    # Слияние: должность сама несёт аккаунт-исполнителя и (для ИИ) задачу/промпт.
    account_id = payload.get("account_id") or None
    task = (payload.get("task") or "").strip() or None
    prompt = (payload.get("prompt") or "").strip() or None
    needs_access = 1 if payload.get("needs_access") else 0
    notes = (payload.get("notes") or "").strip() or None
    if not name and not role:
        return JSONResponse({"error": "нужно имя или название должности"}, status_code=400)
    with database.get_conn() as conn:
        if mid:
            conn.execute(
                "UPDATE org_members SET department_id=?, kind=?, name=?, role=?, phone=?, email=?, "
                "ai_agent_id=?, account_id=?, task=?, prompt=?, needs_access=?, notes=? WHERE id=?",
                (department_id, kind, name, role, phone, email, ai_agent_id, account_id, task,
                 prompt, needs_access, notes, int(mid)),
            )
            new_id = int(mid)
        else:
            cur = conn.execute(
                "INSERT INTO org_members (department_id, kind, name, role, phone, email, ai_agent_id, "
                "account_id, task, prompt, needs_access, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (department_id, kind, name, role, phone, email, ai_agent_id, account_id, task,
                 prompt, needs_access, notes),
            )
            new_id = cur.lastrowid
    return JSONResponse({"ok": True, "id": new_id})


@app.post("/api/org/member/{mid}/move")
def org_member_move(mid: int, payload: dict = Body(...)) -> JSONResponse:
    """Переместить сотрудника в другой отдел (drag-and-drop на схеме)."""
    department_id = payload.get("department_id")
    if not department_id:
        return JSONResponse({"error": "нужно выбрать отдел"}, status_code=400)
    with database.get_conn() as conn:
        conn.execute("UPDATE org_members SET department_id=? WHERE id=?", (int(department_id), mid))
    return JSONResponse({"ok": True})


@app.post("/api/org/member/{mid}/delete")
def org_member_delete(mid: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM org_members WHERE id=?", (mid,))
    return JSONResponse({"ok": True})


@app.get("/api/org/unlinked-accounts")
def org_unlinked_accounts() -> JSONResponse:
    """Аккаунты из resources, не привязанные к org_members — для пула ресурсов."""
    database.init_db()
    with database.get_conn() as conn:
        linked = {r["account_id"] for r in conn.execute(
            "SELECT account_id FROM org_members WHERE account_id IS NOT NULL"
        ).fetchall()}
        rows = conn.execute(
            "SELECT id, label, username, phone, session_alive, status FROM accounts ORDER BY id"
        ).fetchall()
    items = [dict(r) for r in rows if r["id"] not in linked]
    return JSONResponse(items)


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
        # последние отчёты прогрева — карточка показывает «что делал и когда»
        d["warm_runs"] = [dict(r) for r in conn.execute(
            "SELECT text, ts FROM events WHERE account_id=? AND type='warm_run' "
            "ORDER BY id DESC LIMIT 6", (acc_id,))]
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
    from channels.avatar_gen import ensure_avatar
    from channels.telegram import build_client
    from channels.warmup import _setup_profile
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
    acc = dict(row)
    if not acc.get("tg_session"):
        return JSONResponse({"error": "у аккаунта нет сессии — сначала залогинь его (кнопка «Логин»)"}, status_code=400)
    acc["avatar"] = ensure_avatar(acc)   # сток/ИИ-фото под пол из имени, если своё не загружено
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


@app.get("/api/keywords/auto")
def keywords_auto_get() -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        return JSONResponse({
            "auto": database.get_setting(conn, "kw_auto", "off") == "on",
            "interval_min": int(database.get_setting(conn, "kw_interval_min", "60")),
            "last_run": database.get_setting(conn, "kw_last_run", None),
        })


@app.post("/api/keywords/auto")
def keywords_auto_set(payload: dict = Body(...)) -> JSONResponse:
    auto = "on" if payload.get("auto") else "off"
    interval_min = max(15, int(payload.get("interval_min") or 60))
    with database.get_conn() as conn:
        database.set_setting(conn, "kw_auto", auto)
        database.set_setting(conn, "kw_interval_min", str(interval_min))
    return JSONResponse({"ok": True, "auto": auto == "on", "interval_min": interval_min})


@app.post("/api/keywords/listen_now")
def keywords_listen_now() -> JSONResponse:
    """Прослушать чаты по ключам СЕЙЧАС (фоновый процесс)."""
    import subprocess
    import sys
    _spawn("channels.chat_keywords", "--listen")
    return JSONResponse({"ok": True})


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
                                     cwd=str(BASE_DIR.parent), timeout=600, env=env,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
                _log_run("proxy_scheduler", res)
                # после обновления — подлечить прокси прогреваемых аккаунтов
                try:
                    subprocess.run([sys.executable, "-m", "channels.proxy_pool", "--heal"],
                                   cwd=str(BASE_DIR.parent), timeout=600, env=env,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    pass
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
        # --- авто-прослушка чатов по ключам (niches) ---
        try:
            with database.get_conn() as conn:
                kauto = database.get_setting(conn, "kw_auto", "off")
                kint = int(database.get_setting(conn, "kw_interval_min", "60"))
                klast = database.get_setting(conn, "kw_last_run_ts", "0")
            if kauto == "on" and (time.time() - float(klast or 0)) >= kint * 60:
                with database.get_conn() as conn:
                    database.set_setting(conn, "kw_last_run_ts", str(time.time()))
                    database.set_setting(conn, "kw_last_run", __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"))
                res = subprocess.run([sys.executable, "-m", "channels.chat_keywords", "--listen"],
                                     cwd=str(BASE_DIR.parent), timeout=600, env=env,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
                _log_run("kw_scheduler", res)
        except Exception as e:  # noqa: BLE001
            print(f"[kw scheduler] {e}")
        time.sleep(60)


def _startup_account_report() -> None:
    """При старте сервера (в т.ч. после включения ПК): пересчитать готовность аккаунтов
    к прогреву БЫСТРО — по флагам из БД, без сетевых пингов — и, если есть проблемные
    (в статусе 'прогрев', но гейт warming_accounts их не пропустит), положить сводку в
    колокольчик. Так Василий сразу видит «раздай прокси / перелогинь», а не гадает,
    почему часть аккаунтов не греется. Дедуп: одинаковую сводку в течение 15 минут не
    дублируем — иначе рестарты сервера засыпали бы ленту."""
    import time
    try:
        with database.get_conn() as conn:
            rows = conn.execute(
                "SELECT tg_session, proxy, proxy_alive, protected, session_state, session_alive "
                "FROM accounts WHERE status='warming'").fetchall()
            no_sess = no_proxy = dead_proxy = dead_sess = ready = 0
            for r in rows:
                if not (r["tg_session"] or "").strip():
                    no_sess += 1; continue
                if r["protected"]:
                    continue                        # родной — прогрев его не трогает, не проблема
                if not (r["proxy"] or "").strip():
                    no_proxy += 1; continue
                if r["proxy_alive"] == 0:
                    dead_proxy += 1; continue
                if r["session_state"] == "revoked" or r["session_alive"] == 0:
                    dead_sess += 1; continue        # gate пустит (tg_session есть), но прогрев не пройдёт
                ready += 1
            problems = no_proxy + dead_proxy + no_sess + dead_sess
            sig = f"{ready}|{no_proxy}|{dead_proxy}|{no_sess}|{dead_sess}"
            prev_sig = database.get_setting(conn, "startup_report_sig", "")
            prev_ts = float(database.get_setting(conn, "startup_report_ts", "0") or 0)
            warm_auto = database.get_setting(conn, "warm_auto", "off")
            fresh = (time.time() - prev_ts) > 900   # 15 минут
            if problems and (sig != prev_sig or fresh):
                parts = []
                if no_proxy:   parts.append(f"без прокси: {no_proxy}")
                if dead_proxy: parts.append(f"прокси мёртв: {dead_proxy}")
                if dead_sess:  parts.append(f"сессия слетела: {dead_sess}")
                if no_sess:    parts.append(f"нет сессии: {no_sess}")
                if warm_auto != "on":
                    parts.append("⚠ автопрогрев ВЫКЛ")
                database.add_event(
                    conn, "warm_check",
                    f"На прогрев готовы {ready}, с проблемами {problems}",
                    "; ".join(parts) + ". Раздай прокси / перелогинь — эти аккаунты не греются.",
                    level="warn")
            database.set_setting(conn, "startup_report_sig", sig)
            database.set_setting(conn, "startup_report_ts", str(time.time()))
            print(f"[startup] прогрев готовы={ready} проблемные={problems} ({sig}) warm_auto={warm_auto}")
    except Exception as e:  # noqa: BLE001
        print(f"[startup report] {e}")


def _opener_queue_scheduler() -> None:
    """Фоновый тик очереди опенера: каждую минуту досылает следующие строки опенера
    тем, кто ещё не ответил (см. channels/opener_queue). Без этого тика вторая и
    последующие строки многострочного первого сообщения кладутся в очередь, но
    никогда не отправляются — уходит только первая строка."""
    import os
    import subprocess
    import sys
    import time
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    while True:
        time.sleep(60)
        try:
            # быстрая проверка: есть ли вообще что слать (чтобы не плодить процессы впустую)
            with database.get_conn() as conn:
                due = conn.execute(
                    "SELECT COUNT(*) c FROM opener_queue WHERE next_at <= datetime('now')"
                ).fetchone()["c"]
            if not due:
                continue
            res = subprocess.run([sys.executable, "-m", "channels.opener_queue", "--tick"],
                                 cwd=str(BASE_DIR.parent), timeout=600, env=env,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
            _log_run("opener_queue", res)
        except Exception as e:  # noqa: BLE001
            print(f"[opener_queue scheduler] {e}")


@app.on_event("startup")
def _start_scheduler() -> None:
    import threading
    database.init_db()
    _startup_account_report()   # быстрая сводка готовности → колокольчик (до запуска прогрева)
    threading.Thread(target=_proxy_scheduler, daemon=True).start()
    threading.Thread(target=_opener_queue_scheduler, daemon=True).start()
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
            niches = conn.execute("SELECT COUNT(*) c FROM niches WHERE active=1").fetchone()["c"]
        return JSONResponse({"started": listener.STATUS.get("started"),
                             "listening": sum(1 for a in accs if a["ok"]),
                             "accounts": accs, "auto_reply": auto_reply,
                             "hits": listener.STATUS.get("hits", 0), "niches": niches})
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


@app.post("/api/accounts/session_check_all")
def accounts_session_check_all() -> JSONResponse:
    """Живость TG-сессий всех подключённых аккаунтов (фоном) → колонка «Живость»."""
    database.init_db()
    with database.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM accounts "
                         "WHERE tg_session IS NOT NULL AND tg_session<>''").fetchone()["c"]
    if not n:
        return JSONResponse({"error": "нет ни одного подключённого аккаунта"}, status_code=400)
    _spawn("channels.session_check")
    return JSONResponse({"ok": True, "queued": n})


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
                   c.specialization, c.hook, c.enriched_at, c.source, c.created_at, c.email,
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


def _avatar_path(tg_user_id) -> Path:
    """Путь к фото человека (одно на tg_user_id). Тот же файл пишет парсер и читает vision."""
    return AVATAR_DIR / f"{tg_user_id}.jpg"


@app.get("/api/contact/{contact_id}/photo")
def contact_photo(contact_id: int):
    """Отдаёт аватар человека (скачан парсером). Нет — 404 (карточка покажет плейсхолдер)."""
    with database.get_conn() as conn:
        row = conn.execute("SELECT tg_user_id FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not row or not row["tg_user_id"]:
        return JSONResponse({"error": "нет фото"}, status_code=404)
    path = _avatar_path(row["tg_user_id"])
    if not path.exists() or path.stat().st_size == 0:
        return JSONResponse({"error": "нет фото"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg")


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
        # источник — чат, где найден (карточка человека: «в каком чате найден»).
        # NB: chat_hits.chat_id — КАТАЛОЖНЫЙ (chats.id), а tg_user_posts.chat_id — сырой
        # telegram-id (chats.tg_chat_id). JOIN'ы разные, не перепутать.
        src = conn.execute(
            "SELECT c.id AS chat_id, h.chat_title, c.username AS chat_username, c.link AS chat_link "
            "FROM chat_hits h LEFT JOIN chats c ON c.id=h.chat_id "
            "WHERE h.contact_id=? ORDER BY h.id DESC LIMIT 1", (contact_id,)
        ).fetchone()
        if not src or src["chat_id"] is None:
            src = conn.execute(
                "SELECT c.id AS chat_id, p.chat_title, c.username AS chat_username, c.link AS chat_link "
                "FROM tg_user_posts p LEFT JOIN chats c ON c.tg_chat_id=p.chat_id "
                "WHERE p.contact_id=? ORDER BY p.id DESC LIMIT 1", (contact_id,)
            ).fetchone()
    d = dict(row); d["tags"] = _split_tags(d.get("tags"))
    d["company_name"] = comp["name"] if comp else None
    d["history"] = history; d["deal"] = dict(deal) if deal else None
    # has_photo — авторитетно по файлу (флаг в БД мог отстать/файл могли удалить)
    d["has_photo"] = bool(d.get("tg_user_id")) and _avatar_path(d.get("tg_user_id")).exists()
    if src:
        d["source_chat_id"] = src["chat_id"]
        d["source_chat_title"] = src["chat_title"]
        d["source_chat_link"] = (f"https://t.me/{src['chat_username']}" if src["chat_username"]
                                  else src["chat_link"])
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
    """Запускает модуль в фоне (fire-and-forget) — вывод пишем в data/logs/<модуль>.log,
    чтобы при тихом зависании/падении (напр. без интернета) было видно ПОЧЕМУ, а не
    гадать «ничего не происходит»."""
    import subprocess
    import sys
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    name = (args[0] if args else "spawn").replace(".", "_")
    log_path = LOG_DIR / f"{name}.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n===== {__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S} запуск: {' '.join(args)} =====\n")
        f.flush()
        subprocess.Popen([sys.executable, "-m", *args], cwd=str(BASE_DIR.parent),
                         stdout=f, stderr=subprocess.STDOUT)


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


@app.post("/api/enrich/resolve-tg")
def enrich_resolve_tg(payload: dict = Body(...)) -> JSONResponse:
    """Пробив номеров контактов в Telegram (phone_resolve) — узнать tg_user_id, username, аватар, bio."""
    limit = int(payload.get("limit") or 100)
    _spawn("channels.phone_resolve", "--limit", str(limit))
    return JSONResponse({"ok": True, "limit": limit, "message": "запущен пробив TG в фоне. Лог в data/logs/phone_resolve.log"})


@app.post("/api/dossier/lookup")
async def dossier_lookup_api(payload: dict = Body(...)) -> JSONResponse:
    """Досье по телефону/@username в один клик: заходит в TG-профиль живым аккаунтом,
    собирает bio + аватар + личный канал, строит AI-портрет (боли/страхи/желания/крючок)
    и возвращает его. Синхронно (10-40с) — оператор ждёт результат."""
    query = (payload.get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "введите телефон (+7…) или @username"}, status_code=400)
    from agent.dossier_lookup import lookup
    res = await lookup(query)
    if res.get("error"):
        return JSONResponse(res, status_code=400)
    # подтягиваем готовое досье из карточки контакта для показа
    cid = res.get("contact_id")
    if cid:
        with database.get_conn() as conn:
            row = conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
        if row:
            keys = row.keys()
            res["dossier"] = {k: row[k] for k in (
                "name", "username", "phone", "city", "bio", "pains", "fears", "desires",
                "interests", "psychotype", "comm_style", "best_time", "segment", "score",
                "quotes", "rec_message", "photo_analysis", "gender", "summary", "confidence",
            ) if k in keys}
    return JSONResponse(res)


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


def _last_json(output: str | None) -> dict | None:
    """Последняя JSON-строка вывода модуля (модули печатают сводку json.dumps в конце)."""
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                pass
    return None


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


@app.get("/api/chatcat/scan_progress")
def chatcat_scan_progress() -> JSONResponse:
    """Прогресс массового скана для пульта.

    ВАЖНО: объявлен ДО /api/chatcat/{chat_id} — FastAPI матчит роуты по порядку, и
    динамический {chat_id} проглотил бы «scan_progress», пытаясь привести его к int.
    """
    with database.get_conn() as conn:
        raw = database.get_setting(conn, "chatscan_progress", None)
    if not raw:
        return JSONResponse({"running": False})
    try:
        return JSONResponse(json.loads(raw))
    except ValueError:
        return JSONResponse({"running": False})


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
        # мои агенты в этом чате: tg-ссылка на агента + ссылка на его карточку в AXIOM
        agents = conn.execute(
            "SELECT a.id, a.label, a.username, a.status FROM account_chats ac "
            "JOIN accounts a ON a.id=ac.account_id WHERE ac.chat_id=? ORDER BY a.id",
            (chat_id,),
        ).fetchall()
    d = dict(row); d["admins"] = [dict(a) for a in admins]
    d["agents"] = [dict(a) for a in agents]
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
    for k in ("title", "topic", "city", "notes", "status", "link", "can_write", "favorite", "verdict"):
        if k in payload:
            v = (1 if payload.get(k) else 0) if k == "favorite" else (payload.get(k) or None)
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return JSONResponse({"ok": True})
    if "verdict" in payload:   # вердикт из карточки ставит человек — метим источник
        sets += ["verdict_src='человек'", "verdict_at=datetime('now')"]
    vals.append(chat_id)
    with database.get_conn() as conn:
        conn.execute(f"UPDATE chats SET {', '.join(sets)} WHERE id=?", vals)
    return JSONResponse({"ok": True})


@app.post("/api/chatcat/scan_all")
def chatcat_scan_all(payload: dict = Body(...)) -> JSONResponse:
    """Массовый скан каталога рабочими аккаунтами (фоном). Заполняет участников,
    активность, «могу писать», админов; несуществующие чаты помечает вердиктом «мёртвый»."""
    database.init_db()
    favorites = bool(payload.get("favorites"))
    rescan = bool(payload.get("rescan"))
    limit = payload.get("limit")
    args = ["channels.chat_scan_all"]
    if favorites:
        args.append("--favorites")
    if rescan:
        args.append("--rescan")
    if limit:
        args += ["--limit", str(int(limit))]
    with database.get_conn() as conn:
        where = "last_scanned_at IS NULL AND " if not rescan else ""
        fav = "AND COALESCE(favorite,0)=1" if favorites else ""
        n = conn.execute(
            f"SELECT COUNT(*) c FROM chats WHERE {where}"
            f"(username IS NOT NULL AND username<>'' OR link IS NOT NULL AND link<>'') "
            f"AND (verdict IS NULL OR verdict<>'мёртвый') {fav}"
        ).fetchone()["c"]
        workers = conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND COALESCE(protected,0)=0 AND session_alive=1 AND COALESCE(status,'')<>'banned'"
        ).fetchone()["c"]
        database.set_setting(conn, "chatscan_stop", "0")
    if not n:
        return JSONResponse({"error": "нечего сканировать — всё уже просканировано"}, status_code=400)
    _spawn(*args)
    return JSONResponse({"ok": True, "queued": n, "workers": workers})


@app.post("/api/chatcat/scan_stop")
def chatcat_scan_stop() -> JSONResponse:
    """Мягкая остановка: воркеры дочитывают текущий чат и выходят."""
    with database.get_conn() as conn:
        database.set_setting(conn, "chatscan_stop", "1")
    return JSONResponse({"ok": True, "stopped": True})


@app.post("/api/chatcat/verdict")
def chatcat_verdict(payload: dict = Body(...)) -> JSONResponse:
    """Массовый аппрув: пометить выбранные чаты годными/негодными. Решение человека
    приоритетнее ИИ — помечаем verdict_src='человек', чтобы ИИ его потом не перетёр."""
    ids = []
    for x in (payload.get("ids") or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    verdict = (payload.get("verdict") or "").strip()
    if not ids:
        return JSONResponse({"error": "не выбран ни один чат"}, status_code=400)
    if verdict not in ("годен", "не годен", "на проверку", "мёртвый", ""):
        return JSONResponse({"error": "плохой вердикт"}, status_code=400)
    qm = ",".join("?" * len(ids))
    with database.get_conn() as conn:
        if verdict == "":     # снять вердикт
            conn.execute(f"UPDATE chats SET verdict=NULL, verdict_src=NULL, verdict_at=NULL "
                         f"WHERE id IN ({qm})", ids)
        else:
            conn.execute(f"UPDATE chats SET verdict=?, verdict_src='человек', "
                         f"verdict_at=datetime('now') WHERE id IN ({qm})", (verdict, *ids))
    return JSONResponse({"ok": True, "updated": len(ids), "verdict": verdict})


@app.post("/api/chatcat/{chat_id}/enrich")
def chatcat_enrich(chat_id: int) -> JSONResponse:
    """Переобогатить чат ИИ (тема/город/описание/предварительный вердикт) без полного
    пересканирования. Раньше этот роут был обещан в докстроке agent/enrich_chat.py,
    но его не существовало — обогатить можно было только полным ре-сканом."""
    with database.get_conn() as conn:
        row = conn.execute("SELECT username, link FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "чат не найден"}, status_code=404)
    target = row["username"] or row["link"]
    if not target:
        return JSONResponse({"error": "у чата нет ни @username, ни ссылки"}, status_code=400)
    res = _run_capture(["channels.chat_scan", "--target", target, "--id", str(chat_id)], timeout=180)
    return JSONResponse(res)


@app.post("/api/chatcat/inventory")
def chatcat_inventory() -> JSONResponse:
    """Инвентаризация: занести чаты личного аккаунта в каталог (только чтение)."""
    res = _run_capture(["channels.chat_inventory"], timeout=240)
    return JSONResponse({"ok": res.get("ok"), "output": res.get("output")})


@app.post("/api/chats/join")
def chats_join(payload: dict = Body(default={})) -> JSONResponse:
    """Разослать армию по чатам каталога (авто-вступление). per — сколько новых чатов
    на аккаунт за заход; favorites — только ⭐ избранные. Возвращает отчёт куда получилось."""
    per = max(1, min(int(payload.get("per") or 3), 15))
    args = ["channels.chat_join", "--per", str(per)]
    if payload.get("favorites"):
        args.append("--favorites")
    res = _run_capture(args, timeout=1500)
    info = {}
    try:
        info = json.loads((res.get("output") or "").strip().split("\n")[-1])
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({"ok": res.get("ok") and info.get("ok", True),
                         "joined": info.get("joined"), "failed": info.get("failed"),
                         "accounts": info.get("accounts"), "report": info.get("report"),
                         "error": info.get("error"), "output": res.get("output")})


@app.get("/api/coverage")
def coverage() -> JSONResponse:
    """Отчёт покрытия: сколько агентов в скольких чатах, разбивка по аккаунтам и чатам."""
    database.init_db()
    with database.get_conn() as conn:
        per_acc = [dict(r) for r in conn.execute(
            "SELECT ac.account_id AS id, a.label, a.status, COUNT(*) AS chats "
            "FROM account_chats ac JOIN accounts a ON a.id=ac.account_id "
            "GROUP BY ac.account_id ORDER BY chats DESC")]
        chats_covered = conn.execute("SELECT COUNT(DISTINCT chat_id) c FROM account_chats").fetchone()["c"]
        memberships = conn.execute("SELECT COUNT(*) c FROM account_chats").fetchone()["c"]
        joinable = conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND status IN ('active','warming') AND COALESCE(protected,0)=0").fetchone()["c"]
        catalog = conn.execute(
            "SELECT COUNT(*) c FROM chats WHERE (username IS NOT NULL AND username<>'') "
            "OR link LIKE '%t.me/+%' OR link LIKE '%joinchat%'").fetchone()["c"]
        chats = [dict(r) for r in conn.execute(
            "SELECT c.id, c.title, c.username, c.members_count, COUNT(ac.account_id) AS agents "
            "FROM account_chats ac JOIN chats c ON c.id=ac.chat_id "
            "GROUP BY ac.chat_id ORDER BY agents DESC, c.members_count DESC LIMIT 200")]
    return JSONResponse({"joinable_accounts": joinable, "chats_covered": chats_covered,
                         "memberships": memberships, "catalog_joinable": catalog,
                         "per_account": per_acc, "chats": chats})


@app.post("/api/chatcat/{chat_id}/delete")
def chatcat_delete(chat_id: int) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM chat_admins WHERE chat_id=?", (chat_id,))
        # account_chats тоже чистим: раньше связки «аккаунт↔чат» оставались сиротами
        # после удаления чата и завышали «покрытие» (/api/coverage) несуществующими чатами.
        conn.execute("DELETE FROM account_chats WHERE chat_id=?", (chat_id,))
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


def _join_arg(payload: dict) -> list[str]:
    """Общий флаг «сразу вступить в найденное» для discover/bio_scan/similar.
    Клампим как в /api/chats/join — вступления это самый банируемый шаг."""
    n = int(payload.get("join") or 0)
    return ["--join", str(min(n, 15))] if n > 0 else []


@app.post("/api/chatcat/discover")
def chatcat_discover(payload: dict = Body(default={})) -> JSONResponse:
    """Авто-поиск чатов по нише/запросу (channels.chat_discover) → в каталог со статусом 'new'.
    Синхронно (несколько поисковых запросов с паузами ~1-2 мин), возвращает сводку."""
    niche_id = payload.get("niche_id")
    query = (payload.get("query") or "").strip()
    if not niche_id and not query:
        return JSONResponse({"error": "нужна ниша или поисковый запрос"}, status_code=400)
    args = ["channels.chat_discover"]
    if query:
        args += ["--query", query]
    else:
        args += ["--niche", str(int(niche_id))]
    if payload.get("min_members"):
        args += ["--min-members", str(int(payload["min_members"]))]
    if payload.get("groups_only"):
        args += ["--groups-only"]
    join = _join_arg(payload)
    args += join
    res = _run_capture(args, timeout=1500 if join else 300)
    return JSONResponse({"ok": res.get("ok"), "summary": _last_json(res.get("output")),
                         "output": res.get("output")})


@app.post("/api/chatcat/bio_scan")
def chatcat_bio_scan(payload: dict = Body(default={})) -> JSONResponse:
    """Bio-скан ссылок (channels.bio_links): достаёт из bio лидов ссылки на другие/закрытые
    чаты и заносит их в каталог. Синхронно (резолвы с паузами), возвращает сводку."""
    limit = int(payload.get("limit") or 500)
    join = _join_arg(payload)
    res = _run_capture(["channels.bio_links", "--limit", str(limit), *join],
                       timeout=1500 if join else 300)
    return JSONResponse({"ok": res.get("ok"), "summary": _last_json(res.get("output")),
                         "output": res.get("output")})


@app.post("/api/chatcat/similar")
def chatcat_similar(payload: dict = Body(default={})) -> JSONResponse:
    """Размножение каталога по похожим чатам (channels.chat_similar): рекомендации TG от
    уже найденных чатов. Синхронно; глубже 1 круга — заметно дольше, отсюда большой таймаут."""
    args = ["channels.chat_similar"]
    if payload.get("chat_id"):
        args += ["--chat", str(int(payload["chat_id"]))]
    elif payload.get("favorites"):
        args += ["--favorites"]
    elif payload.get("niche_id"):
        args += ["--niche", str(int(payload["niche_id"]))]
    depth = max(1, min(int(payload.get("depth") or 1), 4))
    args += ["--depth", str(depth)]
    if payload.get("min_members"):
        args += ["--min-members", str(int(payload["min_members"]))]
    if payload.get("groups_only"):
        args += ["--groups-only"]
    if payload.get("max_new"):
        args += ["--max-new", str(int(payload["max_new"]))]
    args += _join_arg(payload)
    res = _run_capture(args, timeout=1800 if payload.get("join") else 900)
    return JSONResponse({"ok": res.get("ok"), "summary": _last_json(res.get("output")),
                         "output": res.get("output")})


@app.post("/api/leads/segment")
def leads_segment(payload: dict = Body(default={})) -> JSONResponse:
    """Сегментация базы по сферам (agent.segment): правила по тегам бесплатно,
    остаток — дешёвой моделью. Трогает только контакты без сегмента."""
    args = ["agent.segment", "--limit", str(max(1, min(int(payload.get("limit") or 300), 2000)))]
    if payload.get("rules_only"):
        args += ["--rules-only"]
    if payload.get("renorm"):
        args += ["--renorm"]
    res = _run_capture(args, timeout=1800)
    return JSONResponse({"ok": res.get("ok"), "summary": _last_json(res.get("output")),
                         "output": res.get("output")})


@app.post("/api/maintenance/backfill")
def maintenance_backfill(payload: dict = Body(default={})) -> JSONResponse:
    """Бэкфилл старых записей (channels.backfill): tg_chat_id у чатов (чинит связку
    «в каком чате найден» в досье) и аватары лидов. Идемпотентно — трогает только пустое."""
    args = ["channels.backfill"]
    if payload.get("chats"):
        args += ["--chats"]
    if payload.get("photos"):
        args += ["--photos"]
    if len(args) == 1:
        return JSONResponse({"error": "нечего дозаполнять: укажи chats и/или photos"}, status_code=400)
    args += ["--limit", str(max(1, min(int(payload.get("limit") or 200), 1000)))]
    res = _run_capture(args, timeout=1800)
    return JSONResponse({"ok": res.get("ok"), "summary": _last_json(res.get("output")),
                         "output": res.get("output")})


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
    """Обогатить ключевые слова ниши моделью из config.MODEL (генерирует новые ключи)."""
    from agent import llm
    if not llm.available(config.MODEL):
        return JSONResponse({"error": f"нет ключа под модель «{config.MODEL}» в .env"},
                            status_code=400)

    with database.get_conn() as conn:
        niche = conn.execute("SELECT * FROM niches WHERE id=?", (nid,)).fetchone()
        if not niche:
            return JSONResponse({"error": "ниша не найдена"}, status_code=404)

    current_keys = (niche["keywords"] or "").split(",")
    current_keys = [k.strip() for k in current_keys if k.strip()]

    prompt = f"""Ты эксперт по B2B лидогенерации. Текущие ключевые слова для ниши "{niche['name']}":
{', '.join(current_keys) if current_keys else '(пусто)'}

Сгенерируй 10-15 НОВЫХ релевантных ключевых слов/фраз для поиска лидов в этой нише.
Ключи — реальные поисковые запросы, которые ищут люди в чатах.

Ответ: просто список через запятую, без нумерации."""

    try:
        new_keys_raw = llm.text(config.MODEL, system=None,
                                messages=[{"role": "user", "content": prompt}], max_tokens=400)
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
def hit_to_lead(hid: int, payload: dict = Body(default={})) -> JSONResponse:
    """Занести находку в CRM как контакт (лид) + сразу AI-скоринг (как «Целевые лиды» у OPUS):
    сеем реальную цитату из чата в tg_user_posts (хоть какое-то сырьё уже есть — само
    сообщение-триггер) и запускаем психо-портрет (score/сфера/визитка/рекомендация подхода).
    auto_enrich=false в payload — пропустить AI (напр. при массовом переносе, чтобы не тратить
    токены на каждый разом — тогда доскорить можно позже отдельной кнопкой)."""
    with database.get_conn() as conn:
        h = conn.execute("SELECT * FROM chat_hits WHERE id=?", (hid,)).fetchone()
        if not h:
            return JSONResponse({"error": "не найдено"}, status_code=404)
        niche = conn.execute("SELECT name FROM niches WHERE id=?", (h["niche_id"],)).fetchone()
        tag = f"Ниша: {niche['name']}" if niche else (f"Ключ: {h['keyword']}")
        note = f"[{h['chat_title']}] «{h['keyword']}»: {h['text']}"
        from channels.ru_names import gender_of
        cid = database.upsert_contact(
            conn, source="tg_keyword", username=h["username"], tg_user_id=h["tg_user_id"],
            name=h["name"], tags=tag, notes=note, gender=gender_of(h["name"]),
        )
        conn.execute("UPDATE contacts SET has_tg='yes' WHERE id=?", (cid,))
        conn.execute("UPDATE chat_hits SET status='lead', contact_id=? WHERE id=?", (cid, hid))
        if h["tg_user_id"] and h["text"]:
            conn.execute(
                "INSERT OR IGNORE INTO tg_user_posts (tg_user_id, contact_id, chat_id, chat_title, "
                "text, msg_id, ts) VALUES (?,?,?,?,?,?,?)",
                (h["tg_user_id"], cid, h["chat_id"], h["chat_title"], h["text"],
                 h["source_msg_id"], h["ts"]),
            )
    from agent import llm
    score = None
    if payload.get("auto_enrich", True) and llm.available(config.MODEL):
        try:
            from agent.enrich_person import _posts_for, _save, enrich_person
            with database.get_conn() as conn:
                contact = dict(conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone())
                posts = _posts_for(conn, contact["tg_user_id"]) if contact.get("tg_user_id") else []
            if posts:
                profile = enrich_person(contact, posts)
                _save(cid, profile)
                score = profile.score
        except Exception as e:  # noqa: BLE001 — скоринг best-effort, лид всё равно заведён
            print(f"[hit_to_lead enrich] contact {cid}: {e}")
    return JSONResponse({"ok": True, "contact_id": cid, "score": score})


@app.get("/api/target_leads")
def target_leads() -> JSONResponse:
    """«Целевые лиды» (как у OPUS): контакты из чат-мониторинга с AI-скорингом.
    Счётчики + карточки со score/сферой/визиткой-цитатой/рекомендацией подхода."""
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.username, c.tags, c.status, c.score, c.segment,
                   c.quotes, c.rec_message, c.pains, c.desires, c.psychotype, c.confidence,
                   c.has_photo,
                   (SELECT h.chat_title FROM chat_hits h WHERE h.contact_id=c.id
                      ORDER BY h.id DESC LIMIT 1) AS source_chat,
                   (SELECT COUNT(*) FROM messages m WHERE m.contact_id=c.id AND m.direction='out') AS sent_cnt,
                   (SELECT COUNT(*) FROM messages m WHERE m.contact_id=c.id AND m.direction='in') AS reply_cnt
            FROM contacts c
            WHERE c.id IN (SELECT DISTINCT contact_id FROM chat_hits WHERE contact_id IS NOT NULL)
            ORDER BY COALESCE(c.score,-1) DESC, c.id DESC
            """
        ).fetchall()
        segments = conn.execute(
            "SELECT segment, COUNT(*) c FROM contacts WHERE id IN "
            "(SELECT DISTINCT contact_id FROM chat_hits WHERE contact_id IS NOT NULL) "
            "AND segment IS NOT NULL AND segment<>'' GROUP BY segment ORDER BY c DESC"
        ).fetchall()
    items = [dict(r) for r in rows]
    for d in items:
        d["tags"] = _split_tags(d.get("tags"))
    counts = {
        "processed": sum(1 for d in items if d.get("score") is not None),
        "qualified": sum(1 for d in items if (d.get("score") or 0) >= 0.5),
        "sent": sum(1 for d in items if (d.get("sent_cnt") or 0) > 0),
        "replied": sum(1 for d in items if (d.get("reply_cnt") or 0) > 0),
    }
    return JSONResponse({"items": items, "counts": counts,
                         "segments": [dict(r) for r in segments]})


@app.post("/api/contact/{cid}/enrich_now")
def contact_enrich_now(cid: int) -> JSONResponse:
    """Досчитать/пересчитать AI-скоринг для конкретного контакта прямо сейчас (синхронно)."""
    from agent import llm
    if not llm.available(config.MODEL):
        return JSONResponse({"error": f"нет ключа под модель «{config.MODEL}» в .env"},
                            status_code=400)
    from agent.enrich_person import _posts_for, _save, enrich_person
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "контакт не найден"}, status_code=404)
        contact = dict(row)
        posts = _posts_for(conn, contact["tg_user_id"]) if contact.get("tg_user_id") else []
    if not posts:
        return JSONResponse({"error": "нет сырья (сообщений) для скоринга"}, status_code=400)
    try:
        profile = enrich_person(contact, posts)
        _save(cid, profile)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "score": profile.score, "segment": profile.segment,
                         "rec_message": profile.rec_message})


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
    """Подсказка по шагу визарда от config.MODEL (по умолчанию Haiku, дёшево)."""
    from agent import llm
    if not llm.available(config.MODEL):
        return JSONResponse({"error": f"нет ключа под модель «{config.MODEL}» в .env"},
                            status_code=400)
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


# ---- Источники импорта (автокомплит) ------------------------------------- #
@app.get("/api/import/sources")
def import_sources() -> JSONResponse:
    """Список всех уникальных source из contacts + companies."""
    database.init_db()
    with database.get_conn() as conn:
        cs = [r["source"] for r in conn.execute(
            "SELECT DISTINCT source FROM contacts WHERE source IS NOT NULL AND source<>'' ORDER BY 1"
        ).fetchall()]
        cos = [r["source"] for r in conn.execute(
            "SELECT DISTINCT source FROM companies WHERE source IS NOT NULL AND source<>'' ORDER BY 1"
        ).fetchall()]
    all_src = sorted(set(cs + cos))
    return JSONResponse(all_src)


# ---- Универсальный CSV-импорт (компании + контакты) ----------------------- #
# Маппинг русских заголовков → поля БД (компании)
_COL_MAP = {
    "наименование": "name",
    "инн": "inn",
    "кпп": "kpp",
    "огрн": "ogrn",
    "фио руководителя": "director_name",
    "иннфл руководителя": "director_inn",
    "телефон директора": "director_phone",
    "email директора": "director_email",
    "должность руководителя": "director_role",
    "номер телефона": "phone",
    "адрес": "address",
    "ссылка на сайт": "site",
    "статус": "status",
    "дата регистрации": "registration_date",
    "количество сотрудников": "employee_count",
    "выручка": "revenue",
    "чистая прибыль": "profit",
    "прибыль": "profit",
    "баланс": "balance",
    "арбитраж": "arbitration",
    "полученные лицензии": "licenses",
    "основной вид деятельности": "main_activity",
    "другие виды деятельности": "other_activities",
    "предметы закупок": "procurement_codes",
    "регион регистрации": "region",
    "категория мсп": "sme_category",
    "лизингополучатель": "lessee",
    "город": "city",
    "сайт": "site",
    "email": "email",
    "e-mail": "email",
}

# Поля для contacts, если они есть в шапке
_CONTACT_COL_MAP = {
    "фио руководителя": "person_name",
    "должность руководителя": "person_role",
    "телефон директора": "phone",
    "номер телефона": "phone",
    "email директора": "email",
}


def _parse_universal(text: str, tag: str, source: str = "import") -> tuple[int, int]:
    """Парсит CSV с произвольными столбцами — создаёт компании и привязывает контакты."""
    import csv
    import io
    reader = csv.reader(io.StringIO(text), delimiter=";")
    rows = list(reader)
    if not rows:
        return 0, 0
    header = [h.strip().lower() for h in rows[0]]
    # Сопоставляем заголовки с полями
    col_idx = {}
    for i, h in enumerate(header):
        for pat, field in _COL_MAP.items():
            if pat in h:
                col_idx[field] = i
                break
    if "name" not in col_idx:
        raise ValueError("не нашёл колонку «Наименование» — проверь заголовки CSV")

    def cell(row, field):
        i = col_idx.get(field)
        return row[i].strip() if i is not None and i < len(row) else ""

    def norm(r, field):
        v = cell(r, field)
        if field in ("phone", "director_phone", "director_email", "email"):
            return v or None
        if field in ("employee_count",):
            try:
                return int("".join(c for c in v if c.isdigit())) if v else None
            except ValueError:
                return None
        if field in ("revenue", "profit", "balance", "arbitration"):
            try:
                return float(v.replace(" ", "").replace(",", ".")) if v else None
            except (ValueError, AttributeError):
                return None
        if field == "lessee":
            return 1 if "да" in v.lower() or v == "1" else 0
        return v or None

    added = skipped = 0
    database.init_db()
    with database.get_conn() as conn:
        for row in rows[1:]:
            cname = cell(row, "name")
            if not cname:
                skipped += 1
                continue
            # Параметры компании
            co_vals = {f: norm(row, f) for f in [
                "name", "inn", "kpp", "ogrn", "director_name", "director_inn",
                "director_phone", "director_email", "director_role",
                "phone", "address", "site", "city", "email",
                "status", "registration_date", "employee_count",
                "revenue", "profit", "balance", "arbitration",
                "licenses", "main_activity", "other_activities",
                "procurement_codes", "region", "sme_category", "lessee",
            ] if col_idx.get(f) is not None}
            co_vals["source"] = source

            # Upsert компании
            existing = conn.execute(
                "SELECT id FROM companies WHERE inn=? AND inn IS NOT NULL AND inn<>''",
                (co_vals.get("inn") or "",)
            ).fetchone()
            if not existing and co_vals.get("name"):
                existing = conn.execute(
                    "SELECT id FROM companies WHERE name=?", (co_vals["name"],)
                ).fetchone()

            if existing:
                co_id = existing["id"]
                sets = ", ".join(f"{k}=COALESCE(?,{k})" for k in co_vals)
                conn.execute(
                    f"UPDATE companies SET {sets} WHERE id=?",
                    [*co_vals.values(), co_id]
                )
            else:
                cur = conn.execute(
                    f"INSERT INTO companies ({', '.join(co_vals.keys())}) "
                    f"VALUES ({', '.join('?' for _ in co_vals)})",
                    list(co_vals.values())
                )
                co_id = cur.lastrowid

            # Создаём/обновляем контакт (директор/телефон компании)
            director_phone = norm(row, "director_phone") or norm(row, "phone") or norm(row, "email")
            if director_phone:
                contact_name = norm(row, "director_name") or cname
                contact_role = norm(row, "director_role") or None
                cid = database.upsert_contact(
                    conn, source=source, phone=norm(row, "director_phone") or norm(row, "phone"),
                    email=norm(row, "director_email") or norm(row, "email"),
                    name=contact_name, person_name=contact_name,
                    person_role=contact_role,
                    agency=cname, tags=tag or None,
                    notes=f"импорт из {source}",
                )
                conn.execute(
                    "UPDATE contacts SET company_id=? WHERE id=?",
                    (co_id, cid)
                )
            added += 1
    return added, skipped


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

    # Если Excel (.xlsx) — читаем через openpyxl, конвертируем в CSV-текст для парсера
    if (file.filename or "").endswith(".xlsx"):
        try:
            import openpyxl
            import io
            import csv
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            ws = wb.active
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            for row in ws.iter_rows(values_only=True):
                w.writerow(["" if v is None else str(v) for v in row])
            text = buf.getvalue()
            wb.close()
        except Exception as e:
            return JSONResponse({"error": f"ошибка чтения Excel: {e}"}, status_code=400)
    else:
        text = None
        for enc in ("cp1251", "utf-8-sig", "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return JSONResponse({"error": "не удалось распознать кодировку файла"}, status_code=400)

    src = (source or "import").strip() or "import"
    tag_clean = tag.strip() or "Импорт"
    try:
        # Пробуем универсальный парсер (с Наименование, ИНН и т.д.)
        added, skipped = _parse_universal(text, tag_clean, src)
    except ValueError as e:
        # Если не подошёл — пробуем старый 2ГИС-формат
        try:
            added, skipped = _parse_2gis(text, tag_clean, src)
        except ValueError:
            return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    with database.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]
        co_total = conn.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
    return JSONResponse({"ok": True, "imported": added, "skipped": skipped, "total": total, "companies": co_total})


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
    """Прогреть СЕЙЧАС аккаунты в статусе «прогрев» с сессией И живым прокси
    (фоновый процесс). Без прокси не берём — иначе Telegram видит пачку
    «разных» аккаунтов с одного IP."""
    import subprocess
    import sys
    with database.get_conn() as conn:
        rows = database.warming_accounts(conn)
        skipped = conn.execute(
            "SELECT COUNT(*) c FROM accounts WHERE status='warming' AND tg_session IS NOT NULL "
            "AND tg_session<>'' AND COALESCE(protected,0)=0 AND "
            "(proxy IS NULL OR proxy='' OR proxy_alive=0)"
        ).fetchone()["c"]
    if not rows:
        return JSONResponse(
            {"error": f"нет готовых к прогреву аккаунтов с живым прокси (без прокси/с мёртвым: {skipped}) "
                      "— сначала раздай прокси"},
            status_code=400,
        )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "channels_warmup.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n===== {__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S} запуск: channels.warmup --run =====\n")
        f.flush()
        proc = subprocess.Popen([sys.executable, "-m", "channels.warmup", "--run"],
                                cwd=str(BASE_DIR.parent), stdout=f, stderr=subprocess.STDOUT)
    with database.get_conn() as conn:
        database.set_setting(conn, "warmup_pid", str(proc.pid))
    return JSONResponse({"ok": True, "warming": len(rows), "skipped_no_proxy": skipped})


@app.post("/api/warmup/stop")
def warmup_stop() -> JSONResponse:
    """Останавливает текущий фоновый прогрев (если запущен через «Прогреть всех сейчас»)."""
    import psutil
    with database.get_conn() as conn:
        pid_s = database.get_setting(conn, "warmup_pid", None)
    if not pid_s:
        return JSONResponse({"ok": True, "stopped": False, "note": "прогрев сейчас не запущен"})
    try:
        proc = psutil.Process(int(pid_s))
        cmdline = " ".join(proc.cmdline())
        if "channels.warmup" not in cmdline:
            return JSONResponse({"ok": True, "stopped": False, "note": "процесс уже не тот (перезапущен) — нечего останавливать"})
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            proc.kill()
    except psutil.NoSuchProcess:
        pass
    with database.get_conn() as conn:
        database.set_setting(conn, "warmup_pid", "")
        database.add_event(conn, "info", "⏹ Прогрев остановлен вручную", level="warn")
    return JSONResponse({"ok": True, "stopped": True})


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


@app.post("/api/campaign/{cid}/test")
def campaign_test(cid: int) -> JSONResponse:
    """Тестовый заход: шлёт ТОЛЬКО на свои тест-номера (is_test=1), в обход гейта прогрева.
    Отдельная кнопка «Тест» — проверить скрипт живьём на себе перед боевым запуском.
    Перед отправкой СБРАСЫВАЕТ статус тестовых контактов в 'new' — чтобы можно было
    тестировать многократно, не добавляя номера заново."""
    import subprocess
    import sys
    with database.get_conn() as conn:
        row = conn.execute("SELECT name, message_template, audience_tag FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        if not (row["message_template"] or "").strip():
            return JSONResponse({"error": "сначала заполни текст первого сообщения"}, status_code=400)
        # Сбрасываем статус тестовых контактов — чтобы тест срабатывал повторно
        tag = (row["audience_tag"] or "").strip()
        test_ids: list[int] = []
        if tag:
            test_ids = [r[0] for r in conn.execute(
                "SELECT id FROM contacts WHERE COALESCE(is_test,0)=1 AND tags LIKE ?",
                (f"%{tag}%",),
            ).fetchall()]
            conn.execute(
                "UPDATE contacts SET status='new' WHERE id IN ({})".format(
                    ",".join("?" * len(test_ids))
                ),
                test_ids,
            )
        else:
            test_ids = [r[0] for r in conn.execute(
                "SELECT id FROM contacts WHERE COALESCE(is_test,0)=1"
            ).fetchall()]
            conn.execute("UPDATE contacts SET status='new' WHERE COALESCE(is_test,0)=1")
        # Очищаем старые записи очереди тестовых контактов (чтобы не было дублей)
        if test_ids:
            conn.execute(
                "DELETE FROM opener_queue WHERE contact_id IN ({}) AND campaign_id=?".format(
                    ",".join("?" * len(test_ids))
                ),
                (*test_ids, cid),
            )
        n_test = conn.execute("SELECT COUNT(*) FROM contacts WHERE COALESCE(is_test,0)=1 "
                              "AND status='new'").fetchone()[0]
        if not n_test:
            return JSONResponse({"error": "нет тест-номеров (is_test=1). Добавь свои "
                                          "номера в тест-контакты кампании."}, status_code=400)
        database.add_event(conn, "campaign_test", f"🧪 Тест кампании «{row['name']}»",
                           f"сброс {n_test} тестовых контактов → статус new, отправка",
                           level="good", campaign_id=cid)
    subprocess.Popen(
        [sys.executable, "-m", "channels.campaign_send", str(cid), "--limit", "10", "--test"],
        cwd=str(BASE_DIR.parent),
    )
    return JSONResponse({"ok": True, "test_targets": n_test})


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
