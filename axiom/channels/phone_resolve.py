"""Пробив телефонов в Telegram: номер → личность (tg_user_id, @username, имя, фото, bio).

ЗАЧЕМ. В книжке лежат сотни номеров (из парсинга сайтов/2ГИС), но без tg_user_id по ним
нельзя ни написать, ни собрать досье. Пробив был только внутри отправки первого сообщения
(channels/telegram._resolve_entity) — по одному, «в момент выстрела». Здесь он вынесен в
отдельный безопасный массовый шаг: сначала узнаём, КТО эти люди, потом решаем, кому писать.

⚠️ ГЛАВНОЕ ПРО БАН. ImportContacts — самый заметный для Telegram спам-сигнал: аккаунт,
который подряд добавляет сотни чужих номеров, ведёт себя ровно как спамер. Поэтому:
  • работаем ЖИВЫМИ РАБОЧИМИ аккаунтами, родной номер (protected) не трогаем;
  • дневной потолок на аккаунт (--per, по умолчанию 25) — размазываем нагрузку;
  • КОНТАКТ УДАЛЯЕМ СРАЗУ после пробива (DeleteContacts) — иначе адресная книга
    аккаунта пухнет чужими номерами, и это видно Telegram даже без рассылки;
  • маленькие пачки + человеческие паузы;
  • FloodWait → аккаунт уходит с дистанции, номера достанутся другому.

Что пишем в карточку: tg_user_id, username, имя (если пусто), is_premium, аватар, bio,
has_tg=yes|no, tg_checked_at/tg_checked_by. Номер, которого нет в TG, помечаем has_tg='no'
и больше не трогаем.

Досье из этого НЕ рождается: портрет строится на текстах человека (tg_user_posts,
channels/tg_parser --harvest). Пробив даёт личность, тексты — душу.

Запуск:
    python -m channels.phone_resolve --limit 20          # проба
    python -m channels.phone_resolve --per 25            # всех, по 25 на аккаунт
    python -m channels.phone_resolve --recheck           # и тех, кого не нашли раньше
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time

from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
from telethon.tl.types import InputPhoneContact

from channels.telegram import client_for_account
from channels.tg_parser import _display_name, _download_avatar, _fetch_bio, _is_lead_user
from db import database

BATCH = 5              # номеров в одном ImportContacts — мелкими пачками, как человек
PAUSE = (8.0, 18.0)    # пауза между пачками у одного аккаунта, сек
PER_ACCOUNT = 25       # дневной потолок номеров на аккаунт (антибан)
FLOOD_GIVE_UP = 300    # FloodWait дольше — аккаунт уходит с дистанции


def _norm(phone: str | None) -> str | None:
    """Нормализуем номер к виду +7XXXXXXXXXX — Telegram капризен к формату."""
    if not phone:
        return None
    d = "".join(ch for ch in str(phone) if ch.isdigit())
    if not d:
        return None
    if len(d) == 11 and d[0] == "8":       # 8XXX… → 7XXX…
        d = "7" + d[1:]
    if len(d) == 10:                        # без кода страны — считаем РФ
        d = "7" + d
    return "+" + d


def _targets(limit: int | None, recheck: bool) -> list[dict]:
    """Кого пробиваем: есть телефон, нет tg_user_id, ещё НЕ пробивали.

    Признак «пробивали» — tg_checked_at, а НЕ has_tg. Это разные вещи, и их легко
    перепутать: has_tg='yes' у 120 контактов проставил importer/import_2gis по наличию
    ссылки t.me в карточке 2ГИС — то есть это ДОГАДКА по ссылке, без tg_user_id и без
    единого запроса в Telegram. Фильтруй по has_tg — и как раз самые перспективные
    («у них точно есть TG») никогда не пробьются.
    """
    where = ["phone IS NOT NULL", "phone<>''", "tg_user_id IS NULL"]
    if not recheck:
        where.append("tg_checked_at IS NULL")
    sql = f"SELECT id, name, phone FROM contacts WHERE {' AND '.join(where)} ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with database.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql)]


def _workers() -> list[int]:
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM accounts WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND COALESCE(protected,0)=0 AND COALESCE(session_alive,1)=1 "
            "AND COALESCE(status,'')<>'banned' ORDER BY id"
        ).fetchall()
    return [r["id"] for r in rows]


def _save_found(c: dict, u, bio: str | None, has_photo: bool, acc_id: int) -> None:
    with database.get_conn() as conn:
        conn.execute(
            # name/username через COALESCE-логику: то, что ввёл человек, важнее того,
            # что отдал Telegram — не затираем ручной ввод машинным.
            "UPDATE contacts SET tg_user_id=?, username=COALESCE(username,?), "
            "name=COALESCE(NULLIF(name,''),?), is_premium=?, has_photo=?, "
            "bio=COALESCE(NULLIF(bio,''),?), has_tg='yes', "
            "tg_checked_at=datetime('now'), tg_checked_by=? WHERE id=?",
            (u.id, u.username, _display_name(u), 1 if getattr(u, "premium", False) else 0,
             1 if has_photo else 0, bio, acc_id, c["id"]),
        )


def _save_absent(cid: int, acc_id: int) -> None:
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE contacts SET has_tg='no', tg_checked_at=datetime('now'), tg_checked_by=? "
            "WHERE id=?", (acc_id, cid),
        )


async def _worker(acc_id: int, queue: asyncio.Queue, tally: dict, state: dict, per: int) -> None:
    try:
        client, _ = client_for_account(acc_id)
    except Exception as e:  # noqa: BLE001
        state["dead_workers"].append(f"#{acc_id}: {type(e).__name__}")
        return
    who = f"acc#{acc_id}"
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await client.is_user_authorized():
            state["dead_workers"].append(f"#{acc_id}: сессия не авторизована")
            return
    except Exception as e:  # noqa: BLE001
        # Отличаем «сдохла сессия» от «сдох прокси»: первое — приговор аккаунту, и его
        # надо записать, иначе аккаунт останется session_alive=1 и будет падать каждый раз.
        from channels.session_check import note_failure
        v = note_failure(acc_id, e)
        why = {"revoked": "сессия отозвана", "banned": "БАН номера"}.get(v or "", "нет связи")
        print(f"[{who}] {why} ({type(e).__name__}) — воркер выключен")
        state["dead_workers"].append(f"#{acc_id}: {why} ({type(e).__name__})")
        return
    state["live_workers"].append(acc_id)
    done_by_me = 0
    try:
        while done_by_me < per:
            batch: list[dict] = []
            while len(batch) < BATCH and done_by_me + len(batch) < per:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not batch:
                return
            inputs, by_client_id = [], {}
            for i, c in enumerate(batch):
                ph = _norm(c["phone"])
                if not ph:
                    tally["bad_phone"] += 1
                    state["done"] += 1
                    continue
                inputs.append(InputPhoneContact(client_id=i, phone=ph,
                                                first_name=(c["name"] or "lead")[:60], last_name=""))
                by_client_id[i] = c
            if not inputs:
                continue
            try:
                res = await client(ImportContactsRequest(inputs))
            except FloodWaitError as e:
                for c in by_client_id.values():      # вернём чужому аккаунту
                    await queue.put(c)
                print(f"[{who}] FloodWait {e.seconds}с — ухожу с дистанции")
                state["dead_workers"].append(f"#{acc_id}: FloodWait {e.seconds}с")
                return
            except Exception as e:  # noqa: BLE001
                for c in by_client_id.values():
                    await queue.put(c)
                print(f"[{who}] сбой ImportContacts: {type(e).__name__}: {e}")
                return

            found_users = {}
            for imp in (res.imported or []):
                found_users[imp.client_id] = imp.user_id
            users_by_id = {u.id: u for u in (res.users or [])}

            # СРАЗУ чистим адресную книгу: аккаунт не должен копить чужие номера.
            if res.users:
                try:
                    await client(DeleteContactsRequest(id=[u.id for u in res.users]))
                except Exception as e:  # noqa: BLE001
                    print(f"[{who}] не удалось убрать контакты: {type(e).__name__}")

            for cid_key, c in by_client_id.items():
                uid = found_users.get(cid_key)
                u = users_by_id.get(uid) if uid else None
                if u is None or not _is_lead_user(u):
                    _save_absent(c["id"], acc_id)
                    tally["absent"] += 1
                else:
                    bio = None
                    has_photo = False
                    try:
                        has_photo = bool(await _download_avatar(client, u))
                        bio = await _fetch_bio(client, u)
                    except Exception:  # noqa: BLE001
                        pass
                    _save_found(c, u, bio, has_photo, acc_id)
                    tally["found"] += 1
                    print(f"[{who}] {c['phone']} → {_display_name(u)}"
                          f"{' @' + u.username if u.username else ''}")
                state["done"] += 1
                done_by_me += 1
            _save_progress(state, tally)
            await asyncio.sleep(random.uniform(*PAUSE))
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _save_progress(state: dict, tally: dict) -> None:
    with database.get_conn() as conn:
        database.set_setting(conn, "phoneresolve_progress", json.dumps(
            {"done": state["done"], "total": state["total"], "running": True, **tally},
            ensure_ascii=False))


async def run(limit: int | None, per: int, recheck: bool) -> None:
    database.init_db()
    people = _targets(limit, recheck)
    if not people:
        print(json.dumps({"ok": False, "error": "нет номеров для пробива"}, ensure_ascii=False))
        return
    accs = _workers()
    if not accs:
        print(json.dumps({"ok": False, "error": "нет живых рабочих аккаунтов — пробивать нечем. "
                                                "Родной номер под это не подставляем."},
                         ensure_ascii=False))
        return
    queue: asyncio.Queue = asyncio.Queue()
    for p in people:
        queue.put_nowait(p)
    tally = {"found": 0, "absent": 0, "bad_phone": 0}
    state = {"done": 0, "total": len(people), "live_workers": [], "dead_workers": []}
    cap = len(accs) * per
    t0 = time.time()
    print(f"пробиваю {len(people)} номеров силами {len(accs)} аккаунт(ов), "
          f"потолок {per}/аккаунт → максимум {cap} за заход")
    await asyncio.gather(*[_worker(a, queue, tally, state, per) for a in accs])
    left = queue.qsize()
    with database.get_conn() as conn:
        database.set_setting(conn, "phoneresolve_progress", json.dumps(
            {"done": state["done"], "total": len(people), "running": False, **tally},
            ensure_ascii=False))
    out = {"ok": True, "checked": state["done"], "total": len(people), **tally,
           "left": left, "workers_live": len(state["live_workers"]),
           "minutes": round((time.time() - t0) / 60, 1)}
    if state["dead_workers"]:
        out["dead_workers"] = state["dead_workers"]
    if left:
        out["note"] = f"осталось {left} — упёрлись в дневной потолок, запусти завтра"
    print(json.dumps(out, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: пробив телефонов в Telegram (личность)")
    p.add_argument("--limit", type=int, default=None, help="сколько номеров всего за заход")
    p.add_argument("--per", type=int, default=PER_ACCOUNT, help="потолок номеров на аккаунт")
    p.add_argument("--recheck", action="store_true", help="и те, кого раньше не нашли")
    args = p.parse_args()
    asyncio.run(run(args.limit, args.per, args.recheck))


if __name__ == "__main__":
    main()
