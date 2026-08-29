"""Парсер Telegram-каналов/чатов для AXIOM. Источник лидов №2 (после 2ГИС).

Три режима по целевому каналу/чату:
  • admins  — администраторы (всегда видны) — это часто и есть владельцы/ЛПР;
  • members — участники группы/супергруппы (если список не скрыт);
  • active  — активные комментаторы: идём по сообщениям чата/обсуждения,
              считаем частоту авторов, берём топ — это «живые» лиды.

Найденных кладём в ту же книжку (contacts) как лиды: source='tg_parse',
tg_user_id, username, имя, тег с источником. Дедуп по tg_user_id — повторный
прогон не плодит дублей.

⚠️ Telegram не любит массовый скрейп: используем отдельный прогретый аккаунт,
лимиты и паузы. Подписчики ВЕЩАТЕЛЬНОГО канала скрыты — доступны только админы
и (если есть) чат обсуждения.

Запуск (нужен авторизованный аккаунт — тот же, что в telegram.py):
    python -m channels.tg_parser --target @nedvizhka_sochi --mode admins --save
    python -m channels.tg_parser --target @somechat --mode members --limit 500 --save
    python -m channels.tg_parser --target @somechat --mode active --scan 3000 --top 50 --save
    python -m channels.tg_parser --target @chan --mode all --save   # админы + активные
    python -m channels.tg_parser --target @chat --mode active --harvest --save  # +сырьё для досье (H1)

H1: с флагом --harvest по активным авторам дополнительно собираются ТЕКСТЫ их
сообщений (до 30 за 90 дней) в tg_user_posts и bio в карточку — сырьё, из которого
agent/enrich_person.py строит психо-портрет (боли/страхи/желания/score).
"""
from __future__ import annotations

import argparse
import asyncio
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from telethon.errors import ChatAdminRequiredError, FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    Channel, ChannelParticipantsAdmins, ChannelParticipantAdmin,
    ChannelParticipantCreator, User,
)

import config
from channels.ru_names import gender_of
from channels.telegram import _build_client, client_for_account
from db import database

# Антибан: пауза между «тяжёлыми» вызовами и порции участников.
SCRAPE_PAUSE = (2.0, 5.0)

# H1 (досье): сколько сообщений на человека и за какое окно собирать (--harvest).
POSTS_PER_USER = 30
HARVEST_DAYS = 90
AVATAR_DIR = config.BASE_DIR / "data" / "avatars"  # этап 4: фото для vision-анализа


def _display_name(u: User) -> str:
    name = " ".join(x for x in [u.first_name, u.last_name] if x).strip()
    return name or (u.username and f"@{u.username}") or str(u.id)


async def _download_avatar(client, u: User) -> bool:
    """Качает аватар в data/avatars/{tg_user_id}.jpg. True — файл есть (фото у юзера).

    Один файл на человека (ключ — tg_user_id), тем же путём его читает vision-анализ
    (agent/enrich_person) и веб-карточка. Нет фото/приватность закрыта → False, тихо."""
    try:
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        path = AVATAR_DIR / f"{u.id}.jpg"
        res = await client.download_profile_photo(u, file=str(path))
        return bool(res) and path.exists() and path.stat().st_size > 0
    except Exception:  # noqa: BLE001
        return False


def _is_lead_user(u) -> bool:
    """Годится ли как лид: реальный пользователь, не бот, не удалён."""
    return isinstance(u, User) and not u.bot and not u.deleted


# Роль в чате. Владелец и админы — это ЛПР: им пишут иначе, чем рядовому участнику,
# поэтому роль сохраняем отдельным полем карточки, а не только словом внутри тега.
ROLE_CREATOR = "creator"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLE_ACTIVE = "active"

ROLE_RU = {
    ROLE_CREATOR: "владелец чата",
    ROLE_ADMIN: "админ",
    ROLE_MEMBER: "участник",
    ROLE_ACTIVE: "активный",
}


def _role_of(user: User, default: str = ROLE_MEMBER) -> str:
    """Роль из .participant, который Telethon вешает на User при get_participants.

    Иначе владелец чата был неотличим от рядового участника: режим admins валил всех
    в одно слово «админ», а создателя группы — самого ценного адресата — не выделял."""
    p = getattr(user, "participant", None)
    if isinstance(p, ChannelParticipantCreator):
        return ROLE_CREATOR
    if isinstance(p, ChannelParticipantAdmin):
        return ROLE_ADMIN
    return default


