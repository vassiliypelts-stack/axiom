"""Поддержка своих Telegram-каналов: живые реакции и 1-3 комментария к новым постам.

Зачем. Пост без единой реакции и без обсуждения читается как мёртвый канал, даже
если текст сильный: новый подписчик смотрит на «социальное доказательство» раньше,
чем на содержание. Наши аккаунты и так живут в Telegram (прогрев, живость), поэтому
пусть они ведут себя как обычные подписчики своих же каналов.

Как не спалиться:
  • Реакций под постом В ИТОГЕ 6-8% от подписчиков (boost_react_min/max): процент
    свой у каждого поста, одинаковые «14 реакций» под каждым постом видны сразу.
    Живые реакции входят в этот итог: добавляем только недостающее, а перед каждой
    реакцией ещё раз сверяем счёт под постом.
  • Новые посты ловим раз в 5 минут, а 2 раза в день (boost_sweep_hours, МСК) идёт
    обход последних постов: подписчиков стало больше или реакцию сняли — доливаем
    до цели аккаунтами, которых под этим постом ещё не было.
  • Реакции растягиваются на 1-3 часа, гуще в первые минуты, как у живой ленты:
    подписчики видят пост в разное время. Каждый реагирующий сначала «смотрит»
    пост (+1 просмотр), потом ставит реакцию.
  • Комментарии 1-3 (больше канал, больше можно), каждый зацеплен за конкретную
    деталь поста: вопрос по сути, свой опыт или нюанс. Текст проходит фильтр
    нейрослопа: «отличный пост», «спасибо за информацию» и т.п. не уходят никогда.
    Не прошёл фильтр — комментария не будет: лучше тишина, чем слоп.
  • Комментарии не пишутся ночью по Москве, один аккаунт комментирует не чаще пары
    раз в сутки, комментаторы подбираются из тех, кто давно не писал.

Свой Telegram-клиент модуль не поднимает: одна сессия в двух местах — это
AuthKeyDuplicated и сгоревший аккаунт. Всё делается через подключения слушателя
(listener.CLIENTS), как read_status и send_via_listener. Аккаунт, которого слушатель
сейчас не держит, просто пропускается.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import random
import re
import time

from pydantic import BaseModel, Field

from db import database

DEFAULT_CHANNELS = "winresult,mindcode50,neiro24x7"
SCAN_EVERY_SEC = 300          # как часто смотреть, не вышел ли новый пост
REACT_SPREAD_MIN = 180        # за сколько минут растянуть реакции
MAX_ACTIONS_PER_TICK = 12     # чтобы один тик не превращался в залп
COMMENTS_PER_ACC_DAY = 2      # один и тот же аккаунт не должен мелькать под каждым постом
NIGHT_MSK = (1, 8)            # с 01:00 до 08:00 МСК комментарии не пишем

# Позитивные реакции с весами. Сердце — «❤» БЕЗ U+FE0F: именно так его ждёт
# Telegram, вариант с вариационным селектором может прийти ReactionInvalid.
POSITIVE = [("👍", 30), ("❤", 24), ("🔥", 24), ("👏", 7), ("💯", 4), ("🤝", 3),
            ("🙏", 3), ("⚡", 2), ("🏆", 2), ("🤩", 1)]

# Что сразу выдаёт накрутку или модель. Регэксп по нижнему регистру.
_SLOP = re.compile("|".join([
    r"(отличн|полезн|классн|крут|хорош|интересн|супер|топов|шикарн|прекрасн|замечательн)\w*\s+(пост|стать|материал|контент|текст|разбор)",
    r"спасибо\s+за\s+(пост|стать|информац|материал|контент|полезн|разбор|то,? что)",
    r"благодар\w*\s+за", r"очень\s+(интересно|полезно|познавательно|актуально)",
    r"согласен\s+на\s+(все\s+)?100", r"\bв\s+точку\b", r"^\s*(огонь|топ|база|жиза|супер|класс)\W*$",
    r"подписал\w*", r"автору\s+респект", r"продолжайте", r"ждем\s+(еще|продолжени)",
    r"ценн\w+\s+(информац|совет|мысл)", r"не\s+могу\s+не\s+согласит", r"как\s+никогда\s+актуальн",
    r"вы\s+абсолютно\s+правы", r"полностью\s+согласен", r"раскрыли\s+тему",
    r"важн\w+\s+тем", r"глубок\w+\s+мысл", r"#\w", r"!!",
]))
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


# ---------------------------------------------------------------- настройки --

def settings(conn) -> dict:
    g = lambda k, d: database.get_setting(conn, k, d)  # noqa: E731
    chans = [c for c in (_norm_channel(x) for x in re.split(r"[\s,;]+", g("boost_channels", DEFAULT_CHANNELS) or "")) if c]
    cfg = {
        "enabled": g("boost_enabled", "off") == "on",
        "channels": chans,
        "react_min": _num(g("boost_react_min", "6"), 6.0, 0.0, 50.0),
        "react_max": _num(g("boost_react_max", "8"), 8.0, 0.0, 50.0),
        "sweep_hours": sorted({int(h) for h in re.findall(r"\d+", g("boost_sweep_hours", "10,19") or "")
                               if 0 <= int(h) <= 23}),
        "sweep_posts": int(_num(g("boost_sweep_posts", "5"), 5, 1, 20)),
        "comments_max": int(_num(g("boost_comments_max", "3"), 3, 0, 5)),
        "max_age_h": _num(g("boost_max_age_h", "6"), 6.0, 0.5, 72.0),
        # Общий потолок комментариев в сутки на все каналы и «только вопросы по
        # сути»: старт осторожный, качество сначала смотрим глазами (01.10.2026).
        "comments_day": int(_num(g("boost_comments_day", "2"), 2, 0, 50)),
        "questions_only": g("boost_questions_only", "on") == "on",
    }
    if cfg["react_min"] > cfg["react_max"]:
        cfg["react_min"], cfg["react_max"] = cfg["react_max"], cfg["react_min"]
    return cfg


def _num(v, default, lo, hi):
    try:
        x = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def _norm_channel(raw: str) -> str:
    """'https://t.me/winresult', '@winresult', 'winresult' → 'winresult'."""
    s = (raw or "").strip()
    s = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", s, flags=re.I)
    s = s.lstrip("@").split("/")[0].split("?")[0]
    return s.lower() if re.fullmatch(r"[A-Za-z0-9_]{4,64}", s) else ""


def _utc(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _now() -> _dt.datetime:
    return _dt.datetime.utcnow()


# ----------------------------------------------------------------- аккаунты --

def _eligible(conn, ids: list[int]) -> list[dict]:
    """Кто из подключённых слушателем может поддерживать канал.

    Родные (protected) не трогаем никогда: это личный номер владельца, обычно он же
    админ канала. Служебные (уведомления) тоже: сгорит — встречи пропадут. Под
    паузой PeerFlood/FloodWait или замороженные Telegram'ом — мимо."""
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, status, COALESCE(tg_name,label,username,phone) AS name FROM accounts "
        f"WHERE id IN ({q}) AND status IN ('active','warming') "
        "AND COALESCE(protected,0)=0 AND COALESCE(acc_role,'')<>'service' "
        "AND (spam_pause_until IS NULL OR spam_pause_until < datetime('now')) "
        "AND (flood_wait_until IS NULL OR flood_wait_until < datetime('now')) "
        "AND frozen_at IS NULL AND COALESCE(session_alive,1)<>0", ids).fetchall()
    return [dict(r) for r in rows]


