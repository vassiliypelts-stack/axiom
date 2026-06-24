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


async def _setup_profile(client, acc: dict) -> None:
    """Стартовое оформление профиля (на стадии 0): био и аватар, ТОЛЬКО если пусто
    (не перезатираем существующее). Заполненный профиль реже флагают как спам."""
    # био из описания агента
    try:
        about = (acc.get("description") or "").strip()[:70]
        if about:
            full = await client(functions.users.GetFullUserRequest("me"))
            if not getattr(full.full_user, "about", None):
                from telethon.tl.functions.account import UpdateProfileRequest
                await client(UpdateProfileRequest(about=about))
                print("  профиль: заполнил bio")
    except Exception as e:  # noqa: BLE001
        print(f"  [bio] {e}")
    # аватар из загруженного в карточке агента файла
    try:
        if acc.get("avatar"):
            from pathlib import Path
            p = Path(config.DB_PATH).parent / "avatars" / acc["avatar"]
            if p.exists():
                existing = await client.get_profile_photos("me", limit=1)
                if not existing:
                    from telethon.tl.functions.photos import UploadProfilePhotoRequest
                    f = await client.upload_file(str(p))
                    await client(UploadProfilePhotoRequest(file=f))
                    print("  профиль: поставил аватар")
    except Exception as e:  # noqa: BLE001
        print(f"  [avatar] {e}")


async def _view_stories(client, n: int) -> int:
    """Посмотреть и «прочитать» сторис из ленты (ещё живее). Best-effort —
    если версия Telethon без stories API, тихо пропускаем."""
    if n <= 0:
        return 0
    try:
        from telethon.tl.functions.stories import GetPeerStoriesRequest, ReadStoriesRequest
    except Exception:  # noqa: BLE001
        return 0
    done = 0
    try:
        async for d in client.iter_dialogs(limit=25):
            if done >= n:
                break
            try:
                res = await client(GetPeerStoriesRequest(peer=d.entity))
                items = getattr(getattr(res, "stories", None), "stories", None) or []
                if items:
                    await client(ReadStoriesRequest(peer=d.entity, max_id=max(s.id for s in items)))
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
    from channels.campaign_send import _add_tag, _audience, _greeting, _parts
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
    rows = _audience(camp["audience_tag"], "telegram", cap)
    sent = 0
    for row in rows:
        if sent >= cap:
            break
        name = _greeting(row)
        parts = _parts(camp["message_template"], name, row["agency"] or row["name"])
        if not parts:
            break
        try:
            ent = await _resolve_entity(client, row)
            await _send_parts(client, ent, parts)
        except Exception as e:  # noqa: BLE001
            print(f"  [ca-mix skip {row['id']}] {e}")
            continue
        with database.get_conn() as conn:
            database.set_tg_user_id(conn, row["id"], int(ent.id))
            database.add_message(conn, row["id"], "out", "\n".join(parts), intent=None)
            database.set_status(conn, row["id"], "messaged")
            conn.execute("UPDATE contacts SET tags=? WHERE id=?",
                         (_add_tag(row["tags"], f"кампания #{camp['id']}"), row["id"]))
            conn.execute("INSERT OR IGNORE INTO campaign_contacts (campaign_id, contact_id, account_id) "
                         "VALUES (?,?,?)", (camp["id"], row["id"], acc["id"]))
        sent += 1
        print(f"  [ca-mix {sent}/{cap}] -> {name or row['username']}")
        await asyncio.sleep(random.uniform(20, 60))
    return sent


