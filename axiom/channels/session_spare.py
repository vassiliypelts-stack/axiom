"""Запасная (холодная) сессия аккаунта — страховка от сгоревшего ключа.

ЗАЧЕМ. Купленные аккаунты берутся разово: номер остаётся у продавца, SMS нам не придёт,
и восстановить вход после потери сессии нечем — аккаунт списывается в убыток. Так уже
сгорело 6 лотов: слушатель держал сессию, а параллельная операция (установка 2FA)
открывала ВТОРОЕ подключение тем же ключом, и Telegram убивал ключ как угнанный
(AuthKeyDuplicatedError).

ГЛАВНОЕ ЗАБЛУЖДЕНИЕ, из-за которого этот модуль и понадобился: копия .session (или
конвертация в tdata) — НЕ резервная копия. Это тот же самый ключ авторизации. Две копии,
поднятые одновременно, — ровно тот сценарий, что жжёт аккаунт. Копированием файла
застраховаться нельзя в принципе.

ЧТО РАБОТАЕТ. У аккаунта Telegram может быть несколько НЕЗАВИСИМЫХ авторизаций (как
телефон + десктоп + веб), у каждой свой ключ. Смерть одного ключа не трогает остальные.
Вторую авторизацию выдаёт сам Telegram по механике QR-логина, без SMS: новый клиент
просит токен (auth.exportLoginToken), уже авторизованный — подтверждает его
(auth.acceptLoginToken), после чего новый получает собственный ключ.

ГРАНИЦА ЗАЩИТЫ. Спасает от потери КЛЮЧА (revoked / AuthKeyDuplicated / «завершить
другие сеансы») — то есть от всех случаев, на которых мы уже теряли аккаунты. НЕ спасает
от бана самого аккаунта: бан убивает все сессии разом, запаска умрёт вместе с основной.

ДВЕ ЗАПАСКИ, А НЕ ОДНА. Угнанный ключ (AuthKeyDuplicated) Telegram сносит вместе со
СВЕЖИМИ авторизациями номера — угонщик первым делом плодит себе новые входы, и молодая
запаска выглядит ровно так. По истории 26–29.09.2026: 8 из 8 запасок моложе 44 ч умерли
вместе с основной, все запаски старше 7,8 сут пережили. Одна запаска после подъёма
заменяется свежей, то есть неделю аккаунт фактически без страховки — так 28–29.09
повторно сгорели 7 только что поднятых номеров. Поэтому слотов два (tg_session_spare,
tg_session_spare2): при подъёме тратится старшая, вторая остаётся и страхует, пока новая
набирает возраст. Инвариант: второй слот заполнен только при заполненном первом — все
проверки «есть ли запаска» смотрят на первый.

ПРАВИЛА ХРАНЕНИЯ. Запаска ХОЛОДНАЯ: выпустили, отключили, положили в БД и не трогаем до
беды. Поднимать её «за компанию» с основной нельзя — два активных устройства с разных
адресов сами по себе выглядят подозрительно (ключи разные, мгновенного ожога не будет,
но риск флага растёт). Достаём только когда основная подтверждённо мертва — promote_one().

ПОРЯДОК ПРИ ПОКУПКЕ ВАЖЕН. Сброс чужих сессий (kick_others) убивает ВСЕ авторизации,
кроме текущей, — включая нашу запаску, если она уже выпущена. Поэтому сначала выкидываем
продавца, и только потом выпускаем запаску. Здесь это зашито в одну операцию, чтобы
порядок нельзя было перепутать снаружи.

СЛУШАТЕЛЬ. Выпуск запаски подключает ОСНОВНУЮ сессию (ей подтверждать токен). Если в
этот момент её же держит слушатель — получим то самое второе подключение одним ключом и
сожжём аккаунт. Вызывающая сторона обязана остановить слушателя (как это делает роут
/api/accounts/twofa). CLI-запуск делает это сам.

Запуск:
    python -m channels.session_spare --dry                 # у кого нет запаски
    python -m channels.session_spare --ids 9,10            # выпустить запаску этим
    python -m channels.session_spare --ids 9 --kick-others # + выкинуть продавца (сброс чужих сессий)
    python -m channels.session_spare --promote 9           # основная мертва → поднять запаску
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

from telethon.errors import (
    FreshResetAuthorisationForbiddenError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.functions.auth import (
    AcceptLoginTokenRequest,
    ExportLoginTokenRequest,
    ResetAuthorizationsRequest,
)
from telethon.tl.functions.account import GetAuthorizationsRequest

from channels.telegram import build_client
from db import database

QR_TIMEOUT = 60          # ждём подтверждения токена; сам токен живёт ~30-60с (resp.expires)
CONNECT_TIMEOUT = 25
_PARALLEL = 2            # операция трогает боевые сессии — не гнать пачкой


def _targets(ids: list[int] | None) -> list[dict]:
    """Кому выпускаем: живая сессия, не родной, свободен хотя бы один из двух слотов.

    Родных (protected) не трогаем принципиально: это личные номера хозяина, у него есть
    доступ к SMS и запаска ему не нужна — а лишняя авторизация на личном номере только
    добавляет поверхность атаки."""
    where = ("session_alive=1 AND tg_session IS NOT NULL AND tg_session<>'' "
             "AND COALESCE(protected,0)=0 "
             "AND (COALESCE(tg_session_spare,'')='' OR COALESCE(tg_session_spare2,'')='')")
    cols = ("id, label, phone, tg_session, tg_session_spare, tg_session_spare2, "
            "proxy, api_id, api_hash, tg_2fa")
    with database.get_conn() as conn:
        if ids:
            qm = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT {cols} FROM accounts WHERE id IN ({qm}) AND {where}", ids).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM accounts WHERE {where} ORDER BY id").fetchall()
    return [dict(r) for r in rows]


async def _kick_others(client, acc: dict) -> tuple[bool, str]:
    """Сброс всех чужих авторизаций (текущая остаётся). Так продавец теряет вход,
    который у него остался после продажи.

    Вызывать ТОЛЬКО до выпуска запаски — иначе снесёт и её (см. шапку модуля)."""
    try:
        await client(ResetAuthorizationsRequest())
        return True, "чужие сессии сброшены"
    except FreshResetAuthorisationForbiddenError:
        # Telegram не даёт завершать чужие сеансы, пока текущей сессии меньше суток.
        # Это не ошибка нашей логики — просто рано; запаску выпускаем всё равно.
        return False, "fresh-лок: сброс чужих сессий доступен через ~24ч после входа"
    except Exception as e:  # noqa: BLE001
        return False, f"сброс не прошёл: {str(e)[:70]}"


async def _mint(acc: dict, kick_others: bool) -> tuple[bool, str]:
    """Выпускает вторую независимую авторизацию через QR-механику Telegram.

    Тонкость, из-за которой наивная реализация молча не работает: QRLogin.wait() ставит
    обработчик UpdateLoginToken в момент вызова и ждёт апдейт. Если подтвердить токен ДО
    того, как wait() начал слушать, апдейт пролетит мимо и мы вечно ждём. Поэтому wait()
    запускается задачей ПЕРВЫМ, и только потом основная сессия подтверждает токен.
    На случай, если апдейт всё-таки потерялся, — фолбэк: повторный exportLoginToken уже
    подтверждённого токена сразу отдаёт LoginTokenSuccess.
    """
    aid = acc["id"]
    api_id = int(acc["api_id"]) if acc.get("api_id") else None
    api_hash = acc.get("api_hash")

    try:
        primary = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                               api_id, api_hash)
        # Запаску собираем на ТОМ ЖЕ прокси и тех же api-кредах: второе «устройство» из
        # другой страны или с чужим app_id выглядит для антифрода как угон.
        spare = build_client(StringSession(), acc.get("proxy"), api_id, api_hash,
                              allow_shared_ip=True)
    except Exception as e:  # noqa: BLE001
        return False, f"клиент не собрался: {str(e)[:60]}"

    note = []
    try:
        await asyncio.wait_for(primary.connect(), timeout=CONNECT_TIMEOUT)
        if not await primary.is_user_authorized():
            return False, "основная сессия не авторизована — подтверждать токен нечем"

        if kick_others and (acc.get("tg_session_spare") or "").strip():
            note.append("сброс чужих сессий пропущен — он снёс бы уже выпущенную запаску")
        elif kick_others:
            ok, msg = await _kick_others(primary, acc)
            note.append(msg)
            if not ok and "fresh-лок" not in msg:
                return False, msg          # сброс сломался не по времени — не продолжаем вслепую

        await asyncio.wait_for(spare.connect(), timeout=CONNECT_TIMEOUT)
        qr = await spare.qr_login()

        waiter = asyncio.create_task(qr.wait(QR_TIMEOUT))
        await asyncio.sleep(0.5)           # дать wait() зарегистрировать обработчик апдейта
        await primary(AcceptLoginTokenRequest(qr.token))

        try:
            await waiter
        except SessionPasswordNeededError:
            # 2FA стоит (мы же её и ставили) — новая сессия должна назвать пароль.
            pwd = (acc.get("tg_2fa") or "").strip()
            if not pwd:
                return False, "нужен облачный пароль (2FA), а в базе его нет"
            await spare.sign_in(password=pwd)
        except asyncio.TimeoutError:
            # Апдейт мог потеряться — токен уже подтверждён, спрашиваем результат напрямую.
            waiter.cancel()
            resp = await spare(ExportLoginTokenRequest(
                api_id=spare.api_id, api_hash=spare.api_hash, except_ids=[]))
            if not isinstance(resp, types.auth.LoginTokenSuccess):
                return False, "Telegram не подтвердил токен за отведённое время"
            note.append("подтверждение поймано фолбэком")

        if not await spare.is_user_authorized():
            return False, "запаска не авторизовалась"

        spare_str = spare.session.save()
        if not spare_str:
            return False, "пустая строка сессии — сохранять нечего"

        with database.get_conn() as conn:
            # Свободный слот выбирается в самом UPDATE (SQLite считает все SET по старой
            # строке): первый, если пуст, иначе второй.
            cur = conn.execute(
                "UPDATE accounts SET "
                "tg_session_spare2 = CASE WHEN COALESCE(tg_session_spare,'')<>'' THEN ? ELSE tg_session_spare2 END, "
                "spare2_made_at = CASE WHEN COALESCE(tg_session_spare,'')<>'' THEN datetime('now') ELSE spare2_made_at END, "
                "tg_session_spare = CASE WHEN COALESCE(tg_session_spare,'')='' THEN ? ELSE tg_session_spare END, "
                "spare_made_at = CASE WHEN COALESCE(tg_session_spare,'')='' THEN datetime('now') ELSE spare_made_at END, "
                "spare_note=? WHERE id=? "
                "AND (COALESCE(tg_session_spare,'')='' OR COALESCE(tg_session_spare2,'')='')",
                (spare_str, spare_str, "; ".join(note) or "выпущена", aid))
            if cur.rowcount == 0:
                return False, "оба слота уже заняты — новая авторизация не сохранена"
            database.add_event(
                conn, "account_protected",
                f"🧯 «{acc.get('label') or aid}»: выпущена запасная сессия",
                "Вторая независимая авторизация (свой ключ). Если основную сожжёт — "
                "вход восстанавливается без SMS кнопкой «Поднять запаску».",
                level="good", account_id=aid)
        return True, "; ".join(note + ["запаска выпущена"])
    except Exception as e:  # noqa: BLE001
        return False, f"ошибка: {str(e)[:90]}"
    finally:
        for c in (spare, primary):          # запаску гасим первой — она должна остыть
            try:
                await c.disconnect()
            except Exception:  # noqa: BLE001
                pass


async def _probe(acc: dict, spare_str: str) -> tuple[bool | None, str]:
    """Жива ли запаска: True (+ @username), False — Telegram её не знает, None — проверить
    не удалось (сеть/прокси), такую выбрасывать нельзя."""
    try:
        client = build_client(StringSession(spare_str), acc.get("proxy"),
                              int(acc["api_id"]) if acc.get("api_id") else None,
                              acc.get("api_hash"))
    except Exception as e:  # noqa: BLE001
        return None, f"клиент не собрался: {str(e)[:60]}"
    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            return False, "мертва"
        me = await client.get_me()
        return True, f"@{getattr(me, 'username', None) or me.id}"
    except Exception as e:  # noqa: BLE001
        return None, f"запаска не поднялась: {str(e)[:70]}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def promote_one(acc_id: int) -> tuple[bool, str]:
    """Основная сессия мертва → делаем запаску основной.

    Порядок безопасный: сначала ПРОВЕРЯЕМ, что запаска реально авторизована, и только
    потом перезаписываем tg_session. Иначе можно затереть основную (пусть даже мёртвую)
    на нерабочую запаску и потерять последние следы.

    Пробуем старшую первой: свежую Telegram сносит вместе с угнанным ключом (см. шапку).
    Мёртвые запаски снимаем, чтобы пульт не считал аккаунт застрахованным; оставшаяся
    живая переезжает в первый слот и продолжает страховать."""
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT id, label, tg_session, tg_session_spare, spare_made_at, "
            "tg_session_spare2, spare2_made_at, proxy, api_id, api_hash "
            "FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        return False, "аккаунт не найден"
    acc = dict(row)
    slots = [(s.strip(), at) for s, at in ((acc.get("tg_session_spare"), acc.get("spare_made_at")),
                                            (acc.get("tg_session_spare2"), acc.get("spare2_made_at")))
             if (s or "").strip()]
    if not slots:
        return False, "запаски нет — восстанавливать нечем"
    slots.sort(key=lambda x: x[1] or "")

    chosen: tuple[str, str | None, str] | None = None
    dead: list[str] = []
    errors: list[str] = []
    for s, at in slots:
        ok, info = await _probe(acc, s)
        if ok:
            chosen = (s, at, info)
            break
        if ok is False:
            dead.append(s)
        else:
            errors.append(info)
    keep = [(s, at) for s, at in slots
            if s not in dead and (chosen is None or s != chosen[0])]
    k1 = keep[0] if keep else (None, None)
    k2 = keep[1] if len(keep) > 1 else (None, None)

    with database.get_conn() as conn:
        if chosen is None:
            conn.execute(
                "UPDATE accounts SET tg_session_spare=?, spare_made_at=?, "
                "tg_session_spare2=?, spare2_made_at=?, spare_note=? WHERE id=?",
                (k1[0], k1[1], k2[0], k2[1],
                 f"запасок мертво: {len(dead)}" + (f", не проверено: {len(errors)}" if errors else ""),
                 acc_id))
            if errors:
                return False, errors[0]
            return False, "запаска тоже мертва (аккаунт забанен или сессии сброшены)"

        spare_str, _, who = chosen
        left = "вторая запаска на месте — аккаунт по-прежнему застрахован" if k1[0] else \
            "запасок больше нет — выпусти новую, пока аккаунт жив"
        if dead:
            left += f"; мёртвых запасок снято: {len(dead)}"
        conn.execute(
            "UPDATE accounts SET tg_session=?, tg_session_spare=?, spare_made_at=?, "
            "tg_session_spare2=NULL, spare2_made_at=NULL, "
            "spare_used_at=datetime('now'), spare_note='запаска поднята как основная', "
            "session_alive=1, session_state='alive', "
            "session_reason='восстановлен из запасной сессии', "
            "session_checked_at=datetime('now') WHERE id=?",
            (spare_str, k1[0], k1[1], acc_id))
        database.add_event(
            conn, "account_protected",
            f"♻️ «{acc.get('label') or acc_id}»: восстановлен из запасной сессии",
            f"Основная сессия была мертва, поднята запаска ({who}). {left[:1].upper() + left[1:]}.",
            level="good", account_id=acc_id)
    return True, f"восстановлен: {who}. {left[:1].upper() + left[1:]}"


async def rekey_one(acc_id: int) -> tuple[bool, str]:
    """Живая основная → перейти на собственную запаску и выкинуть все остальные входы.

    ЗАЧЕМ. tdata купленного аккаунта — это ключ с компьютера продавца. Если копия того же
    ключа осталась у продавца (или ушла второму покупателю), Telegram при каждом их выходе
    в сеть видит ключ с двух адресов и сжигает его. Партия 13.10.2026 сгорела так на 16 из
    30 при тех же прокси и том же слушателе, на которых партии 01.10 и 10.10 (33 шт.) живы
    все. Запаска — авторизация, которую выпустили мы сами (QR-механика), её ключа нет ни у
    кого. Поднимаем её основной и с неё сбрасываем все прочие авторизации: старый общий
    ключ умирает вместе с чужой копией.

    Порядок. Клиент запаски бронирует аккаунт (session_lease знает и ключи запасок), так что
    слушатель отпускает старый ключ ДО сброса. Базу переписываем, пока бронь ещё наша, —
    слушатель после снятия брони подключится уже новым ключом. Сброс сносит и вторую
    запаску: новые выпустит автозащита. Запаска моложе суток сбросить не даст (fresh-лок) —
    тогда ничего не меняем."""
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT id, label, tg_session, tg_session_spare, spare_made_at, "
            "tg_session_spare2, spare2_made_at, proxy, api_id, api_hash, session_alive "
            "FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        return False, "аккаунт не найден"
    acc = dict(row)
    slots = [(s.strip(), at) for s, at in ((acc.get("tg_session_spare"), acc.get("spare_made_at")),
                                            (acc.get("tg_session_spare2"), acc.get("spare2_made_at")))
             if (s or "").strip()]
    slots.sort(key=lambda x: x[1] or "")      # старшая первой: её не сносят как «свежий вход»
    api_id = int(acc["api_id"]) if acc.get("api_id") else None

    errors: list[str] = []
    if not slots:
        return await _rekey_fresh(acc_id)
    for spare_str, made_at in slots:
        try:
            client = build_client(StringSession(spare_str), acc.get("proxy"), api_id,
                                  acc.get("api_hash"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"клиент не собрался: {str(e)[:60]}")
            continue
        try:
            await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
            if not await client.is_user_authorized():
                errors.append(f"запаска от {made_at} мертва")
                continue
            try:
                await client(ResetAuthorizationsRequest())
            except FreshResetAuthorisationForbiddenError:
                return False, f"запаска от {made_at} моложе суток — Telegram не даёт сбросить чужие входы"
            me = await client.get_me()
            who = f"@{getattr(me, 'username', None) or me.id}"
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE accounts SET tg_session=?, tg_session_spare=NULL, spare_made_at=NULL, "
                    "tg_session_spare2=NULL, spare2_made_at=NULL, spare_used_at=datetime('now'), "
                    "spare_note='ключ перевыпущен: основной стала наша запаска, прочие входы сброшены', "
                    "session_alive=1, session_state='alive', "
                    "session_reason='ключ перевыпущен (старый общий ключ отозван)', "
                    "session_checked_at=datetime('now') WHERE id=?",
                    (spare_str, acc_id))
                database.add_event(
                    conn, "account_protected",
                    f"🔑 «{acc.get('label') or acc_id}»: ключ перевыпущен",
                    "Основной сессией стала наша запаска, все остальные входы (включая копию "
                    "ключа у продавца) сброшены. Новые запаски выпустит автозащита.",
                    level="good", account_id=acc_id)
            return True, f"перевыпущен на запаску от {made_at} ({who}), прочие входы сброшены"
        except Exception as e:  # noqa: BLE001
            errors.append(f"ошибка: {str(e)[:80]}")
        finally:
            try:
                await client.disconnect()   # снимает бронь — слушатель берёт уже новый ключ
            except Exception:  # noqa: BLE001
                pass
    if all("мертва" in e for e in errors):
        # Все запаски убиты, а основная жива: кто-то со вторым экземпляром основного ключа
        # сбросил «другие сеансы» (наш код этого не делает при выпущенной запаске). Чистим
        # мёртвые слоты и идём через свежий ключ.
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET tg_session_spare=NULL, spare_made_at=NULL, "
                         "tg_session_spare2=NULL, spare2_made_at=NULL, "
                         "spare_note='запаски сброшены чужим входом' WHERE id=?", (acc_id,))
        res_ok, res_msg = await _rekey_fresh(acc_id)
        return res_ok, f"обе запаски были мертвы → {res_msg}"
    return False, "; ".join(errors) or "не получилось"


async def _rekey_fresh(acc_id: int) -> tuple[bool, str]:
    """Перевыпуск, когда живой запаски нет: выпустить свежую, проверить её и ВЫЙТИ из
    старого основного ключа им самим (auth.logOut). Выход убивает этот ключ везде, где
    лежит его копия. Свежая запаска сбросить чужие входы не может (сутки), а выход из
    собственного ключа Telegram разрешает сразу.

    Безопасный порядок: старый ключ гасим только после того, как свежая запаска ответила
    «авторизована». Проверка запаски идёт без своей брони (lease=False): аккаунт уже
    забронирован клиентом основной сессии в этом же процессе."""
    from telethon.tl.functions.auth import LogOutRequest
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT id, label, phone, tg_session, tg_session_spare, tg_session_spare2, "
            "proxy, api_id, api_hash, tg_2fa FROM accounts WHERE id=?", (acc_id,)).fetchone()
    acc = dict(row)
    ok, msg = await _mint(acc, kick_others=False)
    if not ok:
        return False, f"свежая запаска не выпустилась: {msg}"
    with database.get_conn() as conn:
        spare_str = conn.execute("SELECT tg_session_spare FROM accounts WHERE id=?",
                                 (acc_id,)).fetchone()["tg_session_spare"]
    api_id = int(acc["api_id"]) if acc.get("api_id") else None
    primary = build_client(StringSession(acc["tg_session"]), acc.get("proxy"), api_id, acc.get("api_hash"))
    spare = build_client(StringSession(spare_str), acc.get("proxy"), api_id, acc.get("api_hash"),
                         allow_shared_ip=True, lease=False)
    try:
        await asyncio.wait_for(primary.connect(), timeout=CONNECT_TIMEOUT)   # бронь аккаунта
        await asyncio.wait_for(spare.connect(), timeout=CONNECT_TIMEOUT)
        if not await spare.is_user_authorized():
            return False, "свежая запаска не авторизована — старый ключ не трогаю"
        me = await spare.get_me()
        await primary(LogOutRequest())
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE accounts SET tg_session=?, tg_session_spare=NULL, spare_made_at=NULL, "
                "tg_session_spare2=NULL, spare2_made_at=NULL, spare_used_at=datetime('now'), "
                "spare_note='ключ перевыпущен через свежую запаску, старый ключ закрыт выходом', "
                "session_alive=1, session_state='alive', "
                "session_reason='ключ перевыпущен (старый общий ключ закрыт выходом)', "
                "session_checked_at=datetime('now') WHERE id=?", (spare_str, acc_id))
            database.add_event(
                conn, "account_protected",
                f"🔑 «{acc.get('label') or acc_id}»: ключ перевыпущен",
                "Старый ключ (его копия была у кого-то ещё) закрыт выходом, основной стала "
                "свежая авторизация. Новые запаски выпустит автозащита.",
                level="good", account_id=acc_id)
        return True, f"перевыпущен на свежий ключ (@{getattr(me, 'username', None) or me.id}), старый закрыт выходом"
    except Exception as e:  # noqa: BLE001
        return False, f"ошибка: {str(e)[:90]}"
    finally:
        for c in (spare, primary):
            try:
                await c.disconnect()
            except Exception:  # noqa: BLE001
                pass


async def count_authorizations(acc: dict) -> int | None:
    """Сколько всего активных устройств у аккаунта — для диагностики в пульте."""
    try:
        client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                              int(acc["api_id"]) if acc.get("api_id") else None,
                              acc.get("api_hash"))
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
        res = await client(GetAuthorizationsRequest())
        return len(res.authorizations)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def run(ids: list[int] | None, dry: bool, kick_others: bool, promote: int | None,
              rekey: bool = False) -> None:
    database.init_db()

    if rekey:
        # Строго по одному и с паузой: пачка одновременных сбросов сеансов с одного
        # сервера выглядит для антифрода как захват серии аккаунтов.
        done, failed = [], []
        for i, aid in enumerate(ids or []):
            ok, msg = await rekey_one(aid)
            print(f"[#{aid}] {'🔑' if ok else '✗'} {msg}")
            (done if ok else failed).append({"id": aid, "msg": msg})
            if i < len(ids) - 1:
                await asyncio.sleep(20)
        print(json.dumps({"ok": True, "rekeyed": len(done), "failed": failed}, ensure_ascii=False))
        return

    if promote:
        ok, msg = await promote_one(promote)
        print(json.dumps({"ok": ok, "promoted": promote, "msg": msg}, ensure_ascii=False))
        return

    accs = _targets(ids)
    if not accs:
        print(json.dumps(
            {"ok": False, "error": "некому: нет живых боевых аккаунтов без запаски"},
            ensure_ascii=False))
        return
    if dry:
        for a in accs:
            print(f"[dry] #{a['id']} {a.get('label') or ''} ({a.get('phone')}) — выпустили бы запаску")
        print(json.dumps({"ok": True, "dry": True, "would_mint": len(accs)}, ensure_ascii=False))
        return

    sem = asyncio.Semaphore(_PARALLEL)
    done: list[int] = []
    failed: list[dict] = []

    async def _one(a: dict) -> None:
        async with sem:
            ok, msg = await _mint(a, kick_others)
        if ok:
            done.append(a["id"])
            print(f"[#{a['id']}] {a.get('label') or ''}: 🧯 {msg}")
        else:
            failed.append({"id": a["id"], "label": a.get("label"), "err": msg})
            print(f"[#{a['id']}] {a.get('label') or ''}: ✗ {msg}")

    await asyncio.gather(*[_one(a) for a in accs])
    print(json.dumps({"ok": True, "minted": len(done), "failed": failed}, ensure_ascii=False))


def _pause_listener_if_running() -> bool:
    """CLI-запуск может идти мимо пульта — тогда слушателя надо остановить самим,
    иначе он держит основную сессию и подключение из этого процесса её сожжёт."""
    # Больше ничего не делает: основная сессия бронируется сама при подключении
    # (channels.session_lease), слушатель отпускает ровно этот аккаунт. Выключать
    # слушатель целиком и включать обратно нельзя — 23.09.2026 именно это включение
    # посреди захода рассылки сожгло три аккаунта.
    return False


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: запасная (холодная) сессия аккаунта")
    p.add_argument("--ids", help="через запятую id аккаунтов")
    p.add_argument("--dry", action="store_true", help="показать кандидатов, ничего не менять")
    p.add_argument("--kick-others", action="store_true",
                   help="сначала сбросить чужие сессии (выкинуть продавца), потом выпустить запаску")
    p.add_argument("--promote", type=int, metavar="ID",
                   help="основная мертва: поднять запаску этого аккаунта как основную")
    p.add_argument("--rekey", action="store_true",
                   help="с --ids: живым аккаунтам перейти на свою запаску и сбросить прочие входы")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None

    database.init_db()
    paused = False
    if not args.dry:
        paused = _pause_listener_if_running()
    try:
        asyncio.run(run(ids, args.dry, args.kick_others, args.promote, args.rekey))
    finally:
        if paused:
            with database.get_conn() as conn:
                database.set_setting(conn, "listener_enabled", "on")


if __name__ == "__main__":
    main()
