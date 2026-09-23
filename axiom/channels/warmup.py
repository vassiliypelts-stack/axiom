"""Прогрев Telegram-аккаунтов AXIOM.

Два режима:
  • --ping  — БЫСТРЫЙ ТЕСТ: с основного аккаунта (TG_STRING_SESSION) шлёт N коротких
              сообщений на указанные номера/юзернеймы. Нужен, чтобы вживую увидеть,
              что отправка и человеческий темп работают. Логин других номеров не нужен.
  • --run   — ПОЛНЫЙ ПРОГРЕВ: берёт аккаунты в статусе 'warming' (у кого есть сессия,
              залогинены через `python -m channels.account_login --id N`), и раз в
              запуск (= «день») имитирует живую активность: вступает в пару каналов,
              шлёт немного сообщений «якорям» (твоим активным номерам) и другим
              прогреваемым (взаимный прогрев), выходит в онлайн. По стадиям нарастает,
              после READY_STAGE аккаунт переводится в 'active'.

Примеры:
    python -m channels.warmup --ping "+79137876067,+77027417272" --n 3
    python -m channels.warmup --run
"""
from __future__ import annotations

import argparse
import asyncio
import random

from telethon import TelegramClient, functions
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import InputPhoneContact, ReactionEmoji

import config
from channels.telegram import _build_client, build_client
from db import database

# Живые короткие фразы для имитации переписки (между своими аккаунтами).
CHATTER = ["привет)", "как дела?", "ты тут?", "норм всё?", "на связи", "что нового",
           "добрый день", "ок, понял", "хорошего дня)", "тест связи"]
# Безопасные публичные каналы для вступления (правь под себя). Берём по чуть-чуть.
CHANNELS = ["telegram", "durov", "tginfo", "telegram_tips", "trends"]
# Эмодзи-реакции (лайки постов) — как живой пользователь.
LIKE_EMOJIS = ["👍", "❤️", "🔥", "👌", "😁", "🙏"]

# План прогрева на ~2 недели (один запуск = одна «ступень»/день). Плавно нарастает.
# Первые дни — ТОЛЬКО пассив: вступил в канал, почитал ленту, лайкнул, был онлайн.
# Личные сообщения (msgs) начинаются со 2-3 дня и растут медленно — без рывка в спам.
WARM_PLAN = {
    0:  {"channels": 1, "msgs": 0, "react": 1, "read": 5},
    1:  {"channels": 1, "msgs": 0, "react": 1, "read": 6},
    2:  {"channels": 0, "msgs": 1, "react": 2, "read": 6},
    3:  {"channels": 1, "msgs": 1, "react": 2, "read": 8},
    4:  {"channels": 0, "msgs": 2, "react": 2, "read": 8},
    5:  {"channels": 1, "msgs": 2, "react": 3, "read": 10},
    6:  {"channels": 0, "msgs": 2, "react": 3, "read": 10},
    7:  {"channels": 1, "msgs": 3, "react": 3, "read": 10},
    8:  {"channels": 0, "msgs": 3, "react": 4, "read": 12},
    9:  {"channels": 0, "msgs": 3, "react": 4, "read": 12},
    10: {"channels": 1, "msgs": 4, "react": 4, "read": 12},
    11: {"channels": 0, "msgs": 4, "react": 5, "read": 14},
    12: {"channels": 0, "msgs": 4, "react": 5, "read": 14},
    13: {"channels": 0, "msgs": 5, "react": 5, "read": 14},
}
READY_STAGE = 14  # ~2 недели плавного прогрева → 'active'

# --------------------------------------------------------------------------- #
#  ПОДДЕРЖИВАЮЩИЙ ПРОГРЕВ боевых (active) номеров                             #
# --------------------------------------------------------------------------- #
# ЗАЧЕМ. Прогрев кончался на 14-й стадии, и живость обрывалась ровно в тот день,
# когда номер уходил в бой: дальше он ТОЛЬКО писал незнакомцам. Telegram видит
# резкую смену поведения там, где риск максимален — по кампании 9407 все 13
# боевых словили PeerFlood за двое суток.
#
# КАК ИМЕННО (иначе поддержка сама станет почерком фермы):
#   • ПАРЫ, а не «все пишут всем». 56 номеров, каждый пишет двум случайным — это
#     112 ЛС в сутки внутри замкнутой группы, где связан каждый с каждым. Такой
#     граф читается мгновенно. Поэтому на прогон номер общается с ОДНИМ партнёром.
#   • ОЧЕРЕДЬ по давности (last_upkeep_at). Доля UPKEEP_SHARE=1.0 — за прогон
#     проходят ВСЕ боевые. Так было не всегда: доля 0.25 давала номеру живость раз
#     в четыре дня, и 22.09 по 9407 все восемь отправителей слегли в PeerFlood за
#     три часа — холодные исходящие есть, ничего другого нет. Ежедневный прогон не
#     делает поведение машинным, потому что внутри всё равно решает случай: часть
#     номеров молчит (UPKEEP_SKIP), у пар свои сценарии, а вступления в группы
#     ограничены дневной квотой 1-2 на аккаунт в group_research.
#   • ДИАЛОГ, а не пинг. Реплики идут парой «вопрос → ответ» из одного сценария:
#     партнёр отвечает по смыслу. Старый CHATTER («тест связи», «ты тут?») слали
#     оба конца вразнобой, и переписка читалась как обмен пингами двух ботов.
#   • ТИШИНА И ПРОПУСКИ. Живой человек пишет не каждый день: часть прогонов
#     UPKEEP_SKIP проходит вообще без ЛС — только чтение ленты и лайки.
UPKEEP_SHARE = 1.0       # какую долю боевых берём за один прогон (все)
UPKEEP_SKIP = 0.35       # с какой вероятностью номер в этот раз молчит (только пассив)
UPKEEP_READ = (6, 14)    # сколько постов прочитать
UPKEEP_REACT = (1, 3)    # сколько лайков поставить