async def _visible_phone(client, user: User) -> str | None:
    """Номер телефона, ЕСЛИ Telegram его отдал (человек не прячет его настройками).

    Прятать номер — настройка по умолчанию, поэтому у большинства тут будет None, и
    это нормально. Специально «вскрывать» номера (массовый ImportContacts, заходы в
    ЛС) сознательно НЕ делаем: номеров это почти не даёт, а аккаунты за такое жгут.
    Достаточно того, что уже лежит в отданном объекте — лишних запросов ноль."""
    ph = (getattr(user, "phone", None) or "").strip()
    if ph:
        return ph if ph.startswith("+") else f"+{ph}"
    return None


async def _resolve_scan_chat(client, entity):
    """Куда смотреть на «активных»: сам чат (если группа) или связанное обсуждение канала."""
    if isinstance(entity, Channel) and entity.megagroup:
        return entity  # это супергруппа — комментируют прямо здесь
    if isinstance(entity, Channel) and entity.broadcast:
        try:
            full = await client(GetFullChannelRequest(entity))
            linked = getattr(full.full_chat, "linked_chat_id", None)
            if linked:
                return await client.get_entity(linked)
        except Exception as e:  # noqa: BLE001
            print(f"[active] не нашёл чат обсуждения: {e}")
        return None
    return entity


async def collect_admins(client, entity) -> list[User]:
    try:
        ppl = await client.get_participants(entity, filter=ChannelParticipantsAdmins())
    except FloodWaitError as e:
        print(f"[floodwait] жду {e.seconds}с"); await asyncio.sleep(e.seconds + 5)
        ppl = await client.get_participants(entity, filter=ChannelParticipantsAdmins())
    return [u for u in ppl if _is_lead_user(u)]


async def collect_members(client, entity, limit: int, offset: int = 0) -> list[User]:
    """offset — с какого места брать участников: нужен, чтобы поделить большой чат
    между несколькими аккаунтами (каждый тянет свой кусок, нагрузка делится на всех).

    ВАЖНО про offset. get_participants() в нашей версии Telethon его НЕ принимает —
    заход падал TypeError'ом и парсинг не начинался вовсе (живьём: «Точка Банк»,
    7047 участников, 0 собрано). Поэтому смещение делаем срезом: просим limit+offset
    и отбрасываем первые offset. Для конца списка это дороже по трафику, но участники
    приходят порциями по 200, и лишнего запроса на порцию не возникает.
    """
    want = limit + max(0, offset)
    try:
        ppl = await client.get_participants(entity, limit=want)
    except ChatAdminRequiredError:
        print("[members] список участников скрыт (нужны права админа) — пропускаю.")
        return []
    except FloodWaitError as e:
        print(f"[floodwait] жду {e.seconds}с"); await asyncio.sleep(e.seconds + 5)
        ppl = await client.get_participants(entity, limit=want)
    if offset:
        ppl = ppl[offset:]
    return [u for u in ppl if _is_lead_user(u)]


async def _fetch_bio(client, user: User) -> str | None:
    """Тянет bio (about) из полного профиля. Антибан: вызывать дозированно. Ошибка → None."""
    try:
        full = await client(GetFullUserRequest(user))
        return (getattr(full.full_user, "about", None) or None)
    except FloodWaitError as e:
        print(f"[bio] floodwait {e.seconds}с"); await asyncio.sleep(e.seconds + 5)
        return None
    except Exception:  # noqa: BLE001
        return None


