"""Отчёт по каналу/чату: сколько публикует, когда, о чём и что заходит.

ЗАЧЕМ. Перед тем как лезть в нишу, надо понять, чем она живёт: о чём пишут массы,
какие темы всплывают, у кого что заходит. Тот же отчёт по каналу конкурента —
готовая разведка: ритм публикаций, вовлечённость, повестка.

ДВА ШАГА, намеренно разделённые:
  • collect() — сходить в Telegram и сложить посты в channel_posts (дорого, с паузами);
  • analyze() — посчитать отчёт ПО БАЗЕ (бесплатно, мгновенно, сколько угодно раз).
Разделение принципиальное: перебирать срезы и периоды нужно часто, а дёргать Telegram
на каждый пересчёт — это FloodWait и сожжённый аккаунт.

О ЧЁМ ПИШУТ. Считаем частоты слов и пар слов по текстам постов. Русский язык без
нормализации даёт мусор («инвестиции», «инвестиций», «инвестициям» — три разных
слова), поэтому слова схлопываются по усечённой основе, а показывается самая
частая живая форма. Это не лингвистика уровня pymorphy, но для вопроса «какие темы
всплывают» точности хватает, а лишней зависимости не появляется.

ЧЕСТНОСТЬ МЕТРИК. Просмотры/реакции снимаются в момент сбора и у свежего поста ещё
растут. Поэтому «топ по вовлечённости» считается только по постам старше суток —
иначе вчерашний пост всегда проигрывает, а сегодняшний выглядит провальным.

Запуск:
    python -m channels.channel_report --chat 42 --collect --days 30
    python -m channels.channel_report --chat 42                 # отчёт по тому, что уже собрано
    python -m channels.channel_report --chat 42 --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError

from db import database

COLLECT_PAUSE = (0.4, 1.1)     # антибан-пауза между пачками истории
DEFAULT_DAYS = 30
DEFAULT_LIMIT = 500            # потолок постов за один сбор

# Возраст, начиная с которого метрики поста считаются «устоявшимися».
FRESH_HOURS = 24


# --------------------------------------------------------------------------- #
#  Текст: нормализация, стоп-слова, темы                                       #
# --------------------------------------------------------------------------- #
_WORD_RE = re.compile(r"[а-яёa-z][а-яёa-z\-]{2,}", re.I)
_URL_RE = re.compile(r"https?://\S+|t\.me/\S+")

# Служебные слова и вечный фон Telegram-постов. Без этого топ тем на 80% состоит из
# «это/который/подписывайтесь/канал» — и отчёт бесполезен.
_STOP = {
    "это", "этот", "эта", "эти", "того", "тому", "тем", "тех", "как", "так", "там",
    "тут", "вот", "уже", "ещё", "еще", "или", "если", "чтобы", "чтоб", "потому",
    "что", "чем", "кто", "кого", "кому", "где", "когда", "куда", "который", "которые",
    "которых", "которая", "которое", "все", "всё", "весь", "вся", "всех", "всем",
    "был", "была", "было", "были", "быть", "есть", "нет", "для", "над", "под", "при",
    "про", "без", "через", "между", "перед", "после", "из-за", "они", "она", "оно",
    "мы", "вы", "ты", "мне", "меня", "тебя", "себя", "него", "неё", "них", "нас",
    "вас", "им", "их", "его", "её", "ему", "ей", "мой", "моя", "мои", "наш", "наши",
    "ваш", "ваши", "свой", "своя", "свои", "своих", "тоже", "также", "очень", "более",
    "менее", "самый", "самая", "может", "можно", "нужно", "надо", "будет", "будут",
    "буду", "стал", "стало", "стали", "делать", "сделать", "один", "одна", "одно",
    "два", "три", "первый", "второй", "новый", "новая", "новые", "просто", "только",
    "даже", "почти", "лишь", "либо", "нибудь", "ну", "да", "нет", "не", "ни", "но",
    "же", "ли", "бы", "то", "за", "на", "по", "от", "до", "во", "со", "об", "и",
    # частотный фон живой речи — лезет в топ тем и вытесняет смысл
    "почему", "зачем", "больше", "меньше", "всегда", "никогда", "сейчас", "потом",
    "теперь", "сегодня", "вчера", "завтра", "каждый", "каждая", "любой", "другой",
    "другие", "такой", "такие", "хочу", "хочет", "знаю", "думаю", "считаю",
    "говорит", "сказал", "пишет", "смотрите", "давайте", "лучше", "хорошо", "плохо",
    "много", "мало", "раз", "года", "году", "лет", "день", "дня",
    # телеграм-фон
    "подписывайтесь", "подписаться", "подписка", "канал", "канале", "телеграм",
    "telegram", "ссылка", "ссылке", "читать", "далее", "подробнее", "пост", "посте",
    "комментарии", "комментариях", "репост", "share", "https", "http", "www",
    "com", "ru", "the", "and", "for", "you", "with", "that", "this",
}

# Усечение русских окончаний. Порядок важен: длинные суффиксы раньше коротких,
# иначе «инвестициями» обрежется до «инвестициям», а не до общей основы.
_SUFFIXES = (
    "иями", "ями", "ами", "иях", "ях", "ах", "ов", "ев", "ий", "ых", "их", "ая",
    "яя", "ое", "ее", "ые", "ие", "ой", "ей", "ом", "ем", "ам", "ям", "ую", "юю",
    "ия", "ья", "ье", "ью", "ии", "ей", "ы", "и", "а", "я", "о", "е", "у", "ю", "ь",
)


def _stem(word: str) -> str:
    """Грубая основа слова: режем хвост, пока слово длиннее 4 символов.

    Не морфология — эвристика для склейки словоформ в частотном анализе. Короткие
    слова не трогаем: «риск» после обрезки станет «рис», и смысл уедет."""
    w = word.lower().replace("ё", "е")
    if len(w) <= 4:
        return w
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(_URL_RE.sub(" ", text or ""))
            if w.lower() not in _STOP and len(w) > 2]


def top_terms(texts: list[str], n: int = 20) -> list[dict]:
    """Топ тем: частота по основе, наружу — самая ходовая живая форма.

    Считаем ДОКУМЕНТНУЮ частоту (в скольких постах слово встретилось), а не общее
    число употреблений: иначе один пост, где слово повторено 30 раз, выдаёт себя за
    тему всего канала."""
    doc_freq: Counter = Counter()
    forms: dict[str, Counter] = defaultdict(Counter)
    for t in texts:
        seen_here = set()
        for w in _words(t):
            s = _stem(w)
            forms[s][w] += 1
            if s not in seen_here:
                seen_here.add(s)
                doc_freq[s] += 1
    out = []
    total = max(1, len(texts))
    for stem, cnt in doc_freq.most_common(n):
        word = forms[stem].most_common(1)[0][0]
        out.append({"term": word, "posts": cnt, "share": round(100.0 * cnt / total, 1)})
    return out


def top_bigrams(texts: list[str], n: int = 12) -> list[dict]:
    """Устойчивые пары слов — «венчурные инвестиции», «личный бренд».

    Пары информативнее одиночных слов: по слову «личный» тему не понять, по паре —
    сразу ясно. Считаем тоже по документам."""
    doc_freq: Counter = Counter()
    forms: dict[tuple, Counter] = defaultdict(Counter)
    for t in texts:
        ws = _words(t)
        seen_here = set()
        for a, b in zip(ws, ws[1:]):
            key = (_stem(a), _stem(b))
            forms[key][f"{a} {b}"] += 1
            if key not in seen_here:
                seen_here.add(key)
                doc_freq[key] += 1
    out = []
    total = max(1, len(texts))
    for key, cnt in doc_freq.most_common(n):
        if cnt < 2:      # единичная пара — не тема, а случайность
            continue
        out.append({"term": forms[key].most_common(1)[0][0], "posts": cnt,
                    "share": round(100.0 * cnt / total, 1)})
    return out


# --------------------------------------------------------------------------- #
#  Сбор постов                                                                 #
# --------------------------------------------------------------------------- #
def _reactions_count(msg) -> int | None:
    r = getattr(msg, "reactions", None)
    if not r or not getattr(r, "results", None):
        return None
    return sum(int(getattr(x, "count", 0) or 0) for x in r.results)


def _replies_count(msg) -> int | None:
    r = getattr(msg, "replies", None)
    return int(getattr(r, "replies", 0) or 0) if r else None


async def collect(chat_id: int, days: int = DEFAULT_DAYS,
                  limit: int = DEFAULT_LIMIT) -> dict:
    """Забрать посты канала за последние `days` дней в channel_posts.

    Аккаунт берём тот же, что назначен для прослушки чатов (chat_keywords._main_client):
    активное чтение истории — как раз тот расход, ради которого его и отделили от
    личного номера."""
    from channels.chat_keywords import _main_client, _target

    database.init_db()
    with database.get_conn() as conn:
        ch = conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not ch:
        return {"ok": False, "error": f"чата #{chat_id} нет в каталоге"}
    ch = dict(ch)

    since = datetime.now(timezone.utc) - timedelta(days=days)
    client = await _main_client()
    saved = updated = 0
    try:
        await client.start()
        ent = await client.get_entity(_target(ch))
        n = 0
        async for msg in client.iter_messages(ent, limit=limit):
            date = getattr(msg, "date", None)
            if date and date < since:
                break                      # история идёт от новых к старым — дальше только старее
            text = (getattr(msg, "message", None) or "").strip()
            if not text and not getattr(msg, "media", None):
                continue                   # служебные сообщения (вступил/закрепил) — не посты
            with database.get_conn() as conn:
                cur = conn.execute(
                    "INSERT INTO channel_posts (chat_id, tg_chat_id, msg_id, text, views, "
                    "forwards, replies, reactions, ts) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(chat_id, msg_id) DO UPDATE SET "
                    "views=excluded.views, forwards=excluded.forwards, "
                    "replies=excluded.replies, reactions=excluded.reactions",
                    (chat_id, ch.get("tg_chat_id"), msg.id, text[:4000],
                     getattr(msg, "views", None), getattr(msg, "forwards", None),
                     _replies_count(msg), _reactions_count(msg),
                     date.strftime("%Y-%m-%d %H:%M:%S") if date else None),
                )
                if cur.rowcount == 1:
                    saved += 1
                else:
                    updated += 1
            n += 1
            if n % 50 == 0:
                await asyncio.sleep(random.uniform(*COLLECT_PAUSE))
    except FloodWaitError as e:
        return {"ok": False, "error": f"FloodWait {e.seconds}с — Telegram просит подождать",
                "saved": saved}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}", "saved": saved}
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    with database.get_conn() as conn:
        conn.execute("UPDATE chats SET last_scanned_at=datetime('now') WHERE id=?", (chat_id,))
    return {"ok": True, "chat": ch.get("title"), "days": days,
            "new": saved, "refreshed": updated}


# --------------------------------------------------------------------------- #
#  Отчёт                                                                       #
# --------------------------------------------------------------------------- #
_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def analyze(chat_id: int, days: int = DEFAULT_DAYS) -> dict:
    """Отчёт по уже собранным постам. Telegram не трогает — считает по базе."""
    database.init_db()
    with database.get_conn() as conn:
        ch = conn.execute("SELECT id, title, username, kind, members_count "
                          "FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not ch:
            return {"ok": False, "error": f"чата #{chat_id} нет в каталоге"}
        rows = conn.execute(
            "SELECT text, views, forwards, replies, reactions, ts, msg_id FROM channel_posts "
            "WHERE chat_id=? AND ts >= datetime('now', ?) ORDER BY ts DESC",
            (chat_id, f"-{int(days)} days"),
        ).fetchall()
        # Кто в этом чате говорит больше всех. Сырьё собирает tg_parser (--mode active
        # --harvest) в tg_user_posts; связь по СЫРОМУ telegram-id, а не каталожному —
        # это разные вещи, и перепутать их значит получить пустой список на ровном месте.
        voices = conn.execute(
            "SELECT p.tg_user_id, COUNT(*) AS msgs, MAX(p.ts) AS last_ts, "
            "       (SELECT c2.id FROM contacts c2 WHERE c2.tg_user_id=p.tg_user_id LIMIT 1) AS contact_id, "
            "       (SELECT COALESCE(c2.person_name, c2.name) FROM contacts c2 "
            "          WHERE c2.tg_user_id=p.tg_user_id LIMIT 1) AS name, "
            "       (SELECT c2.username FROM contacts c2 WHERE c2.tg_user_id=p.tg_user_id LIMIT 1) AS username "
            "FROM tg_user_posts p WHERE p.chat_id=(SELECT tg_chat_id FROM chats WHERE id=?) "
            "  AND p.ts >= datetime('now', ?) "
            "GROUP BY p.tg_user_id ORDER BY msgs DESC LIMIT 15",
            (chat_id, f"-{int(days)} days"),
        ).fetchall()
    ch = dict(ch)
    posts = [dict(r) for r in rows]
    top_voices = [{"tg_user_id": v["tg_user_id"], "msgs": v["msgs"], "last_ts": v["last_ts"],
                   "contact_id": v["contact_id"],
                   "name": v["name"] or (f"@{v['username']}" if v["username"] else str(v["tg_user_id"])),
                   "username": v["username"]} for v in voices]

    if not posts:
        return {"ok": True, "chat": ch, "days": days, "posts": 0, "voices": top_voices,
                "hint": "постов за период нет — сначала собери: --collect"}

    texts = [p["text"] or "" for p in posts]
    dates = [d for d in (_parse_ts(p["ts"]) for p in posts) if d]
    span_days = max(1, (max(dates) - min(dates)).days + 1) if dates else days

    # ── ритм ────────────────────────────────────────────────────────────────
    by_day: Counter = Counter()
    by_weekday: Counter = Counter()
    by_hour: Counter = Counter()
    for d in dates:
        by_day[d.strftime("%Y-%m-%d")] += 1
        by_weekday[d.weekday()] += 1
        by_hour[d.hour] += 1

    # ── вовлечённость: только по «отстоявшимся» постам ──────────────────────
    cutoff = datetime.utcnow() - timedelta(hours=FRESH_HOURS)
    mature = [p for p in posts if (_parse_ts(p["ts"]) or datetime.min) < cutoff]
    base = mature or posts

    def _avg(key: str) -> float:
        vals = [p[key] for p in base if p[key] is not None]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    avg_views = _avg("views")

    def _engagement(p: dict) -> float:
        """Отклик на пост: реакции + комментарии + пересылки, нормированные на охват.
        Без нормировки топ всегда занимают самые старые посты — у них больше просмотров."""
        acts = sum(int(p[k] or 0) for k in ("reactions", "replies", "forwards"))
        views = int(p["views"] or 0)
        return round(100.0 * acts / views, 2) if views else float(acts)

    top_posts = sorted(base, key=_engagement, reverse=True)[:5]
    link = f"https://t.me/{ch['username']}" if ch.get("username") else None

    return {
        "ok": True,
        "chat": ch,
        "days": days,
        "posts": len(posts),
        "period": {"from": min(dates).strftime("%Y-%m-%d") if dates else None,
                   "to": max(dates).strftime("%Y-%m-%d") if dates else None,
                   "span_days": span_days},
        "pace": {
            "per_day": round(len(posts) / span_days, 2),
            "per_week": round(7.0 * len(posts) / span_days, 1),
            "avg_len": int(sum(len(t) for t in texts) / len(texts)),
            "by_day": [{"date": d, "posts": c} for d, c in sorted(by_day.items())],
            "by_weekday": [{"day": _WEEKDAYS[i], "posts": by_weekday.get(i, 0)} for i in range(7)],
            "by_hour": [{"hour": h, "posts": by_hour.get(h, 0)} for h in range(24)],
        },
        "reach": {
            "avg_views": avg_views,
            "avg_reactions": _avg("reactions"),
            "avg_replies": _avg("replies"),
            "avg_forwards": _avg("forwards"),
            # ER к подписчикам — сравнимая между каналами величина, в отличие от голых просмотров
            "views_per_member": (round(100.0 * avg_views / ch["members_count"], 1)
                                 if ch.get("members_count") else None),
            "mature_posts": len(mature),
        },
        "themes": top_terms(texts, 20),
        "phrases": top_bigrams(texts, 12),
        # Живые голоса чата: сначала видим, КТО задаёт тон, и только потом решаем,
        # по кому строить досье, — а не наоборот.
        "voices": top_voices,
        "top_posts": [{
            "msg_id": p["msg_id"], "ts": p["ts"],
            "views": p["views"], "reactions": p["reactions"], "replies": p["replies"],
            "engagement": _engagement(p),
            "link": f"{link}/{p['msg_id']}" if link else None,
            "text": (p["text"] or "")[:220],
        } for p in top_posts],
    }


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: отчёт по каналу (объём, ритм, темы, отклик)")
    p.add_argument("--chat", type=int, required=True, help="id чата из каталога")
    p.add_argument("--collect", action="store_true", help="сначала забрать свежие посты из Telegram")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help="глубина периода в днях")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="потолок постов за сбор")
    p.add_argument("--json", action="store_true", help="выдать сырой JSON")
    args = p.parse_args()

    if args.collect:
        res = asyncio.run(collect(args.chat, args.days, args.limit))
        print(json.dumps(res, ensure_ascii=False))
        if not res.get("ok"):
            return

    rep = analyze(args.chat, args.days)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return
    if not rep.get("ok"):
        print(rep.get("error"))
        return
    if not rep.get("posts"):
        print(rep.get("hint"))
        return

    ch, pace, reach = rep["chat"], rep["pace"], rep["reach"]
    print(f"\n📊 {ch['title']} ({ch.get('kind') or '?'}"
          + (f", {ch['members_count']} подписчиков" if ch.get("members_count") else "") + ")")
    print(f"   период {rep['period']['from']} → {rep['period']['to']}, постов: {rep['posts']}")
    print(f"   ритм: {pace['per_day']}/день ({pace['per_week']}/неделю), "
          f"средняя длина {pace['avg_len']} знаков")
    print(f"   отклик: {reach['avg_views']} просмотров, {reach['avg_reactions']} реакций, "
          f"{reach['avg_replies']} комментариев (по {reach['mature_posts']} отстоявшимся постам)")
    busy = sorted(pace["by_weekday"], key=lambda x: -x["posts"])[:3]
    print(f"   пишет чаще: {', '.join(d['day'] for d in busy if d['posts'])}")
    print("\n   О ЧЁМ ПИШУТ (доля постов):")
    for t in rep["themes"][:12]:
        bar = "█" * max(1, int(t["share"] / 3))
        print(f"     {t['term']:<20} {t['posts']:>3} ({t['share']:>4}%) {bar}")
    if rep["phrases"]:
        print("\n   УСТОЙЧИВЫЕ СВЯЗКИ:")
        for t in rep["phrases"][:8]:
            print(f"     {t['term']:<28} {t['posts']:>3} ({t['share']}%)")
    print("\n   ЧТО ЗАШЛО ЛУЧШЕ ВСЕГО:")
    for p_ in rep["top_posts"]:
        print(f"     [{p_['engagement']}%] {(p_['text'] or '')[:90]}…")
        if p_["link"]:
            print(f"              {p_['link']}")
    print()


if __name__ == "__main__":
    main()