def _comment_load(conn) -> dict[int, int]:
    """Сколько комментариев каждый аккаунт написал/запланировал за сутки."""
    rows = conn.execute(
        "SELECT account_id, COUNT(*) c FROM boost_actions WHERE kind='comment' "
        "AND status IN ('pending','done') AND due_at > datetime('now','-1 day') "
        "GROUP BY account_id").fetchall()
    return {r["account_id"]: r["c"] for r in rows}


# ------------------------------------------------------------------ расчёт --

def react_target(subs: int, pct: float) -> int:
    """Сколько реакций должно быть под постом в итоге (вместе с живыми)."""
    if pct <= 0:
        return 0
    return max(1, round(subs * pct / 100.0))


def plan_counts(subs: int, existing: int, n_react: int, n_comment: int, pct: float,
                cmax: int) -> tuple[int, int, int]:
    """(цель реакций, сколько реакций добавить, сколько комментариев) для поста."""
    target = react_target(subs, pct)
    reacts = max(0, min(target - existing, n_react))
    # Ступени по размеру канала: под постом маленького канала три комментария
    # подряд выглядят подозрительнее, чем ни одного.
    hi = 1 if subs < 300 else 2 if subs < 1500 else 3
    hi = min(hi, cmax, n_comment)
    comments = random.randint(1, hi) if hi >= 1 else 0
    return target, reacts, comments


