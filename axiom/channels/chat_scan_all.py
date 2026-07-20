"""Массовый скан каталога чатов — РАБОЧИМИ аккаунтами, а не главным.

Зачем: авто-поиск (chat_discover / bio_links / chat_similar) набрасывает чаты сотнями,
но заполнить их было нечем — только кнопка «📊 Анализ» по одному чату в карточке.
В итоге каталог рос как свалка ссылок: тысячи строк со статусом 'new', где ни участников,
ни активности, ни админов, ни «могу ли писать». Этот модуль закрывает разрыв.

Почему не главным аккаунтом: chat_scan по умолчанию ходит через _build_client() —
личный номер из .env. Тысяча resolve-запросов подряд с него = FloodWait (а то и хуже)
именно на том аккаунте, который жальче всего. Здесь работу раскидываем по ЖИВЫМ рабочим
аккаунтам (session_alive=1, см. channels/session_check.py), родные (protected) не трогаем.

Что делает с результатом:
  • успех                 → полный набор полей + status='analyzed' (пишет chat_scan.scan_one);
  • username не существует → verdict='мёртвый' + scan_error — чтобы мусор больше не
                             мозолил глаза и не перепроверялся каждый заход;
  • прочий сбой           → только scan_error, вердикт не ставим (не судим сгоряча:
                             это могла быть сеть/прокси, а чат живой).

Мягкая остановка: settings['chatscan_stop']='1' — воркеры дочитывают текущий чат и выходят.
Прогресс: settings['chatscan_progress'] (JSON) — его показывает пульт.

Запуск:
    python -m channels.chat_scan_all                  # все непросканированные
    python -m channels.chat_scan_all --limit 50       # первые 50
    python -m channels.chat_scan_all --favorites      # только ⭐
    python -m channels.chat_scan_all --rescan         # включая уже сканированные (обновить)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time

from telethon.errors import FloodWaitError

from channels.chat_scan import scan_one
from channels.telegram import client_for_account
from db import database

# Пауза между чатами у ОДНОГО воркера (сек). resolve username — дорогая для TG операция,
# частим — ловим FloodWait. Джиттер, чтобы воркеры не били в такт.
_PAUSE = (4.0, 8.0)
# FloodWait дольше этого — воркер уходит на покой (иначе процесс висит часами).
_FLOOD_GIVE_UP = 600
# Ошибки, означающие «такого username в Telegram просто нет» → чат мёртв.
_DEAD_ERRORS = ("UsernameInvalidError", "UsernameNotOccupiedError")


def _is_dead(exc: Exception) -> bool:
    if type(exc).__name__ in _DEAD_ERRORS:
        return True
    # Telethon на несуществующий username кидает голый ValueError с таким текстом.
    return isinstance(exc, ValueError) and "no user has" in str(exc).lower()


def _targets(limit: int | None, favorites: bool, rescan: bool,
             chat_ids: list[int] | None = None) -> list[dict]:
    """Что сканируем. По умолчанию — то, что ещё ни разу не сканировали."""
    where = ["(username IS NOT NULL AND username<>'' OR link IS NOT NULL AND link<>'')"]
    params: list = []
    if chat_ids:
        where.append(f"id IN ({','.join('?' * len(chat_ids))})")
        params += list(chat_ids)
    elif not rescan:
        where.append("last_scanned_at IS NULL")
    # мёртвые не трогаем: их уже проверили и они не воскресают
    where.append("(verdict IS NULL OR verdict<>'мёртвый')")
    if favorites:
        where.append("COALESCE(favorite,0)=1")
    sql = ("SELECT id, title, username, link FROM chats WHERE " + " AND ".join(where) +
           " ORDER BY COALESCE(favorite,0) DESC, COALESCE(members_count,0) DESC, id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with database.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def _members_map(live: list[int | None]) -> dict[int, int]:
    """chat_id → id ЖИВОГО аккаунта, который в этом чате состоит.

    Зачем: «могу писать» — величина ПЕРСОНАЛЬНАЯ, а chats.can_write одно на чат. Если
    чат просканирует аккаунт, которого там нет, он честно напишет «не вступил» — и
    каталог соврёт про чат, где у нас сидят бойцы. Поэтому такие чаты отдаём тому, кто
    внутри: его взгляд и есть полезная правда («могу ли я оттуда слать»).
    """
    ids = [a for a in live if a is not None]
    if not ids:
        return {}
    qm = ",".join("?" * len(ids))
    with database.get_conn() as conn:
        rows = conn.execute(
            f"SELECT chat_id, account_id FROM account_chats WHERE account_id IN ({qm})", ids
        ).fetchall()
    out: dict[int, int] = {}
    for r in rows:                      # первый попавшийся участник — годится
        out.setdefault(r["chat_id"], r["account_id"])
    return out


def _workers() -> list[int | None]:
    """Живые рабочие аккаунты. Пусто → [None] = главный (лучше медленно, чем никак)."""
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM accounts WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND COALESCE(protected,0)=0 AND session_alive=1 "
            "AND COALESCE(status,'')<>'banned' ORDER BY id"
        ).fetchall()
    ids = [r["id"] for r in rows]
    if not ids:
        print("[!] живых рабочих аккаунтов нет — иду главным (медленно, берегу его паузами)")
        return [None]
    return ids


def _stop_requested() -> bool:
    with database.get_conn() as conn:
        return database.get_setting(conn, "chatscan_stop", "0") == "1"


def _save_progress(done: int, total: int, tally: dict, running: bool = True) -> None:
    with database.get_conn() as conn:
        database.set_setting(conn, "chatscan_progress", json.dumps(
            {"done": done, "total": total, "running": running, **tally}, ensure_ascii=False))


def _mark_dead(chat_id: int, reason: str) -> None:
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE chats SET verdict='мёртвый', verdict_src='скан', verdict_at=datetime('now'), "
            "scan_error=?, status='skip', last_scanned_at=datetime('now') WHERE id=?",
            (reason[:200], chat_id),
        )


def _mark_error(chat_id: int, reason: str) -> None:
    """Сбой, но не приговор: вердикт не ставим — причина могла быть в сети/прокси."""
    with database.get_conn() as conn:
        conn.execute("UPDATE chats SET scan_error=? WHERE id=?", (reason[:200], chat_id))


async def _worker(acc_id: int | None, queue: asyncio.Queue, tally: dict, state: dict,
                  mine: list[dict] | None = None) -> None:
    try:
        client, _ = client_for_account(acc_id)
    except Exception as e:  # noqa: BLE001
        print(f"[acc {acc_id}] не поднять клиент: {e}")
        return
    who = f"acc#{acc_id}" if acc_id else "главный"
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await client.is_user_authorized():
            print(f"[{who}] сессия не авторизована — воркер выключен")
            state["dead_workers"].append(f"#{acc_id}: сессия не авторизована")
            return
    except Exception as e:  # noqa: BLE001
        # Обычно это мёртвый прокси аккаунта. Напрямую НЕ идём: тащить десяток рабочих
        # аккаунтов с домашнего IP — верный способ спалить всю пачку. Лучше пропустить
        # и сказать хозяину, что этим аккаунтам нужен живой прокси.
        # Но если ошибка ПРО СЕССИЮ (отозвана/бан) — это приговор, фиксируем сразу.
        from channels.session_check import note_failure
        v = note_failure(acc_id, e)
        why = {"revoked": "сессия отозвана", "banned": "БАН номера"}.get(
            v or "", "нет связи (скорее всего мёртвый прокси)")
        print(f"[{who}] {why}: {type(e).__name__} — воркер выключен")
        state["dead_workers"].append(f"#{acc_id}: {why} ({type(e).__name__})")
        return
    state["live_workers"].append(acc_id)
    todo = list(mine or [])          # сначала «мои» чаты (я в них состою), потом общие
    try:
        while todo or not queue.empty():
            if _stop_requested():
                print(f"[{who}] получен стоп — выхожу")
                return
            from_mine = bool(todo)
            if todo:
                chat = todo.pop()
            else:
                try:
                    chat = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
            target = chat["username"] or chat["link"]
            try:
                res = await scan_one(client, target, chat["id"])
                tally["ok"] += 1
                if res.get("ai_error"):
                    tally["ai_fail"] += 1
                    state["ai_error"] = res["ai_error"]
                print(f"[{who}] #{chat['id']} {str(chat['title'])[:28]}: "
                      f"{res.get('members')} чел, писать={res.get('can_write')}")
            except FloodWaitError as e:
                # TG попросил подождать. Коротко — ждём и берём чат снова сами; долго —
                # уходим, отдав чат общей очереди (его подхватит другой аккаунт).
                if e.seconds > _FLOOD_GIVE_UP:
                    await queue.put(chat)
                    print(f"[{who}] FloodWait {e.seconds}с — слишком долго, воркер уходит")
                    return
                # «Свой» чат возвращаем СЕБЕ: только участник видит верное «могу писать».
                if from_mine:
                    todo.append(chat)
                else:
                    await queue.put(chat)
                print(f"[{who}] FloodWait {e.seconds}с — жду")
                await asyncio.sleep(e.seconds + 1)
                continue
            except Exception as e:  # noqa: BLE001
                if _is_dead(e):
                    tally["dead"] += 1
                    _mark_dead(chat["id"], f"{type(e).__name__}: {e}")
                    print(f"[{who}] #{chat['id']} {target}: 💀 нет такого чата")
                else:
                    tally["err"] += 1
                    _mark_error(chat["id"], f"{type(e).__name__}: {e}")
                    print(f"[{who}] #{chat['id']} {target}: сбой {type(e).__name__}: {e}")
            finally:
                state["done"] += 1
                if state["done"] % 5 == 0 or queue.empty():
                    _save_progress(state["done"], state["total"], tally)
            await asyncio.sleep(random.uniform(*_PAUSE))
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def run(limit: int | None, favorites: bool, rescan: bool,
              chat_ids: list[int] | None = None) -> None:
    database.init_db()
    with database.get_conn() as conn:
        database.set_setting(conn, "chatscan_stop", "0")
    chats = _targets(limit, favorites, rescan, chat_ids)
    if not chats:
        print(json.dumps({"ok": False, "error": "нечего сканировать — всё уже просканировано"},
                         ensure_ascii=False))
        _save_progress(0, 0, {}, running=False)
        return
    accs = _workers()
    # Чат, где у нас есть свой человек, сканируем ИМ — иначе «могу писать» посчитается
    # от лица постороннего аккаунта и покажет «не вступил» там, где мы внутри.
    members = _members_map(accs)
    mine: dict[int | None, list[dict]] = {a: [] for a in accs}
    queue: asyncio.Queue = asyncio.Queue()
    for c in chats:
        owner = members.get(c["id"])
        if owner in mine:
            mine[owner].append(c)
        else:
            queue.put_nowait(c)
    tally = {"ok": 0, "dead": 0, "err": 0, "ai_fail": 0}
    state = {"done": 0, "total": len(chats), "ai_error": None,
             "live_workers": [], "dead_workers": []}
    t0 = time.time()
    n_mine = sum(len(v) for v in mine.values())
    print(f"сканирую {len(chats)} чатов силами {len(accs)} аккаунт(ов): {accs}")
    if n_mine:
        print(f"  из них {n_mine} — своими участниками (чтобы «могу писать» было честным)")
    _save_progress(0, len(chats), tally)
    await asyncio.gather(*[_worker(a, queue, tally, state, mine.get(a)) for a in accs])
    tally["workers_live"] = len(state["live_workers"])
    tally["workers_dead"] = len(state["dead_workers"])
    _save_progress(state["done"], len(chats), tally, running=False)
    out = {"ok": True, "checked": state["done"], "total": len(chats), **tally,
           "minutes": round((time.time() - t0) / 60, 1)}
    if state["dead_workers"]:
        # Не молчим про выбывших: иначе кажется, что «просто медленно», а на деле
        # половина бригады не вышла на работу из-за мёртвых прокси.
        out["dead_workers"] = state["dead_workers"]
    if state["ai_error"]:
        out["ai_error"] = state["ai_error"]   # напр. кончился баланс/протух ключ LLM
    print(json.dumps(out, ensure_ascii=False))
    if state["dead_workers"]:
        print(f"[!] не вышли на работу {len(state['dead_workers'])} аккаунт(ов) — "
              f"скорее всего мёртвый прокси: {', '.join(state['dead_workers'][:8])}")


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: массовый скан каталога чатов")
    p.add_argument("--limit", type=int, default=None, help="сколько чатов за заход")
    p.add_argument("--favorites", action="store_true", help="только избранные (⭐)")
    p.add_argument("--rescan", action="store_true", help="включая уже просканированные")
    p.add_argument("--chats", default=None, help="только эти чаты каталога (id через запятую)")
    args = p.parse_args()
    ids = [int(x) for x in args.chats.split(",") if x.strip()] if args.chats else None
    asyncio.run(run(args.limit, args.favorites, args.rescan, ids))


if __name__ == "__main__":
    main()