async def collect_active(client, entity, scan: int, top: int,
                         harvest: bool = False, days: int = HARVEST_DAYS,
                         posts_per_user: int = POSTS_PER_USER,
                         period_days: int | None = None,
                         incremental: bool = False) -> tuple[list[tuple[User, int]], dict[int, str], set[int]]:
    """Топ авторов по числу сообщений в чате/обсуждении за последние `scan` сообщений.

    harvest=True (H1): дополнительно собирает ТЕКСТЫ сообщений каждого автора (до
    `posts_per_user` за `days` дней) в tg_user_posts + тянет bio — сырьё для досье.
    Аватар качаем всегда (это активные лиды, их немного — top) — для карточки/vision.

    incremental=True: повторный заход по ЭТОМУ ЖЕ чату сканирует только сообщения
    НОВЕЕ прошлого раза (chats.parse_last_msg_id), а не весь `scan` заново с начала
    ленты. На чате, где парсинг уже гонялся, это отличие между «перечитать 2000
    сообщений опять» и «прочитать десяток новых с прошлого захода» — то, ради чего
    вообще заводился --scan большого размера. Первый заход по чату (watermark ещё
    нет) всегда полный — дельту не с чем сравнивать.
    Возвращает (топ-авторы, {tg_user_id: bio}, {tg_user_id с фото})."""
    chat = await _resolve_scan_chat(client, entity)
    if chat is None:
        print("[active] у цели нет чата обсуждения — нечего сканировать.")
        return [], {}, set()
    chat_id = getattr(chat, "id", None)         # сырой telegram-id → в tg_user_posts.chat_id
    chat_title = getattr(chat, "title", None)
    # Каталожную запись резолвим не только для harvest, а и для инкремента — watermark
    # хранится в chats, без записи в каталоге её негде читать/писать.
    catalog_id = None
    last_msg_id = None
    if chat_id and (harvest or incremental):
        with database.get_conn() as conn:
            catalog_id = database.resolve_catalog_chat(conn, chat_id, chat_title, getattr(chat, "username", None))
            if incremental and catalog_id:
                row = conn.execute("SELECT parse_last_msg_id FROM chats WHERE id=?", (catalog_id,)).fetchone()
                last_msg_id = row["parse_last_msg_id"] if row else None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # Окно «за период»: в авторы идут только писавшие за последние period_days.
    # None/0 — с самого начала (насколько хватит глубины scan). Свежий автор ценнее:
    # он в теме прямо сейчас, а не заходил в чат три года назад.
    since = datetime.now(timezone.utc) - timedelta(days=period_days) if period_days else None
    counts: Counter[int] = Counter()
    texts: dict[int, list[tuple]] = defaultdict(list)  # uid -> [(msg_id, ts, text), ...]
    n = 0
    hit_edge = False
    newest_seen: int | None = None
    iter_kwargs = {"limit": scan}
    if incremental and last_msg_id:
        # min_id — Telethon отдаёт СТРОГО НОВЕЕ этого id: то, чего ещё не видели в
        # прошлый заход. limit тут уже подстраховка на случай нереалистично длинной
        # дельты (тысячи сообщений с прошлого раза), а не основной ограничитель.
        iter_kwargs["min_id"] = last_msg_id
    async for m in client.iter_messages(chat, **iter_kwargs):
        if newest_seen is None:
            newest_seen = m.id     # лента идёт от новых к старым — первое сообщение и есть максимум
        # Лента идёт от новых к старым: вышли за окно — дальше только старее,
        # перебирать остаток незачем.
        if since and m.date and m.date < since:
            hit_edge = True
            break
        if m.sender_id and m.sender_id > 0:  # >0 = пользователь (каналы/анонимы отсекаем)
            counts[m.sender_id] += 1
            if harvest and m.message and m.date and m.date >= cutoff \
                    and len(texts[m.sender_id]) < posts_per_user:
                texts[m.sender_id].append((m.id, str(m.date), m.message.strip()[:1000]))
        n += 1
    if incremental and catalog_id and newest_seen:
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE chats SET parse_last_msg_id=?, parse_last_at=datetime('now') WHERE id=?",
                (newest_seen, catalog_id),
            )
    win = (f" — новых с прошлого раза (min_id={last_msg_id})" if (incremental and last_msg_id)
          else f" за {period_days} дн." if period_days else " за всё время")
    print(f"[active] просмотрено {n} сообщений{win}, уникальных авторов: {len(counts)}"
          + (" (дошли до края окна)" if hit_edge else ""))
    out: list[tuple[User, int]] = []
    bios: dict[int, str] = {}
    photo_ids: set[int] = set()
    for uid, cnt in counts.most_common(top):
        try:
            u = await client.get_entity(uid)
        except Exception:  # noqa: BLE001
            continue
        if not _is_lead_user(u):
            continue
        out.append((u, cnt))
        if await _download_avatar(client, u):   # фото — для карточки человека и vision
            photo_ids.add(u.id)
        if harvest:
            posts = texts.get(uid, [])
            if posts:
                with database.get_conn() as conn:
                    saved = database.save_user_posts(conn, u.id, chat_id, chat_title, posts)
                if saved:
                    print(f"  [harvest] {_display_name(u):24} +{saved} сообщ.")
            bio = await _fetch_bio(client, u)
            if bio:
                bios[u.id] = bio
            await asyncio.sleep(random.uniform(0.5, 1.2))  # доп. пауза: bio — тяжёлый вызов
        await asyncio.sleep(random.uniform(0.3, 0.8))
    return out, bios, photo_ids


