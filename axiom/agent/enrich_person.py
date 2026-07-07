"""H1 — AI-досье ФИЗЛИЦА по его сообщениям из чатов (психо-портрет для продаж).

В отличие от agent/enrich.py (тот обогащает юрлица: ЕГРЮЛ + сайт), здесь источник —
РЕАЛЬНЫЕ сообщения человека, собранные парсером в tg_user_posts (флаг --harvest)
плюс bio. Claude сводит это в портрет: боли / страхи / желания / интересы / психотип /
стиль общения / сегмент / скоринг + готовое рекомендуемое сообщение. У портрета есть
`confidence` — честная достоверность (мало данных → ниже), чтобы не выдавать догадку
за факт (как «достоверность 85% по профилю» на карточках Дениса).

Запуск (нужен ANTHROPIC_API_KEY в .env):
    python -m agent.enrich_person --id 5
    python -m agent.enrich_person --limit 30            # все, у кого есть сырьё, но нет досье
    python -m agent.enrich_person --limit 200 --batch   # пачкой через Batch API (−50%)
"""
from __future__ import annotations

import argparse
import base64

import anthropic
from pydantic import BaseModel, Field

import config
from db import database

AVATAR_DIR = config.BASE_DIR / "data" / "avatars"  # этап 4: фото аватара для vision


class PersonProfile(BaseModel):
    """Психо-портрет лида по его сообщениям из чатов."""

    pains: str = Field(description="Боли: что человека беспокоит/мешает, по его сообщениям. Кратко. Если не видно — ''.")
    fears: str = Field(description="Страхи/риски, которых он избегает. Если не видно — ''.")
    desires: str = Field(description="Желания/цели, к чему стремится. Если не видно — ''.")
    interests: str = Field(description="Темы и интересы через «; » (3-6 шт).")
    psychotype: str = Field(description="Тип принятия решений/психотип кратко (рациональный/эмоциональный/статусный и т.п.).")
    comm_style: str = Field(description="Как с ним лучше общаться (тон, длина, на 'ты'/'вы', что заходит).")
    best_time: str = Field(description="Когда вероятнее активен/на связи (по времени сообщений), если можно понять. Иначе ''.")
    segment: str = Field(description="Сфера/сегмент одним словом-двумя: IT/бизнес/маркетинг/недвижимость/финансы/… ")
    score: float = Field(description="Насколько это релевантный лид-цель, 0.0..1.0 (как 0.90 у Дениса).", ge=0, le=1)
    quotes: str = Field(description="1-3 показательные ДОСЛОВНЫЕ цитаты из его сообщений через « | ».")
    rec_message: str = Field(description="ОДНО готовое персональное первое сообщение (крючок), 1-2 фразы, без воды.")
    photo_analysis: str = Field(description="Если приложено ФОТО аватара — кратко: дресс-код, примерная возрастная вилка, "
                                "признаки статуса/настроения (с оговоркой «по фото»). Если фото нет — ''.")
    summary: str = Field(description="1-2 фразы досье для CRM.")
    confidence: float = Field(description="Достоверность портрета 0.0..1.0: мало сообщений/обрывочно → ниже.", ge=0, le=1)


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


SYSTEM = (
    "Ты — профайлер отдела продаж. По РЕАЛЬНЫМ сообщениям человека из Telegram-чатов "
    "составь психологический портрет для персонального захода в продажах. "
    "Опирайся ТОЛЬКО на то, что видно в сообщениях и bio — не выдумывай. Если данных мало "
    "или они обрывочны — честно снижай confidence и оставляй поля пустыми, а не фантазируй. "
    "Цель — понять боли/желания человека и дать ОДНО точное первое сообщение, от которого "
    "он захочет ответить (не спам, не «партянка» — живо и по делу). "
    "Если приложено фото аватара — кратко опиши в photo_analysis дресс-код, примерный возраст и "
    "признаки статуса, обязательно с оговоркой «по фото» (это догадка, не факт). Фото нет — photo_analysis=''."
)


