"""Отправка первого сообщения по кампании (антибан-лимиты, человеческий темп).

Запускается веб-пультом в отдельном процессе:

    python -m channels.campaign_send <campaign_id> --limit N

Берёт аудиторию кампании (status='new', подходящий канал, фильтр по тегу),
шлёт первое сообщение из шаблона кампании ({name} подставляется), соблюдает
дневной лимит и паузы, пишет в книжку и в campaign_contacts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
from datetime import datetime, timedelta

from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import AddContactRequest
from telethon.tl.types import InputPhoneContact

import config
from db import database
from channels import opener_lint
from channels.telegram import (
    _build_client, build_client, _send_parts, _resolve_entity, OUTREACH_PAUSE,
)
from channels.warmup import _setup_profile
from channels.antiban import classify_error


class OpenerIsPromptError(RuntimeError):
    """В шаблоне первого сообщения лежит промпт/инструкция, а не текст письма.
    Раз каждая строка шаблона уходит человеку отдельным сообщением (см. _parts),
    такой «опенер» нельзя слать вообще ни по одному каналу."""

# Пауза перед СЛЕДУЮЩЕЙ строкой опенера (не портянка, ждём — вдруг человек уже ответил).
# Если за это время статус контакта ушёл от 'messaged' (ответил/потерян) — остаток не шлём,
# см. channels/opener_queue.py.
OPENER_NEXT_LINE_MIN = (1 * 60, 3 * 60)  # секунды: 1–3 минуты (живой темп переписки)


def _load_campaign(cid: int) -> dict | None:
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    return dict(row) if row else None


def _channels(channel: str | None) -> list[str]:
    return [c.strip() for c in (channel or "").split(",") if c.strip()]


def _audience(cid: int, tag: str | None, channel: str, cap: int, test: bool = False,
              exclude_paused: bool = True, verified_only: bool | None = None):
    """Аудитория для TG-отправки: контакты со status='new', достижимые по Telegram.
    test=True — ТОЛЬКО тестовые (is_test=1): «кнопка Тест» шлёт исключительно на свои
    номера, боевой аудитории коснуться не может даже при большом лимите.
    Контакты, поставленные на паузу ИМЕННО в этой кампании (campaign_paused_contacts),
    пропускаем при РЕАЛЬНОЙ отправке (exclude_paused=True) — частичная пауза без
    остановки всей рассылки. Но при проверке «аудитория исчерпана ли» пауза не в счёт
    (exclude_paused=False) — поставленные на паузу контакты НЕ ушли навсегда, кампания
    не должна помечаться done только из-за того, что все оставшиеся сейчас на паузе.

    verified_only — брать только тех, кого достанем БЕЗ ImportContacts (@username или
    уже пробитый tg_user_id). None = взять флаг campaigns.tg_verified_only. Зачем: у
    непробитого номера отправка резолвит его прямо в момент выстрела, и если номера в
    Telegram нет — это промах. Промахов у нас 38% от проверенных, а серия промахов
    подряд с одного аккаунта — самый явный признак спамера. Пробив делает отдельный
    дозированный phone_resolve (25/аккаунт в сутки, контакт удаляется сразу)."""
    where = "status='new' AND (username IS NOT NULL OR phone IS NOT NULL)"
    params: list = []
    if exclude_paused:
        where += " AND id NOT IN (SELECT contact_id FROM campaign_paused_contacts WHERE campaign_id=?)"
        params.append(cid)
    # Этот отправщик шлёт через Telegram, поэтому берём контакты с доступным TG.
    if "telegram" in _channels(channel):
        where += " AND has_tg IN ('yes','unknown')"
        if verified_only is None:
            with database.get_conn() as conn:
                row = conn.execute(
                    "SELECT COALESCE(tg_verified_only,1) v FROM campaigns WHERE id=?", (cid,)
                ).fetchone()
            verified_only = bool(row["v"]) if row else True
        if verified_only:
            where += " AND " + database.TG_REACHABLE_SQL
    if test:
        where += " AND COALESCE(is_test,0)=1"
    # Тестовый номер принадлежит ТОЙ кампании, куда его добавили. Иначе так: свой номер,
    # уже бывший реальным лидом кампании A, добавляют в тест кампании B — ему дописывают
    # тег B и сбрасывают status в 'new'. Тег A при этом никуда не делся, и контакт снова
    # проходит в аудиторию A, да ещё первым (is_test DESC) — живой лид получает повторное
    # первое сообщение. NULL — старые записи, для них поведение как раньше.
    where += " AND (COALESCE(is_test,0)=0 OR test_campaign_id IS NULL OR test_campaign_id=?)"
    params.append(cid)
    if tag:
        where += " AND tags LIKE ?"
        params.append(f"%{tag}%")
    with database.get_conn() as conn:
        # свои тестовые номера (is_test=1) — всегда первыми в очереди: с малым лимитом
        # (напр. 2) заход бьёт именно по ним, а следующий (боевой) заход сам продолжит
        # реальной аудиторией — тестовые уже 'messaged' и не задвоятся.
        return conn.execute(
            f"SELECT * FROM contacts WHERE {where} ORDER BY COALESCE(is_test,0) DESC, id LIMIT ?",
            (*params, cap),
        ).fetchall()


def _spin(text: str) -> str:
    """Спинтакс-рандомизация: {вариант1|вариант2|…} → случайный вариант на каждую отправку.
    {name}/{agency} не трогаем — там нет «|»."""
    import re
    # без .strip() — значащие пробелы в вариантах (напр. «{слушайте, |}») сохраняем
    return re.sub(r"\{([^{}|]*\|[^{}]*)\}",
                  lambda m: random.choice(m.group(1).split("|")), text)


def _humanize(line: str) -> str:
    """Лёгкая «человечность» строки (антибан, не палить ИИ):
    у коротких реплик в личке люди не ставят точку в конце — иногда убираем её.
    Вопрос/восклицание/смайл не трогаем. Текст не корёжим (опечатки в B2B вредят)."""
    s = line.strip()
    if len(s) <= 90 and s.endswith(".") and not s.endswith("..") and random.random() < 0.7:
        s = s[:-1].rstrip()
    return s


def _time_greeting() -> str:
    """{greeting}/{приветствие}: «добрый день» в 20:30 читается ботом — время суток
    берём по MEETING_TZ (тот же часовой пояс, что и у встреч/созвонов)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hour = datetime.now(ZoneInfo(config.MEETING_TZ)).hour
    if 5 <= hour < 12:
        return "доброе утро"
    if 12 <= hour < 18:
        return "добрый день"
    if 18 <= hour < 23:
        return "добрый вечер"
    return "доброй ночи"


