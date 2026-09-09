"""Уведомление владельца в личку Telegram, когда агент договорился о встрече.

ЗАЧЕМ. Встреча и так видна в колокольчике пульта, но туда нужно зайти и посмотреть.
Гораздо надёжнее долетает то, что упало прямо в личку Telegram — там оператор и так
весь день. Шлёт один из аккаунтов (обычно «родной» личный — назначается в пульте),
обычным сообщением через Telethon, а не через бота — выглядит как обычная личка,
не служебное уведомление.

Настройка — пульт → Аккаунты → «уведомляет о встречах» (кому назначить отправителем)
+ поле «получатель уведомлений» (телефон/@ник, куда слать). Хранится в app_settings
под ключами notify_sender_account_id / notify_owner_target. Не настроено — молча
пропускаем: в отличие от listen_account_id здесь нет безопасного отката (слать
уведомление больше некому и незачем гадать получателя).
"""
from __future__ import annotations

import config
from channels.telegram import client_for_account
from db import database

NOTIFY_SENDER_SETTING = "notify_sender_account_id"
NOTIFY_TARGET_SETTING = "notify_owner_target"


def _chat_link(contact_id: int) -> str:
    """Ссылка на РЕАЛЬНУЮ переписку (#chats/{id} → openThread), а не на карточку CRM
    (#dossier). Раньше уведомление вело в досье, а там нужно было ещё сообразить
    нажать «переписка» — оператору важнее всего сразу увидеть, о чём говорили."""
    base = (config.PUBLIC_URL or "").rstrip("/")
    return f"{base}/#chats/{contact_id}" if base else f"#chats/{contact_id}"