def _posts_for(conn, tg_user_id: int, limit: int = 40) -> list:
    return conn.execute(
        "SELECT text, chat_title, ts FROM tg_user_posts WHERE tg_user_id=? ORDER BY ts DESC LIMIT ?",
        (tg_user_id, limit),
    ).fetchall()


def _build_context(contact: dict, posts: list) -> str:
    lines = [
        f"Имя: {contact.get('name') or '-'}",
        f"Город: {contact.get('city') or '-'}",
        f"Теги/сегмент (как помечен парсером): {contact.get('tags') or '-'}",
        f"Bio из профиля: {contact.get('bio') or '-'}",
        "",
        f"Его сообщения из чатов ({len(posts)} шт., свежие сверху):",
    ]
    for p in posts:
        ch = (p["chat_title"] or "").strip()
        lines.append(f"  • [{ch}] {p['text']}")
    return "\n".join(lines)


def _avatar_b64(tg_user_id: int | None) -> str | None:
    """Base64 аватара, если парсер его скачал (data/avatars/{id}.jpg). Иначе None."""
    if not tg_user_id:
        return None
    path = AVATAR_DIR / f"{tg_user_id}.jpg"
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        return base64.standard_b64encode(path.read_bytes()).decode("ascii")
    except Exception:  # noqa: BLE001
        return None


def _user_content(ctx: str, tg_user_id: int | None):
    """Контент сообщения: текст, а при наличии аватара — ещё и картинка (vision)."""
    img = _avatar_b64(tg_user_id)
    if not img:
        return ctx
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
        {"type": "text", "text": ctx + "\n\n(Выше — фото аватара. Дай по нему photo_analysis.)"},
    ]


def enrich_person(contact: dict, posts: list) -> PersonProfile:
    ctx = _build_context(contact, posts)
    resp = _get_client().messages.parse(
        model=config.MODEL,
        max_tokens=900,
        system=SYSTEM,
        messages=[{"role": "user", "content": _user_content(ctx, contact.get("tg_user_id"))}],
        output_format=PersonProfile,
    )
    return resp.parsed_output


def _save(contact_id: int, p: PersonProfile) -> None:
    with database.get_conn() as conn:
        row = conn.execute("SELECT notes FROM contacts WHERE id=?", (contact_id,)).fetchone()
        notes = row["notes"] if row else None
        if p.summary and (not notes or p.summary not in (notes or "")):
            notes = f"{notes} | Досье: {p.summary}" if notes else f"Досье: {p.summary}"
        conn.execute(
            "UPDATE contacts SET pains=?, fears=?, desires=?, interests=?, psychotype=?, "
            "comm_style=?, best_time=?, score=?, segment=?, quotes=?, rec_message=?, "
            "photo_analysis=?, confidence=?, notes=?, enriched_at=datetime('now'), "
            "updated_at=datetime('now') WHERE id=?",
            (p.pains, p.fears, p.desires, p.interests, p.psychotype, p.comm_style, p.best_time,
             p.score, p.segment, p.quotes, p.rec_message, p.photo_analysis, p.confidence,
             notes, contact_id),
        )


def _targets(cid: int | None, tag: str | None, limit: int) -> list:
    database.init_db()
    with database.get_conn() as conn:
        if cid:
            return conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchall()
        # есть сырьё (tg_user_posts) и ещё не профилирован (pains пуст)
        where = ("tg_user_id IN (SELECT DISTINCT tg_user_id FROM tg_user_posts) "
                 "AND (pains IS NULL OR pains='') AND (score IS NULL)")
        params: list = []
        if tag:
            where += " AND tags LIKE ?"
            params.append(f"%{tag}%")
        return conn.execute(
            f"SELECT * FROM contacts WHERE {where} ORDER BY id LIMIT ?", (*params, limit)
        ).fetchall()