def _count_sources(tags: str | None) -> int:
    """Сколько РАЗНЫХ чатов дали этого человека — считаем по тегам «TG-парсинг: X»,
    без отдельной m2m-таблицы (её сознательно не заводили: при 10-50 источниках
    текстового тега достаточно, а лишняя таблица — лишняя точка расхождения)."""
    if not tags:
        return 0
    seen = set()
    for part in tags.split(","):
        part = part.strip()
        if part.startswith("TG-парсинг:"):
            # «TG-парсинг: ЦИТРУС | Общая / участник» → чат без роли-суффикса —
            # роль у одного чата может смениться между заходами (участник → админ),
            # это не должно считаться вторым источником.
            chat = part.split("/")[0].strip()
            seen.add(chat)
    return len(seen)


def _compute_priority(role_code: str | None, source_count: int) -> int:
    """Скоринг по правилам (ТЗ «расширение парсинга», без ML):
    1 = owner/admin источника, 2 = active в 2+ источниках, 3 = active в 1,
    4 = silent (holding, дальше не передаётся сразу). Меньше число — выше приоритет,
    как в самом ТЗ. role_code=None (участник без активности, не holding-статус
    active) — это и есть 'silent': не писал, просто состоит."""
    if role_code in (ROLE_CREATOR, ROLE_ADMIN):
        return 1
    if role_code == ROLE_ACTIVE:
        return 2 if source_count >= 2 else 3
    return 4


# Ранг роли для СРАВНЕНИЯ (не для хранения): выше — важнее. Нужен, чтобы повторный
# заход по чату B, где человек рядовой участник, не понижал уже известную роль
# creator/admin, добытую раньше в чате A — раньше _save_lead писал role_code
# «последним увиденным», и админ мог откатиться до простого участника только
# потому, что второй парсинг случился по другому чату.
_ROLE_RANK = {ROLE_CREATOR: 3, ROLE_ADMIN: 2, ROLE_ACTIVE: 1, ROLE_MEMBER: 0}


def _better_role(old: str | None, new: str | None) -> str | None:
    if not old:
        return new
    if not new:
        return old
    return new if _ROLE_RANK.get(new, 0) > _ROLE_RANK.get(old, 0) else old


def _save_lead(conn, u: User, target: str, role: str, source: str = "tg_parse",
               role_code: str | None = None, phone: str | None = None) -> str:
    """Кладёт пользователя в книжку. Дедуп по tg_user_id. Возвращает 'new'/'dup'.

    source — свой ярлык происхождения («210826точканетворк»). По умолчанию общий
    'tg_parse': раньше он был зашит намертво, и все спарсенные чаты за всё время
    сваливались в одну кучу — в «Контактах» нельзя было отделить участников одной
    группы от другой и посмотреть, сколько дал конкретный заход (фильтр по источнику
    там уже есть, фильтровать было нечего)."""
    existing = database.find_contact_by_tg(conn, tg_user_id=u.id, username=u.username)
    tag = f"TG-парсинг: {target}" + (f" / {role}" if role else "")
    if existing:
        old_tags = existing["tags"] or ""
        new_tags = old_tags
        if tag not in old_tags:
            new_tags = f"{old_tags}, {tag}" if old_tags else tag
            conn.execute("UPDATE contacts SET tags=?, updated_at=datetime('now') WHERE id=?", (new_tags, existing["id"]))
        # Роль — ЛУЧШАЯ из старой и новой, не «последняя увиденная»: без этого повторный
        # заход по чату B, где человек рядовой участник, откатывал бы уже известного
        # admin/creator из чата A обратно до простого member (см. _better_role).
        final_role = _better_role(existing["tg_chat_role"], role_code)
        if final_role != existing["tg_chat_role"]:
            conn.execute("UPDATE contacts SET tg_chat_role=? WHERE id=?", (final_role, existing["id"]))
        if phone:
            conn.execute("UPDATE contacts SET phone=COALESCE(NULLIF(phone,''),?) WHERE id=?",
                         (phone, existing["id"]))
        priority = _compute_priority(final_role, _count_sources(new_tags))
        conn.execute("UPDATE contacts SET parse_priority=? WHERE id=?", (priority, existing["id"]))
        return "dup"
    name = _display_name(u)
    cid = database.upsert_contact(
        conn,
        source=source,
        username=u.username,
        tg_user_id=u.id,
        name=name,
        tags=tag,
        notes=f"Найден парсером TG в {target} ({role})",
        gender=gender_of(name),
        is_premium=1 if getattr(u, "premium", False) else 0,
        phone=phone,
    )
    conn.execute("UPDATE contacts SET has_tg='yes' WHERE id=?", (cid,))
    if role_code:
        conn.execute("UPDATE contacts SET tg_chat_role=? WHERE id=?", (role_code, cid))
    conn.execute("UPDATE contacts SET parse_priority=? WHERE id=?",
                 (_compute_priority(role_code, 1), cid))
    return "new"