def _parts(template: str | None, name: str, agency: str = "", decision: str = "",
           sender: str = "", strict: bool = True) -> list[str]:
    """Шаблон → список сообщений. Каждая непустая строка — отдельное сообщение.
    {name}/{имя} — обращение (ФИО директора, если известно), {agency}/{агентство} —
    название агентства, {decision} — «с Романом Анатольевичем» (если ФИО известно)
    либо «с тем, кто у вас отвечает за развитие бизнеса» (мягкий обход секретаря,
    без давления на первого встречного, если ЛПР ещё не выявлен).
    {sender}/{от_кого} — имя ТОГО АККАУНТА, с которого реально уходит сообщение:
    команда кампании ротирует несколько аккаунтов, и зашитое в текст «меня зовут
    Александр» с аккаунта «Наталья Соколова» палит связку с первой же строки.
    {greeting}/{приветствие} — «добрый день/вечер/утро» по факту времени отправки,
    а не зашитое статично в шаблоне (иначе «добрый день» уходит и в 20:30).
    {a|b|c} — синонимизация (случайный вариант на каждый контакт, антибан).
    Плюс лёгкая человечность (см. _humanize).

    strict=True (по умолчанию) — перед рендером проверяем, что в поле лежит текст,
    а не промпт (opener_lint), и при грубых признаках промпта кидаем
    OpenerIsPromptError. Так «# Формат вывода первого сообщения» не уйдёт людям ни
    из рассылки, ни из прогрева, ни из WA-моста. strict=False — только рендер
    (предпросмотр и проверка «шаблон вообще не пустой»)."""
    if strict:
        problems = opener_lint.lint(template)
        if opener_lint.severe(problems):
            raise OpenerIsPromptError(opener_lint.blocking_message(problems))
    import re
    ag = agency or name or ""
    greet = _time_greeting()
    values = {"name": name or "", "имя": name or "", "agency": ag, "агентство": ag,
              "decision": decision or "", "sender": sender or "", "от_кого": sender or "",
              "greeting": greet, "приветствие": greet}
    text = _spin(template or "")
    # Регистр и пробелы внутри скобок оператору не видны: «{ИМЯ}», «{Name}», «{ name }»
    # раньше молча уезжали получателю фигурными скобками. Подставляем как есть.
    text = re.sub(r"\{\s*([^{}|]+?)\s*\}",
                  lambda m: values.get(m.group(1).lower(), m.group(0)), text)
    return [_humanize(ln) for ln in text.splitlines() if ln.strip()]