# Короткие бытовые сценарии: (первая реплика, ответ партнёра). Отвечает ПАРТНЁР,
# поэтому в чате видно нормальную беседу, а не два независимых потока реплик.
UPKEEP_DIALOGS = [
    ("привет, как сам?", "да норм, потихоньку) у тебя как?"),
    ("слушай, ты далеко?", "не, на районе. а что?"),
    ("видел новости сегодня?", "краем глаза) а что там"),
    ("ты на выходных свободен?", "вроде да, а что планируешь"),
    ("как погода у вас?", "с утра лил дождь, сейчас норм"),
    ("привет) давно не списывались", "и не говори, закрутился совсем"),
    ("ты кофе пьёшь по утрам?", "литрами) без него никак"),
    ("как на работе, завал?", "та как обычно, к вечеру разгребу"),
    ("с наступающими выходными)", "спасибо) и тебя"),
    ("ты фильм тот смотрел?", "ещё нет, всё руки не доходят"),
    ("отдыхал куда-нибудь летом?", "на море выбрался ненадолго, а ты"),
    ("привет, всё в силе?", "да, конечно) договорились"),
]


async def _resolve_target(client: TelegramClient, target: str):
    """@username или телефон → сущность Telegram."""
    t = target.strip()
    if t.startswith("@") or not any(ch.isdigit() for ch in t):
        return await client.get_entity(t.lstrip("@"))
    res = await client(ImportContactsRequest(
        [InputPhoneContact(client_id=0, phone=t, first_name="warm", last_name="")]
    ))
    if res.users:
        return res.users[0]
    raise ValueError(f"номер {t} не найден в Telegram")


async def _go_online(client) -> None:
    """Выйти в онлайн (живой пользователь заходит в приложение)."""
    try:
        await client(UpdateStatusRequest(offline=False))
    except Exception:  # noqa: BLE001
        pass


async def _read_feed(client, n: int) -> int:
    """Почитать ленту: пройти по диалогам, «прочитать» последние сообщения."""
    cnt = 0
    try:
        async for d in client.iter_dialogs(limit=max(n, 1)):
            try:
                async for _ in client.iter_messages(d.entity, limit=3):
                    pass
                await client.send_read_acknowledge(d.entity)
                cnt += 1
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(random.uniform(1.0, 3.0))
    except Exception:  # noqa: BLE001
        pass
    return cnt


async def _react_feed(client, n: int) -> int:
    """Лайкнуть посты в каналах/группах, где состоит аккаунт (как живой юзер)."""
    done = 0
    try:
        async for d in client.iter_dialogs(limit=20):
            if done >= n:
                break
            ent = d.entity
            if not (getattr(ent, "broadcast", False) or getattr(ent, "megagroup", False)):
                continue
            try:
                async for m in client.iter_messages(ent, limit=6):
                    if not m.id:
                        continue
                    await client(SendReactionRequest(
                        peer=ent, msg_id=m.id,
                        reaction=[ReactionEmoji(emoticon=random.choice(LIKE_EMOJIS))],
                    ))
                    done += 1
                    print(f"  лайк в «{getattr(ent, 'title', '?')}»")
                    await asyncio.sleep(random.uniform(3.0, 9.0))
                    break
            except Exception:  # noqa: BLE001
                continue  # реакции могут быть выключены — идём дальше
    except Exception:  # noqa: BLE001
        pass
    return done


