"""Агент-обогатитель AXIOM. Достраивает карточку лида: контактное лицо,
специализацию и персональную «зацепку» для первого холодного сообщения.

Источники v1: данные из 2ГИС (что уже в книжке) + текст сайта/VK по ссылке.
Сведение в структуру делает Claude (structured outputs).

Запуск (веб-пульт зовёт в отдельном процессе):
    python -m agent.enrich --id 5
    python -m agent.enrich --tag "Агентства недвижимости" --limit 20
"""
from __future__ import annotations

import argparse
import re

from pydantic import BaseModel, Field

import config
from agent import llm
from db import database


class Enrichment(BaseModel):
    """Что агент извлекает по лиду."""

    person_name: str | None = Field(description="Имя контактного лица (как обратиться), если удалось понять. Иначе null.")
    person_role: str | None = Field(description="Должность контактного лица (директор/руководитель/риелтор), иначе null.")
    specialization: str = Field(description="На чём специализируется агентство: новостройки/вторичка/аренда/элитка/загородная/инвестиции и т.п. Кратко.")
    hook: str = Field(description="ОДНА короткая персональная зацепка (1 фраза) для первого холодного сообщения от поставщика ИИ-автоматизации. Конкретная, не вода.")
    summary: str = Field(description="1-2 фразы досье для CRM.")



def _first_url(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"https?://[^\s|]+", text)
    return m.group(0) if m else None


def fetch_site(url: str | None) -> str:
    """Тянет домашнюю страницу, грубо чистит HTML → текст (до ~4000 симв.). Ошибки → ''."""
    if not url:
        return ""
    try:
        import requests

        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (AXIOM enrich)"})
        html = r.text
    except Exception:
        return ""
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # 1500 симв. достаточно для специализации + зацепки; меньше входных токенов = дешевле.
    return text[:1500]


DADATA_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"


