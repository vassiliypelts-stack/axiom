"""Обогащение лида ИЗ TELEGRAM: описание профиля + его личный канал.

ЗАЧЕМ ОТДЕЛЬНО от agent/enrich.py. Тот читает сайт и данные справочников — там про
ЮРЛИЦО: чем занимается фирма, кто директор. А продаёт человек. И говорит он об этом
своими словами ровно в двух местах: в bio профиля и в своём канале. Ни сайт, ни 2ГИС,
ни выгрузка каталога этого не дают — оттуда получается «агентство недвижимости,
вероятно, частный агент», с чем в первое сообщение не пойдёшь.

ЧТО ДОСТАЁМ (в карточку досье):
  • offer        — что человек ПРОДАЁТ (услуга/продукт своими словами)
  • niche         — чем занимается / род деятельности
  • social_role   — позиция в социуме: предприниматель | эксперт/тренер | наёмный |
                    госслужащий | инвестор | другое. Для захода это важнее должности:
                    «сам себе хозяин» и «работает на дядю» — разные разговоры.
  • interests     — ключевые темы, о которых пишет
  • hook          — персональная зацепка под первое сообщение

ОТКУДА. bio уже лежит в contacts.bio (его собирает пробив номера, phone_resolve →
_fetch_bio). Если в bio есть ссылка на канал — заходим и читаем ЗАКРЕП (там обычно
оффер целиком) плюс несколько последних постов. Закреп берём первым: это витрина,
человек его пишет один раз и надолго, а лента может быть занята репостами.

ПРОВЕНАНС. Пишем tg_enriched_at и tg_enrich_note («bio + канал, 8 постов») — в
карточке видно, что именно прочитано и когда. Без этого непонятно, откуда взялась
строчка «продаёт наставничество» и можно ли ей верить.

АНТИБАН. Чтение канала — обычная подписка-независимая история публичного канала, но
это всё равно запросы с боевого аккаунта. Идём дозированно (PAUSE между людьми),
берём аккаунт из пула, при FloodWait уходим. Приватные каналы (+hash) не трогаем:
туда нужно вступать, а вступление — отдельный риск и отдельное решение оператора.

Запуск:
    python -m channels.enrich_tg --limit 50        # кого ещё не обогащали из TG
    python -m channels.enrich_tg --ids 9975,9981   # точечно
    python -m channels.enrich_tg --limit 20 --dry  # показать, кого взял бы
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re

from pydantic import BaseModel, Field
from telethon.errors import FloodWaitError

import config
from agent import llm
from channels.telegram import client_for_account
from db import database

POSTS = 8               # сколько последних постов канала читаем (сверх закрепа)
POST_CHARS = 700        # обрезка одного поста — модели хватает, токены не жжём
PAUSE = (3.0, 8.0)      # пауза между людьми
CONNECT_TIMEOUT = 25
ACC_TRIES = 4           # сколько аккаунтов перебрать, если у первых мёртвый прокси

# t.me/name, @name — но НЕ приватные +hash/joinchat (туда нужно вступать)
_CHANNEL_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})|(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})")
# служебные ссылки, которые каналом не являются
_SKIP = {"joinchat", "share", "addstickers", "proxy", "socks", "telegram", "durov"}


class TgProfile(BaseModel):
    """Что вытаскиваем из Telegram-следа человека."""

    offer: str | None = Field(description="Что человек ПРОДАЁТ или предлагает — услуга/продукт его словами. Если не продаёт ничего явно — null.")
    niche: str | None = Field(description="Чем занимается, род деятельности. Кратко, до 10 слов.")
    social_role: str | None = Field(description="Позиция в социуме, ОДНО из: предприниматель, эксперт/тренер, наёмный, госслужащий, инвестор, другое. Если непонятно — null.")
    interests: str | None = Field(description="Ключевые темы, о которых пишет, через «; ». До 5 штук.")
    hook: str | None = Field(description="ОДНА персональная зацепка (1 фраза) для первого сообщения от поставщика ИИ-автоматизации. Опирайся на то, что он реально пишет. Без воды.")


def _channel_from_bio(bio: str | None) -> str | None:
    """Ссылка на канал из bio. Приватные (+hash) намеренно пропускаем."""
    if not bio:
        return None
    for m in _CHANNEL_RE.finditer(bio):
        name = m.group(1) or m.group(2)
        if name and name.lower() not in _SKIP:
            return name
    return None


def _targets(ids: list[int] | None, limit: int | None) -> list[dict]:
    """Кого обогащаем: есть tg_user_id (значит человек в Telegram найден) и ещё не
    обогащали из TG. Порядок — свежие сверху: их обычно и ждут в работе."""
    where = ["tg_user_id IS NOT NULL", "deleted_at IS NULL"]
    params: list = []
    if ids:
        where.append(f"id IN ({','.join('?' * len(ids))})")
        params += ids
    else:
        where.append("tg_enriched_at IS NULL")
    sql = ("SELECT id, name, person_name, username, tg_user_id, bio, niche, offer "
           f"FROM contacts WHERE {' AND '.join(where)} ORDER BY id DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with database.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


async def _read_channel(client, name: str) -> tuple[str | None, list[str]]:
    """(закреп, последние посты) публичного канала. Ошибка/приватный → (None, [])."""
    try:
        ent = await client.get_entity(name)
    except FloodWaitError:
        raise
    except Exception:  # noqa: BLE001 — не резолвится/не существует/приватный
        return None, []
    pinned_text = None
    # Закреп берём ОТДЕЛЬНЫМ запросом с фильтром, а не ищем в ленте: он там может
    # быть давно внизу, за пределами последних POSTS сообщений.
    try:
        from telethon.tl.types import InputMessagesFilterPinned
        msgs = await client.get_messages(ent, limit=1, filter=InputMessagesFilterPinned)
        if msgs:
            pinned_text = (msgs[0].message or "").strip() or None
    except Exception:  # noqa: BLE001
        pinned_text = None
    posts: list[str] = []
    try:
        async for m in client.iter_messages(ent, limit=POSTS):
            t = (m.message or "").strip()
            if t:
                posts.append(t[:POST_CHARS])
    except FloodWaitError:
        raise
    except Exception:  # noqa: BLE001
        pass
    return pinned_text, posts


def _prompt(acc: dict, bio: str | None, pinned: str | None, posts: list[str]) -> str:
    who = acc.get("person_name") or acc.get("name") or "человек"
    parts = [f"Человек: {who}"]
    if acc.get("username"):
        parts.append(f"Telegram: @{acc['username']}")
    if bio:
        parts.append(f"\nОписание профиля (bio):\n{bio.strip()[:800]}")
    if pinned:
        parts.append(f"\nЗакреплённый пост его канала (это его витрина):\n{pinned[:1200]}")
    if posts:
        joined = "\n---\n".join(posts[:POSTS])
        parts.append(f"\nПоследние посты канала:\n{joined[:4000]}")
    parts.append("\nОпираясь ТОЛЬКО на текст выше, заполни поля. Чего нет — null, "
                 "не додумывай и не обобщай до «эксперт в своей области».")
    return "\n".join(parts)


async def _one(client, c: dict) -> tuple[bool, str]:
    bio = (c.get("bio") or "").strip() or None
    chan = _channel_from_bio(bio)
    pinned, posts = (None, [])
    if chan:
        pinned, posts = await _read_channel(client, chan)
    if not bio and not pinned and not posts:
        with database.get_conn() as conn:
            conn.execute("UPDATE contacts SET tg_enriched_at=datetime('now'), "
                         "tg_enrich_note='в профиле пусто: ни описания, ни канала' WHERE id=?",
                         (c["id"],))
        return False, "нечего читать (пустой профиль)"

    try:
        data = llm.structured(
            config.agent_model(),
            "Ты аналитик лидов. Отвечаешь строго по фактам из текста, без домыслов.",
            [{"role": "user", "content": _prompt(c, bio, pinned, posts)}],
            TgProfile,
        )
    except Exception as e:  # noqa: BLE001 — провайдер мог не ответить
        return False, f"модель не ответила: {str(e)[:60]}"
    if data is None:
        return False, "модель не ответила"

    note_bits = []
    if bio:
        note_bits.append("bio")
    if chan:
        note_bits.append(f"канал @{chan}" + (f", постов {len(posts)}" if posts else ", постов нет"))
        if pinned:
            note_bits.append("закреп")
    note = " + ".join(note_bits) or "нет источников"

    with database.get_conn() as conn:
        # COALESCE(NULLIF(...)) — не затираем уже заполненное пустотой: обогащение
        # дополняет карточку, а не переписывает её начисто.
        conn.execute(
            "UPDATE contacts SET "
            "offer=COALESCE(NULLIF(?,''), offer), niche=COALESCE(NULLIF(?,''), niche), "
            "social_role=COALESCE(NULLIF(?,''), social_role), "
            "interests=COALESCE(NULLIF(?,''), interests), hook=COALESCE(NULLIF(?,''), hook), "
            "tg_channel=?, tg_enriched_at=datetime('now'), tg_enrich_note=? WHERE id=?",
            (data.offer or "", data.niche or "", data.social_role or "",
             data.interests or "", data.hook or "",
             (f"https://t.me/{chan}" if chan else None), note, c["id"]),
        )
    return True, f"{note} → {data.social_role or '?'}; {(data.offer or '—')[:50]}"


async def run(ids: list[int] | None, limit: int | None, dry: bool) -> None:
    database.init_db()
    people = _targets(ids, limit)
    if not people:
        print(json.dumps({"ok": False, "error": "некого обогащать: нужен пробитый в TG контакт"},
                         ensure_ascii=False))
        return
    if dry:
        for c in people:
            chan = _channel_from_bio(c.get("bio"))
            print(f"[dry] #{c['id']} {c.get('name') or ''} — bio:{'да' if c.get('bio') else 'нет'} "
                  f"канал:{'@' + chan if chan else 'нет'}")
        print(json.dumps({"ok": True, "dry": True, "would": len(people)}, ensure_ascii=False))
        return

    # Аккаунт для чтения: любой живой боевой. Читаем чужие публичные каналы — это
    # безопаснее вступлений, но всё равно с боевого номера, поэтому дозируем.
    # Берём НЕСКОЛЬКО кандидатов, а не одного: session_alive=1 говорит про сессию, а не
    # про прокси, и на дохлом прокси весь запрос падал ConnectionError'ом в трейсбек.
    with database.get_conn() as conn:
        cands = conn.execute(
            "SELECT id FROM accounts WHERE session_alive=1 AND COALESCE(protected,0)=0 "
            "AND tg_session IS NOT NULL AND tg_session<>'' ORDER BY RANDOM() LIMIT ?",
            (ACC_TRIES,)).fetchall()
    if not cands:
        print(json.dumps({"ok": False, "error": "нет живого боевого аккаунта для чтения"},
                         ensure_ascii=False))
        return

    # ВАЖНО: фолбэка «подключиться мимо прокси» здесь нет и быть не должно — проба с IP
    # сервера при живом слушателе выглядит для Telegram как угон ключа и сжигает аккаунт
    # навсегда (см. предупреждение в channels/session_check.py). Мёртвый прокси лечится
    # только переходом на ДРУГОЙ аккаунт — со своим прокси.
    client = acc_id = None
    why: list[str] = []
    for row in cands:
        cid = row["id"]
        try:
            cand, _ = client_for_account(cid)
        except Exception as e:  # noqa: BLE001 — нет сессии/битая строка: пробуем следующий
            why.append(f"#{cid}: {str(e)[:60]}")
            continue
        try:
            await asyncio.wait_for(cand.connect(), timeout=CONNECT_TIMEOUT)
            if not await cand.is_user_authorized():
                why.append(f"#{cid}: не авторизован")
                await cand.disconnect()
                continue
        except asyncio.TimeoutError:
            why.append(f"#{cid}: таймаут подключения ({CONNECT_TIMEOUT}с)")
            try:
                await cand.disconnect()
            except Exception:  # noqa: BLE001
                pass
            continue
        except Exception as e:  # noqa: BLE001 — почти всегда мёртвый прокси
            why.append(f"#{cid}: {type(e).__name__}: {str(e)[:60]}")
            try:
                await cand.disconnect()
            except Exception:  # noqa: BLE001
                pass
            continue
        client, acc_id = cand, cid
        break

    if client is None:
        print(json.dumps(
            {"ok": False, "error": "не удалось подключиться к Telegram ни одним аккаунтом "
                                   f"(проверь прокси). Пробовал: {'; '.join(why)}"},
            ensure_ascii=False))
        return

    acc = {"id": acc_id}
    done = failed = 0
    try:
        for c in people:
            try:
                ok, msg = await _one(client, c)
            except FloodWaitError as e:
                print(f"[floodwait] {e.seconds}с — останавливаюсь")
                break
            except Exception as e:  # noqa: BLE001 — один лид не должен рвать проход
                ok, msg = False, f"ошибка: {str(e)[:70]}"
            print(f"[#{c['id']}] {c.get('name') or ''}: {'✅' if ok else '✗'} {msg}")
            done += 1 if ok else 0
            failed += 0 if ok else 1
            await asyncio.sleep(random.uniform(*PAUSE))
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps({"ok": True, "enriched": done, "failed": failed, "account": acc["id"]},
                     ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: обогащение лида из Telegram (bio + канал)")
    p.add_argument("--ids", help="через запятую id контактов")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--dry", action="store_true", help="показать кандидатов, ничего не менять")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    # Последний рубеж: пульт ждёт JSON последней строкой. Без этого любое падение
    # (например ConnectionError от Telethon) прилетало оператору сырым трейсбеком
    # в alert'е и читалось как «не отчитался».
    try:
        asyncio.run(run(ids, args.limit, args.dry))
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
