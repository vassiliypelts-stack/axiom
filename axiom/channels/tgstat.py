"""Поиск каналов и чатов через TGStat — пополнение каталога по фильтрам.

ПОЧЕМУ API, А НЕ ПАРСИНГ САЙТА. tgstat.ru отдаёт 403 на автоматические запросы —
там стоит защита от роботов, и обходить её мы не будем. У сервиса есть
документированный API (api.tgstat.ru) с бесплатным тарифом: он и стабильнее, и
отдаёт структурные данные вместо вёрстки, которая ломается от любого редизайна.

ЧТО ДАЁТ. Telegram-поиск (`chat_discover`) ищет по названию и работает вслепую:
ни охвата, ни тематики, ни живости. TGStat — это каталог с категориями, странами,
языками и числом подписчиков, то есть можно спросить «бизнес-каналы РФ от 5 тысяч
подписчиков» и получить осмысленный список, а не всё, где встретилось слово.

Дополняет, а не заменяет:
  • chat_discover  — глобальный поиск Telegram по словам;
  • chat_similar   — размножение от уже найденного (рекомендации Telegram);
  • tgstat         — каталог по фильтрам (категория/страна/язык/размер).

Токен кладётся в .env: TGSTAT_TOKEN=... (получить на https://api.tgstat.ru/docs).
Без токена модуль честно говорит, что делать, и ничего не выдумывает.

Запуск:
    python -m channels.tgstat --search "инвестиции" --min-members 5000
    python -m channels.tgstat --search "бизнес" --category business --save
    python -m channels.tgstat --posts "ищу инвестора" --days 7
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request

import config
from db import database

API = "https://api.tgstat.ru"
TIMEOUT = 25
PAUSE = 0.7          # пауза между запросами: тариф лимитирован, вести себя надо прилично
MAX_PAGES = 10


class TgstatError(RuntimeError):
    """Ошибка на стороне TGStat (нет токена, лимит, невалидный запрос)."""


def _call(method: str, **params) -> dict:
    """Запрос к API. Возвращает поле response, ошибки поднимает как TgstatError."""
    token = (config.TGSTAT_TOKEN or "").strip()
    if not token:
        raise TgstatError(
            "нет токена TGStat. Получи бесплатный на https://api.tgstat.ru/docs "
            "и добавь в axiom/.env строку TGSTAT_TOKEN=..."
        )
    params = {k: v for k, v in params.items() if v not in (None, "", [])}
    url = f"{API}/{method}?" + urllib.parse.urlencode({"token": token, **params})
    req = urllib.request.Request(url, headers={"User-Agent": "AXIOM/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — сеть/JSON: наружу одинаково «не получилось»
        raise TgstatError(f"{type(e).__name__}: {str(e)[:120]}") from e
    if data.get("status") != "ok":
        err = data.get("error") or "неизвестная ошибка"
        hint = {
            "token_invalid": "токен не принят — проверь TGSTAT_TOKEN в .env",
            "limit_reached": "исчерпан лимит запросов тарифа TGStat на сегодня",
        }.get(err, "")
        raise TgstatError(f"TGStat: {err}{'. ' + hint if hint else ''}")
    return data.get("response") or {}


def search_channels(q: str, category: str | None = None, country: str = "ru",
                    language: str | None = None, min_members: int = 0,
                    limit: int = 100, groups: bool | None = None) -> list[dict]:
    """Каналы/чаты по запросу и фильтрам. limit — сколько всего вернуть (постранично).

    `groups`: True — только чаты, False — только каналы, None — всё подряд."""
    out: list[dict] = []
    page = 1
    while len(out) < limit and page <= MAX_PAGES:
        resp = _call("channels/search", q=q, category=category, country=country,
                     language=language, limit=min(50, limit - len(out)), page=page,
                     extended=1)
        items = resp.get("items") or []
        if not items:
            break
        for it in items:
            members = int(it.get("participants_count") or 0)
            if min_members and members < min_members:
                continue
            is_group = bool(it.get("is_group") or it.get("type") == "chat")
            if groups is True and not is_group:
                continue
            if groups is False and is_group:
                continue
            out.append({
                "title": it.get("title"),
                "username": (it.get("username") or "").lstrip("@") or None,
                "link": it.get("link"),
                "members": members,
                "about": (it.get("about") or "")[:500],
                "category": it.get("category"),
                "kind": "супергруппа" if is_group else "канал",
                "tg_chat_id": it.get("tg_id") or None,
            })
        page += 1
        time.sleep(PAUSE)
    return out[:limit]


def search_posts(q: str, days: int = 7, limit: int = 100) -> list[dict]:
    """Посты по ключевым словам за период — «о чём сейчас говорят» по всему Telegram.

    Полезно не для сбора чатов, а для разведки спроса: где и как часто всплывает
    формулировка, которую мы считаем сигналом клиента."""
    resp = _call("posts/search", q=q, limit=min(50, limit),
                 extended=1, period=f"-{int(days)}d")
    return [{
        "channel": (it.get("channel") or {}).get("title"),
        "username": ((it.get("channel") or {}).get("username") or "").lstrip("@") or None,
        "members": (it.get("channel") or {}).get("participants_count"),
        "text": (it.get("text") or "")[:400],
        "views": it.get("views"),
        "link": it.get("link"),
        "date": it.get("date"),
    } for it in (resp.get("items") or [])]


def save_to_catalog(found: list[dict], topic: str | None = None) -> dict:
    """Найденное — в каталог `chats` с пометкой происхождения source='tgstat'.

    Дедуп по username, как и в остальных источниках. У существующих записей source
    не перетираем: чат мог прийти из инвентаря, и «нашли в tgstat» — не главная
    правда о нём."""
    database.init_db()
    added = updated = 0
    with database.get_conn() as conn:
        for f in found:
            uname = f.get("username")
            ex = None
            if uname:
                ex = conn.execute("SELECT id FROM chats WHERE username=?", (uname,)).fetchone()
            if ex:
                conn.execute(
                    "UPDATE chats SET title=COALESCE(?,title), kind=COALESCE(?,kind), "
                    "members_count=COALESCE(?,members_count), link=COALESCE(link,?), "
                    "topic=COALESCE(topic,?), source=COALESCE(source,'tgstat') WHERE id=?",
                    (f.get("title"), f.get("kind"), f.get("members") or None,
                     f.get("link"), topic or f.get("category"), ex["id"]),
                )
                updated += 1
                continue
            conn.execute(
                "INSERT INTO chats (title, username, link, kind, members_count, topic, "
                "status, notes, source) VALUES (?,?,?,?,?,?, 'new', ?, 'tgstat')",
                (f.get("title"), uname, f.get("link"), f.get("kind"),
                 f.get("members") or None, topic or f.get("category"),
                 (f.get("about") or "")[:300] or None),
            )
            added += 1
    return {"added": added, "updated": updated}


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: поиск каналов/чатов через TGStat API")
    p.add_argument("--search", help="поисковый запрос по каналам")
    p.add_argument("--posts", help="искать ПОСТЫ по этой фразе (разведка спроса)")
    p.add_argument("--category", help="категория TGStat (business, cryptocurrency, …)")
    p.add_argument("--country", default="ru", help="страна (по умолчанию ru)")
    p.add_argument("--min-members", type=int, default=0, help="отсекать мельче N подписчиков")
    p.add_argument("--limit", type=int, default=100, help="сколько записей вернуть")
    p.add_argument("--days", type=int, default=7, help="период для --posts")
    p.add_argument("--groups-only", action="store_true", help="только чаты, без каналов")
    p.add_argument("--channels-only", action="store_true", help="только каналы, без чатов")
    p.add_argument("--save", action="store_true", help="записать найденное в каталог")
    args = p.parse_args()

    if not args.search and not args.posts:
        p.print_help()
        return
    groups = True if args.groups_only else (False if args.channels_only else None)
    try:
        if args.posts:
            items = search_posts(args.posts, days=args.days, limit=args.limit)
            print(json.dumps({"ok": True, "found": len(items), "items": items},
                             ensure_ascii=False))
            return
        found = search_channels(args.search, category=args.category, country=args.country,
                                min_members=args.min_members, limit=args.limit, groups=groups)
        res = {"ok": True, "found": len(found)}
        if args.save:
            res.update(save_to_catalog(found, topic=args.category))
        else:
            res["items"] = found
            res["hint"] = "добавь --save, чтобы занести в каталог"
        print(json.dumps(res, ensure_ascii=False))
    except TgstatError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