def _phone_link(phone: str | None) -> str | None:
    """Номер как кликабельная ссылка Telegram: https://t.me/+79137876067 — открывает
    диалог с этим номером сразу, без ручного набора/копирования в поиск."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"https://t.me/+{digits}" if digits else None


def _build_text(row, meeting_at: str | None, notes: str | None, zoom_link: str | None) -> str:
    who = (row["person_name"] or row["name"] or "контакт").strip()
    spec = (row["specialization"] or row["niche"] or "").strip()
    uname = f"@{row['username']}" if row["username"] else "без ника в TG"
    lines = [f"📅 Договорились о встрече: {who}"]
    if spec:
        lines.append(f"Чем занимается: {spec}")
    lines.append(f"TG: {uname}")
    phone_link = _phone_link(row["phone"])
    if phone_link:
        lines.append(f"Номер: {phone_link}")
    if meeting_at:
        lines.append(f"Время: {meeting_at}")
    if zoom_link:
        lines.append(f"Ссылка: {zoom_link}")
    if notes and notes.strip():
        lines.append(f"О чём говорили: {notes.strip()[:300]}")
    lines.append(_chat_link(row["id"]))
    return "\n".join(lines)


def _owner_route(conn, campaign_id: int | None) -> tuple[str | None, str | None]:
    """(sender_id, target) — сначала настройки САМОЙ кампании: разные кампании ведут
    разные люди, уведомление должно падать тому, кто эту встречу/лида ведёт. Пусто в
    кампании → общие настройки пульта. Добираем по отдельности: в кампании может быть
    задан только один из двух, терять из-за этого второй нельзя."""
    sender_id = target = None
    if campaign_id:
        c = conn.execute("SELECT notify_account_id, notify_target FROM campaigns "
                         "WHERE id=?", (campaign_id,)).fetchone()
        if c:
            sender_id = c["notify_account_id"]
            target = (c["notify_target"] or "").strip() or None
    sender_id = sender_id or database.get_setting(conn, NOTIFY_SENDER_SETTING)
    target = target or database.get_setting(conn, NOTIFY_TARGET_SETTING)
    return sender_id, target


def _targets(raw: str | None) -> list[str]:
    """Строка получателей → список. Разделители: запятая, точка с запятой, перенос.

    Получателей теперь МОЖЕТ БЫТЬ НЕСКОЛЬКО (два телефона, @ник и телефон и т.д.):
    один Telegram-аккаунт оператора может оказаться недоступен, а уведомление о
    согласии на встречу пропускать нельзя — цена промаха это пропущенная живая
    встреча."""
    if not raw:
        return []
    parts = [p.strip() for p in str(raw).replace(";", ",").replace("\n", ",").split(",")]
    return [p for p in parts if p]


def _sender_candidates(conn, preferred_id) -> list[int]:
    """Кем слать: сначала назначенный аккаунт, следом — РЕЗЕРВ из живых.

    ЗАЧЕМ РЕЗЕРВ. Отправитель был ровно один, и когда он умер (Василий610 сгорел с
    AuthKeyDuplicatedError), уведомления перестали доходить МОЛЧА — в коде только
    строчка в лог, который никто не читает. 24.08.2026 из-за этого пропущена живая
    встреча с Олегом Дьяконовым: агент договорился, а оператор не узнал.

    Резерв — любые живые аккаунты; «родные» (protected) в приоритете, потому что
    уведомление в личку владельцу с личного номера выглядит естественно, а не как
    служебная рассылка с рабочего."""
    out: list[int] = []
    if preferred_id:
        try:
            out.append(int(preferred_id))
        except (TypeError, ValueError):
            pass
    rows = conn.execute(
        "SELECT id FROM accounts WHERE session_alive=1 AND tg_session IS NOT NULL "
        "AND tg_session<>'' ORDER BY COALESCE(protected,0) DESC, id"
    ).fetchall()
    for r in rows:
        if r["id"] not in out:
            out.append(r["id"])
    return out


async def _send_to_owner(sender_id, target: str, text: str, what: str, contact_id: int) -> None:
    """Доставка уведомления: перебираем отправителей, пока кто-то не отправит, и шлём
    ВСЕМ получателям. Успехом считается хотя бы одна доставка.

    Раньше здесь была ровно одна попытка одним аккаунтом: не авторизован — тихий
    выход. Теперь мёртвый отправитель просто уступает место следующему живому."""
    targets = _targets(target)
    if not targets:
        return
    with database.get_conn() as conn:
        senders = _sender_candidates(conn, sender_id)
    if not senders:
        print("[notify] нет ни одного живого аккаунта для отправки уведомления")
        return

    delivered: list[str] = []
    pending = list(targets)
    for acc_id in senders:
        if not pending:
            break
        try:
            client, _ = client_for_account(int(acc_id))
            await client.connect()
        except Exception as e:  # noqa: BLE001 — мёртвый/битый аккаунт: пробуем следующий
            print(f"[notify] аккаунт #{acc_id} не поднялся ({str(e)[:60]}) — беру следующий")
            continue
        try:
            if not await client.is_user_authorized():
                print(f"[notify] аккаунт #{acc_id} не авторизован — беру следующий")
                continue
            still: list[str] = []
            for t in pending:
                try:
                    await client.send_message(t, text)
                    delivered.append(f"{t} (акк #{acc_id})")
                except Exception as e:  # noqa: BLE001 — этот получатель не вышел, копим на ретрай
                    print(f"[notify] «{t}» с акк #{acc_id} не вышло: {str(e)[:70]}")
                    still.append(t)
            pending = still
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    if delivered:
        print(f"[notify] владелец уведомлён: {what} (контакт #{contact_id}) → {', '.join(delivered)}")
    if pending:
        # Не молчим: если не дошло НИКОМУ — это ровно тот случай, который стоил
        # пропущенной встречи. Кладём тревогу в колокольчик пульта.
        print(f"[notify] НЕ ДОСТАВЛЕНО ни одному получателю: {', '.join(pending)}")
        try:
            with database.get_conn() as conn:
                database.add_event(
                    conn, "warn",
                    "🔴 Уведомление владельцу не доставлено",
                    f"«{what}» по контакту #{contact_id} не ушло получателям: "
                    f"{', '.join(pending)}. Проверь живость аккаунтов-отправителей "
                    f"и поле «получатель уведомлений» в «Аккаунтах».",
                    level="bad", contact_id=contact_id)
        except Exception:  # noqa: BLE001 — тревога не должна ронять основной поток
            pass


async def notify_meeting(contact_id: int, meeting_at: str | None, notes: str | None,
                          zoom_link: str | None = None, campaign_id: int | None = None) -> None:
    """Лучшая попытка — сбой не должен ронять основной поток ответа агента, поэтому
    ничего не бросаем наружу, только логируем."""
    try:
        with database.get_conn() as conn:
            sender_id, target = _owner_route(conn, campaign_id)
            if not sender_id or not target:
                return  # не настроено — тихо пропускаем
            row = conn.execute(
                "SELECT id, name, person_name, username, phone, specialization, niche "
                "FROM contacts WHERE id=?", (contact_id,),
            ).fetchone()
        if not row:
            return
        text = _build_text(row, meeting_at, notes, zoom_link)
        await _send_to_owner(sender_id, target, text, "договорились о встрече", contact_id)
    except Exception as e:  # noqa: BLE001 — вспомогательное уведомление, не должно ронять диалог
        print(f"[notify] сбой отправки владельцу: {e}")


DAILY_REPORT_TARGET_SETTING = "daily_report_target"


async def send_daily_report() -> None:
    """Утренняя сводка по кампаниям в личку — название, всего в кампании, отправлено,
    ответило, выведено на КЭВ (созвон). Одним сообщением по всем активным кампаниям
    (архивные и черновики не в счёт — по ним отчитываться нечем)."""
    try:
        with database.get_conn() as conn:
            sender_id = database.get_setting(conn, NOTIFY_SENDER_SETTING)
            # Отдельная настройка получателя — по умолчанию @neiro_0001 (запрошено явно),
            # но не жёстко зашито: сменится получатель — правится тут же в app_settings.
            target = database.get_setting(conn, DAILY_REPORT_TARGET_SETTING, "@neiro_0001")
            if not sender_id or not target:
                print("[daily report] не настроен отправитель — см. пульт: «Аккаунты» → "
                      "«уведомляет о встречах»")
                return
            camps = conn.execute(
                "SELECT id, name, audience_tag, channel, tg_verified_only FROM campaigns "
                "WHERE archived=0 AND status != 'draft' ORDER BY id"
            ).fetchall()
            if not camps:
                return
            # Заголовок по времени суток: утром сводка отвечает на «что делать
            # сегодня», вечером — на «сработало ли». Один и тот же текст «на утро»
            # в 21:00 читался бы как вчерашний и сбивал бы с толку.
            from channels.antiban import msk_now
            hour = msk_now().hour
            head = ("📊 Сводка по кампаниям на утро" if hour < 15
                    else "📊 Итоги дня по кампаниям")
            lines = [head]
            for camp in camps:
                # Тот же расчёт, что у отчёта по одной кампании (кнопка «📤 Отчёт в ЛС»
                # и экран кампании) — не заводим второй, похожий, который потом
                # разъедется с первым.
                block = campaign_report_text(conn, camp["id"])
                if block:
                    lines.append("\n" + block.replace("📊 «", "🎯 «", 1))
            text = "\n".join(lines)
        await _send_to_owner(sender_id, target, text, "утренняя сводка", 0)
    except Exception as e:  # noqa: BLE001 — фоновый тик не должен ронять пульт
        print(f"[daily report] сбой: {e}")


def campaign_report_text(conn, cid: int) -> str | None:
    """Строка отчёта по ОДНОЙ кампании: работала ли сегодня/вчера, сколько отправлено
    за сегодня/неделю/всё время, ответы, КЭВ. Общая для показа на экране кампании
    (см. web/app.py: /api/campaign/{cid}/report) и для отправки в личку (см.
    send_campaign_report) — один расчёт, а не два похожих."""
    camp = conn.execute("SELECT id, name, audience_tag, channel FROM campaigns WHERE id=?",
                        (cid,)).fetchone()
    if not camp:
        return None
    camp_tag, camp_channel = camp["audience_tag"], camp["channel"]

    def sent_since(period_sql: str | None) -> int:
        where = "campaign_id=?" + (f" AND sent_at >= {period_sql}" if period_sql else "")
        return conn.execute(f"SELECT COUNT(*) c FROM campaign_contacts WHERE {where}", (cid,)).fetchone()["c"]

    total = sent_since(None)
    today = sent_since("date('now')")
    yesterday_only = conn.execute(
        "SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id=? "
        "AND sent_at >= date('now','-1 day') AND sent_at < date('now')", (cid,)
    ).fetchone()["c"]
    week = sent_since("date('now','-7 day')")

    def replied_since(period_sql: str | None) -> int:
        """Ответившие — по ПЕРВОМУ входящему в периоде, а не по любому.

        Иначе диалог, который человек начал неделю назад и продолжает отвечать
        сегодня, попадал бы в «ответили вчера» каждый день заново и завышал
        дневную цифру: за день интересны НОВЫЕ отклики, а не активность старых."""
        where = ""
        if period_sql:
            where = f" AND f.first_in >= {period_sql}"
        return conn.execute(
            "SELECT COUNT(*) c FROM ("
            "  SELECT m.contact_id, MIN(m.ts) first_in FROM messages m "
            "  JOIN campaign_contacts cc ON cc.contact_id=m.contact_id AND cc.campaign_id=? "
            "  WHERE m.direction='in' GROUP BY m.contact_id) f"
            f" WHERE 1=1{where}", (cid,)).fetchone()["c"]

    def kev_since(period_sql: str | None) -> int:
        """КЭВ за период — по дате ПОЯВЛЕНИЯ договорённости (created_at сделки), а не
        по meeting_at: встреча может стоять на следующей неделе, но договорились о
        ней вчера — в отчёт за вчера она и должна попасть."""
        where = f" AND d.created_at >= {period_sql}" if period_sql else ""
        return conn.execute(
            "SELECT COUNT(DISTINCT d.contact_id) c FROM deals d "
            "JOIN campaign_contacts cc ON cc.contact_id=d.contact_id AND cc.campaign_id=? "
            f"WHERE d.meeting_at IS NOT NULL{where}", (cid,)).fetchone()["c"]

    replied = replied_since(None)
    replied_today = replied_since("date('now')")
    replied_week = replied_since("date('now','-7 day')")
    replied_yest = conn.execute(
        "SELECT COUNT(*) c FROM ("
        "  SELECT m.contact_id, MIN(m.ts) first_in FROM messages m "
        "  JOIN campaign_contacts cc ON cc.contact_id=m.contact_id AND cc.campaign_id=? "
        "  WHERE m.direction='in' GROUP BY m.contact_id) f "
        "WHERE f.first_in >= date('now','-1 day') AND f.first_in < date('now')",
        (cid,)).fetchone()["c"]

    kev = kev_since(None)
    kev_today = kev_since("date('now')")
    kev_week = kev_since("date('now','-7 day')")
    kev_yest = conn.execute(
        "SELECT COUNT(DISTINCT d.contact_id) c FROM deals d "
        "JOIN campaign_contacts cc ON cc.contact_id=d.contact_id AND cc.campaign_id=? "
        "WHERE d.meeting_at IS NOT NULL AND d.created_at >= date('now','-1 day') "
        "AND d.created_at < date('now')", (cid,)).fetchone()["c"]

    # Дата старта — по ПЕРВОЙ фактической отправке, а не по campaigns.created_at:
    # кампанию заводят заранее, и «создана 17.07» при первой отправке 07.08 давало бы
    # неверный знаменатель в «сколько дней работаем». События campaign_start для этого
    # не годятся — лента чистится, а отправки в campaign_contacts лежат всегда.
    first_at = conn.execute(
        "SELECT MIN(sent_at) t FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchone()["t"]
    last_at = conn.execute(
        "SELECT MAX(sent_at) t FROM campaign_contacts WHERE campaign_id=?", (cid,)).fetchone()["t"]
    days = 0
    if first_at:
        d = conn.execute("SELECT CAST(julianday('now') - julianday(?) AS INT) d",
                         (first_at,)).fetchone()["d"]
        days = max(1, int(d or 0) + 1)          # день старта тоже считаем рабочим

    # Остаток базы — на сколько ещё хватит при текущем темпе.
    where_left, params_left = _audience_where_for_report(camp_tag, camp_channel)
    left = conn.execute(f"SELECT COUNT(*) c FROM contacts WHERE {where_left}",
                        params_left).fetchone()["c"]

    # «Работала сегодня» — СТРОГО про отправку. Раньше сюда приплюсовывались ответы,
    # и отчёт писал «работала: да» при нуле отправленных: входящее письмо от
    # вчерашнего контакта — это не работа рассылки, а её последствие.
    worked_today = "да" if today else "нет"
    worked_yesterday = "да" if yesterday_only else "нет"

    def _d(ts):
        return (ts or "")[:10] or "—"

    # Отчёт СГРУППИРОВАН ПО ПЕРИОДАМ (всё время / неделя / вчера), а не по метрикам.
    # Раньше метрики шли вперемешку: отправки одной строкой за три периода сразу, а
    # ответы и КЭВ — только «всего». На вопрос «как сработали вчера» отчёт не отвечал:
    # приходилось держать периоды в голове и вычитать одно из другого. Внутри каждого
    # блока порядок один и тот же: отправлено → ответили → КЭВ, чтобы взгляд не искал.
    def _pct(part, whole):
        return f" ({part * 100 // whole}%)" if whole else ""

    def undelivered_since(period_sql: str | None) -> int:
        """Сколько исходящих ЗАПИСАНО, но Telegram их не принял.

        Признак — пустой tg_msg_id: на подтверждённой отправке туда ложатся id
        сообщений, вернувшиеся от Telegram. 09.09.2026 три дожима ушли с мёртвой
        сессии, легли в базу как отправленные и попали в «отправлено», хотя ни один
        собеседник их не получил — расхождение нашлось только ручной сверкой
        переписки. Причина закрыта (недоставленное больше не пишется), но СТАРЫЕ
        записи остаются, да и новая причина может появиться завтра: строка ниже
        показывает разрыв сразу, а не через сверку.

        Считаем только исходящие самой кампании и только те, где отправитель был
        известен, — входящие и ручные сообщения оператора сюда не относятся.
        """
        where = ""
        if period_sql:
            where = f" AND m.ts >= {period_sql}"
        return conn.execute(
            "SELECT COUNT(*) c FROM messages m "
            "JOIN campaign_contacts cc ON cc.contact_id=m.contact_id AND cc.campaign_id=? "
            "WHERE m.direction='out' AND COALESCE(m.tg_msg_id,'')=''"
            f"{where}", (cid,)).fetchone()["c"]

    undeliv_all = undelivered_since(None)
    undeliv_week = undelivered_since("date('now','-7 day')")
    undeliv_today = undelivered_since("date('now')")
    undeliv_yest = conn.execute(
        "SELECT COUNT(*) c FROM messages m "
        "JOIN campaign_contacts cc ON cc.contact_id=m.contact_id AND cc.campaign_id=? "
        "WHERE m.direction='out' AND COALESCE(m.tg_msg_id,'')='' "
        "AND m.ts >= date('now','-1 day') AND m.ts < date('now')", (cid,)).fetchone()["c"]

    def _undeliv(n: int) -> str:
        return f"\n⚠️ НЕ ДОШЛО: {n} — записано, но Telegram не принял" if n else ""

    out = [f"📊 «{camp['name']}»"]
    out.append(f"запущена: {_d(first_at)} · в работе дней: {days}")
    work = f"работала сегодня: {worked_today} · вчера: {worked_yesterday}"
    if not today and last_at:
        work += f" · последняя отправка: {_d(last_at)}"
    out.append(work)
    out.append(f"осталось в базе: {left}")

    out.append("")
    out.append("📅 ЗА ВСЁ ВРЕМЯ")
    out.append(f"отправлено: {total}{_undeliv(undeliv_all)}")
    out.append(f"ответили: {replied}{_pct(replied, total)}")
    out.append(f"на КЭВ: {kev}{_pct(kev, replied)}")

    out.append("")
    out.append("🗓 ЗА НЕДЕЛЮ")
    out.append(f"отправлено: {week}{_undeliv(undeliv_week)}")
    out.append(f"ответили: {replied_week}{_pct(replied_week, week)}")
    out.append(f"на КЭВ: {kev_week}{_pct(kev_week, replied_week)}")

    out.append("")
    out.append("☀️ ЗА ВЧЕРА")
    out.append(f"отправлено: {yesterday_only}{_undeliv(undeliv_yest)}")
    out.append(f"ответили: {replied_yest}{_pct(replied_yest, yesterday_only)}")
    out.append(f"на КЭВ: {kev_yest}{_pct(kev_yest, replied_yest)}")

    # Сегодня — одной короткой строкой: утром она ещё пустая, разворачивать её в
    # полный блок с нулями незачем, но темп текущего дня видеть нужно.
    out.append("")
    today_line = f"сегодня: отправлено {today} · ответили {replied_today} · КЭВ {kev_today}"
    if undeliv_today:
        today_line += f" · ⚠️ не дошло {undeliv_today}"
    out.append(today_line)
    return "\n".join(out)


async def send_campaign_report(cid: int) -> dict:
    """Отчёт по ОДНОЙ кампании в личку — та же строка, что видна на экране кампании
    (кнопка «📤 Отчёт в ЛС»). Получатель — notify_target ЭТОЙ кампании (то же поле,
    что и уведомления о встречах, см. _owner_route): один смысл — «кому в личку
    падают события этой кампании», заводить второе поле-дубликат незачем.
    Возвращает {ok, error} — вызывающий (веб-ручка) сам решает, как показать статус."""
    with database.get_conn() as conn:
        text = campaign_report_text(conn, cid)
        if text is None:
            return {"ok": False, "error": "кампания не найдена"}
        sender_id, target = _owner_route(conn, cid)
    if not sender_id or not target:
        return {"ok": False, "error": "не настроен получатель — задай «уведомляет о встречах» "
                                      "и получателя в настройках этой кампании или в «Аккаунтах»"}
    await _send_to_owner(sender_id, target, text, "отчёт по кампании", 0)
    return {"ok": True}


def _audience_where_for_report(tag: str | None, channel: str | None) -> tuple[str, list]:
    """Урезанная копия web/app.py:_audience_where — своя, чтобы не тянуть web.app сюда
    (channels/ не должен зависеть от web/, это отдельный слой)."""
    where = "deleted_at IS NULL AND status='new' AND (username IS NOT NULL OR phone IS NOT NULL)"
    params: list = []
    if tag:
        where += " AND tags LIKE ?"
        params.append(f"%{tag}%")
    return where, params


async def notify_hot(contact_id: int, last_message: str | None, campaign_id: int | None = None) -> None:
    """Горячий лид: готов действовать ПРЯМО СЕЙЧАС, не в назначенное время (agent.Reply.hot).

    Отдельно от notify_meeting намеренно: встреча — плановое событие, горячий лид — это
    «звони, пока не остыл», и текст должен читаться иначе с первой секунды (эмодзи,
    формулировка), а не как обычное «договорились» с пустым временем."""
    try:
        with database.get_conn() as conn:
            sender_id, target = _owner_route(conn, campaign_id)
            if not sender_id or not target:
                return
            row = conn.execute(
                "SELECT id, name, person_name, username, phone, specialization, niche "
                "FROM contacts WHERE id=?", (contact_id,),
            ).fetchone()
        if not row:
            return
        who = (row["person_name"] or row["name"] or "контакт").strip()
        spec = (row["specialization"] or row["niche"] or "").strip()
        uname = f"@{row['username']}" if row["username"] else "без ника в TG"
        lines = [f"🔥 Горячий лид — звони, пока не остыл: {who}"]
        if spec:
            lines.append(f"Чем занимается: {spec}")
        lines.append(f"TG: {uname}")
        phone_link = _phone_link(row["phone"])
        if phone_link:
            lines.append(f"Номер: {phone_link}")
        if last_message and last_message.strip():
            lines.append(f"Последнее сообщение: {last_message.strip()[:300]}")
        lines.append(_chat_link(row["id"]))
        text = "\n".join(lines)
        await _send_to_owner(sender_id, target, text, "горячий лид", contact_id)
    except Exception as e:  # noqa: BLE001
        print(f"[notify] сбой отправки о горячем лиде: {e}")


if __name__ == "__main__":
    import argparse
    import asyncio

    p = argparse.ArgumentParser(description="Уведомления владельцу AXIOM")
    p.add_argument("--daily-report", action="store_true", help="утренняя сводка по кампаниям")
    p.add_argument("--campaign-report", type=int, metavar="CID",
                   help="отчёт по ОДНОЙ кампании в личку (кнопка «📤 Отчёт в ЛС»)")
    args = p.parse_args()
    if args.daily_report:
        asyncio.run(send_daily_report())
    elif args.campaign_report:
        result = asyncio.run(send_campaign_report(args.campaign_report))
        if not result.get("ok"):
            print(f"[campaign report] {result.get('error')}")