def _sender_name(acc: dict | None) -> str:
    """Имя для {sender}: полное «Имя Фамилия», как аккаунт представляется в переписке
    (tg_name), иначе метка. tg_name отдаём ПОЛНОСТЬЮ — формальное представление в
    первом сообщении («Меня зовут Василий Аксёнов») звучит увереннее, чем голое имя.

    label — ВНУТРЕННИЙ ярлык вида «Василий928» (имя + последние цифры номера, см.
    ru_names.make_label) — цифры там нужны нам для узнавания в таблице, но в текст
    получателю они уходить не должны («Меня зовут Василий928» — живой человек так не
    представляется, выдаёт автоматику с полпинка). Если tg_name не заполнен (типичный
    случай для «родных» личных номеров — упаковку личности сама автоматика на них не
    гоняет), отрезаем цифровой хвост от label и отдаём только то, что осталось (обычно
    голое имя — фамилии там взяться неоткуда)."""
    if not acc:
        return ""
    raw = (acc.get("tg_name") or "").strip()
    if raw:
        return raw
    import re
    return re.sub(r"\d+$", "", (acc.get("label") or "")).strip()


def _greeting(row) -> str:
    """Обращение для {name}: из ФИО директора → «Имя Отчество», иначе имя/название."""
    pn = (row["person_name"] or "").strip()
    if pn:
        parts = pn.split()
        if len(parts) == 3:  # Фамилия Имя Отчество → Имя Отчество (вежливо, по-деловому)
            return f"{parts[1]} {parts[2]}"
        return pn
    return (row["name"] or "").strip()


def _decision_phrase(row) -> str:
    """{decision}: если ФИО директора известно — «с Романом Анатольевичем», иначе
    нейтральный обход секретаря — «с тем, кто у вас отвечает за развитие бизнеса»."""
    pn = (row["person_name"] or "").strip()
    if pn:
        parts = pn.split()
        who = f"{parts[1]} {parts[2]}" if len(parts) == 3 else pn
        return f"с {who}"
    return "с тем, кто у вас отвечает за развитие бизнеса"


def _add_tag(raw: str | None, tag: str) -> str:
    tags = [t.strip() for t in (raw or "").split(",") if t.strip()]
    if tag not in tags:
        tags.append(tag)
    return ",".join(tags)


def _team(cid: int) -> list[dict]:
    """Аккаунты кампании с ЖИВОЙ сессией (для мультиаккаунт-рассылки).
    Берём из campaign_accounts, исключаем забаненных и без сессии. Лимит на аккаунт —
    из campaign_accounts.daily_limit (если задан), иначе из accounts.daily_limit."""
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT a.id, a.label, a.username, a.phone, a.tg_session, a.proxy, "
            "a.api_id, a.api_hash, a.description, a.avatar, a.status, a.tg_name, "
            "COALESCE(ca.daily_limit, a.daily_limit) AS cap "
            "FROM accounts a JOIN campaign_accounts ca ON ca.account_id = a.id "
            "WHERE ca.campaign_id = ? AND a.status <> 'banned' "
            "AND a.tg_session IS NOT NULL AND a.tg_session <> '' "
            "ORDER BY a.id",
            (cid,),
        ).fetchall()
    return [dict(r) for r in rows]


