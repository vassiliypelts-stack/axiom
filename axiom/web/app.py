"""Веб-морда AXIOM — пульт оператора.

Разделы (левое меню): Дашборд, Мои агенты (аккаунты), CRM (база + воронка),
Досье, Кампании, Чаты. Поверх той же SQLite-книжки, что и Telegram-адаптер.

Запуск:

    python -m web.app                 # http://127.0.0.1:8000
    python -m web.app --port 9000
"""
from __future__ import annotations

import argparse
import contextlib
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
        # Те же no-cache заголовки, что и на «/». Без них браузер кэшировал пульт
        # под адресом /login — а именно там оператор и остаётся после входа
        # (…:8000/login#campaigns). После деплоя приходила СТАРАЯ страница: кнопка
        # обновления висела снова, хотя сервер уже обновился.
        resp = HTMLResponse(index_html, status_code=200, headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })
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
        # набор слушающих чаты; старый одиночный ключ — фолбэк для настроек до перехода
        listen_csv = database.get_setting(conn, "listen_account_ids")
        if not listen_csv:
            listen_csv = database.get_setting(conn, "listen_account_id") or ""
        listen_ids = {x.strip() for x in listen_csv.split(",") if x.strip()}
        notify_id = database.get_setting(conn, "notify_sender_account_id")
    out = []
    for r in rows:
        d = dict(r)
        d["tg_connected"] = bool(d.pop("tg_session", None))  # секрет наружу не отдаём
        # Запасная сессия — тоже полноценный доступ к аккаунту. В браузер отдаём только
        # факт наличия: строку не показываем даже оператору, ей нечего делать в UI.
        d["has_spare"] = bool((d.pop("tg_session_spare", None) or "").strip())
        d["chats_count"] = chats_by.get(d["id"], 0)
        # кто шлёт владельцу личное уведомление при назначенной встрече (channels/notify.py)
        d["is_notifier"] = bool(notify_id) and str(d["id"]) == str(notify_id)
        # кто сейчас опрашивает публичные чаты каталога (channels.chat_keywords) — по
        # умолчанию личный номер из .env, назначить аккаунты можно тут же в таблице
        # галочками; их может быть несколько, включая «родные» (чтение чатов им можно)
        d["is_listener"] = str(d["id"]) in listen_ids
        # страна: сохранённый ISO2 или определяем по номеру на лету (+ готовая надпись с флагом)
        code = d.get("country") or phone_geo.detect(d.get("phone"))
        d["country_label"] = phone_geo.label(code) if code else ""
        d["days_alive"] = _days_since(d.get("bought_at") or d.get("created_at"))
        out.append(d)
    return JSONResponse(out)


@app.post("/api/settings/listen_account")
def settings_listen_account(payload: dict = Body(...)) -> JSONResponse:
    """Какие аккаунты опрашивают публичные чаты (channels.chat_keywords).

    Аккаунтов может быть несколько: обход каталога — это тысячи запросов истории, и
    размазать их по нескольким номерам безопаснее, чем гнать одним (FloodWait на 13.8ч
    поймали именно так). Сюда можно назначать и «родных» — чтение чужих сообщений в
    публичном чате им разрешено, в отличие от авто-ответа, прогрева и рассылки, где
    фильтр по protected остаётся (listener.py, warmup.py).

    Принимаем и `account_ids` (список — новый режим), и `account_id` (одиночный —
    старые вызовы), чтобы не ломать то, что уже настроено."""
    ids = payload.get("account_ids")
    if ids is None:
        one = payload.get("account_id")
        ids = [one] if one else []
    clean: list[int] = []
    for x in ids:
        try:
            clean.append(int(x))
        except (TypeError, ValueError):
            continue
    with database.get_conn() as conn:
        if clean:
            qm = ",".join("?" * len(clean))
            found = {r["id"] for r in conn.execute(
                f"SELECT id FROM accounts WHERE id IN ({qm})", clean)}
            missing = [i for i in clean if i not in found]
            if missing:
                return JSONResponse({"error": f"аккаунты не найдены: {missing}"}, status_code=404)
        database.set_setting(conn, "listen_account_ids", ",".join(str(i) for i in clean))
        # старый одиночный ключ держим синхронным: его читает фолбэк в _listen_clients
        database.set_setting(conn, "listen_account_id", str(clean[0]) if clean else "")
    return JSONResponse({"ok": True, "account_ids": clean})


@app.get("/api/settings/notify")
def settings_notify_get() -> JSONResponse:
    """Кто уведомляет владельца о назначенных встречах и куда (см. channels/notify.py)."""
    with database.get_conn() as conn:
        return JSONResponse({
            "sender_account_id": database.get_setting(conn, "notify_sender_account_id", "") or None,
            "target": database.get_setting(conn, "notify_owner_target", "") or "",
        })


@app.post("/api/settings/notify_sender")
def settings_notify_sender(payload: dict = Body(...)) -> JSONResponse:
    """Какой аккаунт шлёт уведомления о встречах в личку владельцу — обычно «родной»
    номер, чтобы сообщение выглядело как обычная переписка, а не служебный алерт."""
    acc_id = payload.get("account_id")
    with database.get_conn() as conn:
        if acc_id:
            row = conn.execute("SELECT id FROM accounts WHERE id=?", (acc_id,)).fetchone()
            if not row:
                return JSONResponse({"error": f"аккаунт #{acc_id} не найден"}, status_code=404)
            database.set_setting(conn, "notify_sender_account_id", str(acc_id))
        else:
            database.set_setting(conn, "notify_sender_account_id", "")
    return JSONResponse({"ok": True})


@app.post("/api/settings/notify_target")
def settings_notify_target(payload: dict = Body(...)) -> JSONResponse:
    """Кому слать уведомления о встречах — телефон (+7...) или @username получателя."""
    target = (payload.get("target") or "").strip()
    with database.get_conn() as conn:
        database.set_setting(conn, "notify_owner_target", target)
    return JSONResponse({"ok": True, "target": target})


@app.get("/api/settings/reply_delay")
def settings_reply_delay_get() -> JSONResponse:
    """Диапазон паузы «увидел → ответил» в диалоге (channels/telegram.py::_reply_delay_range)."""
    with database.get_conn() as conn:
        return JSONResponse({
            "min_sec": int(database.get_setting(conn, "reply_delay_min_sec", "30") or 30),
            "max_sec": int(database.get_setting(conn, "reply_delay_max_sec", "60") or 60),
        })


@app.post("/api/settings/reply_delay")
def settings_reply_delay_set(payload: dict = Body(...)) -> JSONResponse:
    lo = max(1, int(payload.get("min_sec") or 30))
    hi = max(lo, int(payload.get("max_sec") or 60))
    with database.get_conn() as conn:
        database.set_setting(conn, "reply_delay_min_sec", str(lo))
        database.set_setting(conn, "reply_delay_max_sec", str(hi))
    return JSONResponse({"ok": True, "min_sec": lo, "max_sec": hi})


# Модели для живого диалога. Порядок = порядок в списке пульта.
# Claude идёт первым не из вкуса: диалог требует строгой JSON-схемы (реплики, намерение,
# согласовано ли время), а дешёвые провайдеры умеют лишь «верни какой-нибудь JSON» —
# и регулярно отдают пустоту, что означает молчание в ответ живому человеку.
AGENT_MODEL_CHOICES = [
    {"id": "claude-sonnet-5", "name": "Claude Sonnet 5 — рабочая лошадка для диалогов"},
    {"id": "claude-opus-5", "name": "Claude Opus 5 — самый умный, дороже"},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5 — дешёвый, для простых диалогов"},
    {"id": "deepseek:deepseek-chat", "name": "DeepSeek — дёшево, но в диалоге срывается"},
]


@app.get("/api/settings/agent_model")
def settings_agent_model_get() -> JSONResponse:
    """Какая модель ведёт диалоги + есть ли под неё ключ."""
    from agent import llm
    with database.get_conn() as conn:
        picked = (database.get_setting(conn, "agent_model", "") or "").strip()
    current = config.agent_model()
    return JSONResponse({
        "current": current, "picked": picked, "from_env": config.AGENT_MODEL,
        "has_key": llm.available(current),
        "choices": [{**c, "has_key": llm.available(c["id"])} for c in AGENT_MODEL_CHOICES],
    })


@app.post("/api/settings/agent_model")
def settings_agent_model_set(payload: dict = Body(...)) -> JSONResponse:
    """Сменить модель диалогов. Пусто — вернуться к тому, что задано в .env."""
    from agent import llm
    model = (payload.get("model") or "").strip()
    if model and not llm.available(model):
        return JSONResponse(
            {"error": f"под «{model}» нет ключа — агент не сможет ответить. "
                      f"Добавь ключ в .env на сервере или выбери другую модель."},
            status_code=400)
    with database.get_conn() as conn:
        database.set_setting(conn, "agent_model", model)
        database.add_event(conn, "info", f"🤖 Модель агента: {model or config.AGENT_MODEL}",
                           "Сменил модель для живых диалогов из пульта.", level="good")
    return JSONResponse({"ok": True, "current": config.agent_model()})


@app.get("/api/settings/meeting_url")
def settings_meeting_url_get() -> JSONResponse:
    """Постоянная ссылка на созвон (Телемост/Zoom/Meet) — её агент даёт человеку сразу
    при согласии на время (см. integrations/meetings._meeting_url)."""
    with database.get_conn() as conn:
        url = (database.get_setting(conn, "meeting_url", "") or "").strip()
    return JSONResponse({"url": url, "from_env": bool(config.PERMANENT_MEETING_URL and not url)})


@app.post("/api/settings/meeting_url")
def settings_meeting_url_set(payload: dict = Body(...)) -> JSONResponse:
    url = (payload.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "ссылка должна начинаться с https:// — так её и увидит клиент"},
                            status_code=400)
    with database.get_conn() as conn:
        database.set_setting(conn, "meeting_url", url)
    return JSONResponse({"ok": True, "url": url})


@app.get("/api/settings/org_notes")
def settings_org_notes_get() -> JSONResponse:
    """Свободные заметки к оргструктуре — план по департаментам, цели, расчёты."""
    with database.get_conn() as conn:
        notes = database.get_setting(conn, "org_notes", "") or ""
    return JSONResponse({"notes": notes})


@app.post("/api/settings/org_notes")
def settings_org_notes_set(payload: dict = Body(...)) -> JSONResponse:
    notes = payload.get("notes") or ""
    with database.get_conn() as conn:
        database.set_setting(conn, "org_notes", notes)
    return JSONResponse({"ok": True})


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


def _dept_descendants(conn, did: int) -> set[int]:
    """Все потомки отдела (чтобы не дать перетащить отдел внутрь самого себя)."""
    rows = conn.execute("SELECT id, parent_id FROM departments").fetchall()
    kids: dict[int, list[int]] = {}
    for r in rows:
        kids.setdefault(r["parent_id"] or 0, []).append(r["id"])
    out: set[int] = set()
    stack = list(kids.get(did, []))
    while stack:
        x = stack.pop()
        if x in out:
            continue
        out.add(x)
        stack.extend(kids.get(x, []))
    return out


@app.post("/api/org/department")
def org_department_save(payload: dict = Body(...)) -> JSONResponse:
    did = payload.get("id")
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "нужно название отдела"}, status_code=400)
    description = (payload.get("description") or "").strip() or None
    parent_id = payload.get("parent_id") or None
    color = (payload.get("color") or "").strip() or None
    head_member_id = payload.get("head_member_id") or None
    if did and parent_id and int(parent_id) == int(did):
        return JSONResponse({"error": "отдел не может быть родителем самого себя"}, status_code=400)
    with database.get_conn() as conn:
        if did and parent_id and int(parent_id) in _dept_descendants(conn, int(did)):
            return JSONResponse(
                {"error": "нельзя вложить отдел в собственный подотдел"}, status_code=400
            )
        if did:
            conn.execute(
                "UPDATE departments SET name=?, description=?, parent_id=?, color=?, head_member_id=? "
                "WHERE id=?",
                (name, description, parent_id, color, head_member_id, int(did)),
            )
            new_id = int(did)
        else:
            # Новый отдел встаёт последним среди соседей по уровню.
            nxt = conn.execute(
                "SELECT COALESCE(MAX(sort_order),0)+1 n FROM departments "
                "WHERE IFNULL(parent_id,0)=IFNULL(?,0)",
                (parent_id,),
            ).fetchone()["n"]
            cur = conn.execute(
                "INSERT INTO departments (name, description, parent_id, sort_order, color) "
                "VALUES (?,?,?,?,?)",
                (name, description, parent_id, nxt, color),
            )
            new_id = cur.lastrowid
    return JSONResponse({"ok": True, "id": new_id})


@app.post("/api/org/department/{did}/move")
def org_department_move(did: int, payload: dict = Body(...)) -> JSONResponse:
    """Перетаскивание отдела на схеме: смена родителя и/или порядка среди соседей.

    `parent_id` = null — поднять на верхний уровень. `index` — позиция среди соседей
    (0 = первым); если не передан, отдел встаёт последним.
    """
    parent_id = payload.get("parent_id") or None
    index = payload.get("index")
    if parent_id and int(parent_id) == did:
        return JSONResponse({"error": "отдел не может быть родителем самого себя"}, status_code=400)
    with database.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM departments WHERE id=?", (did,)).fetchone():
            return JSONResponse({"error": "отдел не найден"}, status_code=404)
        if parent_id and int(parent_id) in _dept_descendants(conn, did):
            return JSONResponse(
                {"error": "нельзя вложить отдел в собственный подотдел"}, status_code=400
            )
        conn.execute("UPDATE departments SET parent_id=? WHERE id=?", (parent_id, did))
        # Пересобираем порядок соседей: вынимаем переносимый и вставляем на нужное место.
        sibs = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM departments WHERE IFNULL(parent_id,0)=IFNULL(?,0) "
                "ORDER BY sort_order, id",
                (parent_id,),
            ).fetchall()
        ]
        sibs = [x for x in sibs if x != did]
        pos = len(sibs) if index is None else max(0, min(int(index), len(sibs)))
        sibs.insert(pos, did)
        for i, x in enumerate(sibs):
            conn.execute("UPDATE departments SET sort_order=? WHERE id=?", (i, x))
    return JSONResponse({"ok": True})


@app.post("/api/org/department/{did}/delete")
def org_department_delete(did: int, payload: dict = Body(default={})) -> JSONResponse:
    """Удаление отдела. `move_to` — куда переселить сотрудников и подотделы.

    Без `move_to` непустой отдел удалить нельзя (защита от случайной потери людей):
    фронт в этом случае спрашивает, в какой отдел переносить.
    """
    move_to = (payload or {}).get("move_to") or None
    with database.get_conn() as conn:
        n_members = conn.execute(
            "SELECT COUNT(*) c FROM org_members WHERE department_id=?", (did,)
        ).fetchone()["c"]
        n_children = conn.execute(
            "SELECT COUNT(*) c FROM departments WHERE parent_id=?", (did,)
        ).fetchone()["c"]
        if n_members or n_children:
            if not move_to:
                return JSONResponse(
                    {
                        "error": "сначала перенеси сотрудников/подотделы из этого отдела",
                        "need_move": True,
                        "members": n_members,
                        "children": n_children,
                    },
                    status_code=400,
                )
            if int(move_to) == did:
                return JSONResponse({"error": "нельзя перенести в удаляемый отдел"}, status_code=400)
            if not conn.execute("SELECT 1 FROM departments WHERE id=?", (int(move_to),)).fetchone():
                return JSONResponse({"error": "отдел-приёмник не найден"}, status_code=400)
            conn.execute(
                "UPDATE org_members SET department_id=? WHERE department_id=?", (int(move_to), did)
            )
            # Подотделы поднимаем к родителю удаляемого, а не в приёмник — так ветка
            # сохраняет форму; в приёмник уходят только люди.
            parent = conn.execute(
                "SELECT parent_id FROM departments WHERE id=?", (did,)
            ).fetchone()["parent_id"]
            conn.execute("UPDATE departments SET parent_id=? WHERE parent_id=?", (parent, did))
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
    """Переместить сотрудника в другой отдел (drag-and-drop на схеме).

    `index` — позиция внутри отдела; без него сотрудник встаёт последним. Порядок
    внутри отдела пересобирается целиком, чтобы sort_order не разъезжался.
    """
    department_id = payload.get("department_id")
    index = payload.get("index")
    if not department_id:
        return JSONResponse({"error": "нужно выбрать отдел"}, status_code=400)
    department_id = int(department_id)
    with database.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM departments WHERE id=?", (department_id,)).fetchone():
            return JSONResponse({"error": "отдел не найден"}, status_code=400)
        conn.execute("UPDATE org_members SET department_id=? WHERE id=?", (department_id, mid))
        sibs = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM org_members WHERE department_id=? ORDER BY sort_order, id",
                (department_id,),
            ).fetchall()
        ]
        sibs = [x for x in sibs if x != mid]
        pos = len(sibs) if index is None else max(0, min(int(index), len(sibs)))
        sibs.insert(pos, mid)
        for i, x in enumerate(sibs):
            conn.execute("UPDATE org_members SET sort_order=? WHERE id=?", (i, x))
    return JSONResponse({"ok": True})


@app.post("/api/org/member/{mid}/delete")
def org_member_delete(mid: int) -> JSONResponse:
    with database.get_conn() as conn:
        # Если удаляем руководителя — снимаем его с отдела, иначе останется битая ссылка.
        conn.execute("UPDATE departments SET head_member_id=NULL WHERE head_member_id=?", (mid,))
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
    d["has_spare"] = bool((d.pop("tg_session_spare", None) or "").strip())   # то же самое
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
    # Прокси, уже занятый другим аккаунтом, не отдаём молча: два аккаунта на одном
    # выходе — прямая дорога к AuthKeyDuplicatedError (сессия, увиденная с двух IP,
    # жжётся навсегда; три аккаунта так уже потеряны). Раздача из пула это учитывает,
    # а ручное назначение — не учитывало вовсе.
    if payload.get("proxy") and not payload.get("force"):
        key = _proxy_key(payload.get("proxy"))
        if key:
            with database.get_conn() as conn:
                others = conn.execute(
                    "SELECT id, label, proxy FROM accounts WHERE id<>? AND proxy IS NOT NULL "
                    "AND proxy<>'' AND COALESCE(status,'') NOT IN ('archived','banned')",
                    (acc_id,)).fetchall()
            busy = [dict(o) for o in others if _proxy_key(o["proxy"]) == key]
            if busy:
                who = ", ".join(f"{b['label'] or '#'+str(b['id'])}" for b in busy[:5])
                return JSONResponse({
                    "needs_confirm": True,
                    "warn": f"Этот прокси уже занят: {who}.\n\nДва аккаунта на одном адресе "
                            f"Telegram видит как одного человека, а при смене прокси в пуле "
                            f"один из них меняет IP на живой сессии — так сессии и сгорают.\n\n"
                            f"Назначить всё равно?"}, status_code=200)

    vals.append(acc_id)
    renamed = None
    with database.get_conn() as conn:
        conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)
        # У аккаунта ДВА поля с именем: label (что видит оператор в пульте) и
        # tg_name (что уходит в сам профиль Telegram и по чьему полу подбирается
        # фото). Переименование в пульте меняло только label — и личность
        # разъезжалась: в списке «Кристина Орлова», в Telegram «Никита», на
        # аватаре мужчина. Переупаковка это не лечила, потому что честно брала
        # tg_name. Держим их синхронными: назвали человека — значит его так и зовут.
        if "label" in payload:
            new = (payload.get("label") or "").strip()
            row = conn.execute("SELECT tg_name, avatar FROM accounts WHERE id=?", (acc_id,)).fetchone()
            old_tg = ((row["tg_name"] if row else None) or "").strip()
            # Номера и служебные метки («+7999…», «#12», «acc 3») именем не считаем.
            looks_human = bool(new) and not new.startswith(("+", "#")) and not new[0].isdigit()
            if looks_human and new != old_tg:
                conn.execute("UPDATE accounts SET tg_name=? WHERE id=?", (new, acc_id))
                renamed = {"tg_name": new, "was": old_tg or None}
                # Пол мог смениться вместе с именем — тогда старое фото врёт.
                # Снимаем отметку об аватаре, чтобы «оформить сейчас» подобрал новое.
                try:
                    from channels.ru_names import gender_of
                    if old_tg and gender_of(new) != gender_of(old_tg):
                        conn.execute("UPDATE accounts SET avatar=NULL WHERE id=?", (acc_id,))
                        renamed["avatar_reset"] = True
                except Exception:  # noqa: BLE001 — определение пола не критично
                    pass
    return JSONResponse({"ok": True, "renamed": renamed})


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