def _phones_of(users: list) -> dict[int, str]:
    """{tg_user_id: телефон} по тем, у кого он реально виден. Без единого запроса:
    номер уже лежит в объекте User, если человек его не спрятал."""
    out: dict[int, str] = {}
    for u in users:
        ph = _visible_phone_sync(u)
        if ph:
            out[u.id] = ph
    return out


def _visible_phone_sync(user) -> str | None:
    ph = (getattr(user, "phone", None) or "").strip()
    if not ph:
        return None
    return ph if ph.startswith("+") else f"+{ph}"


async def _collect_members_shared(accs: list, entity, target: str, limit: int, first_client):
    """Участники чата, поделённые между несколькими аккаунтами.

    Зачем: выгрести 300-тысячный чат одним аккаунтом — верный флуд-лимит, а то и бан.
    Каждый аккаунт берёт свой диапазон (offset), поэтому на каждого приходится
    limit/N запросов вместо limit. Аккаунты работают ПО ОЧЕРЕДИ, а не параллельно:
    одновременные заходы с разных IP в один чат Telegram тоже не любит.

    Аккаунт, который не смог (мёртвый прокси, не состоит в чате), просто пропускаем —
    его кусок добирать не пытаемся: лучше меньше лидов, чем сожжённый аккаунт.
    """
    if len(accs) <= 1:
        return await collect_members(first_client, entity, limit)

    # СНАЧАЛА выясняем, кто реально может работать по этому чату, и только потом делим.
    # Иначе выходило нечестное деление: limit резался на всех отмеченных, а те, кто в
    # закрытом чате не состоит, срывались на _resolve_target — и их куски (offset)
    # просто выпадали. Оператор получал часть выборки и решал, что чат маленький.
    usable: list = [accs[0]]                      # первый уже подключён и прошёл resolve
    probes: dict = {}
    for acc_id in accs[1:]:
        try:
            client, _ = client_for_account(acc_id)
            await client.connect()
            ent = await _resolve_target(client, target)
            usable.append(acc_id)
            probes[acc_id] = (client, ent)
        except Exception as e:  # noqa: BLE001 — не в чате / мёртвый прокси / нет сессии
            print(f"[members] аккаунт #{acc_id} не берём: {type(e).__name__}: {str(e)[:60]}")
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    if len(usable) < len(accs):
        print(f"[members] делим на {len(usable)} из {len(accs)} — остальные в чат не попали")
    accs = usable

    if len(accs) <= 1:
        # Остались одни: честнее выгрести чат целиком одним аккаунтом, чем отдать ему
        # 1/N лимита и молча потерять остальное.
        print("[members] работоспособен только один аккаунт — беру им весь лимит")
        return await collect_members(first_client, entity, limit)

    per = max(1, limit // len(accs))
    seen: set[int] = set()
    out: list = []
    for i, acc_id in enumerate(accs):
        offset = i * per
        # Первый аккаунт уже подключён вызывающим — переиспользуем, не плодя сессий.
        if i == 0:
            client, own, ent = first_client, False, entity
        else:
            # Клиент и сущность уже получены на этапе проверки выше — второй раз
            # подключаться и резолвить не нужно (лишние запросы к Telegram).
            client, ent = probes[acc_id]
            own = True
        try:
            part = await collect_members(client, ent, per, offset=offset)
            fresh = [u for u in part if u.id not in seen]
            seen.update(u.id for u in fresh)
            out.extend(fresh)
            print(f"[members] аккаунт #{acc_id if acc_id else 'main'}: "
                  f"взял {len(fresh)} (offset {offset})")
        except Exception as e:  # noqa: BLE001
            print(f"[members] аккаунт #{acc_id} сорвался: {type(e).__name__}: {str(e)[:70]}")
        finally:
            if own:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.sleep(random.uniform(*SCRAPE_PAUSE))
    return out


def _acc_labels(accs: list) -> str:
    """Ярлыки аккаунтов для показа в истории: «Вася SDR, Аня» вместо «12, 15»."""
    ids = [a for a in accs if a]
    if not ids:
        return "главный из .env"
    qm = ",".join("?" * len(ids))
    with database.get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, label, username, phone FROM accounts WHERE id IN ({qm})", ids).fetchall()
    by_id = {r["id"]: (r["label"] or r["username"] or r["phone"] or f"#{r['id']}") for r in rows}
    return ", ".join(str(by_id.get(i, f"#{i}")) for i in ids)


def _run_start(target: str, mode: str, source: str, accs: list,
               period_days: int | None) -> int | None:
    """Заводит строку в истории запусков. Ошибку глушим: история — вспомогательная
    вещь, из-за неё парсинг падать не должен."""
    try:
        with database.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO parse_runs (target, mode, source, account_ids, account_label, "
                "period_days) VALUES (?,?,?,?,?,?)",
                (target, mode, source, ",".join(str(a) for a in accs if a),
                 _acc_labels(accs), period_days or 0))
            return cur.lastrowid
    except Exception as e:  # noqa: BLE001
        print(f"[history] не записалось: {str(e)[:70]}")
        return None


def _run_finish(run_id: int, chat_title: str, found: int, new: int, dup: int) -> None:
    try:
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE parse_runs SET chat_title=?, found=?, saved_new=?, saved_dup=?, "
                "ok=1, finished_at=datetime('now') WHERE id=?",
                (chat_title, found, new, dup, run_id))
    except Exception as e:  # noqa: BLE001
        print(f"[history] не обновилось: {str(e)[:70]}")