def _pick(live: list[dict], rr: int) -> dict | None:
    """Следующий отправитель в ротации среди тех, у кого осталась квота."""
    avail = [s for s in live if s["remaining"] > 0]
    if not avail:
        return None
    return avail[rr % len(avail)]


async def run(cid: int, limit: int, test: bool = False) -> None:
    camp = _load_campaign(cid)
    if not camp:
        print(f"кампания #{cid} не найдена")
        return
    chans = _channels(camp["channel"])
    if "telegram" not in chans:
        print(f"канал '{camp['channel']}': отправка через WhatsApp пока не подключена "
              f"(Baileys-мост). Сейчас этот отправщик шлёт только Telegram.")
        return
    if "whatsapp" in chans:
        print("режим мультиканала: TG-достижимым шлём сейчас; WhatsApp-only контакты "
              "дождутся подключения WA-моста.")
    cap = min(limit, camp["daily_limit"] or limit)
    rows = _audience(cid, camp["audience_tag"], camp["channel"], cap, test=test)
    if not rows:
        print("тест: нет тест-контактов (is_test=1) в аудитории" if test
              else "аудитория пуста — некому слать")
        return
    if test:
        print(f"[ТЕСТ] шлём только на свои номера (is_test=1): {len(rows)} шт.")
    if not _parts(camp["message_template"], "", strict=False):
        print("пустой шаблон сообщения — нечего слать")
        return
    # ГЕЙТ «это промпт, а не письмо». Первое касание не генерит модель: шаблон режется
    # по строкам и каждая строка уходит человеку отдельным сообщением. Если оператор
    # (или копайлот визарда) оставил в поле инструкцию — «# Формат вывода первого
    # сообщения», «ВАЖНО:», «- каждая новая строка = отдельное сообщение» — она уйдёт
    # как 15 сообщений подряд. Ловим ДО подключения аккаунтов, кампанию ставим на
    # паузу и пишем в колокольчик, чтобы не тикало молча.
    problems = opener_lint.lint(camp["message_template"])
    if opener_lint.severe(problems):
        msg = opener_lint.blocking_message(problems)
        print(msg)
        with database.get_conn() as conn:
            conn.execute("UPDATE campaigns SET status='paused' WHERE id=?", (cid,))
            database.add_event(conn, "campaign_blocked",
                               f"🛑 Кампания «{camp['name']}»: в первом сообщении промпт, не текст",
                               msg, level="bad", campaign_id=cid)
        return
    if problems:  # мягкие замечания: слать можно, но оператор должен знать
        print("предупреждения по шаблону:\n" + opener_lint.report(problems))

    # Команда кампании (мультиаккаунт). Если команда не задана/без сессий —
    # откатываемся на основной аккаунт из .env (старое поведение, ничего не ломаем).
    team = _team(cid)
    senders: list[dict] = []
    if team:
        # «Основной» (⭐, campaigns.account_id) — первый в очереди ротации: ему
        # достаются контакты раньше остальных, пока не кончится его дневной лимит.
        main_id = camp.get("account_id")
        if main_id:
            team = sorted(team, key=lambda a: 0 if str(a["id"]) == str(main_id) else 1)
        for acc in team:
            label = acc["label"] or acc["username"] or acc["phone"] or f"#{acc['id']}"
            senders.append({
                "id": acc["id"], "acc": acc, "label": label,
                "client": build_client(StringSession(acc["tg_session"]), acc["proxy"],
                                       acc.get("api_id"), acc.get("api_hash")),
                "remaining": max(0, int(acc["cap"] or cap)),
            })
    else:
        senders.append({
            "id": camp.get("account_id"), "acc": None, "label": "основной (.env)",
            "client": _build_client(), "remaining": cap,
        })

    # Подключаем отправителей: старт сессии + оформление профиля (фото/bio, если пусто).
    # Антибан-правило: холодную шлём ТОЛЬКО с прогретых (status='active'). Непрогретые
    # (warming/paused) пропускаем — иначе свежий аккаунт сгорит на первой же рассылке.
    live: list[dict] = []
    skipped_warm: list[str] = []
    needs_sender = any(v in (camp["message_template"] or "") for v in ("{sender}", "{от_кого}"))
    for s in senders:
        acc = s["acc"]
        # В тест-режиме гейт прогрева НЕ применяем: тест уходит только на свои номера
        # (is_test), бана быть не может, а проверить скрипт надо ДО окончания прогрева.
        if not test and acc and acc.get("status") != "active":
            skipped_warm.append(f"{s['label']} ({acc.get('status')})")
            print(f"[{s['label']}] ⏳ пропуск: не прогрет (статус {acc.get('status')}). "
                  f"Холодную шлём только с 'active' — заверши прогрев или переведи в 'active' вручную.")
            continue
        try:
            await s["client"].start()
            if s["acc"]:
                try:
                    await _setup_profile(s["client"], s["acc"])
                except Exception as e:  # оформление не критично для отправки
                    print(f"[{s['label']}] профиль: {e}")
            me = await s["client"].get_me()
            # Шаблон представляется от имени аккаунта, а имени у аккаунта нет —
            # ушло бы «меня зовут ,». Такой отправитель в заход не идёт.
            if needs_sender and not _sender_name(s["acc"]):
                print(f"[{s['label']}] ⏭ пропуск: в шаблоне есть {{sender}}, а у аккаунта "
                      f"не заполнено имя (tg_name/label) — представиться нечем")
                await s["client"].disconnect()
                continue
            print(f"[{s['label']}] готов: @{me.username or me.id}, квота {s['remaining']}")
            live.append(s)
        except Exception as e:
            print(f"[{s['label']}] не удалось подключить (сессия/прокси): {e}")
    if not live:
        if skipped_warm:
            print(f"нет ПРОГРЕТЫХ (active) аккаунтов: {', '.join(skipped_warm)} ещё в прогреве. "
                  f"Холодную с непрогретых не шлём (антибан). Дождись окончания прогрева "
                  f"или вручную переведи аккаунт в статус 'active'.")
        else:
            print("нет живых аккаунтов-отправителей — проверь сессии и прокси команды")
        return

    tag = f"кампания #{cid}"
    print(f"кампания #{cid} «{camp['name']}»: отправителей {len(live)}, всего до {cap} контактов")

    sent = 0
    rr = 0
    for row in rows:
        if sent >= cap:
            break
        s = _pick(live, rr)
        if s is None:
            print("дневные квоты всех аккаунтов исчерпаны — стоп до следующего захода")
            break
        rr += 1
        # ЗАЩИТА ОТ ДУБЛЯ (атомарный захват). Тот же номер мог попасть в этот заход
        # дважды: параллельный запуск, тест сбросил статус в 'new' и боевой заход
        # догнал, гонка между процессами. Атомарно «забираем» контакт: переводим
        # 'new'→'messaged' ОДНИМ UPDATE с условием status='new'. Кто выиграл гонку —
        # у того rowcount=1, он и шлёт; проигравшему вернётся 0, и он пропускает.
        # Так один человек физически не получит два первых сообщения.
        with database.get_conn() as conn:
            claimed = conn.execute(
                "UPDATE contacts SET status='messaged', updated_at=datetime('now') "
                "WHERE id=? AND status='new'", (row["id"],)
            ).rowcount
        if not claimed:
            print(f"[skip] contact {row['id']}: уже занят другим заходом/аккаунтом — дубль не шлём")
            continue
        # обращение: из ФИО директора берём «Имя Отчество», иначе имя/название агентства
        name = _greeting(row)
        # sender — имя ИМЕННО того аккаунта, что сейчас шлёт (ротация команды):
        # «меня зовут {sender}» вместо зашитого в текст чужого имени.
        parts = _parts(camp["message_template"], name, row["agency"] or row["name"],
                       _decision_phrase(row), sender=_sender_name(s["acc"]))
        try:
            entity = await _resolve_entity(s["client"], row)
            # антибан: добавить контакт в книжку перед первым сообщением
            try:
                await s["client"](AddContactRequest(
                    add_phone_privacy_exception=False,
                    add_contact=[InputPhoneContact(
                        client_id=row["id"],
                        phone=row.get("phone") or "",
                        first_name=name.split()[0] if name.split() else name,
                        last_name=" ".join(name.split()[1:]) if len(name.split()) > 1 else "",
                    )]
                ))
            except Exception:
                pass  # не критично — книжка не блокирует отправку
            # только первая строка — без «портянки»; но очередь остатка (opener_queue) привязана
            # к реальному accounts.id, поэтому у «основного (.env)»-отправителя (id=None) шлём
            # опенер целиком сразу — очередь на потом ставить некому.
            await _send_parts(s["client"], entity, parts if s["id"] is None else parts[:1])
        except FloodWaitError as e:
            hrs = round(e.seconds / 3600, 1)
            print(f"[{s['label']}] floodwait {e.seconds}с (~{hrs}ч) — вывожу из ротации на этот заход")
            # отправки НЕ было — возвращаем контакт в 'new', достанется другому заходу
            with database.get_conn() as conn:
                conn.execute("UPDATE contacts SET status='new' WHERE id=? AND status='messaged'", (row["id"],))
                database.add_event(conn, "ban", f"⏳ Флуд-лимит: «{s['label']}»",
                                   f"Telegram запретил отправку на ~{hrs}ч (FloodWait). Холодных ЛС с этого "
                                   f"аккаунта пока слишком много — нужен прогрев и медленнее темп.",
                                   level="warn", campaign_id=cid, account_id=s["id"])
            s["remaining"] = 0
            continue
        except Exception as e:
            cat = classify_error(e)
            if cat == "ban":
                # НОМЕР мёртв/деактивирован Telegram'ом: помечаем banned и выводим из работы.
                # контакт НЕ теряем — возвращаем в 'new', достанется живому аккаунту.
                print(f"[{s['label']}] ⛔ аккаунт забанен/деактивирован ({e}) — статус banned, из ротации")
                with database.get_conn() as conn:
                    conn.execute("UPDATE contacts SET status='new' WHERE id=? AND status='messaged'", (row["id"],))
                    if s["id"]:
                        conn.execute("UPDATE accounts SET status='banned' WHERE id=?", (s["id"],))
                        database.add_event(conn, "account_banned", f"⛔ Аккаунт «{s['label']}» забанен",
                                           f"Telegram: {e}", level="bad", campaign_id=cid, account_id=s["id"])
                s["remaining"] = 0
                continue
            if cat == "session_revoked":
                # НОМЕР жив, отозвана только эта сессия (часто — конфликт наших же
                # параллельных подключений). НЕ banned — просто session_alive=0,
                # лечится перелогином через 🔌 Подключить.
                print(f"[{s['label']}] 🔴 сессия отозвана ({e}) — нужен перелогин, из ротации")
                with database.get_conn() as conn:
                    conn.execute("UPDATE contacts SET status='new' WHERE id=? AND status='messaged'", (row["id"],))
                    if s["id"]:
                        conn.execute(
                            "UPDATE accounts SET session_alive=0, session_state='revoked', "
                            "session_reason=? WHERE id=?", (str(e)[:200], s["id"]))
                        database.add_event(conn, "ban", f"🔴 Сессия отозвана: «{s['label']}»",
                                           f"номер жив — переподключи (🔌 Подключить). {e}",
                                           level="warn", campaign_id=cid, account_id=s["id"])
                s["remaining"] = 0
                continue
            if cat == "spam":
                # PeerFlood: слишком много ЛС незнакомцам → пауза аккаунта на этот заход
                # отправки не было — контакт обратно в 'new'
                print(f"[{s['label']}] ⚠ PeerFlood (много ЛС незнакомцам) — пауза аккаунта на заход")
                with database.get_conn() as conn:
                    conn.execute("UPDATE contacts SET status='new' WHERE id=? AND status='messaged'", (row["id"],))
                s["remaining"] = 0
                continue
            if cat == "blocked":
                # Контакт заблокировал ЭТОТ аккаунт — не общий "lost", а отдельный
                # статус: в CRM сразу видно причину, а не гадать по логам.
                print(f"[{s['label']}] 🚫 контакт {row['id']} заблокировал аккаунт")
                with database.get_conn() as conn:
                    database.set_status(conn, row["id"], "blocked")
                continue
            print(f"[skip] contact {row['id']} ({s['label']}): {e}")
            with database.get_conn() as conn:
                database.set_status(conn, row["id"], "lost")
            continue

        rest = parts[1:]   # остальные строки опенера — не портянкой, а с паузой (см. opener_queue)
        with database.get_conn() as conn:
            database.set_tg_user_id(conn, row["id"], int(entity.id))
            database.add_message(conn, row["id"], "out", parts[0], intent=None,
                                 account_id=s["id"])
            database.set_status(conn, row["id"], "messaged")
            conn.execute("UPDATE contacts SET tags=? WHERE id=?", (_add_tag(row["tags"], tag), row["id"]))
            conn.execute(
                "INSERT OR IGNORE INTO campaign_contacts (campaign_id, contact_id, account_id) VALUES (?,?,?)",
                (cid, row["id"], s["id"]),
            )
            if rest and s["id"] is not None:  # очередь возможна только у реального accounts.id
                next_at = (datetime.utcnow()
                           + timedelta(seconds=random.uniform(*OPENER_NEXT_LINE_MIN))).isoformat(sep=" ", timespec="seconds")
                conn.execute(
                    "INSERT INTO opener_queue (contact_id, account_id, campaign_id, parts_json, next_at) "
                    "VALUES (?,?,?,?,?)",
                    (row["id"], s["id"], cid, json.dumps(rest, ensure_ascii=False), next_at),
                )
            # В ленту: кому и ЧТО именно ушло. Раньше отправка жила только в campaign_logs,
            # и в колокольчике нельзя было увидеть текст первого сообщения.
            to = name or (f"@{row['username']}" if row["username"] else row["phone"])
            database.add_event(
                conn, "outreach", f"📨 {s['label']} → {to}",
                parts[0][:400] + (f" (+{len(rest)} строк(и) следом)" if rest else ""),
                contact_id=row["id"], campaign_id=cid, account_id=s["id"])
        s["remaining"] -= 1
        sent += 1
        print(f"[sent {sent}/{cap}] {s['label']} -> {name or row['username'] or row['phone']}"
              + (f" (+{len(rest)} строк(и) следом, если не ответит)" if rest and s["id"] is not None else ""))
        if sent < cap:
            # темп делим на число аккаунтов (пропускная выше), но каждый аккаунт
            # всё равно паузит между своими сообщениями; не меньше 2 сек.
            await asyncio.sleep(max(2.0, random.uniform(*OUTREACH_PAUSE) / len(live)))

    # Если в аудитории больше никого не осталось — кампания отработана. Пауза не в счёт:
    # контакты на паузе ещё вернутся, из-за них одних "done" ставить нельзя.
    remaining = _audience(cid, camp["audience_tag"], camp["channel"], 1, exclude_paused=False)
    with database.get_conn() as conn:
        done = not remaining
        conn.execute(
            "UPDATE campaigns SET status=? WHERE id=?",
            ("done" if done else "running", cid),
        )
        if done:
            database.add_event(conn, "campaign_done", f"✅ Кампания «{camp['name']}» отработана",
                               f"аудитория исчерпана, в этот заход отправлено {sent}",
                               level="good", campaign_id=cid)
        elif sent == 0:
            database.add_event(conn, "info", f"⚠️ Кампания «{camp['name']}»: отправлено 0",
                               "ни одного не ушло — частая причина: флуд-лимит/спам-блок или "
                               "нет живого аккаунта с прокси. Проверь 🚦 Готовность и колокольчик.",
                               level="warn", campaign_id=cid)
    accs = ", ".join(s["label"] for s in live)
    print(f"кампания #{cid}: отправлено {sent} (аккаунты: {accs})")
    for s in live:
        try:
            await s["client"].disconnect()
        except Exception:
            pass


def main() -> None:
    p = argparse.ArgumentParser(description="Отправка кампании AXIOM")
    p.add_argument("cid", type=int, help="id кампании")
    p.add_argument("--limit", type=int, default=3, help="сколько контактов взять в этот заход")
    p.add_argument("--test", action="store_true",
                   help="тест-режим: слать ТОЛЬКО на свои номера (is_test=1), в обход гейта прогрева")
    args = p.parse_args()
    asyncio.run(run(args.cid, args.limit, test=args.test))


if __name__ == "__main__":
    main()