@app.post("/api/account/{acc_id}/proxy_renew")
async def account_proxy_renew(acc_id: int) -> JSONResponse:
    """Замена прокси в один клик — под красный значок «мёртвый» в колонке «Прокси».

    Берёт готовые механизмы: сначала бесплатный MTProto из пула (proxy_pool), если
    там пусто или всё дохлое — свежий бесплатный SOCKS5 (proxy_find). Кандидата
    проверяем живым коннектом к Telegram и только после этого пишем в карточку с
    proxy_alive=1 — чтобы значок стал зелёным по факту, а не авансом.

    Прокси, уже стоящие на ДРУГИХ аккаунтах, не берём: антибан держится на
    «1 прокси = 1 аккаунт»."""
    import asyncio

    import config
    from channels import proxy_find, proxy_pool

    with database.get_conn() as conn:
        acc = conn.execute("SELECT id FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if not acc:
            return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
        taken = {r["proxy"] for r in conn.execute(
            "SELECT proxy FROM accounts WHERE IFNULL(proxy,'')<>'' AND id<>?", (acc_id,))}

    api_id, api_hash = int(config.TG_API_ID), config.TG_API_HASH
    tried = 0
    found = None
    seen: set[str] = set(taken)
    # 1) бесплатный MTProto из пула — свободных берём сразу пачкой и проверяем
    # параллельно: последовательно это до 8с на кандидата прямо в запросе.
    mts: list[str] = []
    for _ in range(4):
        mt = proxy_pool.pick_free_mt(exclude=seen | set(mts))
        if not mt:
            break
        mts.append(mt)
    if mts:
        tried += len(mts)
        checks = await asyncio.gather(*[
            proxy_pool._test_account_proxy(acc_id, f"#{acc_id}", px, api_id, api_hash)
            for px in mts])
        found = next((px for px, ok in zip(mts, checks) if ok), None)
        seen.update(mts)
    # 2) пул не выручил — идём за свежим бесплатным SOCKS5 (find_fast уже проверяет
    # каждого кандидата реальным запросом к Telegram, повторно не гоняем)
    if not found:
        fresh = await proxy_find.find_fast(need=1, max_test=120, exclude=seen)
        tried += 1 if fresh else 0
        found = fresh[0] if fresh else None
    if not found:
        return JSONResponse(
            {"error": "свободного живого прокси сейчас не нашлось — нажми ещё раз "
                      "(бесплатные списки обновляются) или купи резидентный через Proxy6",
             "tried": tried},
            status_code=503)
    with database.get_conn() as conn:
        conn.execute("UPDATE accounts SET proxy=?, proxy_alive=1, proxy_checked_at=datetime('now') "
                     "WHERE id=?", (found, acc_id))
        database.add_event(conn, "proxy_renew", f"🌐 Прокси заменён: аккаунт #{acc_id}",
                           f"новый прокси {found} (проверен коннектом к Telegram)",
                           level="good", account_id=acc_id)
    return JSONResponse({"ok": True, "proxy": found, "proxy_alive": 1, "tried": tried})


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
    _spawn("channels.chat_keywords", "--limit", "300")
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


_SECRET_RE = None


def _mask_secrets(text: str) -> str:
    """Вырезать секреты из лога ПЕРЕД отдачей наружу.

    Логи пишут всё подряд, включая строки подключения и куски окружения: там живьём
    встречаются ключи API, пароли прокси и StringSession аккаунта. Лог смотрят, чтобы
    понять поломку, а не чтобы раздать доступы — поэтому маскируем на выходе, а не
    надеемся, что «туда ничего такого не попадёт»."""
    global _SECRET_RE
    import re
    if _SECRET_RE is None:
        _SECRET_RE = re.compile(
            r"(sk-[A-Za-z0-9_\-]{8,}"          # ключи Anthropic/DeepSeek/OpenAI
            r"|AIza[A-Za-z0-9_\-]{10,}"         # Google
            r"|[A-Za-z0-9+/]{40,}={0,2}"        # длинные base64 (сессии Telethon)
            r"|(?<=://)[^:@/\s]+:[^@/\s]+(?=@))"  # логин:пароль в прокси-URL
        )
    masked = _SECRET_RE.sub("***", text or "")
    # плюс явные значения из окружения — на случай формата, что не поймал шаблон
    for var in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY",
                "TG_API_HASH", "AXIOM_PASSWORD", "TG_STRING_SESSION"):
        val = (getattr(config, var, "") or "").strip()
        if val and len(val) > 6:
            masked = masked.replace(val, "***")
    return masked