async def _setup_profile(client, acc: dict, force: bool = False) -> list[str]:
    """Оформление профиля: био, аватар и приватность (спрятать номер) из карточки.
    По умолчанию (force=False) — только пустое (прогрев, не перезатираем).
    force=True — поставить поверх существующего + применить приватность (кнопка
    «оформить сейчас»). Заполненный профиль + спрятанный номер реже флагают как спам.
    Возвращает список выполненных действий (для отчёта в интерфейсе)."""
    done: list[str] = []
    # имя профиля: tg_name (чистое «Имя Фамилия», без цифр) в приоритете — так
    # ставится по массовой «🎭 Личности». Если его нет — берём ярлык карточки
    # (ручной ввод), но не трогаем номера/плейсхолдеры вида «+7…» или «#12».
    full_name = (acc.get("tg_name") or "").strip() or (acc.get("label") or "").strip()
    try:
        if full_name and not full_name.startswith(("+", "#")):
            first, _, last = full_name.partition(" ")
            me = await client.get_me()
            if force or not (me.first_name or "").strip():
                from telethon.tl.functions.account import UpdateProfileRequest
                await client(UpdateProfileRequest(first_name=first[:64], last_name=last[:64] or ""))
                done.append("имя профиля")
                print(f"  профиль: поставил имя «{full_name}»")
    except Exception as e:  # noqa: BLE001
        print(f"  [name] {e}")
    # ник (@username): доверие-вызывающий вид — транслит имени + цифры номера
    # («vasiliy928»), не спамный набор символов. Ставим только явным оформлением
    # и только если ника ещё нет — не отбираем уже прижившийся у аккаунта.
    if force:
        try:
            me = await client.get_me()
            from channels.ru_names import _name_only, make_username_base, translit
            cur = (me.username or "").strip().lower()
            # Имя БЕЗ цифр: ярлык «Василий5328» дал бы want='vasiliy5328', и тогда
            # правильный ник @vasiliy328 не прошёл бы проверку «имя входит в ник».
            want = (translit(_name_only(full_name, "")) or "").lower() if full_name else ""
            base = make_username_base(full_name, acc.get("phone"))
            # Ставим/МЕНЯЕМ ник, если его нет ИЛИ он не отражает имя персоны (напр. ник
            # от продавца «xk_9271» при имени «Василий»). Имя и ник должны совпадать.
            #
            # Мало проверить, что имя ВХОДИТ в ник: @vasiliy5328328 (задвоенные цифры
            # от старой сборки) имя содержит, и такой ник чинить бы не стали. Поэтому
            # ник, начинающийся с имени, но не равный нужному base, тоже переставляем —
            # иначе ботский хвост остаётся навсегда.
            stale = bool(cur) and bool(want) and cur != base.lower() and cur.startswith(want)
            if want and (not cur or want not in cur or stale):
                from telethon.tl.functions.account import CheckUsernameRequest, UpdateUsernameRequest
                candidate = None
                # Запасные варианты — тоже ЦИФРЫ НОМЕРА, просто больше: «vasiliy328»
                # занято → «vasiliy5328» → «vasiliy85328». Случайный хвост
                # (@vasiliy32878) читается как сгенерированный ботом, а номер
                # выглядит осмысленно — так живые люди и разбирают тёзок.
                from channels.ru_names import _name_only, phone_digits
                nm = translit(_name_only(full_name, "user")) or "user"
                tail = [phone_digits(acc.get("phone"), n) for n in (4, 5, 6)]
                variants = [base] + [nm + t for t in tail if t]
                # хвост из random — последний рубеж, если и по номеру всё занято
                variants += [base + str(random.randint(10, 99)),
                             base + str(random.randint(100, 999))]
                seen = set()
                for cand in variants:
                    cand = cand[:32]
                    if cand in seen:
                        continue
                    seen.add(cand)
                    if len(cand) < 5:      # минимум Telegram — 5 символов
                        continue
                    try:
                        if await client(CheckUsernameRequest(username=cand)):
                            candidate = cand
                            break
                    except Exception:  # noqa: BLE001 — занят/невалиден — пробуем следующий вариант
                        continue
                if candidate and candidate.lower() != cur:
                    await client(UpdateUsernameRequest(username=candidate))
                    done.append(f"ник @{candidate}")
                    print(f"  профиль: поставил ник @{candidate}")
                    with database.get_conn() as conn:
                        conn.execute("UPDATE accounts SET username=? WHERE id=?", (candidate, acc["id"]))
        except Exception as e:  # noqa: BLE001
            print(f"  [username] {e}")
    # био из описания агента
    try:
        about = (acc.get("description") or "").strip()[:70]
        if about:
            full = await client(functions.users.GetFullUserRequest("me"))
            if force or not getattr(full.full_user, "about", None):
                from telethon.tl.functions.account import UpdateProfileRequest
                await client(UpdateProfileRequest(about=about))
                done.append("описание")
                print("  профиль: заполнил bio")
    except Exception as e:  # noqa: BLE001
        print(f"  [bio] {e}")
    # аватар из загруженного в карточке агента файла
    try:
        if acc.get("avatar"):
            from pathlib import Path
            p = Path(config.DB_PATH).parent / "avatars" / acc["avatar"]
            if p.exists():
                # При force СНАЧАЛА сносим ВСЕ старые фото — иначе на аккаунте копится
                # мешанина из разных людей (Telegram добавляет новое поверх старых, а
                # не заменяет). Должно остаться ровно одно согласованное лицо.
                if force:
                    try:
                        old = await client.get_profile_photos("me")
                        if old:
                            from telethon.tl.functions.photos import DeletePhotosRequest
                            await client(DeletePhotosRequest(id=list(old)))
                            done.append(f"снял старых фото ({len(old)})")
                            print(f"  профиль: удалил {len(old)} старых фото")
                    except Exception as e:  # noqa: BLE001
                        print(f"  [avatar/del] {e}")
                existing = await client.get_profile_photos("me", limit=1)
                if force or not existing:
                    from telethon.tl.functions.photos import UploadProfilePhotoRequest
                    f = await client.upload_file(str(p))
                    await client(UploadProfilePhotoRequest(file=f))
                    done.append("аватар")
                    print("  профиль: поставил аватар")
    except Exception as e:  # noqa: BLE001
        print(f"  [avatar] {e}")
    # Приватность (спрятать номер + защита от репортов) — ставим ОДИН РАЗ на аккаунт,
    # а не при каждом подключении.
    #
    # Раньше apply_privacy вызывался безусловно, а это 5 запросов SetPrivacy. Перед
    # КАЖДЫМ заходом рассылки _setup_profile вызывается для каждого отправителя — и
    # восемь номеров тратили 40 служебных обращений к Telegram ещё до первого письма.
    # Telegram считает не письма, а действия: отсюда PeerFlood на первом же контакте
    # (18.09, кампания 9407 — Антон419 словил флуд, отправив одно сообщение).
    #
    # Настройки приватности в Telegram постоянны: выставленные однажды, они не
    # слетают. Повторная установка тех же значений ничего не меняет, но расходует
    # лимит. Отмечаем в accounts.notes факт установки и больше не трогаем; кнопка
    # «оформить сейчас» (force=True) по-прежнему применяет их принудительно.
    acc_id = acc.get("id")
    already = False
    if acc_id and not force:
        try:
            from db import database as _db
            with _db.get_conn() as _c:
                r = _c.execute("SELECT COALESCE(notes,'') n FROM accounts WHERE id=?",
                               (acc_id,)).fetchone()
            already = bool(r and "[privacy-set]" in (r["n"] or ""))
        except Exception:  # noqa: BLE001 — не смогли прочитать: ставим, как раньше
            already = False
    if not already:
        try:
            from channels.privacy import apply_privacy
            if await apply_privacy(client):
                done.append("🔒 приватность (номер спрятан)")
                if acc_id:
                    try:
                        from db import database as _db
                        with _db.get_conn() as _c:
                            _c.execute(
                                "UPDATE accounts SET notes=COALESCE(notes,'')||' [privacy-set]' "
                                "WHERE id=? AND COALESCE(notes,'') NOT LIKE '%[privacy-set]%'",
                                (acc_id,))
                    except Exception:  # noqa: BLE001 — отметка не критична
                        pass
        except Exception as e:  # noqa: BLE001
            print(f"  [privacy] {e}")
    return done


async def _view_stories(client, n: int, account_id: int | None = None) -> int:
    """Посмотреть и «прочитать» сторис из ленты (ещё живее). Best-effort —
    если версия Telethon без stories API, тихо пропускаем."""
    if n <= 0:
        return 0
    try:
        from telethon.tl.functions.stories import GetPeerStoriesRequest, ReadStoriesRequest
        from telethon.tl.functions.contacts import AddContactRequest
        from telethon.tl.types import User
    except Exception:  # noqa: BLE001
        return 0
    done = 0
    try:
        async for d in client.iter_dialogs(limit=25):
            if done >= n:
                break
            try:
                # Сториз групп/каналов не является личным знакомством. В личную
                # книжку кладём только реальных пользователей, уже видимых в ленте.
                if not isinstance(d.entity, User):
                    continue
                res = await client(GetPeerStoriesRequest(peer=d.entity))
                items = getattr(getattr(res, "stories", None), "stories", None) or []
                if items:
                    await client(ReadStoriesRequest(peer=d.entity, max_id=max(s.id for s in items)))
                    if account_id:
                        name = " ".join(x for x in [getattr(d.entity, "first_name", ""),
                                                       getattr(d.entity, "last_name", "")] if x).strip()
                        # Telegram разрешает добавить уже известного пользователя
                        # в контакты без номера; номер не передаём и не раскрываем.
                        try:
                            await client(AddContactRequest(id=d.entity, first_name=getattr(d.entity, "first_name", "") or "Контакт",
                                                           last_name=getattr(d.entity, "last_name", "") or "", phone=""))
                            action = "contact_added"
                        except Exception:
                            action = "story_seen"
                        with database.get_conn() as conn:
                            contact_id = database.upsert_contact(conn, source="story_seen", tg_user_id=d.entity.id,
                                                                 username=getattr(d.entity, "username", None), name=name,
                                                                 tags="story_seen")
                            conn.execute(
                                "INSERT INTO account_story_contacts (account_id,tg_user_id,username,name,contact_id,last_story_at,last_action) "
                                "VALUES (?,?,?,?,?,datetime('now'),?) "
                                "ON CONFLICT(account_id,tg_user_id) DO UPDATE SET username=excluded.username, "
                                "name=excluded.name, contact_id=excluded.contact_id, last_story_at=datetime('now'), last_action=excluded.last_action",
                                (account_id, d.entity.id, getattr(d.entity, "username", None), name, contact_id, action),
                            )
                    done += 1
                    print(f"  смотрю сторис «{getattr(d.entity, 'title', getattr(d, 'name', '?'))}»")
                    await asyncio.sleep(random.uniform(2.0, 6.0))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return done