def reactions_total(m) -> int:
    r = getattr(m, "reactions", None)
    return sum(int(getattr(x, "count", 0) or 0) for x in (getattr(r, "results", None) or []))


def _react_delays(n: int) -> list[float]:
    """Минуты от выхода поста: гуще в начале, хвост до REACT_SPREAD_MIN."""
    out = []
    for _ in range(n):
        d = random.expovariate(1 / 35.0) + random.uniform(0.5, 3)
        out.append(min(d, REACT_SPREAD_MIN * random.uniform(0.8, 1.0)))
    return sorted(out)


def _skip_night(when: _dt.datetime) -> _dt.datetime:
    """Комментарий, выпавший на ночь по Москве, переносим на утро 08:00-10:30 МСК."""
    msk = when + _dt.timedelta(hours=3)
    if NIGHT_MSK[0] <= msk.hour < NIGHT_MSK[1]:
        morning = msk.replace(hour=NIGHT_MSK[1], minute=0, second=0) + _dt.timedelta(
            minutes=random.uniform(0, 150))
        return morning - _dt.timedelta(hours=3)
    return when


def pick_reaction(allowed: set[str] | None) -> str | None:
    """Случайная позитивная реакция из разрешённых в канале (None = разрешены все)."""
    pool = [(e, w) for e, w in POSITIVE if allowed is None or e in allowed]
    if not pool:
        return None
    return random.choices([e for e, _ in pool], weights=[w for _, w in pool])[0]


# ---------------------------------------------------------------- тексты --

QUESTION_ROLES = [
    "вопрос по одной конкретной детали поста: как это сделать/применить в своей ситуации, "
    "уточнение шага, цифры, срока или инструмента",
    "практичный вопрос «а если…»: как быть в похожей, но другой ситуации",
    "уточняющий вопрос про то, что в посте упомянуто, но не раскрыто: как именно, сколько, "
    "чем, с чего начать",
]

COMMENT_ROLES = [
    QUESTION_ROLES[0],
    "короткий отклик из своего бытового опыта, который подтверждает или дополняет ОДНУ мысль поста "
    "(без громких цифр и регалий)",
    "вежливый нюанс или лёгкое несогласие с одной мыслью поста, с коротким «почему»",
    QUESTION_ROLES[1],
    "очень короткая разговорная реакция на одну фразу или пример из поста, 2-7 слов",
]

COMMENT_SYSTEM = """Ты пишешь комментарии к посту в Telegram-канале от лица РАЗНЫХ обычных подписчиков.
Это живые люди с телефона, не копирайтеры и не боты. Задача: чтобы под постом было
настоящее обсуждение по сути, а не дежурные похвалы.

ЖЁСТКИЕ ПРАВИЛА:
- Каждый комментарий цепляется за ОДНУ конкретную деталь поста: цифру, пример, фразу,
  совет. Читатель должен понять, что человек пост реально прочитал.
- НИКАКИХ похвал посту в целом: «отличный/полезный пост», «спасибо за информацию»,
  «очень интересно», «в точку», «согласен на 100%», «огонь», «автору респект», «ждём
  продолжения». Это нейрослоп, такие комментарии запрещены.
- Не пересказывай пост и не подводи итоги. Не задавай вопрос, ответ на который уже
  есть в посте.
- Пиши как с телефона: без длинного тире «—» (запятая или точка), без буквы «ё», без
  эмодзи, без хэштегов, без «!!», без канцелярита и без перечислений тройками.
  Скобка «)» допустима максимум в одном комментарии. Можно с маленькой буквы, можно
  без точки в конце. Разговорные слова уместны: «а», «кстати», «хм», «у нас», «вот».
- Длина разная: от 2-5 слов до 25 слов максимум. Комментарии не похожи друг на
  друга ни первым словом, ни длиной, ни построением.
- Не выдумывай про себя громких фактов (доходы, должности, «я 10 лет в теме»).
  Опыт, если упоминаешь, бытовой и правдоподобный.
- Не обращайся к автору по имени, не льсти, не упоминай ИИ/нейросети, если пост не
  про них. Не пиши от лица автора канала.
- Если пост без содержания (реклама, анонс, репост ссылки, одна картинка), верни
  меньше комментариев или пустой список. Лучше ничего, чем пустая болтовня.

Верни JSON: {"comments": ["...", "..."]} — ровно в том порядке, в каком даны роли."""