def _run_fail(run_id: int | None, err: str) -> None:
    if not run_id:
        return
    try:
        with database.get_conn() as conn:
            conn.execute("UPDATE parse_runs SET ok=0, error=?, finished_at=datetime('now') "
                         "WHERE id=?", (err[:300], run_id))
    except Exception:  # noqa: BLE001
        pass


def _report(title: str, users: list, counts: dict | None = None) -> None:
    print(f"\n=== {title}: {len(users)} ===")
    for u in users[:60]:
        extra = f"  ×{counts[u.id]}" if counts and u.id in counts else ""
        print(f"  {_display_name(u):30} @{u.username or '-':20}{extra}")


def _persist(users: list, target: str, role: str, source: str = "tg_parse",
             default_role_code: str | None = None,
             phones: dict[int, str] | None = None) -> tuple[int, int]:
    """Возвращает (новых, дублей) — цифры нужны для истории запусков.

    Роль пишем ПОШТУЧНО (_role_of), а не одним словом на всю пачку: среди участников
    попадаются владелец и админы, и терять это на записи в книжку нельзя."""
    new = dup = 0
    phones = phones or {}
    with database.get_conn() as conn:
        for u in users:
            code = _role_of(u, default_role_code or ROLE_MEMBER)
            r = _save_lead(conn, u, target, ROLE_RU.get(code, role), source,
                           role_code=code, phone=phones.get(u.id))
            new += r == "new"; dup += r == "dup"
    print(f"[save] {role}: добавлено {new}, уже было {dup}")
    return new, dup


async def search_chats(client, query: str, limit: int) -> None:
    """Глобальный поиск публичных групп/каналов по запросу. Печатает кандидатов
    с @username, типом и числом участников — чтобы выбрать цели для парсинга."""
    try:
        res = await client(SearchRequest(q=query, limit=min(limit, 50)))
    except FloodWaitError as e:
        print(f"[floodwait] жду {e.seconds}с"); await asyncio.sleep(e.seconds + 5)
        res = await client(SearchRequest(q=query, limit=min(limit, 50)))
    chats = [c for c in res.chats if isinstance(c, Channel) and c.username]
    print(f"\n=== Найдено по «{query}»: {len(chats)} (с @username) ===")
    for c in chats:
        kind = "супергруппа" if c.megagroup else ("канал" if c.broadcast else "группа")
        cnt = getattr(c, "participants_count", None)
        cnt_s = f"  ~{cnt} уч." if cnt else ""
        print(f"  @{c.username:28} {kind:12} {c.title}{cnt_s}")
    print("\nПарсить выбранную: python -m channels.tg_parser --target @username --mode all --save")


