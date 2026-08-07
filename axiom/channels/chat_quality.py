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
import json
import random
import re
from collections import Counter

from telethon.errors import FloodWaitError

from channels import hit_intent
from channels.chat_keywords import _client_for, _main_client, _target, _mark_scanned
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
            sql += " AND COALESCE(quality_verdict,'')=''"
        sql += " ORDER BY COALESCE(quality_at,'') ASC, id LIMIT ?"
        params.append(limit)
        chats = conn.execute(sql, params).fetchall()

    if not chats:
        print(json.dumps({"ok": True, "scanned": 0, "note": "нечего разбирать"},
                         ensure_ascii=False))
        return

    main = await _main_client()
    await main.start()
    owned: dict[int, object] = {}
    tally = Counter()
    done = skipped = 0
    for ch in chats:
        client = main
        if not ch["username"]:
            aid = ch["joined_by"]
            if aid not in owned:
                owned[aid], _err = await _client_for(aid)
            client = owned.get(aid)
            if client is None:
                skipped += 1
                _mark_scanned(ch["id"])
                continue
        try:
            msgs = await _read_chat(client, ch)
            res = analyze(msgs)
            _save(ch["id"], res)
            tally[res["verdict"]] += 1
            done += 1
            print(f"[{res['verdict']:<7}] «{(ch['title'] or '')[:34]}» — {res['why']}")
        except FloodWaitError as e:
            print(f"[flood] пауза {e.seconds}с — прерываю обход")
            break
        except Exception as e:  # noqa: BLE001 — один недоступный чат не рушит проход
            skipped += 1
            print(f"[skip] «{(ch['title'] or '')[:34]}»: {str(e)[:70]}")
        await asyncio.sleep(random.uniform(*PAUSE))

    await main.disconnect()
    for cl in owned.values():
        if cl is not None:
            try:
                await cl.disconnect()
            except Exception:  # noqa: BLE001
                pass
    print(json.dumps({"ok": True, "scanned": done, "skipped": skipped, **dict(tally)},
                     ensure_ascii=False))


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