@app.get("/api/logs")
def logs_tail(name: str = "service", lines: int = 200) -> JSONResponse:
    """Хвост логов прямо из пульта — чтобы разбирать поломку, не заходя на сервер.

    name=service — журнал systemd (туда идёт весь stdout пульта: ошибки агента,
    слушателя, рассылки). Иначе — файл из data/logs/<name>.log.
    Секреты маскируются (см. _mask_secrets). Доступ — под общим паролем пульта."""
    n = max(10, min(int(lines or 200), 2000))
    if name == "service":
        import subprocess
        try:
            r = subprocess.run(
                ["journalctl", "-u", "axiom-web", "-n", str(n), "--no-pager"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            out = (r.stdout or "") + (r.stderr or "")
            if not out.strip():
                return JSONResponse({"name": name, "text": "",
                                     "note": "journalctl пуст или недоступен под этим пользователем"})
        except FileNotFoundError:
            return JSONResponse({"name": name, "text": "",
                                 "note": "journalctl не найден (не systemd-хост)"})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"name": name, "text": "", "note": f"не смог прочитать: {e}"})
        return JSONResponse({"name": name, "text": _mask_secrets(out)[-200000:]})
    # файловые логи фоновых запусков
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "_-")
    p = LOG_DIR / f"{safe}.log"
    if not p.exists():
        avail = sorted(x.stem for x in LOG_DIR.glob("*.log")) if LOG_DIR.exists() else []
        return JSONResponse({"error": f"нет лога «{safe}»", "available": avail}, status_code=404)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    tail = "\n".join(text.splitlines()[-n:])
    return JSONResponse({"name": safe, "text": _mask_secrets(tail)})


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
                # «--listen» у chat_keywords нет и не было: argparse молча падал с кодом 2,
                # и авто-прослушка по расписанию не отрабатывала ни разу — только копила
                # строки «код выхода 2» в kw_scheduler.log. Догоняющий проход = --limit.
                res = subprocess.run([sys.executable, "-m", "channels.chat_keywords", "--limit", "300"],
                                     cwd=str(BASE_DIR.parent), timeout=600, env=env,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
                _log_run("kw_scheduler", res)
        except Exception as e:  # noqa: BLE001
            print(f"[kw scheduler] {e}")
        # --- дозаполнение tg_chat_id у чатов каталога (backfill) ---
        # 1637 чатов из 2477 без tg_chat_id — из-за этого рвётся связка «в каком чате
        # найден человек» в досье. Чинится одним модулем, но резолвить их разом нельзя:
        # 16.07 на ~280 резолвах подряд прилетел FloodWait на 22.8 часа. Поэтому не
        # «запустить и ждать», а капать порциями: за сутки очередь уходит сама, а
        # профиль нагрузки остаётся человеческим. Порции и паузу решает backfill —
        # у него есть пул аккаунтов, лимит на номер и отсечка по ступени прогрева.
        try:
            with database.get_conn() as conn:
                bauto = database.get_setting(conn, "backfill_auto", "off")
                bint = int(database.get_setting(conn, "backfill_interval_min", "360"))
                blast = database.get_setting(conn, "backfill_last_run_ts", "0")
                blimit = int(database.get_setting(conn, "backfill_limit", "100"))
            if bauto == "on" and (time.time() - float(blast or 0)) >= bint * 60:
                with database.get_conn() as conn:
                    database.set_setting(conn, "backfill_last_run_ts", str(time.time()))
                    database.set_setting(conn, "backfill_last_run",
                                         __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"))
                res = subprocess.run([sys.executable, "-m", "channels.backfill", "--chats",
                                      "--limit", str(max(10, min(blimit, 500)))],
                                     cwd=str(BASE_DIR.parent), timeout=1800, env=env,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
                _log_run("backfill_scheduler", res)
        except Exception as e:  # noqa: BLE001
            print(f"[backfill scheduler] {e}")
        # --- фоновый пробив номеров в Telegram (has_tg) ---
        # Пока номер не пробит, рассылка резолвит его ПРЯМО в момент отправки
        # (ImportContacts) — самый заметный для Telegram спам-сигнал. Отсюда правило
        # «сначала пробей, потом шли», но вручную это делать никто не будет: в
        # кампании 180 номеров и 0 пробитых. Пусть капает само — phone_resolve уже
        # умеет главное: 25 номеров на аккаунт в сутки, удаление контакта сразу после
        # пробива и уход аккаунта с дистанции при FloodWait.
        try:
            with database.get_conn() as conn:
                tauto = database.get_setting(conn, "tgcheck_auto", "off")
                tint = int(database.get_setting(conn, "tgcheck_interval_min", "720"))
                tlast = database.get_setting(conn, "tgcheck_last_run_ts", "0")
                tper = int(database.get_setting(conn, "tgcheck_per", "25"))
            if tauto == "on" and (time.time() - float(tlast or 0)) >= tint * 60:
                with database.get_conn() as conn:
                    database.set_setting(conn, "tgcheck_last_run_ts", str(time.time()))
                    database.set_setting(conn, "tgcheck_last_run",
                                         __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"))
                res = subprocess.run([sys.executable, "-m", "channels.phone_resolve",
                                      "--per", str(max(1, min(tper, 50)))],
                                     cwd=str(BASE_DIR.parent), timeout=3600, env=env,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
                _log_run("tgcheck_scheduler", res)
        except Exception as e:  # noqa: BLE001
            print(f"[tgcheck scheduler] {e}")
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


@app.get("/api/agent/why_silent")
def agent_why_silent() -> JSONResponse:
    """«Клиент ответил, а агент молчит» — назвать причину одним запросом.

    Причин ровно пять, и живут они в пяти разных местах: тумблер авто-ответа, сам
    слушатель, статус аккаунта, «родной» флаг и деньги на API. Оператор искать их по
    закоулкам не станет — он видит только тишину в переписке и считает, что сломался
    агент. Здесь собираем всё вместе и сразу говорим, что нажать."""
    database.init_db()
    problems: list[dict] = []
    ok: list[str] = []
    with database.get_conn() as conn:
        auto = database.get_setting(conn, "tg_auto_reply", "on")
        listen_on = database.get_setting(conn, "listener_enabled", "on") != "off"
        # аккаунты, которые РЕАЛЬНО писали людям за последние сутки
        senders = [dict(r) for r in conn.execute(
            "SELECT DISTINCT a.id, a.label, a.phone, a.status, COALESCE(a.protected,0) protected, "
            "  (a.tg_session IS NOT NULL AND a.tg_session <> '') AS has_session "
            "FROM messages m JOIN accounts a ON a.id = m.account_id "
            "WHERE m.direction='out' AND m.ts >= datetime('now','-1 day')"
        ).fetchall()]
        mute = [dict(r) for r in conn.execute(
            "SELECT title, text, ts FROM events WHERE type IN ('agent_error','ban') "
            "AND ts >= datetime('now','-1 day') ORDER BY id DESC LIMIT 5"
        ).fetchall()]
    if auto != "on":
        problems.append({"что": "Выключен тумблер «авто-ответ ИИ»",
                         "делать": "Аккаунты → включить авто-ответ. Пока выключен, агент не отвечает никому."})
    else:
        ok.append("авто-ответ включён")
    if not listen_on:
        problems.append({"что": "Слушатель входящих выключен",
                         "делать": "Аккаунты → включить слушатель, иначе ответы клиентов никто не читает."})
    try:
        from channels import listener
        listening = set(listener.STATUS.get("accounts", {}).keys())
        if not listening:
            problems.append({"что": "Слушатель не подключил ни одного аккаунта",
                             "делать": "Проверь сессии и прокси в «Аккаунтах» (🔌 Подключить)."})
        else:
            ok.append(f"слушает аккаунтов: {len(listening)}")
    except Exception:  # noqa: BLE001
        listening = set()
        problems.append({"что": "Слушатель не запущен",
                         "делать": "Перезапусти пульт кнопкой «⬇ Обновить» — слушатель поднимается вместе с ним."})
    for s in senders:
        who = s["label"] or s["phone"] or f"#{s['id']}"
        if s["protected"]:
            problems.append({"что": f"«{who}» пишет людям, но помечен «родной» — слушатель такие не подключает",
                             "делать": f"Сними отметку «родной» с «{who}» в «Аккаунтах» — иначе ответы на его письма пропадают."})
        elif listening and s["id"] not in listening:
            problems.append({"что": f"«{who}» писал клиентам, но сейчас не слушается",
                             "делать": f"Переподключи «{who}» (🔌 Подключить): сессия или прокси отвалились."})
    from agent import llm
    if not llm.available(config.agent_model()):
        problems.append({"что": f"Нет ключа под модель «{config.agent_model()}»",
                         "делать": "Заполни ключ в .env на сервере — без него агент не может ответить."})
    return JSONResponse({"ok": not problems, "problems": problems, "fine": ok,
                         "recent_errors": mute})


def _meetings_scheduler() -> None:
    """Напоминания о встрече, дожим молчунов, «не дошёл», сторож молчания — раз в 15 мин.

    Ядро (scheduler.collect_due) считало это давно, но ОТПРАВЛЯЛ результат только
    channels/telegram.run_loop(), который поднимается лишь при ручном запуске из
    консоли. На сервере крутится один web.app — значит напоминания о созвонах и дожим
    не работали ВООБЩЕ: сделка назначена, а человеку за час до встречи никто не пишет
    и ссылку не присылает.

    Свой Telethon-клиент здесь заводить нельзя (одна сессия в двух местах = сгоревший
    аккаунт), поэтому шлём через соединения слушателя — см. listener.send_via_listener.
    Аккаунт берём тот, что вёл переписку с этим контактом.

    check_stuck_replies — отдельным вызовом, не через scheduler.tick(): tick() тут не
    подходит целиком, у него своя, более простая send-петля без учёта account_id, а
    сторож ничего не шлёт контакту вообще, только тревогу в колокольчик."""
    import time
    while True:
        time.sleep(900)
        try:
            from channels import antiban, listener
            from scheduler import apply as sched_apply, check_stuck_replies, collect_due
            if not antiban.within_work_hours():
                continue                      # ночью не пишем даже напоминания
            with database.get_conn() as conn:
                actions = collect_due(conn)
                stuck = check_stuck_replies(conn)
                if stuck:
                    print(f"[сторож] новых тревог «не отвечено вовремя»: {stuck}")
            for a in actions:
                if not a.tg_user_id:
                    continue
                with database.get_conn() as conn:
                    row = conn.execute(
                        "SELECT account_id FROM messages WHERE contact_id=? AND direction='out' "
                        "AND account_id IS NOT NULL ORDER BY id DESC LIMIT 1", (a.contact_id,)
                    ).fetchone()
                if not row:
                    continue                  # не знаем, с какого аккаунта вести диалог
                parts = [p for p in a.text.split("\n\n") if p.strip()] or [a.text]
                if not listener.send_via_listener(row["account_id"], int(a.tg_user_id), parts):
                    continue                  # не ушло — пробуем на следующем тике
                with database.get_conn() as conn:
                    sched_apply(conn, a)
                    database.add_event(
                        conn, "scheduler", f"⏰ {a.kind}: {a.name or a.contact_id}",
                        a.text[:200], level="good", contact_id=a.contact_id,
                        account_id=row["account_id"])
                print(f"[sched] {a.kind} -> contact {a.contact_id}")
        except Exception as e:  # noqa: BLE001 — фоновый тик не должен ронять пульт
            print(f"[meetings scheduler] {e}")


def _night_reply_scheduler() -> None:
    """Утренняя досылка ответов тем, кто написал ночью.

    Ночью агент молчит намеренно (09:00–21:30 МСК, см. channels/listener): ответ в три
    часа ночи — это и потерянный лид, и сигнал автоматики для Telegram. Но без этого
    тика такой человек остался бы без ответа НАВСЕГДА: событие Telegram давно ушло,
    переспрашивать он не обязан. Раз в 10 минут проверяем, наступило ли рабочее время,
    и отвечаем накопившимся.
    """
    import os
    import subprocess
    import sys
    import time
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    while True:
        time.sleep(600)
        try:
            from channels import antiban
            if not antiban.within_work_hours():
                continue
            res = subprocess.run([sys.executable, "-m", "channels.telegram", "--night-replies"],
                                 cwd=str(BASE_DIR.parent), timeout=900, env=env,
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace")
            if (res.stdout or "").strip():
                _log_run("night_replies", res)
        except Exception as e:  # noqa: BLE001 — фоновый тик не должен ронять пульт
            print(f"[night replies] {e}")


HOT_LEAD_TIMEOUT_MIN = 10  # см. channels/telegram._agent_reply — сколько молчания терпим


def _hot_lead_scheduler() -> None:
    """Горячий лид (contacts.hot_since, см. agent.Reply.hot) — готов действовать
    ПРЯМО СЕЙЧАС. Оператору уже упало уведомление в личку (channels/notify.notify_hot);
    этот тик — подстраховка на случай, если он не успел откликнуться за
    HOT_LEAD_TIMEOUT_MIN минут: бот сам мягко закрывает разговор, чтобы человек не
    завис в ожидании звонка, который не пришёл вовремя.

    Проверяем каждые 2 минуты — 15-минутный тик планировщика встреч для 10-минутного
    окна слишком грубый, мог бы упустить дедлайн вдвое. Отправляем через уже
    подключённого слушателя (listener.send_via_listener), не заводя свой Telethon-клиент
    — та же причина, что и у планировщика встреч: одна сессия в двух местах уже роняла
    слушатель на этом сервере (см. 11.08.2026)."""
    import time
    while True:
        time.sleep(120)
        try:
            from channels import listener
            with database.get_conn() as conn:
                rows = conn.execute(
                    "SELECT id, tg_user_id FROM contacts WHERE hot_since IS NOT NULL "
                    "AND hot_since <= datetime('now', ?) AND tg_user_id IS NOT NULL",
                    (f"-{HOT_LEAD_TIMEOUT_MIN} minutes",),
                ).fetchall()
            for r in rows:
                with database.get_conn() as conn:
                    acc = conn.execute(
                        "SELECT account_id FROM messages WHERE contact_id=? AND direction='out' "
                        "AND account_id IS NOT NULL ORDER BY id DESC LIMIT 1", (r["id"],)
                    ).fetchone()
                if not acc:
                    continue
                text = "спасибо, до связи)"
                if listener.send_via_listener(acc["account_id"], int(r["tg_user_id"]), [text]):
                    with database.get_conn() as conn:
                        database.add_message(conn, r["id"], "out", text, intent=None,
                                             account_id=acc["account_id"])
                        conn.execute("UPDATE contacts SET hot_since=NULL WHERE id=?", (r["id"],))
                    print(f"[hot] contact {r['id']}: {HOT_LEAD_TIMEOUT_MIN} мин тишины — закрыл мягко")
        except Exception as e:  # noqa: BLE001 — фоновый тик не должен ронять пульт
            print(f"[hot lead scheduler] {e}")


@app.on_event("startup")
def _start_scheduler() -> None:
    import threading
    database.init_db()
    _startup_account_report()   # быстрая сводка готовности → колокольчик (до запуска прогрева)
    threading.Thread(target=_proxy_scheduler, daemon=True).start()
    threading.Thread(target=_opener_queue_scheduler, daemon=True).start()
    threading.Thread(target=_night_reply_scheduler, daemon=True).start()
    threading.Thread(target=_meetings_scheduler, daemon=True).start()
    threading.Thread(target=_hot_lead_scheduler, daemon=True).start()
    # многоаккаунтный слушатель входящих: держит подключёнными все боевые/прогреваемые
    # аккаунты и пишет ответы клиентов в «Диалоги» (авто-ответ — только с активных).
    try:
        from channels.listener import start_in_thread
        start_in_thread()
    except Exception as e:  # noqa: BLE001
        print(f"[listener] не удалось запустить слушатель: {e}")


@app.on_event("shutdown")
def _stop_listener() -> None:
    """Попрощаться с Telegram до того, как процесс умрёт.

    Без этого рестарт сервиса рвал сокеты молча: Telegram ещё держал сессию активной,
    новый процесс подключался (возможно, уже через другой прокси после фейловера) — и
    ключ уходил в эфир с двух IP. Ответ Telegram — AuthKeyDuplicatedError, сессия сгорает
    безвозвратно. Так потеряли #17 и #9320 за день из 12 перезапусков.
    """
    try:
        from channels import listener
        listener.shutdown()
    except Exception as e:  # noqa: BLE001
        print(f"[listener] штатная остановка не удалась: {e}")


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
            enabled = database.get_setting(conn, "listener_enabled", "on") != "off"
            niches = conn.execute("SELECT COUNT(*) c FROM niches WHERE active=1").fetchone()["c"]
        # «жив ли поток»: enabled берётся из БД, а listening — из памяти. Если _supervise
        # упал, пульт продолжал бы показывать бодрый последний снимок. Круг реже RECHECK*3 —
        # значит поток мёртв и нужен рестарт сервера.
        tick = listener.STATUS.get("tick")
        alive = None
        if tick:
            import datetime as _dt
            age = (_dt.datetime.now() - _dt.datetime.fromisoformat(tick)).total_seconds()
            alive = age < listener.RECHECK_SEC * 3
        return JSONResponse({"started": listener.STATUS.get("started"),
                             "listening": sum(1 for a in accs if a["ok"]),
                             "accounts": accs, "auto_reply": auto_reply, "enabled": enabled,
                             "tick": tick, "thread_alive": alive,
                             "hits": listener.STATUS.get("hits", 0), "niches": niches})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=200)


@app.post("/api/listener/toggle")
def listener_toggle(payload: dict = Body(...)) -> JSONResponse:
    """Стоп/Пуск слушателя. Слушатель сам подхватит флаг в течение POLL_SEC и либо
    отключит все аккаунты, либо подключит их заново — процесс перезапускать не нужно."""
    on = "on" if payload.get("enabled") else "off"
    with database.get_conn() as conn:
        database.set_setting(conn, "listener_enabled", on)
    return JSONResponse({"ok": True, "enabled": on == "on"})


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
        # account_id и метка аккаунта — чтобы в ленте было видно, КТО сделал (раньше
        # поле молча терялось, и «вступил/отправил» выглядело как действие ниоткуда).
        evs = conn.execute(
            "SELECT e.id, e.type, e.level, e.title, e.text, e.contact_id, e.campaign_id, "
            "e.account_id, e.ts, a.label AS account "
            "FROM events e LEFT JOIN accounts a ON a.id=e.account_id "
            "ORDER BY e.id DESC LIMIT 40"
        ).fetchall()
    # Входящее пишется в базу ДВАЖДЫ: строкой в messages и событием 'reply'
    # (см. channels/telegram._record_incoming) — и лента показывала каждый ответ
    # парой одинаковых строк подряд. Сырое сообщение прячем: событие несёт то же
    # самое плюс аккаунт и «глазок» с полной карточкой.
    from datetime import datetime as _dt

    def _sec(ts: str | None):
        for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return _dt.strptime((ts or "")[:19], f)
            except ValueError:
                continue
        return None

    replies = [(e["contact_id"], _sec(e["ts"])) for e in evs if e["type"] == "reply"]

    def _dup(m) -> bool:
        t = _sec(m["ts"])
        return any(cid == m["contact_id"] and t and ets and abs((t - ets).total_seconds()) <= 5
                   for cid, ets in replies)

    items = [{"type": "msg", "text": m["text"], "who": m["who"], "ts": m["ts"],
              "contact_id": m["contact_id"]} for m in msgs if not _dup(m)]
    items += [{"type": "meeting", "text": "назначена встреча", "who": m["who"],
               "ts": m["meeting_at"], "contact_id": m["contact_id"]} for m in meets]
    items += [{"type": "event", "id": e["id"], "event_type": e["type"], "level": e["level"],
               "title": e["title"], "text": e["text"], "contact_id": e["contact_id"],
               "campaign_id": e["campaign_id"], "account_id": e["account_id"],
               "account": e["account"], "ts": e["ts"]} for e in evs]
    items.sort(key=lambda x: x["ts"] or "", reverse=True)
    return JSONResponse({"items": items})


@app.get("/api/event/{eid}")
def event_detail(eid: int) -> JSONResponse:
    """Полная карточка события для «глазка» в колокольчике.

    В ленте строка обрезана до одной строки и не отвечает на главные вопросы: КТО это
    сделал, КОМУ и ЧТО именно ушло. Здесь собираем полный текст плюс контекст вокруг:
    аккаунт-исполнитель, контакт, кампания, реальная переписка и что этот аккаунт
    делал рядом по времени (вступления, отправки).
    """
    database.init_db()
    with database.get_conn() as conn:
        e = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not e:
            return JSONResponse({"error": "событие не найдено"}, status_code=404)
        e = dict(e)
        out: dict = {"id": e["id"], "type": e["type"], "level": e["level"],
                     "title": e["title"], "text": e["text"], "ts": e["ts"]}

        if e.get("account_id"):
            a = conn.execute("SELECT id, label, phone, username, tg_name, status, country "
                             "FROM accounts WHERE id=?", (e["account_id"],)).fetchone()
            out["account"] = dict(a) if a else {"id": e["account_id"], "label": "аккаунт удалён"}
        if e.get("campaign_id"):
            c = conn.execute("SELECT id, name, product, status FROM campaigns WHERE id=?",
                             (e["campaign_id"],)).fetchone()
            out["campaign"] = dict(c) if c else None
        if e.get("contact_id"):
            c = conn.execute(
                "SELECT id, name, person_name, username, phone, agency, city, status, tags, "
                "segment, niche, offer FROM contacts WHERE id=?", (e["contact_id"],)).fetchone()
            out["contact"] = dict(c) if c else None
            # ЧТО именно ушло человеку и что он ответил — с указанием аккаунта-отправителя
            out["messages"] = [dict(m) for m in conn.execute(
                "SELECT m.direction, m.text, m.ts, a.label AS account "
                "FROM messages m LEFT JOIN accounts a ON a.id=m.account_id "
                "WHERE m.contact_id=? ORDER BY m.id DESC LIMIT 12", (e["contact_id"],))][::-1]

        # Чем занимался этот аккаунт вокруг события: куда вступил и кому отправил.
        if e.get("account_id"):
            out["joined"] = [dict(r) for r in conn.execute(
                "SELECT ch.title, ch.username, ac.joined_at "
                "FROM account_chats ac JOIN chats ch ON ch.id=ac.chat_id "
                "WHERE ac.account_id=? ORDER BY ac.joined_at DESC LIMIT 10",
                (e["account_id"],))]
            out["sent"] = [dict(r) for r in conn.execute(
                "SELECT l.status, l.detail, l.ts, COALESCE(c.person_name, c.name) AS who, "
                "c.id AS contact_id FROM campaign_logs l "
                "LEFT JOIN contacts c ON c.id=l.contact_id "
                "WHERE l.account_id=? ORDER BY l.id DESC LIMIT 10", (e["account_id"],))]
        elif e.get("campaign_id"):
            out["sent"] = [dict(r) for r in conn.execute(
                "SELECT l.status, l.detail, l.ts, COALESCE(c.person_name, c.name) AS who, "
                "c.id AS contact_id, a.label AS account FROM campaign_logs l "
                "LEFT JOIN contacts c ON c.id=l.contact_id "
                "LEFT JOIN accounts a ON a.id=l.account_id "
                "WHERE l.campaign_id=? ORDER BY l.id DESC LIMIT 10", (e["campaign_id"],))]
    return JSONResponse(out)


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
        # database.get_history() отдаёт только direction/text/intent/ts — этого хватает
        # агенту (ему НЕ нужно знать, какой именно аккаунт писал), но не оператору в
        # «Диалогах»: он смотрит на переписку и не может понять, с какого номера ушло
        # каждое сообщение, если по контакту работало несколько аккаунтов команды.
        # Отдельный запрос здесь, а не правка get_history — её читает и agent/agent.py.
        history = [dict(m) for m in conn.execute(
            "SELECT m.direction, m.text, m.intent, m.ts, m.account_id, "
            "COALESCE(a.label, a.username, a.phone) AS account_label "
            "FROM messages m LEFT JOIN accounts a ON a.id = m.account_id "
            "WHERE m.contact_id = ? ORDER BY m.id", (contact_id,)).fetchall()]
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


@app.post("/api/pipeline/{pid}/update")
def pipelines_update(pid: int, payload: dict = Body(...)) -> JSONResponse:
    """Переименовать воронку и/или переписать её этапы.

    Этапы задавались только при создании: переименовать, добавить или убрать шаг
    было нельзя — приходилось заводить воронку заново и терять привязанные сделки.

    Этап, в котором ещё стоят сделки, молча не удаляем: сначала показываем, сколько
    их и где они, — иначе сделки провалились бы в несуществующую стадию и пропали
    из доски. С `force` переносим такие сделки в первый оставшийся этап.
    """
    with database.get_conn() as conn:
        row = conn.execute("SELECT stages FROM pipelines WHERE id=?", (pid,)).fetchone()
        if not row:
            return JSONResponse({"error": "воронка не найдена"}, status_code=404)
        try:
            old = json.loads(row["stages"] or "[]")
        except Exception:  # noqa: BLE001
            old = []
        sets, vals = [], []
        if payload.get("name"):
            sets.append("name=?"); vals.append(payload["name"].strip())
        if payload.get("product") is not None:
            sets.append("product=?"); vals.append(payload.get("product") or None)

        stages = payload.get("stages")
        if stages is not None:
            clean = []
            seen = set()
            for s in stages:
                key = (s.get("key") or "").strip()
                label = (s.get("label") or "").strip()
                if not key or not label or key in seen:
                    continue
                seen.add(key)
                clean.append({"key": key, "label": label})
            if not clean:
                return JSONResponse({"error": "нужен хотя бы один этап"}, status_code=400)
            gone = [s for s in old if s.get("key") not in seen]
            if gone:
                qs = ",".join("?" for _ in gone)
                busy = conn.execute(
                    f"SELECT stage, COUNT(*) c FROM deals WHERE COALESCE(pipeline_id,"
                    f"(SELECT id FROM pipelines WHERE is_default=1))=? AND stage IN ({qs}) "
                    f"GROUP BY stage", (pid, *[s["key"] for s in gone])).fetchall()
                busy = [dict(b) for b in busy if b["c"]]
                if busy and not payload.get("force"):
                    names = {s["key"]: s.get("label", s["key"]) for s in old}
                    return JSONResponse({
                        "needs_confirm": True,
                        "warn": "В удаляемых этапах ещё есть сделки:\n"
                                + "\n".join(f"• {names.get(b['stage'], b['stage'])}: {b['c']}"
                                            for b in busy)
                                + f"\n\nОни переедут в «{clean[0]['label']}». Продолжить?"}, status_code=200)
                if busy:
                    conn.execute(
                        f"UPDATE deals SET stage=? WHERE COALESCE(pipeline_id,"
                        f"(SELECT id FROM pipelines WHERE is_default=1))=? AND stage IN ({qs})",
                        (clean[0]["key"], pid, *[s["key"] for s in gone]))
            sets.append("stages=?"); vals.append(json.dumps(clean, ensure_ascii=False))

        if not sets:
            return JSONResponse({"ok": True})
        vals.append(pid)
        conn.execute(f"UPDATE pipelines SET {', '.join(sets)} WHERE id=?", vals)
    return JSONResponse({"ok": True})


def _bulk_ids(payload: dict) -> list[int]:
    # мусор в списке пропускаем молча: один кривой id из фронта не должен ронять
    # удаление всей пачки пятисоткой, из которой в браузере видно только «SyntaxError»
    out: list[int] = []
    for x in (payload.get("ids") or []):
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


@app.post("/api/deals/bulk-delete")
def deals_bulk_delete(payload: dict = Body(...)) -> JSONResponse:
    """Удалить выбранные сделки. Контакты и переписка не трогаются — уходит только
    карточка сделки, чтобы вычистить воронку от мусора."""
    ids = _bulk_ids(payload)
    if not ids:
        return JSONResponse({"error": "не выбрано ни одной сделки"}, status_code=400)
    qs = ",".join("?" for _ in ids)
    with database.get_conn() as conn:
        n = conn.execute(f"SELECT COUNT(*) c FROM deals WHERE id IN ({qs})", ids).fetchone()["c"]
        conn.execute(f"DELETE FROM deals WHERE id IN ({qs})", ids)
    return JSONResponse({"ok": True, "deleted": n})


@app.post("/api/contacts/bulk-delete")
def contacts_bulk_delete(payload: dict = Body(...)) -> JSONResponse:
    """Удалить выбранные контакты вместе с их перепиской и сделками.

    Переписку удаляем осознанно: висящие сообщения без карточки нигде не видны и
    только мешают счётчикам. Если по контакту есть сделка — предупреждаем: это уже
    не мусор из импорта, а работа, и снести её случайно обиднее всего.
    """
    ids = _bulk_ids(payload)
    if not ids:
        return JSONResponse({"error": "не выбрано ни одного контакта"}, status_code=400)
    qs = ",".join("?" for _ in ids)
    with database.get_conn() as conn:
        with_deals = conn.execute(
            f"SELECT COUNT(*) c FROM deals WHERE contact_id IN ({qs})", ids).fetchone()["c"]
        if with_deals and not payload.get("force"):
            return JSONResponse({"needs_confirm": True,
                                 "warn": f"По выбранным контактам есть сделок: {with_deals}. "
                                         f"Они удалятся вместе с контактами. Продолжить?"})
        n = conn.execute(f"SELECT COUNT(*) c FROM contacts WHERE id IN ({qs})", ids).fetchone()["c"]
        conn.execute(f"DELETE FROM deals WHERE contact_id IN ({qs})", ids)
        conn.execute(f"DELETE FROM messages WHERE contact_id IN ({qs})", ids)
        conn.execute(f"DELETE FROM campaign_contacts WHERE contact_id IN ({qs})", ids)
        conn.execute(f"DELETE FROM opener_queue WHERE contact_id IN ({qs})", ids)
        # Хвосты, которые тоже держат contact_id. Забытая campaign_paused_contacts —
        # не косметика: строка «этот контакт в кампании на паузе» переживала удаление,
        # и следующий контакт, получивший тот же id из AUTOINCREMENT, молча выпадал
        # из рассылки. Остальное — журналы и распарсенные посты, без карточки мусор.
        for t in ("campaign_paused_contacts", "campaign_logs", "inbox_items", "tg_user_posts"):
            conn.execute(f"DELETE FROM {t} WHERE contact_id IN ({qs})", ids)
        # события — журнал, его не переписываем: просто отвязываем от удалённого
        conn.execute(f"UPDATE events SET contact_id=NULL WHERE contact_id IN ({qs})", ids)
        conn.execute(f"UPDATE chat_hits SET contact_id=NULL, status='new' "
                     f"WHERE contact_id IN ({qs})", ids)
        conn.execute(f"DELETE FROM contacts WHERE id IN ({qs})", ids)
    return JSONResponse({"ok": True, "deleted": n})


@app.post("/api/companies/bulk-delete")
def companies_bulk_delete(payload: dict = Body(...)) -> JSONResponse:
    """Удалить компании. Контакты не удаляем — только отвязываем: человек остаётся
    в базе, даже если юрлицо оказалось мусором из импорта."""
    ids = _bulk_ids(payload)
    if not ids:
        return JSONResponse({"error": "не выбрано ни одной компании"}, status_code=400)
    qs = ",".join("?" for _ in ids)
    with database.get_conn() as conn:
        n = conn.execute(f"SELECT COUNT(*) c FROM companies WHERE id IN ({qs})", ids).fetchone()["c"]
        freed = conn.execute(f"SELECT COUNT(*) c FROM contacts WHERE company_id IN ({qs})",
                             ids).fetchone()["c"]
        conn.execute(f"UPDATE contacts SET company_id=NULL WHERE company_id IN ({qs})", ids)
        conn.execute(f"UPDATE deals SET company_id=NULL WHERE company_id IN ({qs})", ids)
        conn.execute(f"DELETE FROM companies WHERE id IN ({qs})", ids)
    return JSONResponse({"ok": True, "deleted": n, "contacts_kept": freed})


@app.get("/api/chatscan/status")
def chatscan_status() -> JSONResponse:
    """Отдельный тумблер сканирования ЧАТОВ по ключам.

    Слушатель делает два разных дела: ловит ответы клиентов в личке (переписка
    кампании) и ищет ключи в чатах (лидген). Раньше их глушил один выключатель, и
    остановка мониторинга чатов заодно обрывала ответы живых людей."""
    database.init_db()
    with database.get_conn() as conn:
        return JSONResponse({
            "enabled": database.get_setting(conn, "chatscan_enabled", "on") != "off"})


@app.post("/api/chatscan/toggle")
def chatscan_toggle(payload: dict = Body(...)) -> JSONResponse:
    with database.get_conn() as conn:
        database.set_setting(conn, "chatscan_enabled",
                             "on" if payload.get("enabled") else "off")
    return JSONResponse({"ok": True, "enabled": bool(payload.get("enabled"))})


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
def deals_list(pipeline_id: int | None = None, archived: int = 0) -> JSONResponse:
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
                 AND COALESCE(d.archived,0)=?
               ORDER BY d.updated_at DESC, d.id DESC""",
            (pid, pid, database.get_default_pipeline_id(conn), 1 if archived else 0),
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


@app.get("/api/today")
def today_tasks() -> JSONResponse:
    """Задачи на сегодня: кому напомнить о встрече, кого дожать, кто не дошёл.

    Ядро (scheduler.collect_due) считало это давно, но результат жил только внутри
    фонового цикла: увидеть «что система собирается сделать за меня» было негде.
    Здесь тот же расчёт показывается человеку — с готовым текстом, который уйдёт,
    и ссылкой на карточку, чтобы можно было вмешаться до отправки.

    Только чтение: ничего не отправляет и не помечает. Отправкой занимается
    планировщик в channels/telegram.run_loop()."""
    from scheduler import collect_due
    database.init_db()
    with database.get_conn() as conn:
        actions = collect_due(conn)
        out = []
        for a in actions:
            row = conn.execute(
                "SELECT COALESCE(person_name, name) AS who, username, phone, status, "
                "(SELECT MAX(ts) FROM messages m WHERE m.contact_id=contacts.id) AS last_ts "
                "FROM contacts WHERE id=?", (a.contact_id,)).fetchone()
            out.append({
                "kind": a.kind,                       # reminder | followup | noshow
                "contact_id": a.contact_id,
                "who": (row["who"] if row else None) or a.name or f"#{a.contact_id}",
                "username": row["username"] if row else None,
                "status": row["status"] if row else None,
                "last_ts": row["last_ts"] if row else None,
                "text": a.text,                       # что именно уйдёт человеку
                "followup_n": getattr(a, "followup_n", None),
                "deal_id": getattr(a, "deal_id", None),
                # Без tg_user_id планировщик отправить не сможет — честно помечаем,
                # иначе задача вечно висит в списке и выглядит как зависшая.
                "sendable": bool(a.tg_user_id),
            })
    order = {"reminder": 0, "noshow": 1, "followup": 2}   # срочное выше
    out.sort(key=lambda x: (order.get(x["kind"], 9), x["who"] or ""))
    return JSONResponse({
        "items": out,
        "counts": {k: sum(1 for x in out if x["kind"] == k)
                   for k in ("reminder", "noshow", "followup")},
        "unsendable": sum(1 for x in out if not x["sendable"]),
    })


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


@app.post("/api/deal/{did}/archive")
def deal_archive(did: int, payload: dict = Body(default={})) -> JSONResponse:
    """Убрать сделку из воронки не удаляя — мусорная карточка не должна виснуть
    в колонке навсегда, но и восстановимость (в отличие от delete) не теряем."""
    archived = 1 if payload.get("archived", True) else 0
    with database.get_conn() as conn:
        conn.execute("UPDATE deals SET archived=?, updated_at=datetime('now') WHERE id=?", (archived, did))
    return JSONResponse({"ok": True, "archived": bool(archived)})


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
    """Пробив номеров контактов в Telegram (phone_resolve) — узнать tg_user_id, username, аватар, bio.

    tag — сузить до аудитории одной кампании; без него порядок ORDER BY id съедает
    дневной потолок на самых старых контактах в базе, и свежая кампания ждёт очереди."""
    limit = int(payload.get("limit") or 100)
    tag = (payload.get("tag") or "").strip()
    args = ["channels.phone_resolve", "--limit", str(limit)]
    if tag:
        args += ["--tag", tag]
    _spawn(*args)
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
                "niche", "offer", "web_note",
            ) if k in keys}
            # прямую ссылку на канал из lookup кладём в dossier для карточки
            if res.get("channel_link"):
                res["dossier"]["channel_link"] = res["channel_link"]
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
            "SELECT c.*, (SELECT COUNT(*) FROM chat_admins a WHERE a.chat_id=c.id) AS admins_count, "
            # откуда чат приехал: «похож на Хартманна» должно читаться в карточке,
            # а не восстанавливаться из прозы в notes
            "(SELECT p.title FROM chats p WHERE p.id=c.parent_chat_id) AS parent_title, "
            "(SELECT COUNT(*) FROM channel_posts cp WHERE cp.chat_id=c.id) AS posts_count "
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


@app.get("/api/chatcat/quality")
def chatcat_quality_status() -> JSONResponse:
    """Сколько чатов разобрано и какой расклад по вердиктам."""
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT COALESCE(verdict,'—') v, COALESCE(verdict_src,'') src, COUNT(*) c "
            "FROM chats GROUP BY v, src").fetchall()
        left = conn.execute(
            "SELECT COUNT(*) c FROM chats WHERE COALESCE(verdict,'')='' "
            "AND ((username IS NOT NULL AND username<>'') "
            "OR (in_account='yes' AND tg_chat_id IS NOT NULL))").fetchone()["c"]
    by: dict = {}
    for r in rows:
        by.setdefault(r["v"], {"ai": 0, "человек": 0})
        by[r["v"]][r["src"] if r["src"] in ("ai", "человек") else "ai"] += r["c"]
    return JSONResponse({"left": left, "by_verdict": by})


@app.post("/api/chatcat/quality")
def chatcat_quality_run(payload: dict = Body(default={})) -> JSONResponse:
    """Разобрать порцию чатов: прочитать выборку сообщений и поставить
    предварительный вердикт (channels/chat_quality). Решения человека не трогает."""
    limit = max(1, min(int(payload.get("limit") or 50), 300))
    args = ["channels.chat_quality", "--all", "--limit", str(limit)]
    if payload.get("only_new", True):
        args.append("--only-new")
    res = _run_capture(args, timeout=1800)
    data = _last_json(res.get("output"))
    if data is None:
        tail = (res.get("output") or "").strip()[-400:]
        return JSONResponse({"ok": False, "error": "разбор не отчитался. Лог: " + (tail or "(пусто)")})
    data["log"] = (res.get("output") or "").splitlines()[-25:]
    return JSONResponse(data)


@app.get("/api/chatcat/backfill")
def chatcat_backfill_status() -> JSONResponse:
    """Сколько чатов ещё без tg_chat_id + настройки авто-дозаполнения.

    Без tg_chat_id рвётся связка «в каком чате найден человек» (досье джойнит
    tg_user_posts.chat_id → chats.tg_chat_id), и в карточке лида пропадает источник."""
    database.init_db()
    with database.get_conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) c FROM chats WHERE tg_chat_id IS NULL "
            "AND username IS NOT NULL AND username<>''").fetchone()["c"]
        done = conn.execute("SELECT COUNT(*) c FROM chats WHERE tg_chat_id IS NOT NULL").fetchone()["c"]
        return JSONResponse({
            "left": left, "done": done,
            "auto": database.get_setting(conn, "backfill_auto", "off") == "on",
            "interval_h": int(database.get_setting(conn, "backfill_interval_min", "360")) // 60,
            "limit": int(database.get_setting(conn, "backfill_limit", "100")),
            "last_run": database.get_setting(conn, "backfill_last_run", None),
        })


@app.post("/api/chatcat/backfill")
def chatcat_backfill_set(payload: dict = Body(default={})) -> JSONResponse:
    """Включить/настроить авто-дозаполнение. run=true — прогнать одну порцию сейчас."""
    with database.get_conn() as conn:
        if "auto" in payload:
            database.set_setting(conn, "backfill_auto", "on" if payload.get("auto") else "off")
        if payload.get("interval_h"):
            database.set_setting(conn, "backfill_interval_min",
                                 str(max(1, int(payload["interval_h"])) * 60))
        if payload.get("limit"):
            database.set_setting(conn, "backfill_limit",
                                 str(max(10, min(int(payload["limit"]), 500))))
    if payload.get("run"):
        return JSONResponse(_run_capture(
            ["channels.backfill", "--chats", "--limit", str(payload.get("limit") or 100)],
            timeout=1800))
    return JSONResponse({"ok": True})


@app.post("/api/chatcat/tgstat")
def chatcat_tgstat(payload: dict = Body(...)) -> JSONResponse:
    """Пополнить каталог из TGStat по фильтрам (категория/страна/размер).

    ВАЖНО: объявлен ДО /api/chatcat/{chat_id} — иначе FastAPI попытается привести
    «tgstat» к int (та же ловушка, что со scan_progress выше)."""
    q = (payload.get("q") or "").strip()
    if not q:
        return JSONResponse({"error": "нужен поисковый запрос"}, status_code=400)
    args = ["channels.tgstat", "--search", q, "--save",
            "--limit", str(max(1, min(int(payload.get("limit") or 100), 500))),
            "--country", (payload.get("country") or "ru")]
    if payload.get("category"):
        args += ["--category", str(payload["category"])]
    if payload.get("min_members"):
        args += ["--min-members", str(int(payload["min_members"]))]
    if payload.get("groups_only"):
        args.append("--groups-only")
    elif payload.get("channels_only"):
        args.append("--channels-only")
    return JSONResponse(_run_capture(args, timeout=300))


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


@app.get("/api/chatcat/{chat_id}/report")
def chatcat_report(chat_id: int, days: int = 30) -> JSONResponse:
    """Отчёт по каналу: объём, ритм, охват, о чём пишут, что зашло.

    Только чтение базы — Telegram не трогаем. Поэтому периоды можно перебирать
    сколько угодно: сбор постов делается отдельной кнопкой (см. /report/collect)."""
    from channels.channel_report import analyze
    return JSONResponse(analyze(chat_id, max(1, min(int(days or 30), 365))))


@app.post("/api/chatcat/{chat_id}/report/collect")
def chatcat_report_collect(chat_id: int, payload: dict = Body(default={})) -> JSONResponse:
    """Сходить в Telegram за свежими постами канала (долго — отдельным процессом)."""
    days = max(1, min(int(payload.get("days") or 30), 365))
    limit = max(10, min(int(payload.get("limit") or 500), 3000))
    with database.get_conn() as conn:
        row = conn.execute("SELECT title, username, tg_chat_id FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "чат не найден"}, status_code=404)
    if not row["username"] and not row["tg_chat_id"]:
        return JSONResponse({"error": "у чата нет ни @username, ни telegram-id — "
                                      "нечем адресовать. Сначала «Просканировать»."},
                            status_code=400)
    res = _run_capture(["channels.channel_report", "--chat", str(chat_id),
                        "--collect", "--days", str(days), "--limit", str(limit)],
                       timeout=600)
    # Модуль печатает сводку JSON последней строкой. Без разбора наружу уходило
    # только {ok, output}, и фронт показывал «готово» даже когда сбор упал (нет
    # сессии у аккаунта прослушки, канал недоступен, FloodWait) — кнопка молчала,
    # а постов не прибавлялось. Отдаём разобранный результат и причину.
    data = _last_json(res.get("output"))
    if data is None:
        tail = (res.get("output") or "").strip()[-400:]
        return JSONResponse({"ok": False,
                             "error": "модуль не вернул результат. Лог: " + (tail or "(пусто)")},
                            status_code=200)
    if not data.get("ok"):
        data.setdefault("error", "сбор не удался")
    return JSONResponse(data)


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
                         "pending": info.get("pending"),
                         "accounts": info.get("accounts"), "report": info.get("report"),
                         "daily_cap": info.get("daily_cap"), "capped": info.get("capped") or [],
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
    """Ниши + КАЧЕСТВО их улова.

    Ниша, набранная из названий услуг («нужен сайт», «разработка лендинга»), ловит
    не заказчиков, а конкурентов: этими же словами исполнитель описывает себя в
    рекламе. На живой базе это дало 275 конкурентов на 3 клиента — и понять причину
    по интерфейсу было нельзя, находки просто «не появлялись» в фильтре «клиенты».
    Теперь доля видна прямо на карточке ниши, а при явном перекосе пульт говорит,
    что менять."""
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM niches ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            st = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN intent='client' THEN 1 ELSE 0 END) AS client, "
                "SUM(CASE WHEN intent='vendor' THEN 1 ELSE 0 END) AS vendor "
                "FROM chat_hits WHERE niche_id=?", (r["id"],)
            ).fetchone()
            total = st["total"] or 0
            client = st["client"] or 0
            d["hits"] = {"total": total, "client": client, "vendor": st["vendor"] or 0,
                         "client_share": round(100.0 * client / total, 1) if total else None}
            # Совет даём только когда выборки хватает, чтобы не пугать на пяти находках.
            d["advice"] = ("Ключи ловят рекламу, а не заказчиков: почти все находки — "
                           "конкуренты. Так бывает, когда ключи это названия услуг "
                           "(«нужен сайт», «разработка бота») — ими исполнитель "
                           "описывает себя. Замени на то, КАК просит клиент: "
                           "«посоветуйте», «кто может сделать», «ищу подрядчика»."
                           ) if total >= 30 and client / total < 0.05 else None
            out.append(d)
    return JSONResponse(out)


@app.post("/api/niches")
def niche_create(payload: dict = Body(...)) -> JSONResponse:
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "нужно название ниши"}, status_code=400)
    with database.get_conn() as conn:
        # hunt_mode — кого эта ниша складывает в «Запросы»: clients (по умолчанию),
        # vendors (изучать рынок/продавать исполнителям) или all.
        mode = (payload.get("hunt_mode") or "clients").strip()
        if mode not in ("clients", "vendors", "all"):
            mode = "clients"
        cur = conn.execute(
            "INSERT INTO niches (name, keywords, active, hunt_mode) VALUES (?,?,1,?)",
            (name, payload.get("keywords") or "", mode))
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/niche/{nid}/update")
def niche_update(nid: int, payload: dict = Body(...)) -> JSONResponse:
    sets, vals = [], []
    for k in ("name", "keywords", "active", "hunt_mode"):
        if k in payload:
            if k == "active":
                v = int(bool(payload[k]))
            elif k == "hunt_mode":
                v = (payload.get(k) or "clients").strip()
                if v not in ("clients", "vendors", "all"):
                    v = "clients"
            else:
                v = payload.get(k) or ""
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
def hits_list(status: str = "new", intent: str = "") -> JSONResponse:
    """Находки по ключам. intent — кого показывать: client (ищет услугу),
    vendor (сам предлагает), unknown (не разобрали), пусто — всех.

    Классификация лежит в chat_hits.intent, её ставит channels/hit_intent при записи.
    У находок, сделанных до появления фильтра, поле пустое — они попадают в «не
    разобрано», а не теряются."""
    database.init_db()
    where, args = "h.status=?", [status]
    if intent == "unknown":
        where += " AND COALESCE(h.intent,'unknown')='unknown'"
    elif intent in ("client", "vendor"):
        where += " AND h.intent=?"
        args.append(intent)
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT h.*, n.name AS niche_name, c.username AS chat_username, c.link AS chat_link "
            "FROM chat_hits h "
            "LEFT JOIN niches n ON n.id=h.niche_id "
            "LEFT JOIN chats c ON c.id=h.chat_id "
            f"WHERE {where} ORDER BY h.id DESC LIMIT 500", args
        ).fetchall()
        counts = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM chat_hits GROUP BY status")}
        # счётчики по типу — для вкладок «клиенты / конкуренты / не разобрано»
        by_intent = {r["i"]: r["c"] for r in conn.execute(
            "SELECT COALESCE(intent,'unknown') i, COUNT(*) c FROM chat_hits "
            "WHERE status=? GROUP BY i", (status,))}
    items = []
    for r in rows:
        d = dict(r)
        # ссылка прямо на сообщение в чате (перейти, увидеть контекст, продолжить переписку)
        if d.get("chat_username") and d.get("source_msg_id"):
            d["msg_link"] = f"https://t.me/{d['chat_username']}/{d['source_msg_id']}"
        else:
            d["msg_link"] = d.get("chat_link") or None    # приватный чат — хотя бы ссылка на сам чат
        items.append(d)
    return JSONResponse({"items": items, "counts": counts, "by_intent": by_intent})


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
            # chat_hits.chat_id — КАТАЛОЖНЫЙ chats.id, а tg_user_posts.chat_id — СЫРОЙ
            # telegram-id (по нему джойнят chats.tg_chat_id, см. карточку досье и
            # agent/segment). Класть сюда каталожный — значит потерять «в каком чате
            # найден» у лида, заведённого из находки.
            raw = conn.execute("SELECT tg_chat_id FROM chats WHERE id=?", (h["chat_id"],)).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO tg_user_posts (tg_user_id, contact_id, chat_id, chat_title, "
                "text, msg_id, ts) VALUES (?,?,?,?,?,?,?)",
                (h["tg_user_id"], cid, (raw["tg_chat_id"] if raw else None), h["chat_title"],
                 h["text"], h["source_msg_id"], h["ts"]),
            )
    from agent import llm
    score = None
    # Почему скоринга нет — обязательно наружу. Раньше ошибка модели (протухший ключ,
    # лимит, нет сырья) уходила только в лог сервера, а оператор видел карточку с
    # прочерком и решал, что перенос не сработал вообще.
    score_error = None
    if not payload.get("auto_enrich", True):
        score_error = "скоринг пропущен (auto_enrich=false)"
    elif not llm.available(config.MODEL):
        score_error = f"нет рабочего ключа под модель {config.MODEL} — скоринг не считался"
    else:
        try:
            from agent.enrich_person import _posts_for, _save, enrich_person
            with database.get_conn() as conn:
                contact = dict(conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone())
                posts = _posts_for(conn, contact["tg_user_id"]) if contact.get("tg_user_id") else []
            if not posts:
                score_error = "нечего скорить: у контакта нет ни одного собранного сообщения"
            else:
                profile = enrich_person(contact, posts)
                _save(cid, profile)
                score = profile.score
        except Exception as e:  # noqa: BLE001 — скоринг best-effort, лид всё равно заведён
            print(f"[hit_to_lead enrich] contact {cid}: {e}")
            score_error = f"скоринг не посчитался: {str(e)[:160]}"
    return JSONResponse({"ok": True, "contact_id": cid, "score": score,
                         "score_error": score_error})


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


def _sniff_delimiter(text: str) -> str:
    """Разделитель CSV по строке заголовка. Русский Excel пишет «;», выгрузки из CRM и
    Google Sheets — «,», часть панелей — таб. Жёстко зашитая «;» роняла импорт любого
    файла с запятыми в «не нашёл колонку» ещё до разбора данных."""
    head = (text.splitlines() or [""])[0]
    return max((";", ",", "\t"), key=head.count) if head else ";"


def _parse_people(text: str, tag: str, source: str = "import") -> tuple[int, int]:
    """Обычный список ЛЮДЕЙ: Имя / Телефон / Telegram / Город / Чем занимается.

    Зачем отдельно от _parse_universal: тот заточен под выгрузки ЮРЛИЦ (требует колонку
    «Наименование», знает ИНН и ОГРН) и колонку с @username не понимает вовсе. Из-за
    этого простейший файл клиентов на «Имя;Телефон;Telegram» пульт отбивал ошибкой, а
    единственный импортёр, который его понимал, жил в консоли (importer/import_contacts)
    и до веба подключён не был.

    Заголовки берём из того же словаря ALIASES, что и консольный импорт, — один список
    синонимов на оба входа. Колонка со специализацией важна отдельно: на ней держится
    заход «вы тоже по этой теме? — тогда мы коллеги» ({спец} в шаблоне рассылки).
    """
    import csv
    import io
    from importer.import_contacts import ALIASES, normalize_phone, normalize_username

    reader = csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(text))
    rows = list(reader)
    if not rows:
        return 0, 0
    colmap = {i: ALIASES[h] for i, h in
              ((i, (h or "").strip().lower()) for i, h in enumerate(rows[0])) if h in ALIASES}
    if not colmap or not ({"phone", "username"} & set(colmap.values())):
        raise ValueError("не нашёл ни колонки с телефоном, ни с Telegram — "
                         "нужен заголовок вида «Телефон» / «Username» / «Телеграм»")

    added = skipped = 0
    database.init_db()
    with database.get_conn() as conn:
        for row in rows[1:]:
            r = {f: (row[i].strip() if i < len(row) and row[i] else "") for i, f in colmap.items()}
            phone = normalize_phone(r.get("phone", ""))
            username = normalize_username(r.get("username", ""))
            if not phone and not username:
                skipped += 1                  # строка без единого способа связи
                continue
            # Колонка «Имя» в списке людей часто держит полное ФИО. Кладём его ещё и в
            # person_name — тогда рассылка обращается «Пётр Сергеевич, добрый день»
            # (campaign_send._greeting), а не «Иванов Пётр Сергеевич», что читается
            # автоматикой из госуслуг с первой секунды.
            nm = (r.get("name") or "").strip()
            # Колонка «Описание» в ALIASES тоже ведёт в specialization, но в выгрузках
            # экспертов это простыня на 400–1000 знаков. В шаблоне {спец} подставляется
            # одной строкой («вы всё так же работаете как {спец}?»), поэтому длинный текст
            # уводим в niche, а короткий оставляем специализацией.
            spec, long_desc = _person_topics(
                r.get("specialization") or "", r.get("person_role") or "", "", "", "")
            if len(r.get("specialization") or "") > 120:
                spec, long_desc = _person_topics(
                    "", r.get("person_role") or "", r.get("specialization") or "", "", "")
            cid = database.upsert_contact(
                conn, source=source, phone=phone, username=username,
                name=nm or None, person_name=nm if len(nm.split()) == 3 else None,
                city=r.get("city") or None, agency=r.get("agency") or None,
                tags=", ".join(t for t in (tag, r.get("tags")) if t) or None,
                notes=r.get("notes") or None,
                specialization=spec, niche=long_desc,
                person_role=r.get("person_role") or None,
                email=r.get("email") or None,
                match_name=True,
            )
            # @username — это «достанем без ImportContacts» (см. TG_REACHABLE_SQL):
            # такой контакт идёт в рассылку сразу, без дозированного пробива номера.
            if username:
                conn.execute("UPDATE contacts SET has_tg='yes' WHERE id=? AND "
                             "COALESCE(has_tg,'unknown')='unknown'", (cid,))
            added += 1
    return added, skipped


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
    # Колонки «про человека». В компанию они не пишутся (co_vals собирается по отдельному
    # белому списку ниже) — нужны только для карточки контакта. Без них выгрузка экспертов
    # приезжала как «ФИО + телефон и всё»: у 216 контактов из vsetreningi специализация
    # пустая, и холодный заход «вы всё так же по теме {спец}?» разваливался на первом шаге.
    # Порядок важен: словарь обходится сверху вниз с проверкой «pat in h», поэтому
    # «должность» стоит ПОСЛЕ «должность руководителя», иначе перехватило бы её заголовок.
    "описание": "description",
    "навыки": "skills",
    "основные направления": "directions",
    "категории": "categories",
    "категория": "categories",
    "должность": "director_role",
    "специализация": "categories",
    "чем занимается": "description",
}

# «Все консультанты», «Все компании» и т.п. — это не тема эксперта, а название раздела
# каталога, откуда сделана выгрузка. В {спец} такое подставлять нельзя: получается
# «вы всё так же работаете как все консультанты?».
_JUNK_CATEGORIES = {"все консультанты", "все компании", "все тренеры", "все эксперты",
                    "все специалисты", "прочее", "другое", "-"}


def _person_topics(*values: str | None) -> tuple[str | None, str | None]:
    """Из колонок «Категории/Должность/Описание/Навыки» собирает пару
    (короткая специализация, развёрнутое описание).

    Разделение принципиальное. specialization уходит в шаблон одной строкой
    («вы всё так же работаете как {спец}?») — туда годится только короткая тема вроде
    «увеличение продаж». Полный текст «Описания» в выгрузках — медиана 450 символов,
    максимум под 10 000; подставленный в реплику, он превращает первое касание в простыню.
    Поэтому длинный текст кладём в niche — агент читает его как контекст, но в реплику
    целиком не тащит.
    """
    cats, role, desc, skills, dirs = ([v or "" for v in values] + [""] * 5)[:5]
    topics = [t.strip() for t in cats.replace("|", ";").split(";") if t.strip()]
    topics = [t for t in topics if t.lower() not in _JUNK_CATEGORIES]
    spec = ", ".join(topics[:2]) or (role or "").strip() or None
    if spec and len(spec) > 120:
        spec = spec[:117].rstrip(" ,.;") + "…"
    long = " · ".join(x.strip() for x in (desc, dirs, skills) if x and x.strip())
    # Неразрывный пробел из html-выгрузок ломает и поиск, и перенос строк в переписке.
    long = long.replace("\xa0", " ").replace("\r", " ").strip() or None
    if long and len(long) > 1200:
        long = long[:1197].rstrip() + "…"
    return spec, long

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
    reader = csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(text))
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
        if field in ("phone", "director_phone"):
            # Телефон приводим к +7XXXXXXXXXX. Без этого в книжку ложилось «8 (913) 000-11-22»
            # как есть: дедуп у нас идёт сравнением строк, и тот же человек из соседнего
            # файла («+7913…») заводил вторую карточку и получал второе первое сообщение.
            return norm_phone(v) or (v or None)
        if field in ("director_email", "email"):
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

    # Один и тот же номер у нескольких РАЗНЫХ людей — это не личный телефон, а номер
    # агентства/организатора, который каталог показывает во всех профилях сразу.
    # В выгрузке vsetreningi таких 12 номеров, за одним стоит до пяти «экспертов».
    # Брать его как контактный нельзя вдвойне: писать будем не тому человеку, а дедуп по
    # телефону (upsert_contact) склеит всех пятерых в одну карточку с чужим именем.
    _by_phone: dict[str, set] = {}
    for r in rows[1:]:
        p = norm(r, "director_phone") or norm(r, "phone")
        if p:
            _by_phone.setdefault(p, set()).add(cell(r, "name").strip().lower())
    shared_phones = {p for p, names in _by_phone.items() if len(names) > 1}

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
                spec, long_desc = _person_topics(
                    cell(row, "categories"), contact_role or "", cell(row, "description"),
                    cell(row, "skills"), cell(row, "directions"))
                pers_phone = norm(row, "director_phone") or norm(row, "phone")
                note = f"импорт из {source}"
                if pers_phone in shared_phones:
                    note += (f" · общий номер каталога {pers_phone} — в карточку не пишем, "
                             f"он указан ещё у других людей из этой же выгрузки")
                    pers_phone = None
                cid = database.upsert_contact(
                    conn, source=source, phone=pers_phone,
                    email=norm(row, "director_email") or norm(row, "email"),
                    name=contact_name, person_name=contact_name,
                    person_role=contact_role,
                    specialization=spec, niche=long_desc,
                    agency=cname, tags=tag or None,
                    notes=note,
                    # см. upsert_contact: файл людей — единственный случай, где имя можно
                    # считать ключом, иначе строка без телефона плодит по карточке за импорт
                    match_name=True,
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
    reader = csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(text))
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
    # Три формата подряд, от узкого к общему: выгрузка юрлиц (Наименование/ИНН) →
    # выгрузка 2ГИС → обычный список людей (Имя/Телефон/Telegram). Последний нужен чаще
    # всего и раньше не поддерживался вовсе: пульт отвечал «не нашёл колонку
    # «Наименование»» на файл, где её и не должно быть.
    errors = []
    added = skipped = None
    for parser in (_parse_universal, _parse_2gis, _parse_people):
        try:
            added, skipped = parser(text, tag_clean, src)
            break
        except ValueError as e:
            errors.append(str(e))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    if added is None:
        return JSONResponse({"error": "не понял формат файла. " + " / ".join(errors)},
                            status_code=400)
    with database.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]
        co_total = conn.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
    return JSONResponse({"ok": True, "imported": added, "skipped": skipped, "total": total, "companies": co_total})


# ---- Чаты ----------------------------------------------------------------- #
@app.get("/api/chats")
def chats() -> JSONResponse:
    """Список диалогов + аккаунт, с которого ведётся каждый.

    Аккаунт берём из ПОСЛЕДНЕГО сообщения (messages.account_id) — того же источника,
    что и подписи под пузырями в самой переписке (contact_detail). Раньше брали из
    campaign_contacts, и это было ненадёжно ДВОЯКО: такой записи вовсе нет, если
    контакт написал первым сам (не через рассылку), а «🔄 Обнулить тест» её ещё и
    удаляет намеренно. В обоих случаях аккаунт пропадал из фильтра «любой аккаунт» и
    из подписи в списке слева — оператор не мог понять, с какого номера идёт диалог,
    хотя сама переписка (и подписи в ней) были на месте."""
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
                   (SELECT COALESCE(a.label, a.username, a.phone) FROM messages m
                      LEFT JOIN accounts a ON a.id=m.account_id
                      WHERE m.contact_id=c.id AND m.account_id IS NOT NULL
                      ORDER BY m.id DESC LIMIT 1) AS account_label,
                   (SELECT m.account_id FROM messages m
                      WHERE m.contact_id=c.id AND m.account_id IS NOT NULL
                      ORDER BY m.id DESC LIMIT 1) AS account_id,
                   -- кампания: контакт мог участвовать в нескольких по очереди
                   -- (ретаргет), берём последнюю по sent_at — у campaign_contacts
                   -- нет автоинкрементного id, только UNIQUE(campaign_id, contact_id)
                   (SELECT cc.campaign_id FROM campaign_contacts cc
                      WHERE cc.contact_id=c.id ORDER BY cc.sent_at DESC LIMIT 1) AS campaign_id,
                   (SELECT camp.name FROM campaign_contacts cc
                      JOIN campaigns camp ON camp.id=cc.campaign_id
                      WHERE cc.contact_id=c.id ORDER BY cc.sent_at DESC LIMIT 1) AS campaign_name
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
def projects_list(archived: int = 0) -> JSONResponse:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            """SELECT p.*, co.name AS client_company_name, c.person_name AS client_contact_name,
                      c.name AS client_contact_display
               FROM projects p
               LEFT JOIN companies co ON co.id=p.client_company_id
               LEFT JOIN contacts c ON c.id=p.client_contact_id
               WHERE (?=1 AND p.status='archived') OR (?=0 AND IFNULL(p.status,'')<>'archived')
               ORDER BY p.id DESC""",
            (1 if archived else 0, 1 if archived else 0),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["campaigns"] = conn.execute("SELECT COUNT(*) c FROM campaigns WHERE project_id=?", (r["id"],)).fetchone()["c"]
            # клиент — компания ИЛИ человек, никогда оба: фронту проще один готовый ярлык
            d["client_name"] = d.get("client_company_name") or d.get("client_contact_name") or d.get("client_contact_display")
            out.append(d)
    return JSONResponse(out)


@app.post("/api/projects")
def projects_create(payload: dict = Body(...)) -> JSONResponse:
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "нужно название проекта"}, status_code=400)
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, entity, description, client_company_id, client_contact_id, "
            "goal, ideal_result, deadline) VALUES (?,?,?,?,?,?,?,?)",
            (name, payload.get("entity") or None, payload.get("description") or None,
             payload.get("client_company_id") or None, payload.get("client_contact_id") or None,
             payload.get("goal") or None, payload.get("ideal_result") or None,
             payload.get("deadline") or None),
        )
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@app.post("/api/project/{pid}/update")
def projects_update(pid: int, payload: dict = Body(...)) -> JSONResponse:
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET name=?, entity=?, description=?, status=?, client_company_id=?, "
            "client_contact_id=?, goal=?, ideal_result=?, deadline=? WHERE id=?",
            (payload.get("name"), payload.get("entity") or None, payload.get("description") or None,
             payload.get("status") or "active", payload.get("client_company_id") or None,
             payload.get("client_contact_id") or None, payload.get("goal") or None,
             payload.get("ideal_result") or None, payload.get("deadline") or None, pid),
        )
    return JSONResponse({"ok": True})


@app.post("/api/project/{pid}/archive")
def projects_archive(pid: int, payload: dict = Body(default={})) -> JSONResponse:
    """Убрать проект с глаз, не теряя кампании: список проектов копится, а старые
    направления бизнеса не удаляют — их закрывают, историю кампаний не рвём."""
    archived = payload.get("archived", True)
    with database.get_conn() as conn:
        conn.execute("UPDATE projects SET status=? WHERE id=?",
                     ("archived" if archived else "active", pid))
    return JSONResponse({"ok": True, "archived": bool(archived)})


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


def _audience_where(cid, tag, channel, channel_clause: str | None = None,
                    verified_only: bool = False) -> tuple[str, list]:
    """Условие «кто сейчас в очереди этой кампании» — ЕДИНСТВЕННЫЙ источник правды.

    Раньше те же условия дублировались в preflight, и копии разъехались: там tag шёл
    через .strip(), здесь — сырой, а `audience_tag` при сохранении не тримится. Хвостовой
    пробел в теге давал разные выборки, из-за чего «достижимо» оказывалось больше самой
    аудитории и предупреждение про ImportContacts пропадало. Считаем в одном месте.

    channel_clause — подменить канальное условие (preflight меряет ТОЛЬКО TG-достижимость).
    """
    # На паузе — не в счёт "в очереди": они пока не уйдут, пока не снимут паузу.
    where = ("status='new' AND (username IS NOT NULL OR phone IS NOT NULL) "
             "AND id NOT IN (SELECT contact_id FROM campaign_paused_contacts WHERE campaign_id=?)")
    params: list = [cid]
    cc = _channel_clause(channel) if channel_clause is None else channel_clause
    if cc:
        where += " AND " + cc
    # verified_only повторяет фильтр отправки (campaign_send._audience): когда защита от
    # бана включена, непробитые в очередь не идут, и счётчик аудитории обязан это учесть —
    # иначе карточка обещает 992, а уходит 170, и расхождение опять не объяснить.
    if verified_only and "telegram" in [c.strip() for c in (channel or "").split(",")]:
        where += " AND " + database.TG_REACHABLE_SQL
    tag = (tag or "").strip()
    if tag:
        where += " AND tags LIKE ?"
        params.append(f"%{tag}%")
    return where, params


def _audience_count(conn, cid, tag, channel, verified_only: bool = False) -> int:
    where, params = _audience_where(cid, tag, channel, verified_only=verified_only)
    return conn.execute(f"SELECT COUNT(*) c FROM contacts WHERE {where}", params).fetchone()["c"]


def _camp_row(conn, r) -> dict:
    d = dict(r)
    d["tg_verified_only"] = 1 if d.get("tg_verified_only", 1) in (1, "1", True, None) else 0
    d["audience"] = _audience_count(conn, d["id"], d.get("audience_tag"), d.get("channel"),
                                    verified_only=bool(d["tg_verified_only"]))
    # Сколько ещё подтянется само по мере пробива — чтобы «аудитория 170» не выглядела
    # как потеря базы: остальные не выброшены, они ждут очереди на пробив.
    d["audience_pending_check"] = max(
        _audience_count(conn, d["id"], d.get("audience_tag"), d.get("channel")) - d["audience"], 0)
    d["sent"] = conn.execute(
        "SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=?", (d["id"],)
    ).fetchone()["c"]
    # Сколько контактов сняли вручную в окне «Кто в рассылке» (кнопка «☐ снять все»
    # кладёт в паузу СРАЗУ ВСЮ текущую очередь). Карточка молчала про это — «в очереди
    # 0» выглядело как «база кончилась», хотя на деле все были живы, просто отключены.
    d["paused"] = conn.execute(
        "SELECT COUNT(*) c FROM campaign_paused_contacts WHERE campaign_id=?", (d["id"],)
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
    # Защита от бана по умолчанию ВКЛЮЧЕНА: новая кампания не должна начинать жизнь
    # с резолва сотен непробитых номеров прямо во время рассылки.
    vonly = 1 if payload.get("tg_verified_only", True) else 0
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (name, product, audience_tag, channel, account_id, daily_limit, "
            "message_template, agent_prompt, kp_text, project_id, tg_verified_only, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'draft')",
            (f["name"], f["product"], f["audience_tag"], f["channel"], account_id, daily_limit,
             f["message_template"], f["agent_prompt"], f["kp_text"], project_id, vonly),
        )
        # Переговорка и ответственный кампании: заполнены прямо в форме создания —
        # значит их нельзя терять до первого «сохранить» уже существующей кампании.
        conn.execute(
            "UPDATE campaigns SET meeting_url=?, notify_target=?, agent_model=?, "
            "notify_account_id=? WHERE id=?",
            ((payload.get("meeting_url") or "").strip() or None,
             (payload.get("notify_target") or "").strip() or None,
             (payload.get("agent_model") or "").strip() or None,
             int(payload["notify_account_id"]) if payload.get("notify_account_id") else None,
             cur.lastrowid),
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
        # Флаг шлём только если он реально пришёл: частичные сохранения (из других форм,
        # не содержащих галочку) не должны молча снимать защиту от бана.
        conn.execute(
            "UPDATE campaigns SET name=?, product=?, audience_tag=?, channel=?, account_id=?, "
            "daily_limit=?, message_template=?, agent_prompt=?, kp_text=?, project_id=? WHERE id=?",
            (f["name"], f["product"], f["audience_tag"], f["channel"], account_id, daily_limit,
             f["message_template"], f["agent_prompt"], f["kp_text"], project_id, cid),
        )
        if "tg_verified_only" in payload:
            conn.execute("UPDATE campaigns SET tg_verified_only=? WHERE id=?",
                         (1 if payload.get("tg_verified_only") else 0, cid))
        # Своя переговорка и свой ответственный у кампании. Пишем только пришедшие поля:
        # форма может сохраняться частично, и молча стирать чужую настройку нельзя.
        for key in ("meeting_url", "notify_target", "agent_model"):
            if key in payload:
                conn.execute(f"UPDATE campaigns SET {key}=? WHERE id=?",
                             ((payload.get(key) or "").strip() or None, cid))
        if "notify_account_id" in payload:
            acc = payload.get("notify_account_id")
            conn.execute("UPDATE campaigns SET notify_account_id=? WHERE id=?",
                         (int(acc) if acc else None, cid))
        if account_ids is not None:
            _sync_campaign_accounts(conn, cid, account_ids, account_limits)
    return JSONResponse({"ok": True, "id": cid})


@app.post("/api/campaign/{cid}/test_contacts")
def campaign_test_contacts(cid: int, payload: dict = Body(...)) -> JSONResponse:
    """Свои номера/юзернеймы для теста ЭТОЙ кампании — без отдельной тестовой кампании.
    Формат строки: "<номер или @username> [Имя Отчество]" — всё после первого пробела
    идёт в обращение {name} (см. channels.campaign_send._greeting). Без имени — обращение
    подставится по номеру/юзернейму, как раньше.
    Помечает is_test=1 — такие контакты видит ТОЛЬКО кнопка «🧪 Тест» (см. channels/
    campaign_send._audience), в боевой заход они не попадают и его квоту не съедают.
    Сбрасывает статус в 'new' (даже если контакт уже был раньше) и добавляет тег
    аудитории кампании — иначе не попадут в фильтр _audience по audience_tag."""
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
                first, _, rest = line.partition(" ")
                uname = first.strip().lstrip("@")
                display_name = rest.strip()
                row_id = database.upsert_contact(conn, source="test", username=uname,
                                                  name=display_name or first.strip(), tags=tag)
            else:
                # Номер сам по себе содержит пробелы («+7 999 123-45-67»), поэтому резать
                # строку по ПЕРВОМУ пробелу нельзя: от номера оставалось «+7», norm_phone
                # возвращал None и контакт молча выбрасывался. Берём ведущий «телефонный»
                # кусок, остальное — обращение.
                import re as _re
                m = _re.match(r"^([+\d][\d\s()+.-]*)(.*)$", line)
                p = norm_phone(m.group(1)) if m else None
                if not p:
                    continue
                display_name = m.group(2).strip()
                row_id = database.upsert_contact(conn, source="test", phone=p,
                                                  name=display_name or p, tags=tag)
            # test_campaign_id — чтобы тестовый номер не всплыл первым в ЧУЖОЙ кампании,
            # чьи теги остались на контакте (см. campaign_send._audience).
            # Статус откатываем в 'new' только у тех, с кем разговор ещё не начался:
            # иначе добавление номера в тест роняло живой диалог обратно в «сырьё» и
            # человеку прилетал опенер поверх переписки (см. _TEST_KEEP_STATUS).
            keep = ",".join("?" * len(_TEST_KEEP_STATUS))
            conn.execute(
                f"UPDATE contacts SET is_test=1, test_campaign_id=?, "
                f"status=CASE WHEN status IN ({keep}) THEN status ELSE 'new' END WHERE id=?",
                (cid, *_TEST_KEEP_STATUS, row_id))
            added += 1
    if not added:
        return JSONResponse({"error": "не нашёл ни валидного номера, ни @username"}, status_code=400)
    return JSONResponse({"ok": True, "added": added})


@app.get("/api/campaign/{cid}/preview")
def campaign_preview(cid: int) -> JSONResponse:
    """Показать РЕАЛЬНЫЙ текст, который уйдёт каждому получателю — без единой отправки
    в Telegram. Рендерит тот же шаблон (обращение по ФИО, синонимизация {a|b|c}), что и
    боевая рассылка, на тестовых (is_test=1) и на первых из обычной аудитории контактах."""
    from channels.campaign_send import _parts, _greeting, _decision_phrase, _sender_name
    from channels import opener_lint
    database.init_db()
    with database.get_conn() as conn:
        camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not camp:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        camp = dict(camp)
        # {sender} показываем как у реального отправителя: основной аккаунт кампании,
        # иначе первый из команды — иначе оператор не увидит, чьим именем представляется.
        acc = conn.execute(
            "SELECT a.tg_name, a.label FROM accounts a "
            "LEFT JOIN campaign_accounts ca ON ca.account_id=a.id AND ca.campaign_id=? "
            "WHERE a.id=? OR ca.campaign_id=? ORDER BY (a.id=?) DESC, a.id LIMIT 1",
            (cid, camp.get("account_id") or 0, cid, camp.get("account_id") or 0),
        ).fetchone()
        sender = _sender_name(dict(acc)) if acc else ""
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
        # strict=False: предпросмотр обязан ПОКАЗАТЬ даже испорченный шаблон —
        # именно чтобы оператор увидел, что уйдёт, и починил (проблемы рядом, в warnings).
        parts = _parts(camp.get("message_template"), name, r["agency"] or r["name"],
                       _decision_phrase(r), sender=sender, strict=False)
        out.append({
            "contact_id": r["id"], "handle": ("@" + r["username"]) if r["username"] else (r["phone"] or "—"),
            "greeting": name or "(без имени — обращение будет пустым)",
            "is_test": bool(r["is_test"]) if "is_test" in r.keys() else False,
            "parts": parts,
        })
    problems = opener_lint.lint(camp.get("message_template"))
    return JSONResponse({"campaign": camp.get("name"), "count": len(out), "items": out,
                         "blocked": bool(opener_lint.severe(problems)),
                         "warnings": opener_lint.report(problems)})


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
        aud = _audience_count(conn, cid, camp.get("audience_tag"), camp.get("channel"))
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

    # Чем аудиторию РЕАЛЬНО достать. У кого нет @username и TG не подтверждён, отправка
    # резолвит номер прямо в момент выстрела через ImportContacts — самый заметный для
    # Telegram спам-сигнал (см. предупреждение в channels/phone_resolve). Сотня таких
    # подряд с одного аккаунта = заявка на бан, поэтому пробив надо делать ОТДЕЛЬНЫМ
    # дозированным шагом заранее, а не во время рассылки.
    tg_campaign = "telegram" in [c.strip() for c in (camp.get("channel") or "").split(",")]
    if tg_campaign:
        # Считаем ТОЛЬКО по TG-подмножеству, а не по общей канальной клаузе: у кампании
        # «telegram,whatsapp» клауза пропускает и WA-only контакты (has_tg='no'), а в
        # confirmed они не попадут никогда — и висели бы в «непробитых» вечно, сколько
        # ни жми «Пробить номера в TG». where/params — из общей _audience_where, чтобы
        # выборка не разъехалась с той, по которой считается сама аудитория.
        where, params = _audience_where(cid, camp.get("audience_tag"), camp.get("channel"),
                                        channel_clause="has_tg IN ('yes','unknown')")
        base = f"SELECT COUNT(*) c FROM contacts WHERE {where}"
        with database.get_conn() as conn:
            tg_aud = conn.execute(base, params).fetchone()["c"]
            by_uname = conn.execute(
                base + " AND username IS NOT NULL AND username<>''", params).fetchone()["c"]
            # Достижимость считаем ТЕМ ЖЕ условием, которым отбирает отправка
            # (database.TG_REACHABLE_SQL). Раньше здесь в «достижимые» шло has_tg='yes' —
            # но это догадка импортёра 2ГИС по ссылке t.me, без запроса в Telegram:
            # 14 таких контактов без username и tg_user_id считались готовыми, а на деле
            # резолвились ImportContacts во время рассылки. Предупреждение занижало риск.
            confirmed = conn.execute(
                base + " AND " + database.TG_REACHABLE_SQL, params).fetchone()["c"]
        blind = max(tg_aud - confirmed, 0)
        vonly = bool(camp.get("tg_verified_only", 1))
        if blind and vonly:
            add(True, "ok",
                f"🛡 Защита от бана включена: слать будем только {confirmed} достижимым "
                f"(по @username: {by_uname}). Ещё {blind} ждут пробива и в рассылку НЕ пойдут — "
                f"жми «Пробить номера в TG», по мере пробива они добавятся сами")
        elif blind:
            add(False, "warn",
                f"⚠️ Защита выключена: {blind} из {tg_aud} непробиты, их номера будут резолвиться "
                f"прямо во время рассылки (ImportContacts, риск бана). Включи «слать только "
                f"подтверждённым» или сначала «Пробить номера в TG». Достижимы сейчас: {confirmed}")
        elif tg_aud:
            add(True, "ok", f"TG подтверждён у всей аудитории ({confirmed})")
    else:
        confirmed = blind = 0

    ready = all(c["ok"] for c in checks if c["level"] == "fail")
    return JSONResponse({"ready": ready, "checks": checks, "audience": aud, "team": len(team),
                         "reachable": confirmed, "unresolved": blind})


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
            "SELECT cc.contact_id, cc.sent_at, c.name, c.username, c.phone, c.person_name, c.status, c.source, "
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
        # Показываем МНОГО (не только первые 500) — иначе "показано" в шапке не бьётся
        # с "в очереди N" на карточке кампании (тот счётчик считает всю аудиторию).
        pend = conn.execute(
            f"SELECT id,name,username,phone,person_name,status,source,COALESCE(is_test,0) is_test "
            f"FROM contacts WHERE {where} ORDER BY COALESCE(is_test,0) DESC, id LIMIT 3000",
            params,
        ).fetchall()
        paused_ids = database.paused_contact_ids(conn, cid)
        audience_total = _audience_count(conn, cid, tag, channel)

    def handle(r) -> str:
        return ("@" + r["username"]) if r["username"] else (r["phone"] or "—")

    acc_name = (acc and (acc.get("label") or acc.get("phone"))) or "—"
    rows = []
    for r in sent_rows:
        rows.append({
            "id": r["contact_id"], "name": r["person_name"] or r["name"], "handle": handle(r),
            "sent": True, "sent_at": r["sent_at"], "source": r["source"],
            "account": r["acc_label"] or r["acc_phone"] or acc_name, "status": r["status"],
        })
    for r in pend:
        if r["id"] in sent_ids:
            continue
        rows.append({
            "id": r["id"], "name": r["person_name"] or r["name"], "handle": handle(r),
            "sent": False, "sent_at": None, "account": acc_name, "status": r["status"], "source": r["source"],
            "is_test": bool(r["is_test"]), "is_paused": r["id"] in paused_ids,
        })
    return JSONResponse({"account": acc, "account_name": acc_name, "sent_count": len(sent_ids),
                         "total": len(rows), "audience_total": audience_total, "rows": rows})


def _proxy_key(px: str | None) -> str:
    """Ключ адреса для сравнения: host:port без логина/пароля и схемы.

    Один и тот же выход в интернет записывают по-разному — «socks5://user:pass@1.2.3.4:1080»
    и «1.2.3.4:1080» это ОДИН адрес, и Telegram видит его одинаково. Сравнивать сырые
    строки бесполезно: пересечение так не находится."""
    raw = (px or "").strip()
    if not raw:
        return ""
    if raw.startswith("tg://"):
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(raw).query)
        return f"{(q.get('server') or [''])[0]}:{(q.get('port') or [''])[0]}".lower()
    if "://" in raw:
        from urllib.parse import urlparse
        p = urlparse(raw)
        return f"{p.hostname or ''}:{p.port or ''}".lower()
    body = raw.rsplit("@", 1)[-1]           # user:pass@host:port → host:port
    parts = body.split(":")
    return f"{parts[0]}:{parts[1]}".lower() if len(parts) >= 2 else body.lower()


@app.get("/api/proxy/conflicts")
def proxy_conflicts() -> JSONResponse:
    """Кто с кем делит выход в интернет и кто сидит без прокси.

    Это главная причина потери аккаунтов: одна сессия, увиденная с двух IP, жжётся
    Telegram навсегда (AuthKeyDuplicatedError — так уже потеряны три аккаунта). Два
    аккаунта на одном адресе опасны вдвойне: при переключении прокси в пуле один из
    них внезапно меняет IP на живой сессии, и это выглядит как угон.
    """
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, label, phone, proxy, status, proxy_alive FROM accounts "
            "WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND COALESCE(status,'') NOT IN ('archived','banned') "
            "AND COALESCE(protected,0)=0").fetchall()
    groups: dict = {}
    no_proxy = []
    for r in rows:
        key = _proxy_key(r["proxy"])
        who = {"id": r["id"], "label": r["label"], "phone": r["phone"],
               "status": r["status"], "proxy_alive": r["proxy_alive"]}
        if not key:
            no_proxy.append(who)
            continue
        groups.setdefault(key, []).append(who)
    shared = [{"address": k, "accounts": v} for k, v in groups.items() if len(v) > 1]
    shared.sort(key=lambda g: -len(g["accounts"]))
    return JSONResponse({
        "shared": shared,
        "shared_accounts": sum(len(g["accounts"]) for g in shared),
        "no_proxy": no_proxy,
        "total": len(rows),
        "clean": len(rows) - len(no_proxy) - sum(len(g["accounts"]) for g in shared),
    })


@app.get("/api/accounts/twofa")
def accounts_twofa_status() -> JSONResponse:
    """У кого стоит облачный пароль, а кого ещё могут увести.

    У купленного аккаунта НОМЕР остаётся у продавца: он в любой момент входит по
    SMS и завершает наши сеансы. Так уже потеряли два лота целиком. 2FA не броня,
    но без неё увод занимает минуту, а с ней — неделю ожидания сброса и заметный
    след (см. channels/twofa)."""
    database.init_db()
    with database.get_conn() as conn:
        # колонка называется tg_2fa (см. channels/twofa._targets) — в ней лежит НАШ
        # пароль; пусто = аккаунт может увести владелец номера
        rows = conn.execute(
            "SELECT id, label, phone, tg_2fa, status, session_alive, tg_session, protected "
            "FROM accounts WHERE COALESCE(status,'') NOT IN ('archived','banned') "
            "AND COALESCE(protected,0)=0").fetchall()
    protected, unprotected = [], []
    for r in rows:
        d = {"id": r["id"], "label": r["label"], "phone": r["phone"], "status": r["status"]}
        if (r["tg_2fa"] or "").strip():
            protected.append(d)
        elif (r["tg_session"] or "").strip():
            unprotected.append(d)
    return JSONResponse({"protected": len(protected), "unprotected": unprotected,
                         "at_risk": len(unprotected)})


@contextlib.contextmanager
def _listener_released(active: bool = True):
    """Отпустить сессии слушателем на время операции, которая сама подключается к Telegram.

    ЗАЧЕМ ОТДЕЛЬНЫМ ПОМОЩНИКОМ. Слушатель — фоновый поток пульта — держит подключения к
    сессиям аккаунтов. Любая операция, открывающая ВТОРОЕ подключение тем же ключом,
    сжигает аккаунт: Telegram считает это угоном (AuthKeyDuplicatedError). Так за одну
    секунду сгорело три аккаунта на установке 2FA, а восстановить их нечем — номер
    остаётся у продавца. Купленный аккаунт разовый, то есть каждый такой промах = деньги.

    Пауза жила инлайном в одном роуте — и это ровно та форма, при которой следующий
    похожий роут добавляют, забыв её скопировать. Любой новый эндпоинт, лезущий в
    Telegram сессией аккаунта, обязан оборачиваться сюда.
    """
    paused = False
    if active:
        with database.get_conn() as conn:
            was_on = database.get_setting(conn, "listener_enabled", "on") != "off"
        if was_on:
            with database.get_conn() as conn:
                database.set_setting(conn, "listener_enabled", "off")
            paused = True
            import time as _time
            _time.sleep(7)   # POLL_SEC=5 на обнаружение + запас на отключение клиентов
    try:
        yield
    finally:
        if paused:
            with database.get_conn() as conn:
                database.set_setting(conn, "listener_enabled", "on")


@app.get("/api/accounts/spare")
def accounts_spare_status() -> JSONResponse:
    """Сколько аккаунтов застраховано запасной сессией, а сколько ходит без страховки."""
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN tg_session_spare IS NOT NULL AND tg_session_spare<>'' THEN 1 ELSE 0 END) with_spare "
            "FROM accounts WHERE session_alive=1 AND COALESCE(protected,0)=0 "
            "AND tg_session IS NOT NULL AND tg_session<>''").fetchone()
        dead_with_spare = conn.execute(
            "SELECT id, label FROM accounts WHERE COALESCE(session_alive,1)=0 "
            "AND tg_session_spare IS NOT NULL AND tg_session_spare<>''").fetchall()
    total = row["total"] or 0
    with_spare = row["with_spare"] or 0
    return JSONResponse({
        "total": total, "with_spare": with_spare, "without_spare": total - with_spare,
        # мёртвые, которые можно поднять прямо сейчас — это деньги, лежащие на полу
        "recoverable": [dict(r) for r in dead_with_spare],
    })


@app.post("/api/accounts/spare")
def accounts_spare_mint(payload: dict = Body(default={})) -> JSONResponse:
    """Выпустить запасную (холодную) сессию: вторую независимую авторизацию Telegram.

    Не копия основной сессии — у копии тот же ключ, и поднятая параллельно она аккаунт
    сжигает, а не страхует (см. шапку channels/session_spare.py). kick_others=true
    дополнительно сбрасывает чужие сессии (выкидывает продавца) — строго ДО выпуска
    запаски, иначе сброс снёс бы и её."""
    ids = [int(i) for i in (payload.get("ids") or [])]
    dry = bool(payload.get("dry"))
    args = ["channels.session_spare"]
    if ids:
        args += ["--ids", ",".join(str(i) for i in ids)]
    if dry:
        args.append("--dry")
    if payload.get("kick_others"):
        args.append("--kick-others")

    with _listener_released(active=not dry):
        res = _run_capture(args, timeout=900)

    data = _last_json(res.get("output"))
    if data is None:
        tail = (res.get("output") or "").strip()[-400:]
        return JSONResponse({"ok": False, "error": "не отчитался. Лог: " + (tail or "(пусто)")})
    data["log"] = (res.get("output") or "").splitlines()[-20:]
    return JSONResponse(data)


@app.post("/api/account/{acc_id}/spare_promote")
def account_spare_promote(acc_id: int) -> JSONResponse:
    """Основная сессия мертва → поднять запаску как основную. Запаска при этом
    расходуется: после успеха надо выпустить новую, пока аккаунт жив."""
    with _listener_released():
        res = _run_capture(["channels.session_spare", "--promote", str(acc_id)], timeout=300)
    data = _last_json(res.get("output"))
    if data is None:
        tail = (res.get("output") or "").strip()[-400:]
        return JSONResponse({"ok": False, "error": "не отчитался. Лог: " + (tail or "(пусто)")})
    return JSONResponse(data)


@app.post("/api/accounts/twofa")
def accounts_twofa_set(payload: dict = Body(default={})) -> JSONResponse:
    """Поставить облачный пароль. Пароль генерится и пишется в БД ДО установки —
    иначе при сбое между «поставили в Telegram» и «записали себе» аккаунт запирается
    навсегда. ids пустой = все живые боевые без 2FA.

    На живом прогоне поймали AuthKeyDuplicatedError сразу на трёх аккаунтах — все
    получили 2FA в ЭТУ ЖЕ секунду. Причина: этот эндпоинт открывает НОВОЕ подключение
    к Telegram, а слушатель (фоновый поток пульта) в это время уже держит СВОЁ
    подключение к тем же сессиям. Два одновременных подключения по одному ключу —
    ровно то, за что Telegram жжёт сессию как угнанную. Поэтому на время установки
    слушатель отпускает аккаунты и подключается заново, когда всё готово."""
    ids = [int(i) for i in (payload.get("ids") or [])]
    dry = bool(payload.get("dry"))
    args = ["channels.twofa"]
    if ids:
        args += ["--ids", ",".join(str(i) for i in ids)]
    if dry:
        args.append("--dry")

    with _listener_released(active=not dry):
        res = _run_capture(args, timeout=900)

    data = _last_json(res.get("output"))
    if data is None:
        tail = (res.get("output") or "").strip()[-400:]
        return JSONResponse({"ok": False, "error": "не отчитался. Лог: " + (tail or "(пусто)")})
    data["log"] = (res.get("output") or "").splitlines()[-20:]
    return JSONResponse(data)


@app.post("/api/accounts/import_keys")
async def accounts_import_keys(file: UploadFile = File(None),
                               text: str = Form(""),
                               status: str = Form("warming")) -> JSONResponse:
    """Завести аккаунты из дампа магазина: строки вида «authkey_hex:dc_id».

    Раньше это умел только консольный модуль (channels.import_authkeys), то есть
    требовался SSH и ручная заливка файла на сервер. Здесь то же самое, но файлом
    из пульта — и главное, с ПРОВЕРКОЙ ДУБЛЕЙ: один и тот же ключ, заведённый
    дважды, даёт две карточки на один номер, и рассылка пишет с него в два потока.

    Ключ — это полный доступ к аккаунту. Файл нигде не сохраняем: читаем в память,
    заводим и забываем.
    """
    from channels.account_add_fields import build_session, save_to_db, verify
    raw = ""
    if file is not None:
        raw = (await file.read()).decode("utf-8", errors="replace")
    if not raw.strip():
        raw = text or ""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        return JSONResponse({"error": "пустой файл: нужны строки «ключ:dc»"}, status_code=400)

    # Чем сверяем дубли: auth_key внутри уже сохранённых сессий. Сравнивать сами
    # строки сессий нельзя — один ключ даёт разные строки при разных DC-адресах.
    from telethon.sessions import StringSession
    known: dict[str, dict] = {}
    with database.get_conn() as conn:
        for r in conn.execute("SELECT id, label, phone, tg_session FROM accounts "
                              "WHERE tg_session IS NOT NULL AND tg_session<>''").fetchall():
            try:
                ss = StringSession(r["tg_session"])
                if ss.auth_key:
                    known[ss.auth_key.key.hex().lower()] = {
                        "id": r["id"], "label": r["label"], "phone": r["phone"]}
            except Exception:  # noqa: BLE001 — битую сессию просто не учитываем
                continue

    added, skipped, dead = [], [], []
    for i, line in enumerate(lines, 1):
        if ":" not in line:
            dead.append({"line": i, "why": "нет «:dc» в строке"})
            continue
        authkey, _, dc_s = line.rpartition(":")
        authkey = authkey.strip().lower()
        if authkey in known:
            skipped.append({"line": i, "already": known[authkey]})
            continue
        try:
            session_str = build_session(authkey, int(dc_s))
        except Exception as e:  # noqa: BLE001
            dead.append({"line": i, "why": str(e)[:120]})
            continue
        # Каждый ключ — в своей «песочнице»: осечка на одном (нет номера в профиле,
        # конфликт UNIQUE(phone), обрыв связи) не должна ронять весь заход. Раньше
        # такое исключение уходило в 500, браузер получал HTML вместо JSON и писал
        # «Unexpected token 'I', "Internal S"… is not valid JSON» — без единого
        # намёка, на какой строке беда.
        try:
            alive, info = await verify(session_str)
            if not alive:
                dead.append({"line": i, "why": info.get("reason") or "ключ мёртв"})
                continue
            phone = (info.get("phone") or "").strip()
            if not phone:
                # save_to_db собирает номер как «+» + phone: пустой даёт «+», а
                # второй такой же валит UNIQUE(phone) и весь запрос вместе с ним.
                dead.append({"line": i, "why": "аккаунт жив, но номер скрыт в профиле — "
                                               "заведи его через «Добавить аккаунт» вручную"})
                continue
            label = save_to_db(phone, session_str, info, "", "", status)
            known[authkey] = {"label": label, "phone": phone}
            added.append({"line": i, "label": label, "phone": phone,
                          "username": info.get("username")})
        except Exception as e:  # noqa: BLE001
            dead.append({"line": i, "why": f"{type(e).__name__}: {str(e)[:140]}"})
    return JSONResponse({"ok": True, "total": len(lines),
                         "added": added, "skipped": skipped, "dead": dead})


def _label_first_name(label: str) -> str:
    """Имя из ярлыка «Александр504» → «Александр».

    make_label (channels/ru_names.py) клеит имя и хвост номера БЕЗ пробела —
    «{first}{digits}». Прежняя проверка искала цифры отдельным словом через
    split() и на такой ярлык ничего не находила: он один токен. Из-за этого почти
    весь парк (обычный, штатный формат ярлыка) считался «расхождением» — 37 из 38
    записей были ложным срабатыванием, и единственная настоящая проблема тонула
    в шуме."""
    import re
    return re.sub(r"\d+$", "", (label or "").strip()).strip()


@app.get("/api/accounts/identity_check")
def accounts_identity_check() -> JSONResponse:
    """Аккаунты, у которых личность разъехалась: в пульте одно имя, в Telegram другое.

    label — что видит оператор, tg_name — что реально стоит в профиле и по чьему
    полу подобрано фото. Раньше переименование в пульте меняло только label, и
    получалось «Кристина Орлова» в списке, «Никита» в переписке и мужское фото.
    Собеседник видит несоответствие сразу же, а оператор — нет.

    Сравниваем ТОЛЬКО первое имя: label обычно «Имя+цифры», tg_name — «Имя Фамилия»,
    и требовать полного совпадения строк бессмысленно даже в штатном случае.
    """
    database.init_db()
    from channels.ru_names import gender_of
    out = []
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, label, tg_name, username, avatar, phone FROM accounts "
            "WHERE COALESCE(status,'') <> 'archived'").fetchall()
    for r in rows:
        label = (r["label"] or "").strip()
        tg = (r["tg_name"] or "").strip()
        if not label or not tg or label.startswith(("+", "#")):
            continue
        base_label = _label_first_name(label)
        # Сравниваем ПЕРВЫЕ имена с обеих сторон: label — «Имя+цифры» или, если
        # заведён вручную, «Имя Фамилия»; tg_name — всегда «Имя Фамилия». Сверка
        # полных строк ловила ложный мисматч на любом двухсловном ярлыке, где имя
        # с фамилией совпадали дословно (напр. «Василий Аксиоменко» vs «Василий
        # Аксиоменко» — то же самое имя, но не первым словом против целой строки).
        label_first = base_label.split()[0] if base_label.split() else base_label
        tg_first = tg.split()[0] if tg.split() else tg
        if not label_first or label_first.lower() == tg_first.lower():
            continue
        g1, g2 = gender_of(label_first), gender_of(tg)
        out.append({
            "id": r["id"], "label": label, "tg_name": tg,
            "username": r["username"], "has_avatar": bool(r["avatar"]),
            "gender_conflict": bool(g1 and g2 and g1 != g2),
            "why": ("пол не совпадает: в пульте и в Telegram разные личности"
                    if (g1 and g2 and g1 != g2) else "имя в пульте и в Telegram разное"),
        })
    out.sort(key=lambda x: (not x["gender_conflict"], x["id"]))
    return JSONResponse({"items": out, "count": len(out),
                         "gender_conflicts": sum(1 for x in out if x["gender_conflict"])})


@app.post("/api/accounts/identity_fix")
def accounts_identity_fix(payload: dict = Body(default={})) -> JSONResponse:
    """Свести имя к одному: source='label' (как в пульте) или 'tg' (как в Telegram).

    Само переименование в Telegram делает «оформить сейчас» (_setup_profile) — тут
    только приводим базу в порядок и, если сменился пол, снимаем старое фото."""
    ids = [int(i) for i in (payload.get("ids") or [])]
    source = payload.get("source") or "label"
    if not ids:
        return JSONResponse({"error": "не выбрано ни одного аккаунта"}, status_code=400)
    from channels.ru_names import gender_of
    fixed = 0
    with database.get_conn() as conn:
        for aid in ids:
            r = conn.execute("SELECT label, tg_name FROM accounts WHERE id=?", (aid,)).fetchone()
            if not r:
                continue
            label = (r["label"] or "").strip()
            tg = (r["tg_name"] or "").strip()
            base_label = _label_first_name(label)
            if source == "tg" and tg:
                conn.execute("UPDATE accounts SET label=? WHERE id=?", (tg, aid))
            elif base_label:
                conn.execute("UPDATE accounts SET tg_name=? WHERE id=?", (base_label, aid))
                if tg and gender_of(base_label) != gender_of(tg):
                    conn.execute("UPDATE accounts SET avatar=NULL WHERE id=?", (aid,))
            else:
                continue
            fixed += 1
    return JSONResponse({"ok": True, "fixed": fixed,
                         "note": "теперь нажми «оформить сейчас» — имя и фото уйдут в Telegram"})


@app.get("/api/deploy/status")
def deploy_status() -> JSONResponse:
    """Что стоит на сервере и на сколько отстали от GitHub."""
    import subprocess
    root = str(BASE_DIR.parent)

    def git(*args, timeout=60):
        try:
            r = subprocess.run(["git", *args], cwd=root, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout)
            return (r.stdout or "").strip(), (r.stderr or "").strip(), r.returncode
        except Exception as e:  # noqa: BLE001
            return "", str(e)[:200], 1

    branch, _, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    here, _, _ = git("log", "--oneline", "-1")
    git("fetch", "origin", timeout=120)
    behind, _, _ = git("rev-list", "--count", "HEAD..origin/main")
    ahead, _, _ = git("rev-list", "--count", "origin/main..HEAD")
    dirty, _, _ = git("status", "--porcelain")
    pending, _, _ = git("log", "--oneline", "HEAD..origin/main")
    return JSONResponse({
        "branch": branch, "current": here,
        "behind": int(behind or 0), "ahead": int(ahead or 0),
        "dirty": bool(dirty.strip()),
        "dirty_files": [l[3:] for l in dirty.splitlines()[:10]],
        "pending": pending.splitlines()[:15],
    })


@app.post("/api/deploy")
def deploy_run(payload: dict = Body(default={})) -> JSONResponse:
    """Обновить код с GitHub и перезапуститься — кнопкой из пульта.

    Раньше деплой был ручным походом в консоль сервера, и из-за этого работа
    неделями лежала в origin/main, не доезжая до боевого пульта: сервер вообще
    оказался на ветке wip-snapshot, где `git pull` честно отвечал «Already up to
    date» и никого не смущал.

    Перезапуск делаем без sudo: unit объявлен с Restart=on-failure, поэтому выход
    с ненулевым кодом systemd поднимет сам через RestartSec. Выходим ОТЛОЖЕННО,
    отдельным потоком, — иначе ответ не успеет уйти в браузер.
    """
    import os
    import subprocess
    import threading
    import time as _t
    root = str(BASE_DIR.parent)
    log: list[str] = []

    def git(*args, timeout=180) -> bool:
        try:
            r = subprocess.run(["git", *args], cwd=root, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            log.append(f"$ git {' '.join(args)}\n{out}")
            return r.returncode == 0
        except Exception as e:  # noqa: BLE001
            log.append(f"$ git {' '.join(args)}\nОШИБКА: {str(e)[:200]}")
            return False

    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True,
                           text=True, encoding="utf-8", errors="replace").stdout.strip()
    if dirty and not payload.get("force"):
        return JSONResponse({"ok": False, "needs_confirm": True,
                             "warn": "На сервере есть несохранённые правки — обновление их перезапишет:\n"
                                     + "\n".join(l[3:] for l in dirty.splitlines()[:10])
                                     + "\n\nОбновлять всё равно?"}, status_code=200)

    if not git("fetch", "origin"):
        return JSONResponse({"ok": False, "error": "не достучались до GitHub", "log": log})
    if payload.get("force") and dirty:
        git("checkout", "--", ".")            # чужие правки затираем только по явному согласию
    branch = payload.get("branch") or "main"
    git("checkout", branch)                    # сервер мог сидеть на ветке-снапшоте
    if not git("reset", "--hard", f"origin/{branch}"):
        return JSONResponse({"ok": False, "error": "не удалось обновиться", "log": log})
    now = subprocess.run(["git", "log", "--oneline", "-1"], cwd=root, capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout.strip()

    if payload.get("restart", True):
        def _bye():
            _t.sleep(1.5)                      # дать ответу уйти в браузер
            os._exit(1)                        # Restart=on-failure → systemd поднимет
        threading.Thread(target=_bye, daemon=True).start()

    return JSONResponse({"ok": True, "now": now, "log": log,
                         "restarting": bool(payload.get("restart", True))})


@app.get("/api/tgcheck")
def tgcheck_status() -> JSONResponse:
    """Сколько номеров ещё не пробито в Telegram + настройки фонового пробива."""
    database.init_db()
    with database.get_conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) c FROM contacts WHERE phone IS NOT NULL AND phone<>'' "
            "AND COALESCE(has_tg,'unknown')='unknown'").fetchone()["c"]
        yes = conn.execute("SELECT COUNT(*) c FROM contacts WHERE has_tg='yes'").fetchone()["c"]
        no = conn.execute("SELECT COUNT(*) c FROM contacts WHERE has_tg='no'").fetchone()["c"]
        return JSONResponse({
            "left": left, "found": yes, "absent": no,
            "auto": database.get_setting(conn, "tgcheck_auto", "off") == "on",
            "interval_h": int(database.get_setting(conn, "tgcheck_interval_min", "720")) // 60,
            "per": int(database.get_setting(conn, "tgcheck_per", "25")),
            "last_run": database.get_setting(conn, "tgcheck_last_run", None),
        })


@app.post("/api/tgcheck")
def tgcheck_set(payload: dict = Body(default={})) -> JSONResponse:
    """Включить/настроить фоновый пробив. run=true — прогнать порцию сейчас."""
    with database.get_conn() as conn:
        if "auto" in payload:
            database.set_setting(conn, "tgcheck_auto", "on" if payload.get("auto") else "off")
        if payload.get("interval_h"):
            database.set_setting(conn, "tgcheck_interval_min",
                                 str(max(1, int(payload["interval_h"])) * 60))
        if payload.get("per"):
            database.set_setting(conn, "tgcheck_per", str(max(1, min(int(payload["per"]), 50))))
    if payload.get("run"):
        args = ["channels.phone_resolve", "--per", str(payload.get("per") or 25)]
        if payload.get("tag"):
            args += ["--tag", str(payload["tag"])]
        res = _run_capture(args, timeout=3600)
        return JSONResponse(_last_json(res.get("output")) or res)
    return JSONResponse({"ok": True})


@app.get("/api/campaign/{cid}/audience")
def campaign_audience(cid: int, limit: int = 1000) -> JSONResponse:
    """Поимённо: кто в рассылке этой кампании, а кто выпал и ПОЧЕМУ.

    Расхождение «выбрал 288 — в очереди 180» до сих пор нельзя было объяснить, не
    залезая в базу: карточка показывала итог, а причины отсева молчали. Здесь по
    каждому контакту с тегом кампании видно, пойдёт он или нет и что мешает —
    и тут же снимается галочка с ненужных (пауза именно в этой кампании,
    глобальный статус контакта не трогаем)."""
    database.init_db()
    with database.get_conn() as conn:
        camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not camp:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        camp = dict(camp)
        tag = (camp.get("audience_tag") or "").strip()
        where = "1=1"
        params: list = []
        if tag:
            where = "tags LIKE ?"
            params.append(f"%{tag}%")
        rows = conn.execute(
            f"SELECT id, COALESCE(person_name, name) AS who, username, phone, status, "
            f"has_tg, has_wa, tg_checked_at, checked_at, tags, agency, city "
            f"FROM contacts WHERE {where} "
            f"ORDER BY (status='new') DESC, id LIMIT ?", (*params, max(1, min(limit, 5000)))
        ).fetchall()
        paused = {r["contact_id"] for r in conn.execute(
            "SELECT contact_id FROM campaign_paused_contacts WHERE campaign_id=?", (cid,)).fetchall()}
        sent = {r["contact_id"] for r in conn.execute(
            "SELECT contact_id FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchall()}

    is_tg = "telegram" in [c.strip() for c in (camp.get("channel") or "").split(",")]
    items, reasons = [], {}
    for r in rows:
        d = dict(r)
        why = None
        if d["id"] in paused:
            why = "снят вручную"
        elif d["status"] != "new":
            why = ("уже написали" if d["id"] in sent else f"статус «{d['status']}»")
        elif not (d["username"] or d["phone"]):
            why = "нет ни @username, ни телефона"
        elif is_tg and (d["has_tg"] or "unknown") == "no":
            why = "в Telegram не найден"
        d["blocked_by"] = why
        d["in_queue"] = why is None
        d["paused"] = d["id"] in paused
        if why:
            reasons[why] = reasons.get(why, 0) + 1
        items.append(d)
    return JSONResponse({
        "campaign": camp.get("name"), "tag": tag, "channel": camp.get("channel"),
        "total": len(items),
        "in_queue": sum(1 for i in items if i["in_queue"]),
        "reasons": reasons,          # почему остальные не пойдут, с количеством
        # Сколько ещё не проверено: пока номер не пробит, рассылка резолвит его прямо
        # в момент отправки (ImportContacts) — самый быстрый способ поймать бан.
        "unchecked_tg": sum(1 for i in items if (i.get("has_tg") or "unknown") == "unknown"),
        "unchecked_wa": sum(1 for i in items if (i.get("has_wa") or "unknown") == "unknown"),
        "items": items,
    })


@app.post("/api/campaign/{cid}/pause_contacts")
def campaign_pause_contacts(cid: int, payload: dict = Body(...)) -> JSONResponse:
    """Частичная пауза: указанные контакты рассылка этой кампании пропустит, пока
    не снимут паузу. Остальная очередь продолжает слаться — в отличие от общего
    Стопа, который останавливает всю кампанию целиком."""
    ids = [int(i) for i in (payload.get("contact_ids") or [])]
    if not ids:
        return JSONResponse({"error": "не выбрано ни одного контакта"}, status_code=400)
    with database.get_conn() as conn:
        database.pause_campaign_contacts(conn, cid, ids)
    return JSONResponse({"ok": True, "paused": len(ids)})


@app.post("/api/campaign/{cid}/unpause_contacts")
def campaign_unpause_contacts(cid: int, payload: dict = Body(...)) -> JSONResponse:
    ids = [int(i) for i in (payload.get("contact_ids") or [])]
    if not ids:
        return JSONResponse({"error": "не выбрано ни одного контакта"}, status_code=400)
    with database.get_conn() as conn:
        database.unpause_campaign_contacts(conn, cid, ids)
    return JSONResponse({"ok": True, "unpaused": len(ids)})


@app.post("/api/campaign/{cid}/unpause_next")
def campaign_unpause_next(cid: int, payload: dict = Body(...)) -> JSONResponse:
    """Контролируемый раскат: снять паузу ровно со следующих N контактов, а не со
    всех разом. 13.08.2026 просьба оператора — после «снять все» вся живая
    аудитория ушла бы одним заходом, а хочется сначала посмотреть на 1-3 живых
    диалогах, что говорит модель, доправить промпт, и только потом открывать
    следующую пачку — без ожидания «ещё полгода тестировать»."""
    n = int(payload.get("n") or 3)
    if n <= 0:
        return JSONResponse({"error": "n должно быть больше нуля"}, status_code=400)
    with database.get_conn() as conn:
        paused = sorted(database.paused_contact_ids(conn, cid))
        batch = paused[:n]
        if not batch:
            return JSONResponse({"error": "снимать нечего — пауза пуста"}, status_code=400)
        database.unpause_campaign_contacts(conn, cid, batch)
        rows = conn.execute(
            f"SELECT id, COALESCE(person_name, name) AS who FROM contacts "
            f"WHERE id IN ({','.join('?' * len(batch))})", batch,
        ).fetchall()
        database.add_event(
            conn, "info", f"▶ Открыта пачка: {len(batch)} контакт(ов)",
            "Открыты: " + ", ".join((r["who"] or f"#{r['id']}") for r in rows),
            level="good", campaign_id=cid,
        )
    return JSONResponse({
        "ok": True, "opened": len(batch),
        "names": [(r["who"] or f"#{r['id']}") for r in rows],
        "left_paused": len(paused) - len(batch),
    })


_ECON_FIELDS = ("goal_start", "result_note", "cost_proxy", "cost_accounts", "cost_ai",
                "cost_other", "revenue_per_deal", "manager_salary", "manager_leads")
_ENGAGED = ("in_dialog", "meeting_set", "met", "won")
# КЭВ (созвон) достигнут — статус проставляет database.record_meeting() по факту
# meeting_agreed из разбора переписки агентом, не вручную.
_KEV_REACHED = ("meeting_set", "met", "won")

# Статусы, при которых тест-номер НЕЛЬЗЯ откатывать в 'new' и слать ему опенер заново.
# Кнопка «🧪 Тест» намеренно сбрасывает свои номера, чтобы гонять проверку многократно,
# но делала это безусловно — и в живой диалог прилетало «добрый день, правильно
# обращаюсь?» поверх переписки (вживую: три раза подряд), а статус диалога затирался.
# refused добавлен к _ENGAGED отдельно: человек уже отказался, повторный опенер ему —
# чистый спам, хотя «вовлечённым» он не считается.
_TEST_KEEP_STATUS = _ENGAGED + ("refused",)


@app.get("/api/campaign/{cid}/econ")
def campaign_econ(cid: int) -> JSONResponse:
    """Аналитика кампании: цели, расходы, стоимость лида/КЭВ, ROI, робот vs человек."""
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
        kmarks = ",".join("?" for _ in _KEV_REACHED)
        kev = conn.execute(
            f"SELECT COUNT(DISTINCT cc.contact_id) c FROM campaign_contacts cc "
            f"JOIN contacts ct ON ct.id=cc.contact_id WHERE cc.campaign_id=? AND ct.status IN ({kmarks})",
            (cid, *_KEV_REACHED),
        ).fetchone()["c"]

    def num(k):
        v = row.get(k)
        return float(v) if v not in (None, "") else 0.0

    total_cost = num("cost_proxy") + num("cost_accounts") + num("cost_ai") + num("cost_other")
    rev = deals * num("revenue_per_deal")
    cost_per_lead = round(total_cost / leads) if leads else None
    cost_per_deal = round(total_cost / deals) if deals else None
    cost_per_kev = round(total_cost / kev) if kev else None
    roi = round((rev - total_cost) / total_cost * 100) if total_cost else None
    # робот vs человек
    human_cpl = round(num("manager_salary") / num("manager_leads")) if num("manager_leads") else None
    econ = {k: row.get(k) for k in _ECON_FIELDS}
    return JSONResponse({
        "econ": econ,
        "metrics": {
            "reached": reached, "leads": leads, "deals": deals, "kev": kev,
            "total_cost": round(total_cost), "revenue": round(rev),
            "cost_per_lead": cost_per_lead, "cost_per_deal": cost_per_deal,
            "cost_per_kev": cost_per_kev, "roi": roi,
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
        # Гейт «в поле промпт, а не письмо». Каждая строка этого поля уходит человеку
        # ОТДЕЛЬНЫМ сообщением, поэтому оставленная инструкция («# Формат вывода…»,
        # «ВАЖНО:», «- каждая новая строка = отдельное сообщение») превращается в
        # десяток сообщений живым людям. Грубые признаки — жёсткий отказ (правь текст,
        # обхода нет), мягкие — обычное подтверждение оператором.
        from channels import opener_lint
        problems = opener_lint.lint(row["message_template"])
        block = opener_lint.blocking_message(problems)
        if block:
            return JSONResponse({"error": block}, status_code=400)
        if problems and not force:
            return JSONResponse({"needs_confirm": True,
                                 "warn": "Шаблон первого сообщения выглядит подозрительно:\n"
                                         + opener_lint.report(problems)
                                         + "\n\nВсё равно отправить живым людям?"})
        # Очередь пуста не всегда значит «база кончилась» — «☐ снять все» в окне «Кто
        # в рассылке» кладёт в паузу ВСЮ текущую очередь разом. Запускать процесс,
        # который найдёт 0 контактов и молча выйдет, — только путать оператора: он
        # видел «запущено», а в ленте ничего не появилось. Проверяем ДО подпроцесса.
        camp_row = conn.execute("SELECT audience_tag, channel, tg_verified_only FROM campaigns "
                                "WHERE id=?", (cid,)).fetchone()
        queue_now = _audience_count(conn, cid, camp_row["audience_tag"], camp_row["channel"],
                                    verified_only=bool(camp_row["tg_verified_only"] if
                                                       camp_row["tg_verified_only"] is not None else 1))
        paused_now = conn.execute(
            "SELECT COUNT(*) c FROM campaign_paused_contacts WHERE campaign_id=?", (cid,)
        ).fetchone()["c"]
        if queue_now == 0 and paused_now > 0 and not force:
            return JSONResponse({"needs_confirm": True,
                                 "warn": f"В очереди 0 контактов, потому что {paused_now} снято "
                                         f"вручную в окне «Кто в рассылке» (кнопка «снять все» "
                                         f"выключает сразу всю текущую очередь). Верни их галочкой "
                                         f"там, иначе запуск сейчас ничего не отправит.\n\n"
                                         f"Всё равно запустить?"})
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
        # Запущенная кампания без авто-ответа — это рассылка в одну сторону: агент
        # отправил первое сообщение, человек ответил, а бот молчит, потому что
        # где-то раньше выключили глобальный тумблер. Обнаружили живьём — 5 дней
        # переписки скопилось без единого ответа с боевых аккаунтов. Тумблер в
        # интерфейсе убран: авто-ответ теперь просто часть «кампания запущена».
        database.set_setting(conn, "tg_auto_reply", "on")
    # Шлём в отдельном процессе, чтобы не блокировать веб и не конфликтовать с event loop FastAPI.
    subprocess.Popen(
        [sys.executable, "-m", "channels.campaign_send", str(cid), "--limit", str(limit)],
        cwd=str(BASE_DIR.parent),
    )
    checking = _kick_tgcheck(cid)
    return JSONResponse({"ok": True, "launched": limit, "tgcheck_started": checking})


def _kick_tgcheck(cid: int) -> int:
    """Догнать пробивом аудиторию кампании, у которой включена защита от бана.

    Без этого защита выглядит как поломка: галочка стоит, непробитых 180, в очередь
    попадает 0 — и кампания «молча не шлёт». Пробив дозированный (25 номеров на аккаунт
    в сутки, контакт удаляется из книги сразу), поэтому запускать его можно смело: он
    не «долбит» Telegram, а капает. --tag сужает до аудитории ИМЕННО этой кампании,
    иначе phone_resolve идёт по ORDER BY id и свежая кампания ждёт очереди неделями.

    Возвращает, сколько номеров ждут пробива (0 = догонять нечего).
    """
    import subprocess    # как и в остальных запускающих функциях этого модуля —
    import sys           # sys/subprocess тут импортируются локально, не на уровне файла
    try:
        with database.get_conn() as conn:
            camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
            if not camp or not camp["tg_verified_only"]:
                return 0
            camp = dict(camp)
            if "telegram" not in [c.strip() for c in (camp.get("channel") or "").split(",")]:
                return 0
            where, params = _audience_where(cid, camp.get("audience_tag"), camp.get("channel"))
            pending = conn.execute(
                f"SELECT COUNT(*) c FROM contacts WHERE {where} AND NOT {database.TG_REACHABLE_SQL}"
                " AND phone IS NOT NULL AND phone<>'' AND tg_checked_at IS NULL",
                params).fetchone()["c"]
            per = int(database.get_setting(conn, "tgcheck_per", "25"))
        if not pending:
            return 0
        args = [sys.executable, "-m", "channels.phone_resolve", "--per", str(per)]
        tag = (camp.get("audience_tag") or "").strip()
        if tag:
            args += ["--tag", tag]
        subprocess.Popen(args, cwd=str(BASE_DIR.parent))
        return pending
    except Exception as e:  # noqa: BLE001
        print(f"[tgcheck kick] {e}")
        return 0


@app.post("/api/campaign/{cid}/stop")
def campaign_stop(cid: int) -> JSONResponse:
    """Остановить кампанию: новые заходы (▶ Запустить / автопланировщик опенера)
    больше не запускаются. Уже отправляющийся в фоне процесс (если запущен только
    что) доработает свою пачку — он короткоживущий и сам завершится.

    ВАЖНО: чистим и очередь доотправки опенера (opener_queue). Раньше «Стоп» её не
    трогал — статус кампании менялся, а фоновый тик продолжал доливать оставшиеся
    строки первого сообщения по одной раз в 1-3 минуты. Со стороны это выглядело как
    «нажал стоп, а оно всё равно пишет людям» ещё полчаса."""
    with database.get_conn() as conn:
        row = conn.execute("SELECT name, status FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        # Даже если кампания уже не 'running', очередь могла остаться висеть — стоп
        # обязан её погасить, иначе остановить недописанные опенеры нечем вообще.
        dropped = conn.execute("DELETE FROM opener_queue WHERE campaign_id=?", (cid,)).rowcount
        if row["status"] != "running":
            if not dropped:
                return JSONResponse({"error": f"кампания не запущена (статус: {row['status']})"},
                                    status_code=400)
            database.add_event(conn, "campaign_stop", f"⏸ Догашена очередь опенера «{row['name']}»",
                               f"снято недоотправленных опенеров: {dropped}",
                               level="info", campaign_id=cid)
            return JSONResponse({"ok": True, "dropped_openers": dropped})
        conn.execute("UPDATE campaigns SET status='paused' WHERE id=?", (cid,))
        database.add_event(conn, "campaign_stop", f"⏸ Кампания остановлена «{row['name']}»",
                           "новые заходы не запускаются, пока не нажмёшь «▶ Запустить» заново"
                           + (f"; снято недоотправленных опенеров: {dropped}" if dropped else ""),
                           level="info", campaign_id=cid)
    return JSONResponse({"ok": True, "dropped_openers": dropped})


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
        # Тест шлёт на свои номера, но смотреть, во что превратился промпт, надо в
        # предпросмотре, а не 15 сообщениями себе в Telegram по одному в 2 минуты.
        from channels import opener_lint
        block = opener_lint.blocking_message(opener_lint.lint(row["message_template"]))
        if block:
            return JSONResponse({"error": block}, status_code=400)
        # Сбрасываем статус тестовых контактов — чтобы тест срабатывал повторно.
        # НО только у тех, с кем разговор ещё не начался: сброс шёл безусловно, и
        # человеку, уже ведущему переписку, прилетал опенер «добрый день, правильно
        # обращаюсь?» поверх диалога (вживую поймали три штуки подряд), а статус
        # in_dialog/meeting_set затирался на 'new' — состояние диалога терялось.
        tag = (row["audience_tag"] or "").strip()
        where_test = "COALESCE(is_test,0)=1"
        params_test: list = []
        if tag:
            where_test += " AND tags LIKE ?"
            params_test.append(f"%{tag}%")
        rows_test = conn.execute(
            f"SELECT id, status FROM contacts WHERE {where_test}", params_test).fetchall()
        test_ids = [r["id"] for r in rows_test]
        # кого пропускаем и почему — покажем оператору словами, а не молча
        skipped = [r["id"] for r in rows_test if (r["status"] or "") in _TEST_KEEP_STATUS]
        resettable = [r["id"] for r in rows_test if r["id"] not in set(skipped)]
        if resettable:
            conn.execute(
                "UPDATE contacts SET status='new' WHERE id IN ({})".format(
                    ",".join("?" * len(resettable))),
                resettable,
            )
        # Очищаем старые записи очереди тестовых контактов (чтобы не было дублей).
        # Только у тех, кого реально перезапускаем: у пропущенных остаток опенера
        # трогать нельзя — там идёт живая переписка.
        if resettable:
            conn.execute(
                "DELETE FROM opener_queue WHERE contact_id IN ({}) AND campaign_id=?".format(
                    ",".join("?" * len(resettable))
                ),
                (*resettable, cid),
            )
        # Считаем ИМЕННО тех, кто уйдёт в этот заход. Раньше здесь был глобальный
        # COUNT по всем is_test в базе — цифра не сходилась с тем, что реально уходит
        # по кампании с тегом.
        n_test = len(resettable)
        if not n_test:
            if skipped:
                return JSONResponse({"error": (
                    f"все тест-номера ({len(skipped)}) уже в диалоге — опенер им повторно "
                    f"не шлём, чтобы не затирать переписку. Добавь новый номер или напиши "
                    f"в существующий диалог руками.")}, status_code=400)
            return JSONResponse({"error": "нет тест-номеров (is_test=1). Добавь свои "
                                          "номера в тест-контакты кампании."}, status_code=400)
        note = f"сброс {n_test} тестовых контактов → статус new, отправка"
        if skipped:
            note += f"; пропущено {len(skipped)} — уже в диалоге, опенер не дублируем"
        database.add_event(conn, "campaign_test", f"🧪 Тест кампании «{row['name']}»",
                           note, level="good", campaign_id=cid)
    subprocess.Popen(
        [sys.executable, "-m", "channels.campaign_send", str(cid), "--limit", "10", "--test"],
        cwd=str(BASE_DIR.parent),
    )
    return JSONResponse({"ok": True, "test_targets": n_test, "skipped_in_dialog": len(skipped)})


@app.post("/api/campaign/{cid}/test/reset_dialogs")
def campaign_test_reset_dialogs(cid: int) -> JSONResponse:
    """Обнулить переписку с ТЕСТОВЫМИ номерами этой кампании — чтобы гонять тест «с
    нуля», а не как продолжение вчерашнего разговора.

    Простой сброс статуса (кнопка «🧪 Тест») специально НЕ трогает историю сообщений
    (см. campaign_test выше) — на живом лиде их терять нельзя. Но для СВОИХ тестовых
    номеров это оборачивается обратной стороной: агент при следующем ответе читает
    всю историю целиком и путает старый диалог с новым («второе представление»,
    ссылка со вчерашней договорённости и т.п.) — оператор жаловался ровно на это.

    Поэтому здесь — полное удаление: сообщения, сделки (встречи), очередь опенера,
    запись «уже отправлено» в этой кампании. Статус — в 'new'. is_test=1 — ЖЁСТКОЕ
    условие запроса (не полагаемся на то, что в audience_tag случайно не окажется
    боевого контакта): без него один вызов мог бы стереть переписку живому лиду.
    """
    with database.get_conn() as conn:
        row = conn.execute("SELECT name, audience_tag FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            return JSONResponse({"error": "кампания не найдена"}, status_code=404)
        where = "COALESCE(is_test,0)=1"
        params: list = []
        tag = (row["audience_tag"] or "").strip()
        if tag:
            where += " AND tags LIKE ?"
            params.append(f"%{tag}%")
        ids = [r["id"] for r in conn.execute(f"SELECT id FROM contacts WHERE {where}", params).fetchall()]
        if not ids:
            return JSONResponse({"error": "нет тестовых номеров у этой кампании"}, status_code=400)
        qmarks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM messages WHERE contact_id IN ({qmarks})", ids)
        conn.execute(f"DELETE FROM deals WHERE contact_id IN ({qmarks})", ids)
        conn.execute(f"DELETE FROM opener_queue WHERE contact_id IN ({qmarks})", ids)
        conn.execute(f"DELETE FROM campaign_contacts WHERE campaign_id=? AND contact_id IN ({qmarks})",
                    (cid, *ids))
        conn.execute(f"UPDATE contacts SET status='new' WHERE id IN ({qmarks})", ids)
        database.add_event(conn, "campaign_test", f"🔄 Обнулена переписка «{row['name']}»",
                           f"тестовых контактов: {len(ids)} — сообщения, встречи и очередь удалены, "
                           f"статус new", level="good", campaign_id=cid)
    return JSONResponse({"ok": True, "reset": len(ids)})


@app.post("/api/campaign/{cid}/archive")
def campaign_archive(cid: int, payload: dict = Body(default={})) -> JSONResponse:
    """В архив / из архива. Не удаляет ничего — просто прячет из основного списка
    кампаний, чтобы старые/неактуальные не путались под ногами. Достать обратно
    можно в любой момент — данные (лог, статистика) остаются нетронутыми."""
    archived = 1 if payload.get("archived", True) else 0
    with database.get_conn() as conn:
        conn.execute("UPDATE campaigns SET archived=? WHERE id=?", (archived, cid))
    return JSONResponse({"ok": True, "archived": bool(archived)})


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