def _profile_schema() -> dict:
    s = PersonProfile.model_json_schema()
    s["additionalProperties"] = False
    s["required"] = list(s.get("properties", {}).keys())
    return s


def run_batch(rows: list) -> None:
    """Те же запросы пачкой через Batch API (−50%). Сырьё (посты) тянем синхронно из БД."""
    import json
    import time

    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = _get_client()
    schema = _profile_schema()
    reqs: list = []
    skipped = 0
    with database.get_conn() as conn:
        for row in rows:
            c = dict(row)
            posts = _posts_for(conn, c["tg_user_id"]) if c.get("tg_user_id") else []
            if not posts:
                skipped += 1
                continue
            ctx = _build_context(c, posts)
            reqs.append(
                Request(
                    custom_id=f"c{c['id']}",
                    params=MessageCreateParamsNonStreaming(
                        model=config.MODEL,
                        max_tokens=900,
                        system=SYSTEM,
                        messages=[{"role": "user", "content": _user_content(ctx, c.get("tg_user_id"))}],
                        output_config={"format": {"type": "json_schema", "schema": schema}},
                    ),
                )
            )
    if not reqs:
        print(f"нет сырья для досье (пропущено {skipped})"); return
    batch = client.messages.batches.create(requests=reqs)
    print(f"батч {batch.id} отправлен на {len(reqs)} лид(ов), жду…")
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        rc = b.request_counts
        print(f"  …в работе: {rc.processing}, готово: {rc.succeeded + rc.errored}")
        time.sleep(20)
    ok = fail = 0
    for result in client.messages.batches.results(batch.id):
        cid = int(result.custom_id.lstrip("c"))
        if result.result.type != "succeeded":
            fail += 1
            print(f"[fail #{cid}] {result.result.type}")
            continue
        try:
            text = next(blk.text for blk in result.result.message.content if blk.type == "text")
            p = PersonProfile(**json.loads(text))
            _save(cid, p)
            ok += 1
            print(f"[ok #{cid}] score={p.score:.2f} conf={p.confidence:.2f} | {p.rec_message[:45]}…")
        except Exception as ex:  # noqa: BLE001
            fail += 1
            print(f"[fail #{cid}] {ex}")
    print(f"батч готово: ok={ok}, fail={fail}")


def run(cid: int | None, tag: str | None, limit: int, batch: bool = False) -> None:
    rows = _targets(cid, tag, limit)
    if not rows:
        print("нечего профилировать (нет лидов с сырьём в tg_user_posts)")
        return
    if batch:
        run_batch(rows)
        return
    print(f"профилирую {len(rows)} человек(а)…")
    with database.get_conn() as conn:
        posts_map = {r["id"]: _posts_for(conn, r["tg_user_id"]) if r["tg_user_id"] else [] for r in rows}
    for row in rows:
        c = dict(row)
        posts = posts_map.get(c["id"], [])
        if not posts:
            print(f"[skip #{c['id']}] {c.get('name')}: нет сообщений в tg_user_posts")
            continue
        try:
            p = enrich_person(c, posts)
            _save(c["id"], p)
            print(f"[ok #{c['id']}] {c.get('name')}: score={p.score:.2f} conf={p.confidence:.2f} seg={p.segment}")
        except Exception as ex:  # noqa: BLE001
            print(f"[fail #{c['id']}] {c.get('name')}: {ex}")
    print("готово")


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM H1 — досье физлица по сообщениям из чатов")
    p.add_argument("--id", type=int, default=None, help="профилировать один контакт по id")
    p.add_argument("--tag", default=None, help="фильтр по тегу")
    p.add_argument("--limit", type=int, default=30, help="сколько взять в пачку")
    p.add_argument("--batch", action="store_true", help="через Batch API (−50%%, асинхронно)")
    args = p.parse_args()
    if not config.ANTHROPIC_API_KEY:
        print("Нет ANTHROPIC_API_KEY в .env")
        return
    run(args.id, args.tag, args.limit, args.batch)


if __name__ == "__main__":
    main()