class _Comments(BaseModel):
    comments: list[str] = Field(default_factory=list, description="тексты комментариев")


def _model_spec() -> str:
    import os
    import config
    return os.getenv("BOOST_MODEL", "") or config.agent_model(None)


def humanize(text: str) -> str:
    """Финальная шлифовка: deslop + мелкие человеческие привычки."""
    from channels import deslop
    s = deslop.clean(text).strip().strip('"«»').strip()
    s = s.replace("\n", " ")
    if s.endswith(".") and not s.endswith("..") and random.random() < 0.6:
        s = s[:-1]
    if s and s[0].isupper() and random.random() < 0.35 and not (len(s) > 1 and s[1].isupper()):
        s = s[0].lower() + s[1:]
    return s


def is_slop(text: str, post_text: str = "") -> bool:
    t = (text or "").lower().replace("ё", "е")
    if not t or len(t) > 220 or len(t.split()) < 2:
        return True
    if _SLOP.search(t) or _EMOJI.search(text):
        return True
    if "нейросет" in t and "нейросет" not in (post_text or "").lower():
        return True
    return False


def generate_comments(post_text: str, channel_title: str, n: int,
                      questions_only: bool = False) -> list[str]:
    """n комментариев по сути поста. Отбракованные слопом просто выпадают."""
    if n <= 0 or len((post_text or "").strip()) < 60:
        return []
    from agent import llm
    pool = QUESTION_ROLES if questions_only else COMMENT_ROLES
    roles = random.sample(pool, k=min(n, len(pool)))
    user = (f"Канал: {channel_title}\n\nПОСТ:\n{post_text[:3500]}\n\n"
            f"Нужно {len(roles)} комментари{'й' if len(roles) == 1 else 'я'}, роли по порядку:\n"
            + "\n".join(f"{i + 1}. {r}" for i, r in enumerate(roles)))
    try:
        out = llm.structured(_model_spec(), COMMENT_SYSTEM, [{"role": "user", "content": user}],
                             output_format=_Comments, max_tokens=700, timeout=90)
    except Exception as e:  # noqa: BLE001
        print(f"[boost] модель не ответила: {e}")
        return []
    seen: set[str] = set()
    good = []
    for c in (out.comments if out else [])[:n]:
        c = humanize(c or "")
        key = re.sub(r"\W+", "", c.lower())[:40]
        if is_slop(c, post_text) or key in seen:
            print(f"[boost] отбраковал комментарий: {c!r}")
            continue
        seen.add(key)
        good.append(c)
    return good


# ------------------------------------------------------- Telegram (в loop слушателя) --

def _run(coro, timeout: float = 90.0):
    from channels import listener
    if listener._LOOP is None:
        raise RuntimeError("слушатель не запущен")
    return asyncio.run_coroutine_threadsafe(coro, listener._LOOP).result(timeout=timeout)