async def _send_chatter(client, ent, n: int, label: str = "") -> int:
    sent = 0
    for _ in range(n):
        msg = random.choice(CHATTER)
        try:
            async with client.action(ent, "typing"):
                await asyncio.sleep(random.uniform(1.0, 3.0))
            await client.send_message(ent, msg)
            sent += 1
            print(f"  -> {label}: {msg}")
        except FloodWaitError as e:
            print(f"  [floodwait] {e.seconds}с"); await asyncio.sleep(e.seconds + 5)
        except Exception as e:  # noqa: BLE001
            print(f"  [skip] {label}: {e}")
            break
        await asyncio.sleep(random.uniform(4.0, 12.0))
    return sent


# --------------------------------------------------------------------------- #
#  PING — быстрый тест отправки с основного аккаунта                           #
# --------------------------------------------------------------------------- #
async def ping(targets: list[str], n: int) -> None:
    client = _build_client()
    await client.start()
    me = await client.get_me()
    print(f"шлю с @{me.username or me.id} на {len(targets)} номер(ов), по {n} сообщ.")
    for t in targets:
        try:
            ent = await _resolve_target(client, t)
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {t}: {e}")
            continue
        await _send_chatter(client, ent, n, label=t)
    await client.disconnect()
    print("готово (ping)")


# --------------------------------------------------------------------------- #
#  RUN — полный прогрев аккаунтов в статусе 'warming'                          #
# --------------------------------------------------------------------------- #
async def _ca_mix(client, acc: dict, stage: int) -> int:
    """АНТИ-БАН ОПЦИЯ (выкл по умолчанию): на поздних стадиях вплести немного
    реальных первых касаний ЦА по кампании аккаунта. Ramping: стадия5→1, 6→2, 7+→3.
    Шлёт от ПРОГРЕВАЕМОГО аккаунта (не с основного). Реальные люди — осознанно."""
    cap = min(max(stage - 4, 0), 3)
    if cap <= 0:
        return 0
    from channels.campaign_send import _add_tag, _audience, _greeting, _parts, _sender_name
    from channels.telegram import _resolve_entity, _send_parts
    with database.get_conn() as conn:
        camp = conn.execute(
            "SELECT c.* FROM campaigns c JOIN campaign_accounts ca ON ca.campaign_id=c.id "
            "WHERE ca.account_id=? AND c.channel='telegram' AND IFNULL(c.message_template,'')<>'' "
            "ORDER BY c.id DESC LIMIT 1", (acc["id"],),
        ).fetchone()
    if not camp:
        return 0
    camp = dict(camp)
    # Тот же гейт, что и в рассылке: в ЦА-микс идут РЕАЛЬНЫЕ люди, и если в шаблоне
    # кампании лежит промпт, а не письмо, — прогрев не должен его разослать.
    from channels import opener_lint
    if opener_lint.severe(opener_lint.lint(camp["message_template"])):
        print(f"  [ca-mix] пропуск: у кампании #{camp['id']} в первом сообщении промпт, не текст")
        return 0
    rows = _audience(camp["id"], camp["audience_tag"], "telegram", cap)
    sent = 0
    for row in rows:
        if sent >= cap:
            break
        name = _greeting(row)
        parts = _parts(camp["message_template"], name, row["agency"] or row["name"],
                       sender=_sender_name(acc))
        if not parts:
            break
        try:
            ent = await _resolve_entity(client, row)
            sent_ids = await _send_parts(client, ent, parts)
        except Exception as e:  # noqa: BLE001
            print(f"  [ca-mix skip {row['id']}] {e}")
            continue
        with database.get_conn() as conn:
            database.set_tg_user_id(conn, row["id"], int(ent.id))
            database.add_message(conn, row["id"], "out", "\n".join(parts), intent=None,
                                 tg_msg_ids=sent_ids)
            database.set_status(conn, row["id"], "messaged")
            conn.execute("UPDATE contacts SET tags=? WHERE id=?",
                         (_add_tag(row["tags"], f"кампания #{camp['id']}"), row["id"]))
            conn.execute("INSERT OR IGNORE INTO campaign_contacts (campaign_id, contact_id, account_id) "
                         "VALUES (?,?,?)", (camp["id"], row["id"], acc["id"]))
        sent += 1
        print(f"  [ca-mix {sent}/{cap}] -> {name or row['username']}")
        await asyncio.sleep(random.uniform(20, 60))
    return sent


# --------------------------------------------------------------------------- #
#  ТИХИЙ СТУК — короткое «Здравствуйте, Максим?» на время прогрева              #
# --------------------------------------------------------------------------- #
# Проверка «читает ли человек вообще», а не продажа. Молодой номер (до 14 дней) в
# холодную рассылку не допускается — он ловит PeerFlood на первом же полноценном
# письме (18-19.09 по 9407 так встали шесть номеров). Но короткий человеческий
# вопрос «Здравствуйте, Максим?» — это не рассылочный почерк: одно предложение,
# без ссылок, без оффера, без переносов строк. Такой стук Telegram не читает как
# спам, а мы узнаём, живой ли контакт и читает ли он личку.
#
# РОВНО ОДНО сообщение в сутки на аккаунт. Не «до одного» и не «одно за заход»:
# заходов прогрева в день несколько, и без суточного счёта номер разослал бы по
# числу заходов.
#
# Ответил — питч уходит ВТОРЫМ сообщением (KNOCK_PITCH ниже), уже по-человечески:
# человек откликнулся, значит пишем живому. Дальше диалог ведёт обычный агент
# кампании, как после любого первого касания.
KNOCK_HELLO = ("Добрый день", "Здравствуйте", "Приветствую", "Салют")

