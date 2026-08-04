"""Бэкфилл старых записей: tg_chat_id у чатов и фото у лидов.

Зачем. Поля появились позже данных, поэтому у записей, заведённых до них, они пустые:
  • `chats.tg_chat_id` — без него ломается связка «в каком чате найден человек»
    (досье джойнит `tg_user_posts.chat_id` → `chats.tg_chat_id`), пропадают ссылки на чат;
  • `contacts.has_photo` + `data/avatars/{tg_user_id}.jpg` — раньше аватар качался только
    для активных авторов при --harvest, у остальных лидов фото нет.

Модуль идемпотентный: гоняй сколько угодно, трогает только пустое.

    python -m channels.backfill --chats            # дозаполнить tg_chat_id (по @username)
    python -m channels.backfill --photos           # докачать аватары лидов
    python -m channels.backfill --all --limit 300  # и то, и другое
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random

from telethon.errors import FloodWaitError

from telethon.sessions import StringSession

import config
from channels.telegram import _build_client, build_client
from db import database

RESOLVE_PAUSE = (0.6, 1.4)   # антибан: дозируем резолвы сущностей
PHOTO_PAUSE = (0.5, 1.2)     # антибан: дозируем скачивания фото
# FloodWait дольше — не ждём, а передаём работу следующему аккаунту пула.
# Ловили вживую 16.07: после ~280 резолвов подряд Telegram выдал FloodWait 82169с (22.8ч),
# и прогон послушно уснул на сутки. Суточный лимит резолвов — на АККАУНТ, а не на задачу:
# упёрся один — работать может следующий (см. ResolverPool).
MAX_FLOOD_WAIT = 600
# Сколько резолвов даём одному аккаунту за прогон. Ограничение не техническое, а
# антибанное: 1.5 тыс. запросов подряд с одного номера — ровно тот профиль нагрузки,
# на котором и словили сутки блокировки. Порция * размер пула ≈ вся очередь за прогон.
PER_ACCOUNT_RESOLVES = 150
# Ступень прогрева, ниже которой аккаунт к массовым резолвам не допускаем: у «вчера
# зарегистрированного» номера тысяча запросов к API — самый быстрый путь в бан.
MIN_WARM_STAGE = 3


def _mark_photos_from_disk() -> int:
    """Фото уже на диске → проставить has_photo. Без сети, бесплатно.
    Файлы лидов называются `{tg_user_id}.jpg`; аватары агентов лежат тут же
    (`a1.png`, `gen_*.jpg`), поэтому берём ТОЛЬКО числовые имена."""
    from channels.tg_parser import AVATAR_DIR
    if not AVATAR_DIR.exists():
        return 0
    ids = [int(p.stem) for p in AVATAR_DIR.glob("*.jpg") if p.stem.isdigit() and p.stat().st_size > 0]
    if not ids:
        return 0
    with database.get_conn() as conn:
        database.mark_photos_by_tg(conn, set(ids))
        return conn.execute(
            f"SELECT COUNT(*) FROM contacts WHERE COALESCE(has_photo,0)=1 "
            f"AND tg_user_id IN ({','.join('?' * len(ids))})", ids
        ).fetchone()[0]


# Кого вообще можно дозаполнить. Одно условие на выборку кандидатов И на счётчик
# «осталось»: когда они разъезжались, «осталось» считало в том числе skip/banned
# (включая помеченные дублями ниже) — цифра залипала, и оператор жал «Дозаполнить» впустую.
_RESOLVABLE = ("tg_chat_id IS NULL AND username IS NOT NULL AND username<>'' "
               "AND COALESCE(status,'') NOT IN ('skip','banned')")


class FloodStop(Exception):
    """Telegram сказал ждать слишком долго — этот аккаунт своё отработал.
    Не приговор прогону: работу подхватит следующий аккаунт пула."""

    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"FloodWait {seconds}с")


def _resolver_accounts() -> list[dict]:
    """Рабочие аккаунты, которыми можно молотить резолвы.

    «Родные» (protected) исключены жёстко — это личные номера владельца, автоматика их
    не трогает нигде в проекте, и массовый резолв не исключение (именно на личном и
    словили сутки FloodWait). Нужны живая сессия, не бан и прогрев от MIN_WARM_STAGE.
    Назначенный в пульте «слушает чаты» идёт первым: оператор уже выбрал, какой номер
    не жалко расходовать на такую работу."""
    with database.get_conn() as conn:
        listen_id = database.get_setting(conn, "listen_account_id")
        rows = conn.execute(
            "SELECT id, label, username, phone, tg_session, proxy, api_id, api_hash "
            "FROM accounts "
            "WHERE COALESCE(protected,0)=0 AND COALESCE(status,'') <> 'banned' "
            "AND tg_session IS NOT NULL AND tg_session <> '' "
            "AND COALESCE(session_alive,1) <> 0 "
            "AND (COALESCE(status,'')='active' OR COALESCE(warm_stage,0) >= ?) "
            "ORDER BY id",
            (MIN_WARM_STAGE,),
        ).fetchall()
    accs = []
    for r in rows:
        a = dict(r)
        a["label"] = a["label"] or a["username"] or a["phone"] or f"#{a['id']}"
        accs.append(a)
    if listen_id:
        accs.sort(key=lambda a: 0 if str(a["id"]) == str(listen_id) else 1)
    return accs


class ResolverPool:
    """Очередь аккаунтов для массовых обращений к Telegram (резолв @username, аватары).

    ЗАЧЕМ. Суточный лимит таких запросов Telegram считает НА АККАУНТ. Пока вся работа шла
    одним клиентом (и по умолчанию — личным номером из .env), 1.5 тыс. чатов упирались в
    стену: ~280 резолвов, FloodWait 22.8ч, прогон стоит до завтра. Пул раздаёт работу
    порциями: аккаунт отработал свою порцию (или получил FloodWait) — отходит, дальше
    молотит следующий. Десять рабочих номеров по 150 запросов закрывают очередь за прогон.

    Пул сам НЕ решает, что делать с ошибками резолва — только выдаёт клиента с остатком
    квоты и принимает сигнал «этот всё»."""

    def __init__(self, per_account: int = PER_ACCOUNT_RESOLVES):
        self._accounts = _resolver_accounts()
        self._per = per_account
        self._idx = -1
        self._client = None
        self._acc: dict | None = None
        self._used = 0
        self.burned: list[str] = []     # кто упёрся в FloodWait
        self.worked: list[str] = []     # кто реально поработал (для отчёта оператору)
        self.fallback = False           # пришлось взять .env-аккаунт (пул пуст)

    def available(self) -> int:
        return len(self._accounts)

    async def client(self):
        """Клиент с непотраченной квотой. None — рабочие аккаунты кончились."""
        if self._client is not None and self._used < self._per:
            return self._client
        await self._rotate()
        return self._client

    async def _rotate(self) -> None:
        await self._drop_current()
        while self._idx + 1 < len(self._accounts):
            self._idx += 1
            acc = self._accounts[self._idx]
            try:
                cl = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                                  acc.get("api_id"), acc.get("api_hash"))
                await cl.connect()
                if not await cl.is_user_authorized():
                    await cl.disconnect()
                    print(f"[пул] {acc['label']}: сессия не авторизована — пропускаю")
                    continue
            except Exception as e:  # noqa: BLE001
                print(f"[пул] {acc['label']}: не подключился — {str(e)[:70]}")
                continue
            self._client, self._acc, self._used = cl, acc, 0
            self.worked.append(acc["label"])
            print(f"[пул] работает {acc['label']} (порция до {self._per})")
            return
        self._client, self._acc = None, None

    async def _drop_current(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 — отключение не должно ронять прогон
                pass
        self._client, self._acc = None, None

    def spend(self) -> None:
        self._used += 1

    async def flood(self, seconds: int) -> None:
        """Текущий аккаунт поймал длинный FloodWait — снимаем с работы досрочно."""
        if self._acc:
            self.burned.append(f"{self._acc['label']} ({seconds // 3600}ч)")
            print(f"[пул] {self._acc['label']}: FloodWait {seconds}с "
                  f"({seconds // 3600}ч) — передаю работу следующему")
        self._used = self._per      # следующий client() возьмёт другой аккаунт

    async def start_fallback(self) -> None:
        """Пул пуст (нет ни одного подходящего рабочего аккаунта) — работаем .env-номером.
        Раньше это было поведением по умолчанию и молча жгло личный аккаунт; теперь это
        аварийный путь с явным предупреждением."""
        print("[пул] ⚠ нет рабочих аккаунтов для резолва (нужны живая сессия, не «родной», "
              f"прогрев ≥{MIN_WARM_STAGE}) — работаю ЛИЧНЫМ аккаунтом из .env. "
              "Назначь рабочие номера, чтобы не рисковать личным.")
        self._client = _build_client()
        await self._client.start()
        self._acc = {"label": "личный (.env)"}
        self._used = 0
        self.fallback = True
        self.worked.append("личный (.env)")

    async def close(self) -> None:
        await self._drop_current()

    def report(self) -> dict:
        out: dict = {"accounts_used": self.worked}
        if self.burned:
            out["flood_limited"] = self.burned
        if self.fallback:
            out["fallback_env_account"] = True
        return out


async def _resolve(client, username: str):
    """Сущность по @username. Короткий FloodWait — ждём и повторяем ЭТУ ЖЕ запись (ради неё
    и ждали). Длинный — FloodStop наверх: аккаунт упёрся в лимит, дальше идти бессмысленно.
    None = чат не резолвится (удалён/переименован)."""
    for attempt in (1, 2):
        try:
            return await client.get_entity(username)
        except FloodWaitError as ex:
            if ex.seconds > MAX_FLOOD_WAIT:
                raise FloodStop(ex.seconds) from ex
            if attempt == 2:
                return None
            print(f"[floodwait] жду {ex.seconds}с")
            await asyncio.sleep(ex.seconds + 5)
        except Exception as ex:  # noqa: BLE001
            print(f"[skip] @{username}: {str(ex)[:70]}")
            return None
    return None


async def _backfill_chats(pool: ResolverPool, limit: int) -> dict:
    """chats без tg_chat_id, но с @username → резолвим сущность → дозаполняем id.

    Резолвим не одним клиентом, а пулом рабочих аккаунтов: упёрся один в суточный лимит —
    ту же запись доделывает следующий, прогон не встаёт до завтра."""
    conn = database.get_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT id, title, username FROM chats WHERE {_RESOLVABLE} "
            f"ORDER BY COALESCE(favorite,0) DESC, COALESCE(members_count,0) DESC, id LIMIT ?",
            (limit,)
        ).fetchall()]
        filled = failed = 0
        exhausted = False   # рабочие аккаунты кончились, очередь не дочищена
        i = 0
        while i < len(rows):
            ch = rows[i]
            client = await pool.client()
            if client is None:
                exhausted = True
                print(f"[стоп] рабочие аккаунты кончились: дозаполнено {filled}, "
                      f"осталось {len(rows) - i} из этой порции — продолжи позже.")
                break
            try:
                e = await _resolve(client, ch["username"])
            except FloodStop as fs:
                # ЭТОТ аккаунт упёрся в суточный лимит — не теряем запись,
                # а отдаём её следующему (i намеренно не двигаем)
                await pool.flood(fs.seconds)
                continue
            pool.spend()
            tg_id = getattr(e, "id", None) if e else None
            if not tg_id:
                failed += 1
            else:
                members = getattr(e, "participants_count", None)
                # `with conn` коммитит, НЕ закрывая: коммит на каждой записи тут
                # принципиален — проход идёт десятки минут и может оборваться (уже ловили
                # тихую смерть фонового прогона), одна транзакция на 1800 чатов означала
                # бы потерю всей работы разом.
                with conn:
                    # такой tg_chat_id мог уже быть у другой записи — не плодим дубль
                    dup = conn.execute("SELECT id FROM chats WHERE tg_chat_id=? AND id<>?",
                                       (tg_id, ch["id"])).fetchone()
                    if dup:
                        print(f"[дубль] @{ch['username']} → чат #{dup['id']} уже с этим "
                              f"tg_chat_id, помечаю skip")
                        conn.execute("UPDATE chats SET status='skip', "
                                     "notes=COALESCE(notes,'')||' | дубль #'||? WHERE id=?",
                                     (dup["id"], ch["id"]))
                        failed += 1
                    else:
                        conn.execute("UPDATE chats SET tg_chat_id=?, "
                                     "members_count=COALESCE(members_count,?) WHERE id=?",
                                     (tg_id, members, ch["id"]))
                        filled += 1
            i += 1
            if i < len(rows):
                await asyncio.sleep(random.uniform(*RESOLVE_PAUSE))
        left = conn.execute(f"SELECT COUNT(*) FROM chats WHERE {_RESOLVABLE}").fetchone()[0]
    finally:
        conn.close()
    out = {"candidates": len(rows), "filled": filled, "failed": failed, "left": left, **pool.report()}
    if exhausted:
        out["note"] = (f"дозаполнено {filled} из {len(rows)} этой порции — рабочие аккаунты "
                       f"исчерпали дневную квоту резолвов, продолжи прогон позже")
    return out


async def _backfill_photos(pool: ResolverPool, limit: int) -> dict:
    """Лиды с tg_user_id, но без фото → качаем аватар в data/avatars/{tg_user_id}.jpg.
    Тот же пул рабочих аккаунтов, что и для чатов — тот же суточный лимит на резолв."""
    from channels.tg_parser import _download_avatar
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, tg_user_id, username FROM contacts WHERE tg_user_id IS NOT NULL "
            "AND COALESCE(has_photo,0)=0 ORDER BY COALESCE(score,0) DESC, id LIMIT ?", (limit,)
        ).fetchall()]
    got: set[int] = set()
    nophoto = failed = 0
    exhausted = False
    i = 0
    while i < len(rows):
        ct = rows[i]
        client = await pool.client()
        if client is None:
            exhausted = True
            print(f"[стоп] рабочие аккаунты кончились: скачано {len(got)}, "
                  f"осталось {len(rows) - i} из этой порции — продолжи позже.")
            break
        try:
            u = await client.get_entity(int(ct["tg_user_id"]))
        except FloodWaitError as ex:
            if ex.seconds > MAX_FLOOD_WAIT:
                await pool.flood(ex.seconds)   # этот аккаунт всё — передаём следующему, запись не теряем
                continue
            print(f"[floodwait] жду {ex.seconds}с")
            await asyncio.sleep(ex.seconds + 5)
            continue
        except Exception as ex:  # noqa: BLE001
            pool.spend()
            failed += 1   # удалён/недоступен по приватности
            print(f"[skip] #{ct['id']} ({ct['tg_user_id']}): {str(ex)[:70]}")
            i += 1
            await asyncio.sleep(random.uniform(*PHOTO_PAUSE))
            continue
        pool.spend()
        if await _download_avatar(client, u):
            got.add(int(ct["tg_user_id"]))
        else:
            nophoto += 1   # аватара просто нет или скрыт приватностью
        i += 1
        if i < len(rows):
            await asyncio.sleep(random.uniform(*PHOTO_PAUSE))
    if got:
        with database.get_conn() as conn:
            database.mark_photos_by_tg(conn, got)
    out = {"candidates": len(rows), "downloaded": len(got), "no_photo": nophoto, "failed": failed, **pool.report()}
    if exhausted:
        out["note"] = f"скачано {len(got)} из {len(rows)} этой порции — квота исчерпана, продолжи позже"
    return out


async def run(do_chats: bool, do_photos: bool, limit: int) -> None:
    database.init_db()
    summary: dict = {"ok": True}

    if do_photos:
        # сначала бесплатный проход: что уже лежит на диске — просто отметить
        summary["photos_from_disk"] = _mark_photos_from_disk()

    need_net = do_chats or do_photos
    if not need_net:
        print(json.dumps(summary, ensure_ascii=False))
        return

    pool = ResolverPool()
    if not pool.available():
        await pool.start_fallback()
    try:
        if do_chats:
            summary["chats"] = await _backfill_chats(pool, limit)
            print(f"[chats] {summary['chats']}")
        if do_photos:
            summary["photos"] = await _backfill_photos(pool, limit)
            print(f"[photos] {summary['photos']}")
    finally:
        await pool.close()

    print(json.dumps(summary, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM бэкфилл: tg_chat_id у чатов, фото у лидов")
    p.add_argument("--chats", action="store_true", help="дозаполнить chats.tg_chat_id по @username")
    p.add_argument("--photos", action="store_true", help="докачать аватары лидов")
    p.add_argument("--all", action="store_true", help="и чаты, и фото")
    p.add_argument("--limit", type=int, default=200, help="сколько записей за прогон (на каждый вид)")
    args = p.parse_args()
    do_chats = args.chats or args.all
    do_photos = args.photos or args.all
    if not do_chats and not do_photos:
        p.error("нужен --chats, --photos или --all")
    if not config.TG_API_ID:
        print(json.dumps({"ok": False, "error": "нет TG_API_ID в .env"}, ensure_ascii=False))
        return
    asyncio.run(run(do_chats, do_photos, args.limit))


if __name__ == "__main__":
    main()