async def _resolve_target(client, target: str):
    """Цель как её ждёт get_entity: @username/ссылка/телефон — как есть, но голый
    числовой ID (для ЗАКРЫТЫХ чатов без публичной ссылки) так не резолвится —
    Telethon трактует ЛЮБУЮ строку как username и не проверяет, что это число.

    Для канала/супергруппы Telegram пускает только с парой «id + access_hash»,
    выданной конкретному аккаунту — а строковая сессия между отдельными запусками
    свой кэш сущностей не хранит, поэтому голый PeerChannel(id) сработает лишь
    если сущность попадёт в кэш ЗА ЭТОТ ЖЕ прогон. Поэтому при числовой цели сразу
    идём в iter_dialogs() этого аккаунта — он и так должен состоять в чате (это и
    есть весь смысл парсинга закрытой группы без ссылки) — и возвращаем ПОЛНУЮ
    сущность оттуда (не голый InputPeer): у неё уже есть access_hash для методов
    вроде GetParticipantsRequest, и вдобавок title — для человекочитаемых тегов у
    сохранённых лидов (см. run(): label = entity.title)."""
    t = target.strip()
    if not t.lstrip("-").isdigit():
        return await client.get_entity(target)
    # «Голый» ID канала/супергруппы у Telethon хранится БЕЗ -100/минус-префикса,
    # ровно как отдаёт chat_inventory (getattr(e,"id")) — принимаем оба варианта,
    # чтобы цель можно было скопировать и из каталога чатов, и из полного tg://
    # chat_id вида -100xxxxxxxxxx.
    raw_id = int(t.replace("-100", "", 1)) if t.startswith("-100") else abs(int(t))
    async for dialog in client.iter_dialogs():
        e = dialog.entity
        if getattr(e, "id", None) == raw_id:
            return e
    raise ValueError(f"аккаунт не состоит в чате с id={raw_id} (или он ещё не встретился "
                     f"в его диалогах) — цель по числовому id работает только для чатов, "
                     f"где выбранный аккаунт реально состоит")