# Имени в базе нет у половины контактов (573 из 1073 по 9407). Молчать по ним —
# терять половину проверки, а «Здравствуйте?» без имени выглядит как бот. Поэтому
# для безымянных стук другой: называем общий контекст (пересеклись в крымских
# чатах) и спрашиваем, как обращаться. Вопрос про имя человек воспринимает как
# нормальное начало разговора и отвечает охотнее, чем на «вам интересно?».
KNOCK_NONAME = (
    "Добрый день! Мы с вами пересекались в чатах по Крыму) Как могу к вам обращаться?",
    "Здравствуйте! Кажется, мы с вами в одних крымских чатах) Как к вам обращаться?",
    "Приветствую! Мы с вами в чатах по Крыму пересекаемся) Как вас зовут?",
)

# Второе сообщение — уходит ТОЛЬКО тому, кто ответил на стук. Текст Василия
# (22.09.2026): знакомство через комментарии + вопрос про сообщество и землю.
KNOCK_PITCH = (
    "Обратил внимание на вас в комментариях в крымских каналах — "
    "значит, тема Крыма вам близка 🙂\n"
    "Вам интересно участие в сообществе, где у каждого участника своя земля "
    "1-2 га в шаговой доступности к морю в Крыму, с миндальным садом, который "
    "при этом приносит доход от 1,5 млн рублей в год?"
)


def _knock_text(row) -> str:
    """«Здравствуйте, Максим?» — приветствие плюс имя с вопросительным знаком.

    Имя берём тем же разбором ФИО, что и рассылка (_greeting), иначе на половине
    базы выходит «Викторович?» вместо «Максим?».

    Имени нет (половина базы, плюс организации и латиница) — шлём вариант из
    KNOCK_NONAME: общий контекст и вопрос «как могу к вам обращаться?». Пустую
    строку эта функция больше не возвращает — стук уходит всем.
    """
    from channels.campaign_send import _greeting
    from channels import fio
    # person_name — разобранное ФИО живого человека; name может оказаться названием
    # фирмы («Эталон недвижимость»), и тогда _greeting вернёт её первое слово.
    # «Добрый день, Эталон?» — мгновенный провал проверки, поэтому берём имя только
    # из person_name, а из name — лишь когда это узнаваемое русское имя.
    pn = (row["person_name"] or "").strip()
    name = (_greeting(row) or "").strip() if pn else ""
    if not name:
        cand = (row["name"] or "").strip().split()
        first_raw = cand[0] if cand else ""
        if first_raw and fio._is_known_first(first_raw):
            name = first_raw
    first = name.split()[0] if name else ""
    if not first or not first.isalpha() or len(first) < 3:
        return random.choice(KNOCK_NONAME)
    return f"{random.choice(KNOCK_HELLO)}, {first}?"


def _knock_sent_today(conn, acc_id: int) -> int:
    """Сколько стуков этот аккаунт уже отправил за сутки (UTC, как пишет БД)."""
    return conn.execute(
        "SELECT COUNT(*) c FROM campaign_contacts WHERE account_id=? "
        "AND knock_at IS NOT NULL AND date(knock_at)=date('now')", (acc_id,)
    ).fetchone()["c"]


async def _knock(client, acc: dict) -> int:
    """Один короткий стук от прогреваемого аккаунта. Возвращает 1, если отправлен."""
    from channels.campaign_send import _add_tag, _audience
    from channels.telegram import _resolve_entity

    with database.get_conn() as conn:
        if _knock_sent_today(conn, acc["id"]):
            return 0                      # суточная норма уже выбрана
        camp = conn.execute(
            "SELECT c.id, c.audience_tag FROM campaigns c "
            "JOIN campaign_accounts ca ON ca.campaign_id=c.id "
            "WHERE ca.account_id=? AND c.channel='telegram' AND COALESCE(c.archived,0)=0 "
            "ORDER BY c.id DESC LIMIT 1", (acc["id"],),
        ).fetchone()
    if not camp:
        return 0
    # Берём с запасом: часть контактов не отрезолвится в Telegram.
    rows = _audience(camp["id"], camp["audience_tag"], "telegram", 12)
    for row in rows:
        text = _knock_text(row)
        try:
            ent = await _resolve_entity(client, row)
            msg = await client.send_message(ent, text)
        except Exception as e:  # noqa: BLE001
            print(f"  [стук skip {row['id']}] {e}")
            continue
        with database.get_conn() as conn:
            database.set_tg_user_id(conn, row["id"], int(ent.id))
            database.add_message(conn, row["id"], "out", text, intent=None,
                                 tg_msg_ids=[int(msg.id)] if msg else None)
            # Статус НЕ трогаем: стук — не первое касание кампании, человек ещё
            # ничего о проекте не услышал. Пометим контакт как «постучались»,
            # чтобы рассылка не написала ему заново своё первое сообщение.
            conn.execute("UPDATE contacts SET tags=? WHERE id=?",
                         (_add_tag(row["tags"], "стук"), row["id"]))
            conn.execute(
                "INSERT INTO campaign_contacts (campaign_id, contact_id, account_id, knock_at) "
                "VALUES (?,?,?,datetime('now')) "
                "ON CONFLICT(campaign_id, contact_id) DO UPDATE SET "
                "account_id=excluded.account_id, knock_at=excluded.knock_at",
                (camp["id"], row["id"], acc["id"]))
        print(f"  [стук] -> {text}")
        return 1
    return 0