async def _channel_snapshot(client, username: str, max_age_h: float, last_n: int = 0) -> dict:
    """Свежие посты канала + число подписчиков + есть ли обсуждение + разрешённые реакции."""
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.types import ChatReactionsNone, ChatReactionsSome, ReactionEmoji

    ent = await client.get_entity(username)
    full = await client(GetFullChannelRequest(ent))
    fc = full.full_chat
    ar = getattr(fc, "available_reactions", None)
    if isinstance(ar, ChatReactionsNone):
        allowed: set[str] | None = set()
    elif isinstance(ar, ChatReactionsSome):
        allowed = {r.emoticon.replace("️", "") for r in ar.reactions if isinstance(r, ReactionEmoji)}
    else:
        allowed = None
    # last_n — разовая поддержка последних постов кнопкой, без оглядки на возраст.
    cutoff = (_dt.datetime.min.replace(tzinfo=_dt.timezone.utc) if last_n
              else _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=max_age_h))
    posts: dict[object, dict] = {}
    async for m in client.iter_messages(ent, limit=max(15, last_n * 4)):
        if getattr(m, "action", None) or not m.date or m.date < cutoff:
            continue
        # Альбом = несколько сообщений с одним grouped_id; поддерживаем его один раз,
        # через сообщение с подписью (на него и вешаются комментарии).
        key = m.grouped_id or m.id
        cur = posts.get(key)
        txt = m.message or ""
        if cur is None or (txt and not cur["text"]) or (not txt and not cur["text"] and m.id < cur["msg_id"]):
            posts[key] = {"msg_id": m.id, "text": txt, "date": m.date.replace(tzinfo=None),
                          "reactions": reactions_total(m)}
    return {"title": getattr(ent, "title", username), "subs": int(getattr(fc, "participants_count", 0) or 0),
            "linked": bool(getattr(fc, "linked_chat_id", None)), "allowed": allowed,
            "posts": sorted(posts.values(), key=lambda p: p["msg_id"])[-last_n if last_n else 0:]}


async def _ensure_joined(client, ent) -> None:
    from telethon.tl.functions.channels import JoinChannelRequest
    if getattr(ent, "left", False):
        await client(JoinChannelRequest(ent))
        await asyncio.sleep(random.uniform(2, 6))


async def _do_react(client, username: str, msg_id: int, emoji: str, target: int | None = None) -> str | None:
    """Поставить реакцию. None — не ставили: под постом цель уже набрана."""
    from telethon.errors import RPCError
    from telethon.tl.functions.messages import GetMessagesViewsRequest, SendReactionRequest
    from telethon.tl.types import ReactionEmoji

    ent = await client.get_entity(username)
    await _ensure_joined(client, ent)
    # Сначала «посмотрел» пост (живой просмотр), потом не сразу отреагировал.
    try:
        await client(GetMessagesViewsRequest(peer=ent, id=[msg_id], increment=True))
    except RPCError:
        pass
    await asyncio.sleep(random.uniform(4, 15))
    if target:
        # За час-другой люди могли наставить своих: итог важнее нашего плана.
        cur = await client.get_messages(ent, ids=msg_id)
        if cur is not None and reactions_total(cur) >= target:
            return None
    try:
        await client(SendReactionRequest(peer=ent, msg_id=msg_id, reaction=[ReactionEmoji(emoticon=emoji)]))
    except RPCError as e:
        if "REACTION" not in str(e).upper() or emoji == "👍":
            raise
        emoji = "👍"
        await client(SendReactionRequest(peer=ent, msg_id=msg_id, reaction=[ReactionEmoji(emoticon=emoji)]))
    return emoji


async def _do_comment(client, username: str, msg_id: int, text: str) -> int:
    from telethon.errors import RPCError
    from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
    from telethon.tl.functions.messages import GetMessagesViewsRequest
    from telethon.tl.types import PeerChannel

    ent = await client.get_entity(username)
    await _ensure_joined(client, ent)
    try:
        await client(GetMessagesViewsRequest(peer=ent, id=[msg_id], increment=True))
    except RPCError:
        pass
    # «Читает и набирает»: пауза зависит от длины текста.
    await asyncio.sleep(random.uniform(8, 20) + len(text) / random.uniform(4, 7))
    try:
        m = await client.send_message(ent, text, comment_to=msg_id)
    except RPCError as e:
        # Обсуждение требует вступить в чат комментариев — вступаем и повторяем.
        if "GUEST" not in type(e).__name__.upper() and "GUEST" not in str(e).upper():
            raise
        full = await client(GetFullChannelRequest(ent))
        chat = await client.get_entity(PeerChannel(full.full_chat.linked_chat_id))
        await client(JoinChannelRequest(chat))
        await asyncio.sleep(random.uniform(3, 8))
        m = await client.send_message(ent, text, comment_to=msg_id)
    return int(getattr(m, "id", 0) or 0)