async def run(target: str, mode: str, limit: int, scan: int, top: int, save: bool,
              harvest: bool = False, days: int = HARVEST_DAYS,
              account_id: int | None = None, source: str = "tg_parse",
              account_ids: list[int] | None = None, period_days: int | None = None,
              incremental: bool = False) -> None:
    """account_id=None — главный аккаунт из .env; иначе рабочий аккаунт по id.

    ⚠️ Парсинг резолвит username'ы, а ResolveUsername — самый лимитируемый вызов TG.
    Массовый сбор главным аккаунтом ведёт к FloodWait на сутки (проверено на живом:
    @iivairf словил 22.6 ч). Для любых пачек передавай --account рабочего аккаунта.
    """
    database.init_db()
    # Один аккаунт — частный случай списка: дальше по коду ветвление уже не нужно.
    accs = list(account_ids) if account_ids else ([account_id] if account_id else [None])
    run_id = _run_start(target, mode, source, accs, period_days) if save else None

    client, _ = client_for_account(accs[0])
    await client.connect()
    me = await client.get_me()
    print(f"Подключён как @{me.username or me.id}; цель: {target}"
          + (f"; аккаунтов в работе: {len(accs)}" if len(accs) > 1 else ""))

    if mode == "search":
        await search_chats(client, target, limit)
        await client.disconnect()
        return

    entity = await _resolve_target(client, target)
    # Числовой tg_chat_id в тегах/заметках контакта нечитаем оператору («TG-парсинг:
    # 1156783003») — подменяем на название чата, когда цель была числом.
    label = getattr(entity, "title", None) or target

    found = new_total = dup_total = 0

    if mode in ("admins", "all"):
        admins = await collect_admins(client, entity)
        _report("Админы", admins)
        found += len(admins)
        if save:
            n, d = _persist(admins, label, "админ", source, ROLE_ADMIN,
                            _phones_of(admins))
            new_total += n; dup_total += d
        await asyncio.sleep(random.uniform(*SCRAPE_PAUSE))

    if mode == "members":
        members = await _collect_members_shared(accs, entity, target, limit, client)
        _report("Участники", members)
        found += len(members)
        if save:
            n, d = _persist(members, label, "участник", source, ROLE_MEMBER,
                            _phones_of(members))
            new_total += n; dup_total += d

    if mode in ("active", "all"):
        active, bios, photo_ids = await collect_active(client, entity, scan, top, harvest=harvest,
                                                       days=days, period_days=period_days,
                                                       incremental=incremental)
        users = [u for u, _ in active]
        counts = {u.id: c for u, c in active}
        _report("Активные комментаторы", users, counts)
        found += len(users)
        if save:
            n, d = _persist(users, label, "активный", source, ROLE_ACTIVE, _phones_of(users))
            new_total += n; dup_total += d
            with database.get_conn() as conn:
                if bios:  # bio пишем в уже созданные карточки лидов
                    for uid, bio in bios.items():
                        database.set_bio_by_tg(conn, uid, bio)
                    print(f"[save] bio записано: {len(bios)}")
                if photo_ids:  # помечаем, у кого скачан аватар (для карточки)
                    database.mark_photos_by_tg(conn, photo_ids)
                    print(f"[save] фото скачано: {len(photo_ids)}")
        if harvest:
            print("[harvest] сырьё для досье собрано в tg_user_posts — дальше agent/enrich_person.py")

    await client.disconnect()
    if run_id:
        _run_finish(run_id, label, found, new_total, dup_total)
    print("\nГотово." + ("" if save else "  (сухой прогон — добавь --save, чтобы записать в книжку)"))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM парсер Telegram-каналов/чатов")
    p.add_argument("--target", required=True, help="@username/ссылка/id канала; для --mode search это поисковый запрос")
    p.add_argument("--mode", choices=["admins", "members", "active", "all", "search"], default="admins")
    p.add_argument("--limit", type=int, default=500, help="макс участников в режиме members")
    p.add_argument("--scan", type=int, default=2000, help="сколько сообщений просмотреть в режиме active")
    p.add_argument("--top", type=int, default=50, help="сколько топ-авторов взять в режиме active")
    p.add_argument("--save", action="store_true", help="записать найденных в книжку (иначе только печать)")
    p.add_argument("--harvest", action="store_true", help="H1: собрать тексты+bio авторов в tg_user_posts (сырьё для досье)")
    p.add_argument("--days", type=int, default=HARVEST_DAYS, help="окно сбора сообщений для --harvest (дней)")
    p.add_argument("--source", default="tg_parse",
                   help="ярлык происхождения для «Контактов» (напр. 210826точканетворк). "
                        "По умолчанию общий tg_parse — тогда разные чаты сваливаются в одну "
                        "кучу и в CRM их не отделить друг от друга фильтром по источнику")
    p.add_argument("--accounts", default=None,
                   help="НЕСКОЛЬКО аккаунтов через запятую (напр. 12,15,18) — работа по чату "
                        "делится между ними: каждый берёт свой кусок участников, поэтому на "
                        "каждый приходится в N раз меньше запросов и флуд-лимит не ловится. "
                        "Заходят по очереди, а не одновременно")
    p.add_argument("--period-days", type=int, default=None, dest="period_days",
                   help="режим active: считать авторами только писавших за последние N дней "
                        "(не задан/0 — с самого начала, насколько хватит --scan)")
    p.add_argument("--incremental", action="store_true",
                   help="режим active: повторный заход по этому же чату сканирует ТОЛЬКО "
                        "сообщения новее прошлого раза (watermark в chats.parse_last_msg_id), "
                        "а не весь --scan заново с начала ленты. Первый заход по чату всегда "
                        "полный — дельту не с чем сравнивать")
    p.add_argument("--account", type=int, default=None, dest="account_id",
                   help="аккаунт из БД, которым парсить (по id) — обязателен для ЗАКРЫТЫХ чатов: "
                        "список участников видит только тот, кто реально в чате состоит. "
                        "Не задан — используется главный аккаунт из .env (годится только для "
                        "публичных @чатов, куда .env-аккаунт не обязан быть вступившим)")
    args = p.parse_args()
    acc_ids = [int(x) for x in (args.accounts or "").split(",") if x.strip()] or None
    asyncio.run(run(args.target, args.mode, args.limit, args.scan, args.top, args.save,
                    harvest=args.harvest, days=args.days, account_id=args.account_id,
                    source=args.source, account_ids=acc_ids, period_days=args.period_days,
                    incremental=args.incremental))


if __name__ == "__main__":
    main()