async def _warm_one(acc, anchors, peers, ca_mix: bool = False, knock: bool = False) -> None:
    """Обёртка: гарантирует, что клиент закроется, чем бы ни кончилась ступень.

    Тело прогрева — ~90 строк сетевых вызовов (вступления, реакции, ЛС), и любой из них
    может бросить (FloodWait, обрыв прокси, отозванная сессия). Вызывающий цикл ошибку
    ловит и идёт к следующему аккаунту, но клиент оставался жить с висящими
    _send_loop/_recv_loop. На 50 прогреваемых аккаунтах это заметная утечка.
    """
    client = build_client(StringSession(acc["tg_session"]), acc["proxy"],
                          acc.get("api_id"), acc.get("api_hash"))
    try:
        await _warm_one_body(client, acc, anchors, peers, ca_mix, knock)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def _warm_one_body(client, acc, anchors, peers, ca_mix: bool = False,
                         knock: bool = False) -> None:
    await client.connect()
    if not await client.is_user_authorized():
        print(f"[skip #{acc['id']}] сессия не авторизована — перелогинь: python -m channels.account_login --id {acc['id']}")
        return
    stage = acc["warm_stage"] or 0
    plan = WARM_PLAN.get(min(stage, max(WARM_PLAN)), WARM_PLAN[max(WARM_PLAN)])
    me = await client.get_me()
    def audit(text: str) -> None:
        """Поштучный журнал для пульта: фактическое действие и его время."""
        with database.get_conn() as conn:
            database.add_event(conn, "warm_action", f"🔥 {acc.get('label') or acc['id']}",
                               text, level="info", account_id=acc["id"])
    print(f"[#{acc['id']} @{me.username or me.id}] стадия {stage}: каналы {plan['channels']}, "
          f"ЛС {plan['msgs']}, лайки {plan.get('react', 0)}, чтение {plan.get('read', 0)}")

    # 1) заходим в онлайн (живой пользователь открыл приложение)
    await _go_online(client)
    audit("зашёл в Telegram онлайн")
    await asyncio.sleep(random.uniform(2, 6))

    # на старте — оформляем профиль (bio/аватар), если пусто
    if stage == 0:
        await _setup_profile(client, acc)

    # Считаем, что реально сделали — для отчёта в карточке аккаунта («что делал сегодня»).
    joined = reads = reacts = stories = sent_total = 0

    # 2) вступаем в каналы (по плану, по чуть-чуть)
    for ch in random.sample(CHANNELS, min(plan["channels"], len(CHANNELS))):
        try:
            await client(JoinChannelRequest(ch))
            joined += 1
            audit(f"вступил в канал @{ch}")
            print(f"  вступил в @{ch}")
        except Exception as e:  # noqa: BLE001
            print(f"  [канал @{ch}] {e}")
        await asyncio.sleep(random.uniform(5, 15))

    # 3) Со стадии 5 один слот чтения заменяем полезной проверкой публичного чата.
    # Это не дополнительная активность: вместо чтения своей ленты аккаунт читает
    # незнакомую группу — для Telegram поведение то же, а каталог наполняется.
    #
    # Верхней границы больше нет. Было «5–7», и аккаунт, перешагнувший 7-ю ступень,
    # переставал исследовать совсем: на 23.09 в прогреве 36 номеров, почти все выше
    # 7-й стадии, и очередь в 16 тысяч групп двигали единицы. Дневную квоту (1-2
    # группы) держит сам group_research._daily_budget, поэтому снятие потолка темп
    # не разгоняет — оно лишь возвращает в работу тех, кто просто дозревает.
    research = None
    if stage >= 5:
        try:
            from channels import group_research
            research = await group_research.run_one(client, acc["id"])
        except Exception as exc:  # одна карточка не должна срывать весь прогрев
            print(f"  [исследование] пропущено: {type(exc).__name__}: {exc}")
    reads = await _read_feed(client, max(0, plan.get("read", 8) - (1 if research else 0)))
    if reads:
        audit(f"прочитал ленту: {reads} чатов")

    # 4) лайкаем посты (реакции в каналах/группах)
    reacts = await _react_feed(client, plan.get("react", 0))
    if reacts:
        audit(f"поставил реакции: {reacts} постов")

    # 4b) смотрим сторис из ленты (ещё живее)
    stories = await _view_stories(client, plan.get("react", 1), acc["id"])
    if stories:
        audit(f"посмотрел сторис: {stories}")

    # 5) лёгкая переписка со «своими» (якоря + другие прогреваемые) — только если по плану есть ЛС
    targets = [a for a in anchors] + [p for p in peers if p["id"] != acc["id"]]
    random.shuffle(targets)
    left = plan["msgs"]
    for t in targets:
        if left <= 0:
            break
        peer = t["username"] or t["phone"]
        if not peer:
            continue
        try:
            ent = await _resolve_target(client, peer)
        except Exception as e:  # noqa: BLE001
            print(f"  [цель {peer}] {e}")
            continue
        s = await _send_chatter(client, ent, 1, label=peer)
        if s:
            audit(f"написал своему аккаунту {peer}")
        left -= s
        sent_total += s

    # 6) опционально: вплести немного реальной ЦА (анти-бан, выкл по умолчанию)
    if ca_mix and stage >= 5:
        await _ca_mix(client, acc, stage)

    # 7) тихий стук: одно короткое «Здравствуйте, Максим?» в сутки. Работает с
    # первой же ступени — в этом и смысл, проверять читаемость базы, пока номер
    # дозревает до холодной рассылки. Ответившим питч уходит отдельно, из
    # channels.listener, а не отсюда.
    if knock:
        try:
            if await _knock(client, acc):
                audit("постучался в один контакт базы")
        except Exception as e:  # noqa: BLE001
            print(f"  [стук] пропуск: {e}")

    new_stage = stage + 1
    activate = new_stage >= READY_STAGE
    who = acc.get("label") or acc.get("phone") or f"#{acc['id']}"
    # Человекочитаемый отчёт «что сделал за прогон» — его показывает карточка аккаунта,
    # чтобы было видно: прогрев реально работал, а не «нажал кнопку и тишина».
    parts = []
    if joined:      parts.append(f"вступил в {joined} канал(а)")
    if reads:       parts.append(f"прочитал ленту ({reads})")
    if reacts:      parts.append(f"лайкнул {reacts}")
    if stories:     parts.append(f"глянул {stories} сторис")
    if sent_total:  parts.append(f"написал {sent_total} сообщ.")
    if research:    parts.append(f"исследовал чат #{research['chat_id']} ({research['status']})")
    summary = ", ".join(parts) if parts else "зашёл онлайн (без активных действий в этот раз)"
    with database.get_conn() as conn:
        database.bump_warm(conn, acc["id"], new_stage, activate=activate)
        database.add_event(conn, "warm_run", f"🔥 Прогрев: {who}",
                           f"стадия {stage}→{new_stage}: {summary}",
                           level="info", account_id=acc["id"])
        if activate:
            database.add_event(conn, "warm_ready", f"🌡 Аккаунт прогрет: {who}",
                               "переведён в «активен» — можно ставить в рассылку",
                               level="good", account_id=acc["id"])
    print(f"  стадия → {new_stage} · {summary}{' · ГОТОВ (active)' if activate else ''}")
    # disconnect делает обёртка _warm_one (finally) — здесь он больше не нужен