# ------------------------------------------------------------------- план --

def scan(force: bool = False, last_n: int = 0, sweep: bool = False) -> dict:
    """Найти новые посты своих каналов и расписать под ними реакции/комментарии.
    last_n > 0 — взять последние N постов каждого канала независимо от возраста.
    sweep — обход: последние sweep_posts постов, уже известным доливаем реакции
    до цели (комментарии только под новыми постами, при первом плане)."""
    from channels import listener

    with database.get_conn() as conn:
        cfg = settings(conn)
        if not force:
            last = float(database.get_setting(conn, "boost_last_scan_ts", "0") or 0)
            if time.time() - last < SCAN_EVERY_SEC:
                return {"skipped": "рано"}
        database.set_setting(conn, "boost_last_scan_ts", str(time.time()))
        accs = _eligible(conn, list(listener.CLIENTS.keys()))
    if sweep:
        last_n = max(last_n, cfg["sweep_posts"])
    if not accs:
        return {"error": "нет подключённых слушателем аккаунтов, пригодных для поддержки"}

    result = {"planned_posts": 0, "reacts": 0, "comments": 0, "topped_up": 0, "channels": {}}
    for ch in cfg["channels"]:
        # Смотреть канал берём случайный аккаунт, каждый раз другой. Не прочитал —
        # пробуем следующего: у отдельного номера бывает свой лимит на поиск по
        # username (01.10.2026 @mindcode50 у одного «не существовал», у другого нашёлся).
        snap = None
        for reader in random.sample(accs, k=min(3, len(accs))):
            client = listener.CLIENTS.get(reader["id"])
            if client is None:
                continue
            try:
                snap = _run(_channel_snapshot(client, ch, cfg["max_age_h"], last_n))
                break
            except Exception as e:  # noqa: BLE001
                result["channels"][ch] = f"не прочитался: {str(e)[:120]}"
                print(f"[boost] @{ch} (#{reader['id']}): {e}")
        if snap is None:
            continue
        with database.get_conn() as conn:
            done = {r["msg_id"] for r in conn.execute(
                "SELECT msg_id FROM boost_posts WHERE channel=?", (ch,)).fetchall()}
        fresh = [p for p in snap["posts"] if p["msg_id"] not in done]
        result["channels"][ch] = {"subs": snap["subs"], "new_posts": len(fresh)}
        for p in fresh:
            r, c = _plan_post(ch, snap, p, accs, cfg)
            result["planned_posts"] += 1
            result["reacts"] += r
            result["comments"] += c
        if sweep:
            for p in snap["posts"]:
                if p["msg_id"] in done:
                    n = _top_up(ch, snap, p, accs, cfg)
                    result["topped_up"] += n
                    result["reacts"] += n
    return result


def _top_up(ch: str, snap: dict, post: dict, accs: list[dict], cfg: dict) -> int:
    """Обход: долить реакции под уже известным постом до цели. Цель считается от
    ТЕКУЩЕГО числа подписчиков, процент у поста свой и не меняется между обходами."""
    allowed = snap["allowed"]
    if not (allowed is None or allowed & {e for e, _ in POSITIVE}):
        return 0
    with database.get_conn() as conn:
        row = conn.execute("SELECT target_pct FROM boost_posts WHERE channel=? AND msg_id=?",
                           (ch, post["msg_id"])).fetchone()
        pct = (row["target_pct"] if row else None) or random.uniform(cfg["react_min"], cfg["react_max"])
        target = react_target(snap["subs"], pct)
        conn.execute("UPDATE boost_posts SET subs=?, target_pct=?, target=? WHERE channel=? AND msg_id=?",
                     (snap["subs"], pct, target, ch, post["msg_id"]))
        acts = conn.execute(
            "SELECT account_id, status FROM boost_actions WHERE channel=? AND msg_id=? AND kind='react' "
            "AND status IN ('pending','done')", (ch, post["msg_id"])).fetchall()
    used = {r["account_id"] for r in acts}
    pending = sum(1 for r in acts if r["status"] == "pending")
    # Наши уже поставленные реакции сидят в post["reactions"], ждущие — ещё нет.
    need = target - post["reactions"] - pending
    pool = [a for a in accs if a["id"] not in used]
    random.shuffle(pool)
    pool = pool[:max(0, need)]
    if not pool:
        return 0
    base = _now()
    rows = []
    for a, d in zip(pool, _react_delays(len(pool))):
        emoji = pick_reaction(allowed)
        if emoji:
            rows.append((ch, post["msg_id"], a["id"], "react", emoji, _utc(base + _dt.timedelta(minutes=d))))
    with database.get_conn() as conn:
        conn.executemany(
            "INSERT INTO boost_actions (channel, msg_id, account_id, kind, payload, due_at) "
            "VALUES (?,?,?,?,?,?)", rows)
    print(f"[boost] обход @{ch}/{post['msg_id']}: есть {post['reactions']}, цель {target}, "
          f"ждут {pending}, доливаю {len(rows)}")
    return len(rows)


