"""Предварительный вердикт по чату: годен для лидгена или мусор.

ЗАЧЕМ. В каталоге тысячи чатов, и вручную их не пересмотреть. Но и слушать все
подряд бессмысленно: в половине сидят такие же рассыльщики, публикующие прайсы по
кругу (живой пример — RAVEN STUDIO, восемь одинаковых объявлений подряд). Модуль
читает выборку сообщений и раскладывает чаты на «годен / мусор / посмотреть
глазами», чтобы оператор смотрел не тысячу, а десятки.

ЧТО СЧИТАЕМ. Не «спамность» вообще, а признаки, по которым чат для нас бесполезен:

  • РЕКЛАМА. Доля сообщений, где hit_intent видит продавца, а не заказчика. Тот же
    разбор, что и для находок, — второй классификатор здесь не нужен.
  • ПОВТОРЫ. Один и тот же текст, опубликованный не раз. Считаем по нормализованному
    тексту: рассыльщик слегка меняет эмодзи, но не суть.
  • МОНОПОЛИЯ АВТОРОВ. Если 3 человека дали больше половины сообщений — это не
    сообщество, а доска объявлений нескольких продавцов.
  • ЖИВОЙ РАЗГОВОР. Короткие реплики, вопросы, обращения друг к другу. Их наличие
    важнее любых минусов: там, где люди разговаривают, встречаются и заказчики.
  • ЗАПРОСЫ. Прямые «ищу / посоветуйте / кто может» — то, ради чего мы и слушаем.
    Даже один такой на выборку сильно поднимает ценность чата.

ВЕРДИКТ — предварительный, последнее слово за оператором:
  good    — есть живое общение и/или запросы, рекламы умеренно;
  trash   — почти сплошь реклама и повторы, живого общения нет;
  unclear — данных мало (чат тихий или прочли слишком мало) — глянуть глазами.

Порог намеренно мягкий к «good»: пропустить рабочий чат дороже, чем показать
оператору лишний.

Запуск:
    python -m channels.chat_quality --all --limit 200      # пройти каталог
    python -m channels.chat_quality --chat 42              # один чат
    python -m channels.chat_quality --all --only-new       # только неразобранные
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import random
import re
from collections import Counter

from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from channels import hit_intent
from channels.chat_keywords import _client_for, _target, _mark_scanned
from channels.telegram import build_client
from db import database

# Сколько сообщений читаем на чат. Меньше 60 — статистика врёт (одна рекламная
# серия перевешивает всё), больше 250 — лишняя нагрузка на аккаунт ради тех же выводов.
SAMPLE = 150
# Реклама выше этой доли — сигнал «доска объявлений», а не сообщество.
AD_SHARE_TRASH = 0.75
# Столько живых реплик достаточно, чтобы признать чат разговорным.
LIVE_MIN = 8
# Пауза между чатами: массовый обход не должен выглядеть как выкачивание каталога.
PAUSE = (1.2, 3.0)

_WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ]{2,}")
# Реплика живого разговора: короткая, без ссылок и прайсов, часто с обращением.
_TALK = re.compile(r"\b(спасибо|согласен|согласна|да\b|нет\b|привет|подскажите|а\s+как|"
                   r"почему|кто\-нибудь|ребят|коллеги|думаю|кажется|верно|точно)\b", re.I)
_QUESTION = re.compile(r"\?\s*$")


# Сколько чатов один аккаунт читает за сутки. Прогон 146 чатов подряд одним номером
# закончился FloodWait на 21.8 часа — Telegram счёл темп нечеловеческим. Норма нужна
# не «на всякий случай»: каталог в 2.4 тыс. чатов физически не прочитать одним
# аккаунтом, его надо делить между несколькими.
DAILY_PER_ACCOUNT = 40
_QUOTA_KEY = "chat_quality_quota"     # {"2026-08-07": {"20": 37, "15": 12}}
_FLOOD_KEY = "chat_quality_flood"     # {"20": "2026-08-08T14:03:00"} — до когда не трогать


def _today() -> str:
    return datetime.date.today().isoformat()


def _load_json_setting(key: str) -> dict:
    try:
        with database.get_conn() as conn:
            raw = database.get_setting(conn, key, "") or "{}"
        return json.loads(raw)
    except Exception:  # noqa: BLE001 — битую настройку не тащим, начинаем с чистой
        return {}


def _save_json_setting(key: str, data: dict) -> None:
    with database.get_conn() as conn:
        database.set_setting(conn, key, json.dumps(data, ensure_ascii=False))


def _quota_used() -> dict:
    """Сколько каждый аккаунт прочитал СЕГОДНЯ. Вчерашние дни выбрасываем."""
    q = _load_json_setting(_QUOTA_KEY)
    return q.get(_today(), {})


def _quota_bump(acc_id: int, n: int = 1) -> None:
    q = _load_json_setting(_QUOTA_KEY)
    today = q.setdefault(_today(), {})
    today[str(acc_id)] = today.get(str(acc_id), 0) + n
    _save_json_setting(_QUOTA_KEY, {_today(): today})   # старые дни не храним


def _flooded() -> dict:
    """Аккаунты, которым Telegram запретил читать, и до какого времени."""
    out = {}
    now = datetime.datetime.now()
    for aid, until in _load_json_setting(_FLOOD_KEY).items():
        try:
            if datetime.datetime.fromisoformat(until) > now:
                out[aid] = until
        except Exception:  # noqa: BLE001
            continue
    return out


def _mark_flood(acc_id: int, seconds: int) -> None:
    """Вывести аккаунт из ротации до конца флуда — чтобы следующий прогон его не трогал."""
    data = _load_json_setting(_FLOOD_KEY)
    until = datetime.datetime.now() + datetime.timedelta(seconds=max(60, seconds))
    data[str(acc_id)] = until.isoformat(timespec="seconds")
    _save_json_setting(_FLOOD_KEY, data)


def _readers() -> list[dict]:
    """Кто может читать чаты: живая сессия, не родной номер, не в флуде, есть остаток
    дневной нормы. Родной (protected) не берём принципиально — на нём и словили
    13.8-часовой лимит в июле."""
    used = _quota_used()
    flood = _flooded()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, label, tg_session, proxy, api_id, api_hash FROM accounts "
            "WHERE tg_session IS NOT NULL AND tg_session<>'' AND session_alive=1 "
            "AND COALESCE(protected,0)=0 AND COALESCE(status,'') NOT IN ('banned','archived') "
            "ORDER BY id").fetchall()
    out = []
    for r in rows:
        aid = str(r["id"])
        if aid in flood:
            continue
        left = DAILY_PER_ACCOUNT - used.get(aid, 0)
        if left <= 0:
            continue
        d = dict(r)
        d["left"] = left
        out.append(d)
    # первым — тот, кто сегодня читал меньше всех: нагрузка размазывается ровно
    out.sort(key=lambda d: -d["left"])
    return out


def _norm(text: str) -> str:
    """Текст без эмодзи, ссылок и регистра — чтобы «слегка подправленный» репост
    рассыльщика считался тем же сообщением, каким он и является."""
    t = (text or "").lower()
    t = re.sub(r"https?://\S+|t\.me/\S+|@[\w_]+", " ", t)
    t = " ".join(_WORD.findall(t))
    return t[:400]


def analyze(messages: list[dict]) -> dict:
    """Метрики и вердикт по выборке сообщений. Чистая функция — тестируется без сети."""
    texts = [(m.get("text") or "").strip() for m in messages]
    texts = [t for t in texts if t]
    total = len(texts)
    if total < 15:
        return {"verdict": "unclear", "why": f"мало сообщений для оценки ({total})",
                "total": total, "ad_share": None, "repeat_share": None,
                "live": 0, "requests": 0, "authors": 0}

    ads = requests_ = live = 0
    for t in texts:
        intent, _ = hit_intent.by_rules(t)
        if intent == hit_intent.VENDOR:
            ads += 1
        elif intent == hit_intent.CLIENT:
            requests_ += 1
        # живая реплика: короткая, разговорная или вопрос к людям
        if len(t) <= 180 and (_TALK.search(t) or _QUESTION.search(t)):
            live += 1

    # Повтор считаем по паре «автор + текст»: десять человек, написавших «спасибо»,
    # это живой чат, а один продавец с тем же прайсом двадцать раз — доска объявлений.
    pairs = [(m.get("author"), _norm((m.get("text") or "")))
             for m in messages if (m.get("text") or "").strip()]
    dup = sum(c - 1 for pair, c in Counter(p for p in pairs if p[1]).items() if c > 1)
    authors = Counter(m.get("author") for m in messages if m.get("author"))
    top3 = sum(c for _, c in authors.most_common(3))

    ad_share = ads / total
    repeat_share = dup / total
    monopoly = (top3 / total) if authors else 0.0

    # Порядок проверок важен: сначала то, ради чего мы вообще слушаем чаты.
    if requests_ >= 2 or (requests_ >= 1 and live >= LIVE_MIN):
        verdict = "good"
        why = f"есть живые запросы ({requests_}) и разговор ({live} реплик)"
    elif ad_share >= AD_SHARE_TRASH and live < LIVE_MIN:
        verdict = "trash"
        why = f"реклама {ad_share:.0%}, живого общения почти нет ({live})"
    elif repeat_share >= 0.4:
        verdict = "trash"
        why = f"{repeat_share:.0%} сообщений — повторы одного и того же текста"
    elif monopoly >= 0.6 and live < LIVE_MIN:
        verdict = "trash"
        why = f"{monopoly:.0%} сообщений от трёх авторов — доска объявлений"
    elif live >= LIVE_MIN:
        verdict = "good"
        why = f"живое общение ({live} реплик), реклама {ad_share:.0%}"
    else:
        verdict = "unclear"
        why = f"ни запросов, ни явного мусора (реклама {ad_share:.0%}, живых {live})"

    return {"verdict": verdict, "why": why, "total": total,
            "ad_share": round(ad_share, 3), "repeat_share": round(repeat_share, 3),
            "monopoly": round(monopoly, 3), "live": live, "requests": requests_,
            "authors": len(authors)}


# Наш вердикт → та же ось, которой пользуется оператор (chats.verdict).
# Отдельного поля не заводим: у чата один вердикт, разница лишь в том, кто его
# поставил — ai предварительно или человек окончательно (chats.verdict_src).
_VERDICT_RU = {"good": "годен", "trash": "не годен", "unclear": "на проверку"}


def _touch(chat_id: int, err: str | None = None) -> None:
    """Отметить ПОПЫТКУ разбора, даже неудачную.

    Очередь сортируется по verdict_at, а он писался только при успехе. Поэтому чат,
    который прочитать не удалось (нет аккаунта-участника, канал закрылся), навсегда
    оставался первым в очереди: каждый следующий заход брал те же одиннадцать штук,
    спотыкался о них и до остальных двух тысяч не доходил. Прогон крутился вхолостую.
    """
    with database.get_conn() as conn:
        conn.execute("UPDATE chats SET verdict_at=datetime('now'), scan_error=? WHERE id=?",
                     ((err or "")[:200] or None, chat_id))


def _save(chat_id: int, res: dict) -> bool:
    """Записать предварительный вердикт. Решение ЧЕЛОВЕКА не трогаем никогда:
    оператор уже посмотрел глазами, и машинная переоценка его отменять не вправе."""
    with database.get_conn() as conn:
        row = conn.execute("SELECT verdict, verdict_src FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        if row and (row["verdict_src"] or "") == "человек" and (row["verdict"] or "").strip():
            return False
        conn.execute(
            "UPDATE chats SET verdict=?, verdict_src='ai', verdict_at=datetime('now'), "
            "quality_json=? WHERE id=?",
            (_VERDICT_RU.get(res["verdict"], "на проверку"),
             json.dumps(res, ensure_ascii=False), chat_id))
    return True


async def _read_chat(client, ch) -> list[dict]:
    out: list[dict] = []
    async for msg in client.iter_messages(_target(ch), limit=SAMPLE):
        text = (msg.message or "").strip()
        if not text:
            continue
        out.append({"text": text, "author": getattr(msg, "sender_id", None)})
    return out


async def run(chat_id: int | None, limit: int, only_new: bool) -> None:
    database.init_db()
    with database.get_conn() as conn:
        sql = ("SELECT id, title, username, tg_chat_id, tg_access_hash, kind, joined_by "
               "FROM chats WHERE ((username IS NOT NULL AND username<>'') "
               "OR (in_account='yes' AND tg_chat_id IS NOT NULL))")
        params: list = []
        if chat_id:
            sql += " AND id=?"
            params.append(chat_id)
        elif only_new:
            # «Не разобранные» — это и те, у кого вердикта нет вовсе, и те, кому его
            # ставил человек... нет: человека не трогаем совсем (см. _save). Берём
            # только пустой вердикт.
            sql += " AND COALESCE(verdict,'')=''"
        sql += " ORDER BY COALESCE(verdict_at,'') ASC, id LIMIT ?"
        params.append(limit)
        chats = conn.execute(sql, params).fetchall()

    if not chats:
        print(json.dumps({"ok": True, "scanned": 0, "note": "нечего разбирать"},
                         ensure_ascii=False))
        return

    # ПУЛ ЧИТАТЕЛЕЙ вместо одного аккаунта. Каталог в 2.4 тыс. чатов одним номером не
    # прочитать: 146 чатов подряд закончились FloodWait на 21.8 часа. Делим нагрузку —
    # каждому дневная норма, упёрся или словил лимит — работает следующий.
    readers = _readers()
    if not readers:
        used, flood = _quota_used(), _flooded()
        print(json.dumps({
            "ok": False,
            "error": (f"свободных аккаунтов нет: дневную норму ({DAILY_PER_ACCOUNT} чатов) "
                      f"выбрали {len(used)}, во флуде {len(flood)}. Подожди или добавь "
                      f"живых аккаунтов."),
            "quota_used": used, "flooded": flood,
        }, ensure_ascii=False))
        return

    clients: dict[int, object] = {}          # acc_id → подключённый клиент
    owned: dict[int, object] = {}            # для закрытых чатов — только участник
    tally = Counter()
    done = skipped = 0
    ri = 0                                    # указатель по кругу читателей

    async def _client_of(acc: dict):
        """Подключаем читателя лениво и держим до конца прогона."""
        aid = acc["id"]
        if aid in clients:
            return clients[aid]
        cl = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
        await cl.connect()
        if not await cl.is_user_authorized():
            await cl.disconnect()
            clients[aid] = None
            return None
        clients[aid] = cl
        return cl

    for ch in chats:
        if not readers:
            print("[stop] свободных читателей не осталось — остальное в следующий заход")
            break
        # Закрытый чат читает ТОЛЬКО тот, кто в нём состоит: выбора нет, пул не поможет.
        if not ch["username"]:
            aid = ch["joined_by"]
            if aid not in owned:
                owned[aid], _err = await _client_for(aid)
            client = owned.get(aid)
            reader = None
            if client is None:
                skipped += 1
                _mark_scanned(ch["id"])
                _touch(ch["id"], "нечем читать: аккаунт-участник без живой сессии")
                continue
        else:
            reader = readers[ri % len(readers)]
            client = await _client_of(reader)
            if client is None:
                readers.remove(reader)
                _touch(ch["id"], f"читатель #{reader['id']} не авторизован")
                continue

        try:
            msgs = await _read_chat(client, ch)
            res = analyze(msgs)
            _save(ch["id"], res)
            tally[res["verdict"]] += 1
            done += 1
            if reader:
                _quota_bump(reader["id"])
                reader["left"] -= 1
                if reader["left"] <= 0:
                    print(f"[норма] #{reader['id']} «{reader['label']}» выбрал дневной лимит")
                    readers.remove(reader)
                else:
                    ri += 1
            print(f"[{res['verdict']:<7}] «{(ch['title'] or '')[:34]}» — {res['why']}")
        except FloodWaitError as e:
            # Аккаунт выводим из ротации до конца лимита и продолжаем следующим —
            # раньше прогон обрывался целиком на первом же флуде.
            hrs = round(e.seconds / 3600, 1)
            if reader:
                _mark_flood(reader["id"], e.seconds)
                readers = [r for r in readers if r["id"] != reader["id"]]
                print(f"[flood] #{reader['id']} «{reader['label']}» на {hrs}ч — вывожу из ротации")
            else:
                print(f"[flood] аккаунт-участник на {hrs}ч — чат пропущен")
                _touch(ch["id"], f"FloodWait {e.seconds}с")
            continue
        except Exception as e:  # noqa: BLE001 — один недоступный чат не рушит проход
            skipped += 1
            _touch(ch["id"], f"{type(e).__name__}: {e}")
            print(f"[skip] «{(ch['title'] or '')[:34]}»: {str(e)[:70]}")
        await asyncio.sleep(random.uniform(*PAUSE))

    for cl in list(clients.values()) + list(owned.values()):
        if cl is not None:
            try:
                await cl.disconnect()
            except Exception:  # noqa: BLE001
                pass
    print(json.dumps({"ok": True, "scanned": done, "skipped": skipped,
                      "readers_left": len(readers), **dict(tally)}, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="Предварительный вердикт по чатам каталога")
    p.add_argument("--chat", type=int, help="разобрать один чат по id")
    p.add_argument("--all", action="store_true", help="пройти каталог")
    p.add_argument("--only-new", action="store_true", help="только ещё не разобранные")
    p.add_argument("--limit", type=int, default=100, help="сколько чатов за проход")
    a = p.parse_args()
    if not a.chat and not a.all:
        p.error("укажи --chat <id> или --all")
    asyncio.run(run(a.chat, a.limit, a.only_new))


if __name__ == "__main__":
    main()