async def _upkeep_pair(a: dict, b: dict) -> int:
    """Живой диалог между двумя своими номерами: a спрашивает, b отвечает.

    Отправку ведут ОБА клиента, поэтому в чате остаётся нормальная переписка.
    Холодных ЛС здесь нет: собеседник — свой же аккаунт, контакт уже знакомый,
    и PeerFlood такой трафик не считает.
    """
    from channels.telegram import build_client
    from telethon.sessions import StringSession
    ask, reply = random.choice(UPKEEP_DIALOGS)
    ca = build_client(StringSession(a["tg_session"]), a["proxy"],
                      a.get("api_id"), a.get("api_hash"))
    cb = build_client(StringSession(b["tg_session"]), b["proxy"],
                      b.get("api_id"), b.get("api_hash"))
    na = a.get("label") or f"#{a['id']}"
    nb = b.get("label") or f"#{b['id']}"
    sent = 0
    try:
        await ca.start()
        await cb.start()
        peer_b = b["username"] or b["phone"]
        peer_a = a["username"] or a["phone"]
        if not (peer_a and peer_b):
            return 0
        ent_b = await _resolve_target(ca, peer_b)
        async with ca.action(ent_b, "typing"):
            await asyncio.sleep(random.uniform(1.5, 4.0))
        await ca.send_message(ent_b, ask)
        sent += 1
        print(f"  {na} -> {nb}: {ask}")
        # Пауза «человек прочитал и печатает ответ».
        await asyncio.sleep(random.uniform(20, 90))
        ent_a = await _resolve_target(cb, peer_a)
        async with cb.action(ent_a, "typing"):
            await asyncio.sleep(random.uniform(1.5, 4.0))
        await cb.send_message(ent_a, reply)
        sent += 1
        print(f"  {nb} -> {na}: {reply}")
    except FloodWaitError as e:
        print(f"  [floodwait] {e.seconds}с — пару пропускаю")
    except Exception as e:  # noqa: BLE001
        print(f"  [пара {na}/{nb}] {e}")
    finally:
        for c in (ca, cb):
            try:
                await c.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return sent


async def _upkeep_passive(acc: dict) -> None:
    """Пассивная живость номера: онлайн, чтение ленты, лайки, сторис. Без ЛС."""
    from channels.telegram import build_client
    from telethon.sessions import StringSession
    client = build_client(StringSession(acc["tg_session"]), acc["proxy"],
                          acc.get("api_id"), acc.get("api_hash"))
    who = acc.get("label") or f"#{acc['id']}"
    try:
        await client.start()
        await _go_online(client)
        # Исследование не заканчивается вместе с 14-й ступенью прогрева: аккаунт
        # остаётся в уже взятых группах и дальше спокойно пополняет свою долю
        # каталога. run_one сам держит дневную квоту, поэтому частый upkeep не
        # ускорит вступления сверх безопасного темпа.
        research = None
        try:
            from channels import group_research
            research = await group_research.run_one(client, acc["id"])
        except Exception as exc:  # один проблемный чат не отменяет обычную живость
            print(f"  [{who}] исследование: {type(exc).__name__}: {exc}")
        reads = await _read_feed(client, random.randint(*UPKEEP_READ))
        reacts = await _react_feed(client, random.randint(*UPKEEP_REACT))
        stories = await _view_stories(client, random.randint(1, 2), acc["id"])
        suffix = (f", исследовал #{research['chat_id']} ({research['status']})"
                  if research else "")
        print(f"  [{who}] прочитано {reads}, лайков {reacts}, сторис {stories}{suffix}")
    except Exception as e:  # noqa: BLE001
        print(f"  [{who}] пассив: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def run_upkeep(only_id: int | None = None) -> None:
    """Поддерживающий прогрев боевых номеров: очередь, пары, диалоги.

    Обычный run() трогает только status='warming' и, дойдя до 14-й стадии,
    забывает про аккаунт навсегда — этим и держится живость уже боевых.
    """
    database.init_db()
    with database.get_conn() as conn:
        accs = [dict(a) for a in database.upkeep_accounts(conn)]
    if only_id is not None:
        accs = [a for a in accs if a["id"] == only_id]
        if not accs:
            print(f"аккаунт #{only_id} не годится: нужен active + сессия + живой прокси")
            return
    if not accs:
        print("нет боевых аккаунтов для поддержки (нужен active + сессия + живой прокси)")
        return

    # Очередь: первыми идут те, кого дольше всех не трогали. Так каждый номер
    # выходит на связь раз в несколько дней, а не каждый прогон.
    accs.sort(key=lambda a: (a.get("last_upkeep_at") or ""))
    take = max(2, int(len(accs) * UPKEEP_SHARE)) if only_id is None else len(accs)
    # ОТПРАВИТЕЛИ ИДУТ ВНЕ ОЧЕРЕДИ. Доля 25% от всех боевых означала, что конкретный
    # номер получает поддержку раз в четыре дня. Для аккаунта, который каждый день
    # пишет незнакомцам, это и есть почерк бота: исходящие холодные есть, а живой
    # активности между ними нет. 22.09 так слегли все восемь отправителей 9407 —
    # PeerFlood за три часа, при том что у каждого свой прокси и лимит 2/сутки.
    # Поэтому тех, кто реально в команде запущенных кампаний, берём КАЖДЫЙ прогон
    # и сверх общей доли: им живость нужнее всех.
    if only_id is None:
        with database.get_conn() as conn:
            senders = {r["account_id"] for r in conn.execute(
                "SELECT DISTINCT ca.account_id FROM campaign_accounts ca "
                "JOIN campaigns c ON c.id = ca.campaign_id "
                "WHERE c.status='running' AND COALESCE(c.archived,0)=0")}
        hot = [a for a in accs if a["id"] in senders]
        rest = [a for a in accs if a["id"] not in senders]
        batch = hot + rest[:max(0, take - len(hot))]
    else:
        batch = accs[:take]
    print(f"поддержка: {len(batch)} из {len(accs)} боевых "
          f"(очередь по давности, доля {int(UPKEEP_SHARE * 100)}%)")

    # Разбиваем на пары для диалогов; кому пары не хватило — только пассив.
    #
    # UPKEEP_SKIP касается ТОЛЬКО переписки между своими: живой человек пишет не
    # каждый день, и часть номеров в этот раз молчит. Но «молчит» здесь никогда не
    # значило «выпал из прогона»: пропущенные всё равно идут в silent и проходят
    # _upkeep_passive — онлайн, лента, лайки и исследование одной группы. Именно
    # там вызывается group_research, поэтому каталог наполняют все боевые каждый
    # заход, а не четверть из них.
    pool = [a for a in batch if not (random.random() < UPKEEP_SKIP)]
    silent = [a for a in batch if a not in pool]
    random.shuffle(pool)
    pairs = [(pool[i], pool[i + 1]) for i in range(0, len(pool) - 1, 2)]
    if len(pool) % 2:
        silent.append(pool[-1])

    for a, b in pairs:
        # Обе стороны сначала ведут себя как люди: полистали ленту, полайкали.
        await _upkeep_passive(a)
        await _upkeep_passive(b)
        await _upkeep_pair(a, b)
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET last_upkeep_at=datetime('now') "
                         "WHERE id IN (?,?)", (a["id"], b["id"]))
        await asyncio.sleep(random.uniform(60, 240))

    for acc in silent:
        await _upkeep_passive(acc)
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET last_upkeep_at=datetime('now') WHERE id=?",
                         (acc["id"],))
        await asyncio.sleep(random.uniform(30, 120))

    print(f"поддержка закончена: диалогов {len(pairs)}, "
          f"только пассив {len(silent)}")