def _plan_post(ch: str, snap: dict, post: dict, accs: list[dict], cfg: dict) -> tuple[int, int]:
    allowed = snap["allowed"]
    react_ok = allowed is None or bool(allowed & {e for e, _ in POSITIVE})
    with database.get_conn() as conn:
        load = _comment_load(conn)
    commenters_pool = [a for a in accs if a["status"] == "active"
                       and load.get(a["id"], 0) < COMMENTS_PER_ACC_DAY]
    with database.get_conn() as conn:
        today = conn.execute(
            "SELECT COUNT(*) c FROM boost_actions WHERE kind='comment' "
            "AND status IN ('pending','done') AND due_at > datetime('now','-1 day')").fetchone()["c"]
    left = max(0, cfg["comments_day"] - today)
    pct = random.uniform(cfg["react_min"], cfg["react_max"])
    target, n_react, n_comm = plan_counts(snap["subs"], post.get("reactions", 0),
                                          len(accs) if react_ok else 0,
                                          min(len(commenters_pool), left) if snap["linked"] else 0,
                                          pct, cfg["comments_max"])
    texts = generate_comments(post["text"], snap["title"], n_comm,
                              cfg["questions_only"]) if n_comm else []

    # Комментаторы: меньше всех писавшие за сутки, при равенстве случайно.
    random.shuffle(commenters_pool)
    commenters_pool.sort(key=lambda a: load.get(a["id"], 0))
    commenters = commenters_pool[:len(texts)]
    # Реагирующие: комментаторы обычно и реакцию ставят, остальное добираем случайно.
    others = [a for a in accs if a not in commenters]
    random.shuffle(others)
    reactors = (commenters + others)[:n_react]

    base = max(post["date"], _now() - _dt.timedelta(minutes=5))
    rows = []
    for a, d in zip(reactors, _react_delays(len(reactors))):
        emoji = pick_reaction(allowed)
        if emoji:
            rows.append((ch, post["msg_id"], a["id"], "react", emoji, _utc(base + _dt.timedelta(minutes=d))))
    t = base + _dt.timedelta(minutes=random.uniform(6, 30))
    for a, txt in zip(commenters, texts):
        rows.append((ch, post["msg_id"], a["id"], "comment", txt, _utc(_skip_night(t))))
        t += _dt.timedelta(minutes=random.uniform(12, 80))

    with database.get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO boost_posts (channel, msg_id, subs, target_pct, target, reacts, comments, post_text) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ch, post["msg_id"], snap["subs"], pct, target, sum(1 for r in rows if r[3] == "react"),
             len(texts), (post["text"] or "")[:500]))
        conn.executemany(
            "INSERT INTO boost_actions (channel, msg_id, account_id, kind, payload, due_at) "
            "VALUES (?,?,?,?,?,?)", rows)
    print(f"[boost] @{ch}/{post['msg_id']} ({snap['subs']} подп., есть {post.get('reactions', 0)}, "
          f"цель {target}): +{sum(1 for r in rows if r[3] == 'react')} реакций, {len(texts)} комм.")
    return sum(1 for r in rows if r[3] == "react"), len(texts)


# --------------------------------------------------------------- исполнение --