async def _warm_one(acc, anchors, peers, ca_mix: bool = False) -> None:
    client = build_client(StringSession(acc["tg_session"]), acc["proxy"],
                          acc.get("api_id"), acc.get("api_hash"))
    await client.connect()
    if not await client.is_user_authorized():
        print(f"[skip #{acc['id']}] сессия не авторизована — перелогинь: python -m channels.account_login --id {acc['id']}")
        await client.disconnect()
        return
    stage = acc["warm_stage"] or 0
    plan = WARM_PLAN.get(min(stage, max(WARM_PLAN)), WARM_PLAN[max(WARM_PLAN)])
    me = await client.get_me()
    print(f"[#{acc['id']} @{me.username or me.id}] стадия {stage}: каналы {plan['channels']}, "
          f"ЛС {plan['msgs']}, лайки {plan.get('react', 0)}, чтение {plan.get('read', 0)}")

    # 1) заходим в онлайн (живой пользователь открыл приложение)
    await _go_online(client)
    await asyncio.sleep(random.uniform(2, 6))

    # на старте — оформляем профиль (bio/аватар), если пусто
    if stage == 0:
        await _setup_profile(client, acc)

    # 2) вступаем в каналы (по плану, по чуть-чуть)
    for ch in random.sample(CHANNELS, min(plan["channels"], len(CHANNELS))):
        try:
            await client(JoinChannelRequest(ch))
            print(f"  вступил в @{ch}")
        except Exception as e:  # noqa: BLE001
            print(f"  [канал @{ch}] {e}")
        await asyncio.sleep(random.uniform(5, 15))

    # 3) читаем ленту (прокрутил, прочитал последние сообщения)
    await _read_feed(client, plan.get("read", 8))

    # 4) лайкаем посты (реакции в каналах/группах)
    await _react_feed(client, plan.get("react", 0))

    # 4b) смотрим сторис из ленты (ещё живее)
    await _view_stories(client, plan.get("react", 1))

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
        left -= await _send_chatter(client, ent, 1, label=peer)

    # 6) опционально: вплести немного реальной ЦА (анти-бан, выкл по умолчанию)
    if ca_mix and stage >= 5:
        await _ca_mix(client, acc, stage)

    new_stage = stage + 1
    activate = new_stage >= READY_STAGE
    with database.get_conn() as conn:
        database.bump_warm(conn, acc["id"], new_stage, activate=activate)
        if activate:
            who = acc.get("label") or acc.get("phone") or f"#{acc['id']}"
            database.add_event(conn, "warm_ready", f"🌡 Аккаунт прогрет: {who}",
                               "переведён в «активен» — можно ставить в рассылку",
                               level="good", account_id=acc["id"])
    print(f"  стадия → {new_stage}{' · ГОТОВ (active)' if activate else ''}")
    await client.disconnect()


async def run(only_id: int | None = None) -> None:
    database.init_db()
    with database.get_conn() as conn:
        accs = [dict(a) for a in database.warming_accounts(conn)]
        anchors = [dict(a) for a in database.warm_anchors(conn)]
        ca_mix = database.get_setting(conn, "warm_ca_mix", "off") == "on"
    if only_id is not None:
        accs = [a for a in accs if a["id"] == only_id]
        if not accs:
            print(f"аккаунт #{only_id} не годится для прогрева: нужен статус 'прогрев' И "
                  f"авторизованная сессия (TG ✓). Подключи его и поставь статус 'прогрев'.")
            return
    if not accs:
        print("нет аккаунтов в прогреве с сессией. Сначала: python -m channels.account_login --id N "
              "(и статус 'warming' в «Мои агенты»)")
        return
    print(f"прогреваю {len(accs)} аккаунт(ов); якорей-получателей: {len(anchors)}; ЦА-микс: {'вкл' if ca_mix else 'выкл'}")
    for acc in accs:
        try:
            await _warm_one(acc, anchors, accs, ca_mix=ca_mix)
        except Exception as e:  # noqa: BLE001
            print(f"[fail #{acc['id']}] {e}")
        await asyncio.sleep(random.uniform(8, 20))
    print("прогрев за этот заход завершён")


def main() -> None:
    p = argparse.ArgumentParser(description="Прогрев Telegram-аккаунтов AXIOM")
    p.add_argument("--ping", help="тест: номера/юзернеймы через запятую, кому слать с основного аккаунта")
    p.add_argument("--n", type=int, default=3, help="сколько сообщений на цель в режиме --ping")
    p.add_argument("--run", action="store_true", help="полный прогрев аккаунтов в статусе 'warming'")
    p.add_argument("--id", type=int, help="прогреть только один аккаунт по id (для теста из пульта)")
    args = p.parse_args()
    if args.ping:
        targets = [t.strip() for t in args.ping.split(",") if t.strip()]
        asyncio.run(ping(targets, args.n))
    elif args.run or args.id:
        asyncio.run(run(only_id=args.id))
    else:
        p.print_help()


if __name__ == "__main__":
    main()