async def run(only_id: int | None = None) -> None:
    database.init_db()
    # само-лечение прокси: проверяем прокси прогреваемых и битые заменяем на живые
    # бесплатные из пула — чтобы прогрев не коннектился через мёртвый/мусорный IP
    # (иначе застревает на ступени). Сбой лечения не должен ронять прогрев.
    try:
        from channels import proxy_pool
        ids = [only_id] if only_id is not None else None
        await proxy_pool.heal(ids=ids, warming_only=True)
    except Exception as e:  # noqa: BLE001
        print(f"[warmup] авто-лечение прокси пропущено: {e}")
    with database.get_conn() as conn:
        accs = [dict(a) for a in database.warming_accounts(conn)]
        anchors = [dict(a) for a in database.warm_anchors(conn)]
        ca_mix = database.get_setting(conn, "warm_ca_mix", "off") == "on"
        knock = database.get_setting(conn, "warm_knock", "off") == "on"
    if only_id is not None:
        accs = [a for a in accs if a["id"] == only_id]
        if not accs:
            print(f"аккаунт #{only_id} не годится для прогрева: нужен статус 'прогрев', "
                  f"авторизованная сессия (TG ✓) И назначенный НЕ мёртвый прокси (проверь в колонке "
                  f"«Прокси» — без прокси не греем, чтобы не светить Telegram общим IP пачки аккаунтов).")
            return
    if not accs:
        print("нет готовых к прогреву аккаунтов: нужен статус 'прогрев' + сессия + живой прокси "
              "у каждого. Разда́й прокси (кнопка «🆓 Бесплатный прокси» или «🌐 Раздать прокси»), "
              "потом запускай прогрев.")
        return
    print(f"прогреваю {len(accs)} аккаунт(ов); якорей-получателей: {len(anchors)}; "
          f"ЦА-микс: {'вкл' if ca_mix else 'выкл'}; тихий стук: {'вкл' if knock else 'выкл'}")
    for acc in accs:
        try:
            await _warm_one(acc, anchors, accs, ca_mix=ca_mix, knock=knock)
        except Exception as e:  # noqa: BLE001
            from channels.antiban import classify_error
            cat = classify_error(e)
            if cat == "ban":
                print(f"[#{acc['id']}] ⛔ забанен/деактивирован во время прогрева ({e}) — статус banned")
                with database.get_conn() as conn:
                    conn.execute("UPDATE accounts SET status='banned' WHERE id=?", (acc["id"],))
                    database.add_event(conn, "account_banned",
                                       f"⛔ Аккаунт «{acc.get('label') or acc['id']}» забанен при прогреве",
                                       f"Telegram: {e}", level="bad", account_id=acc["id"])
            elif cat == "session_revoked":
                # номер жив, отозвана только сессия — не banned, просто пометить и перелогинить
                print(f"[#{acc['id']}] 🔴 сессия отозвана при прогреве ({e}) — нужен перелогин")
                with database.get_conn() as conn:
                    conn.execute("UPDATE accounts SET session_alive=0, session_state='revoked', "
                                "session_reason=? WHERE id=?", (str(e)[:200], acc["id"]))
            else:
                print(f"[fail #{acc['id']}] {e}")
        await asyncio.sleep(random.uniform(8, 20))
    print("прогрев за этот заход завершён")


def main() -> None:
    p = argparse.ArgumentParser(description="Прогрев Telegram-аккаунтов AXIOM")
    p.add_argument("--ping", help="тест: номера/юзернеймы через запятую, кому слать с основного аккаунта")
    p.add_argument("--n", type=int, default=3, help="сколько сообщений на цель в режиме --ping")
    p.add_argument("--run", action="store_true", help="полный прогрев аккаунтов в статусе 'warming'")
    p.add_argument("--id", type=int, help="прогреть только один аккаунт по id (для теста из пульта)")
    p.add_argument("--upkeep", action="store_true",
                   help="поддерживающий прогрев БОЕВЫХ (active) номеров: пары, диалоги, живость")
    args = p.parse_args()
    if args.ping:
        targets = [t.strip() for t in args.ping.split(",") if t.strip()]
        asyncio.run(ping(targets, args.n))
    elif args.upkeep:
        asyncio.run(run_upkeep(only_id=args.id))
    elif args.run or args.id:
        asyncio.run(run(only_id=args.id))
    else:
        p.print_help()


if __name__ == "__main__":
    main()