def execute_due() -> dict:
    """Выполнить действия, чьё время пришло. Порядок перемешан, между ними паузы."""
    from channels import antiban, listener

    ids = list(listener.CLIENTS.keys())
    if not ids:
        return {"done": 0, "failed": 0, "skipped": 0}
    q = ",".join("?" * len(ids))
    with database.get_conn() as conn:
        # Только аккаунты, которые слушатель держит прямо сейчас: остальные ждут, но
        # не занимают места в пачке и не блокируют чужие задания.
        due = [dict(r) for r in conn.execute(
            f"SELECT b.*, p.target FROM boost_actions b LEFT JOIN boost_posts p "
            f"ON p.channel=b.channel AND p.msg_id=b.msg_id "
            f"WHERE b.status='pending' AND b.due_at <= datetime('now') "
            f"AND b.account_id IN ({q}) ORDER BY b.due_at LIMIT ?", (*ids, MAX_ACTIONS_PER_TICK)).fetchall()]
        # Совсем протухшее (сервер лежал полдня) не догоняем пачкой — отменяем.
        conn.execute(
            "UPDATE boost_actions SET status='cancelled', error='просрочено' "
            "WHERE status='pending' AND due_at < datetime('now','-6 hours')")
    due = [a for a in due if a["due_at"] >= _utc(_now() - _dt.timedelta(hours=6))]
    stats = {"done": 0, "failed": 0, "skipped": 0}
    for i, a in enumerate(due):
        client = listener.CLIENTS.get(a["account_id"])
        if client is None:  # отключился между выборкой и исполнением
            stats["skipped"] += 1
            continue
        if i:
            time.sleep(random.uniform(2, 7))
        try:
            if a["kind"] == "react":
                used = _run(_do_react(client, a["channel"], a["msg_id"], a["payload"], a.get("target")))
                if used is None:
                    with database.get_conn() as conn:
                        conn.execute("UPDATE boost_actions SET status='cancelled', done_at=datetime('now'), "
                                     "error=? WHERE id=?", (f"цель {a['target']} уже набрана", a["id"]))
                    stats["skipped"] += 1
                    continue
                note = used
            else:
                mid = _run(_do_comment(client, a["channel"], a["msg_id"], a["payload"]), timeout=180)
                note = f"msg {mid}"
            with database.get_conn() as conn:
                conn.execute("UPDATE boost_actions SET status='done', done_at=datetime('now'), error=? "
                             "WHERE id=?", (note if a["kind"] == "comment" else None, a["id"]))
            stats["done"] += 1
        except Exception as e:  # noqa: BLE001
            kind = antiban.classify_error(e)
            with database.get_conn() as conn:
                conn.execute("UPDATE boost_actions SET status='failed', done_at=datetime('now'), error=? "
                             "WHERE id=?", (f"{kind}: {type(e).__name__}: {str(e)[:150]}", a["id"]))
                if kind in ("flood", "spam", "ban", "session_revoked", "frozen"):
                    # Аккаунту сейчас не до поддержки — снимаем остальные его задания.
                    conn.execute("UPDATE boost_actions SET status='cancelled', error=? "
                                 "WHERE status='pending' AND account_id=?",
                                 (f"аккаунт: {kind}", a["account_id"]))
            print(f"[boost] #{a['account_id']} {a['kind']} @{a['channel']}/{a['msg_id']}: {e}")
            stats["failed"] += 1
    return stats


def tick() -> None:
    """Один шаг фонового планировщика пульта (раз в минуту)."""
    with database.get_conn() as conn:
        on = settings(conn)["enabled"]
    # Выключил тумблер — значит стоп и для уже расписанного: задания ждут
    # (а через 6 часов отменяются как просроченные), но не исполняются.
    if not on:
        return
    scan()
    if _sweep_due():
        print(f"[boost] обход каналов: {scan(force=True, sweep=True)}")
    execute_due()


def _sweep_due() -> bool:
    """Пора ли обход: текущий час МСК в boost_sweep_hours и в этот час ещё не ходили."""
    msk = _now() + _dt.timedelta(hours=3)
    with database.get_conn() as conn:
        hours = settings(conn)["sweep_hours"]
        if msk.hour not in hours:
            return False
        slot = msk.strftime("%Y-%m-%d %H")
        if database.get_setting(conn, "boost_last_sweep", "") == slot:
            return False
        database.set_setting(conn, "boost_last_sweep", slot)
    return True