def dadata_lookup(name: str | None, city: str | None) -> dict | None:
    """ЕГРЮЛ через DaData: по «название + город» → ИНН, ОГРН, ФИО руководителя, ОКВЭД.
    Нужен DADATA_API_KEY в .env (бесплатный тариф). Пусто/ошибка → None."""
    if not config.DADATA_API_KEY or not name:
        return None
    try:
        import requests

        q = f"{name} {city or ''}".strip()
        r = requests.post(
            DADATA_URL,
            json={"query": q, "count": 1},
            headers={
                "Authorization": f"Token {config.DADATA_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=8,
        )
        sugg = (r.json() or {}).get("suggestions") or []
    except Exception:
        return None
    if not sugg:
        return None
    d = sugg[0].get("data", {})
    director, role = None, None
    mgmt = d.get("management") or {}
    if mgmt.get("name"):
        director, role = mgmt["name"], (mgmt.get("post") or "руководитель")
    elif d.get("fio"):
        f = d["fio"]
        director = " ".join(x for x in [f.get("surname"), f.get("name"), f.get("patronymic")] if x)
        role = "ИП"
    # учредители: на free-тарифе DaData приходит null; на платном — список dict.
    founders = []
    for f in (d.get("founders") or []):
        nm = (f.get("fio") or {}).get("source") or f.get("name")
        if nm:
            founders.append(nm)
    return {
        "inn": d.get("inn"),
        "ogrn": d.get("ogrn"),
        "director": director,
        "role": role,
        "founders": founders,
        "okved": d.get("okved"),
        "status": (d.get("state") or {}).get("status"),
        "full_name": (d.get("name") or {}).get("full_with_opf"),
    }


SYSTEM = (
    "Ты — аналитик отдела продаж. Обогащаешь карточку B2B-лида — агентства недвижимости. "
    "Продаём им ИИ-автоматизацию (боты, обработка заявок, снятие рутины). "
    "На входе: данные из справочника 2ГИС и текст их сайта (если есть). "
    "Извлеки контактное лицо (если видно), специализацию и составь ОДНУ конкретную персональную зацепку "
    "для первого холодного сообщения — без воды и общих фраз. Если данных мало — опирайся на специализацию."
)


def _build_context(contact: dict, dd: dict | None, site: str) -> str:
    """Собирает пользовательский промпт по лиду (2ГИС + ЕГРЮЛ + текст сайта)."""
    egrul = ""
    if dd:
        egrul = (
            f"\n\nИз ЕГРЮЛ (DaData): юрлицо={dd.get('full_name') or '-'}, "
            f"руководитель={dd.get('director') or '-'} ({dd.get('role') or '-'}), "
            f"ОКВЭД={dd.get('okved') or '-'}, статус={dd.get('status') or '-'}, ИНН={dd.get('inn') or '-'}"
        )
    ctx = (
        f"Агентство: {contact.get('name') or '-'}\n"
        f"Город: {contact.get('city') or '-'}\n"
        f"Теги/категория: {contact.get('tags') or '-'}\n"
        f"Заметки (сайт/vk/email/адрес): {contact.get('notes') or '-'}"
    )
    if contact.get("username"):
        ctx += f"\nTelegram: @{contact.get('username')}"
    ctx += egrul
    ctx += f"\n\nТекст сайта:\n{site or '(не удалось получить)'}"
    return ctx


def enrich_contact(contact: dict) -> tuple[Enrichment, dict | None]:
    dd = dadata_lookup(contact.get("name"), contact.get("city"))
    site = fetch_site(_first_url(contact.get("notes")))
    ctx = _build_context(contact, dd, site)
    out = llm.structured(
        config.MODEL, system=SYSTEM,
        messages=[{"role": "user", "content": ctx}],
        output_format=Enrichment,
        max_tokens=600,  # карточка короткая: специализация + зацепка + 1-2 фразы досье
    )
    return out, dd


def run_batch(rows: list) -> None:
    """Обогащение через Batch API: те же запросы асинхронно, −50% к цене.
    DaData/сайт тянем синхронно (это не модель), а вызовы модели уходят пачкой.
    Batch есть только у Anthropic — вызывать при llm.supports_batch(config.MODEL)."""
    import json
    import time

    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    # батч живёт долго (поллинг до часа) — берём первый ключ и держим одного клиента
    client = anthropic.Anthropic(api_key=llm.keys()[0])
    schema = llm.json_schema(Enrichment)
    model = llm.split(config.MODEL)[1]
    reqs: list = []
    dd_map: dict[int, dict | None] = {}
    print(f"готовлю батч на {len(rows)} лид(ов): тяну ЕГРЮЛ/сайты…")
    for row in rows:
        c = dict(row)
        dd = dadata_lookup(c.get("name"), c.get("city"))
        dd_map[c["id"]] = dd
        site = fetch_site(_first_url(c.get("notes")))
        ctx = _build_context(c, dd, site)
        reqs.append(
            Request(
                custom_id=f"c{c['id']}",
                params=MessageCreateParamsNonStreaming(
                    model=model,
                    max_tokens=600,
                    system=SYSTEM,
                    messages=[{"role": "user", "content": ctx}],
                    output_config={"format": {"type": "json_schema", "schema": schema}},
                ),
            )
        )
    batch = client.messages.batches.create(requests=reqs)
    print(f"батч {batch.id} отправлен, жду (обычно до часа)…")
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        rc = b.request_counts
        print(f"  …обрабатывается: {rc.processing}, готово: {rc.succeeded + rc.errored}")
        time.sleep(20)
    ok = fail = 0
    for result in client.messages.batches.results(batch.id):
        cid = int(result.custom_id.lstrip("c"))
        dd = dd_map.get(cid)
        if result.result.type != "succeeded":
            fail += 1
            print(f"[fail #{cid}] {result.result.type}")
            continue
        try:
            text = next(blk.text for blk in result.result.message.content if blk.type == "text")
            e = Enrichment(**json.loads(text))
            _save(cid, e, dd)
            ok += 1
            print(f"[ok #{cid}] лицо={(dd and dd.get('director')) or '-'}, hook={e.hook[:40]}…")
        except Exception as ex:
            fail += 1
            print(f"[fail #{cid}] {ex}")
    print(f"батч готово: ok={ok}, fail={fail}")


def _save(contact_id: int, e: Enrichment, dd: dict | None) -> None:
    # ФИО директора из ЕГРЮЛ в приоритете над догадкой Claude
    person_name = (dd and dd.get("director")) or e.person_name
    person_role = (dd and dd.get("role")) or e.person_role
    inn = dd.get("inn") if dd else None
    ogrn = dd.get("ogrn") if dd else None
    founders = "; ".join(dd["founders"]) if dd and dd.get("founders") else None
    with database.get_conn() as conn:
        row = conn.execute("SELECT notes FROM contacts WHERE id=?", (contact_id,)).fetchone()
        notes = row["notes"] if row else None
        if e.summary and (not notes or e.summary not in notes):
            notes = f"{notes} | Досье: {e.summary}" if notes else f"Досье: {e.summary}"
        conn.execute(
            "UPDATE contacts SET person_name=?, person_role=?, specialization=?, hook=?, "
            "inn=COALESCE(?,inn), ogrn=COALESCE(?,ogrn), founders=COALESCE(?,founders), notes=?, "
            "enriched_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
            (person_name, person_role, e.specialization, e.hook, inn, ogrn, founders, notes, contact_id),
        )


def _targets(cid: int | None, tag: str | None, limit: int, only_missing_hooks: bool = False,
             hooks_any: bool = False) -> list:
    database.init_db()
    with database.get_conn() as conn:
        if cid:
            return conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchall()
        if hooks_any:
            # дозалить хуки ВСЕМ без зацепки (даже если ФИО директора не пробито —
            # зацепка строится из специализации/заметок, ФИО не обязательно)
            where = "(hook IS NULL OR hook='')"
        elif only_missing_hooks:
            # дозалить хуки тем, у кого директор уже пробит, а зацепки ещё нет
            where = "(hook IS NULL OR hook='') AND person_name IS NOT NULL"
        else:
            where = "enriched_at IS NULL"
        params: list = []
        if tag:
            where += " AND tags LIKE ?"
            params.append(f"%{tag}%")
        return conn.execute(f"SELECT * FROM contacts WHERE {where} ORDER BY id LIMIT ?", (*params, limit)).fetchall()


def run(cid: int | None, tag: str | None, limit: int, no_llm: bool = False,
        batch: bool = False, hooks: bool = False, hooks_all: bool = False) -> None:
    rows = _targets(cid, tag, limit, only_missing_hooks=hooks, hooks_any=hooks_all)
    if not rows:
        print("нечего обогащать")
        return
    if batch and not no_llm and not llm.supports_batch(config.MODEL):
        print(f"[инфо] Batch API есть только у Anthropic, а модель — «{config.MODEL}». "
              f"Иду обычным путём (по одному).")
        batch = False
    if batch and not no_llm:
        run_batch(rows)
        return
    print(f"обогащаю {len(rows)} контакт(ов){' (только DaData, без Claude)' if no_llm else ''}…")
    for row in rows:
        c = dict(row)
        try:
            if no_llm:
                # без Claude: только ЕГРЮЛ (директор/ИНН/ОГРН). Зацепку добьём позже.
                dd = dadata_lookup(c.get("name"), c.get("city"))
                e = Enrichment(person_name=None, person_role=None, specialization="", hook="", summary="")
                _save(c["id"], e, dd)
            else:
                e, dd = enrich_contact(c)
                _save(c["id"], e, dd)
            director = (dd and dd.get("director")) or "-"
            print(f"[ok #{c['id']}] {c.get('name')}: лицо={director}, ИНН={(dd or {}).get('inn', '-')}")
        except Exception as ex:
            print(f"[fail #{c['id']}] {c.get('name')}: {ex}")
    print("готово")


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM агент-обогатитель")
    p.add_argument("--id", type=int, default=None, help="обогатить один контакт по id")
    p.add_argument("--tag", default=None, help="фильтр аудитории по тегу (для пачки)")
    p.add_argument("--limit", type=int, default=20, help="сколько контактов взять в пачку")
    p.add_argument("--no-llm", action="store_true", help="только DaData (директор/ИНН), без Claude")
    p.add_argument("--batch", action="store_true", help="через Batch API (−50%% к цене, асинхронно)")
    p.add_argument("--hooks", action="store_true", help="дозалить хуки тем, у кого директор есть, а зацепки нет")
    p.add_argument("--hooks-all", dest="hooks_all", action="store_true",
                   help="дозалить хуки ВСЕМ без зацепки (в т.ч. без ФИО директора)")
    args = p.parse_args()
    if not args.no_llm and not llm.available(config.MODEL):
        print(f"Нет ключа под модель «{config.MODEL}» — проверь .env")
        return
    run(args.id, args.tag, args.limit, args.no_llm, args.batch, args.hooks, args.hooks_all)


if __name__ == "__main__":
    main()
