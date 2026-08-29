"""Обогащение компаний через Checko (checko.ru): реквизиты + финансы + риск-флаги.

ЗАЧЕМ ОТДЕЛЬНО ОТ DaData. DaData на бесплатном тарифе даёт реквизиты: ИНН, ОГРН,
руководителя, адрес, ОКВЭД. Checko поверх этого отдаёт то, чего там нет и что реально
меняет разговор с клиентом:
  • выручку и прибыль по годам (видно, растёт компания или падает);
  • учредителей (кто настоящий владелец, а не наёмный директор);
  • телефоны из ЕГРЮЛ;
  • категорию МСП, численность, налоговый режим;
  • риск-флаги: массовый руководитель/учредитель, дисквалификация, санкции.

ЛИМИТ. Бесплатный тариф — 100 запросов в СУТКИ. Это жёсткий потолок, и его легко
проесть впустую: у нас 1000+ компаний, а один заход по 20 штук с финансами — это
уже 40 запросов. Поэтому:
  • считаем расход ПЕРЕД каждым запросом и останавливаемся, не доводя до отказа;
  • верим счётчику самого Checko (meta.today_request_count) — он точнее локального:
    переживает перезапуск пульта и учитывает запросы из других мест;
  • финансы (второй запрос на компанию) берём ТОЛЬКО по явному флагу --finances;
  • не ходим повторно за тем, что уже собрано (checko_checked_at).

Порядок работы с источниками: сначала DaData (безлимитно, реквизиты), Checko — точечно
там, где нужны деньги и владельцы. Иначе суточный лимит уйдёт на то, что уже есть.

Запуск:
    python -m channels.checko --company 21              # одну компанию
    python -m channels.checko --limit 20                # пачкой, без финансов
    python -m channels.checko --limit 10 --finances     # с финансами (2 запроса на компанию)
    python -m channels.checko --quota                   # сколько осталось на сегодня
"""
from __future__ import annotations

import argparse
import json
import time

import config
from db import database

BASE = "https://api.checko.ru/v2"
DAILY_LIMIT = 100        # бесплатный тариф Checko
SAFETY_GAP = 2           # оставляем запас, чтобы не упереться в отказ на последнем шаге
PAUSE = 0.35             # между запросами

# Коды бухотчётности (форма 2): выручка и чистая прибыль.
CODE_REVENUE = "2110"
CODE_PROFIT = "2400"


def _key() -> str:
    """Ключ: сперва из БД (app_settings), потом из .env.

    В БД он надёжнее: деплой делает git reset --hard и правки на сервере стирает, а
    .env хоть и не в репозитории, но задаётся при установке и требует доступа к
    серверу. Через app_settings ключ можно вписать прямо из пульта и он переживёт
    любой деплой."""
    try:
        with database.get_conn() as conn:
            v = database.get_setting(conn, "checko_api_key")
        if v and v.strip():
            return v.strip()
    except Exception:  # noqa: BLE001 — БД ещё не поднята: падать из-за ключа незачем
        pass
    return (getattr(config, "CHECKO_API_KEY", "") or "").strip()


def _get(path: str, params: dict) -> tuple[dict | None, str | None, int | None]:
    """(data, ошибка, израсходовано_сегодня). Счётчик берём из ответа Checko."""
    key = _key()
    if not key:
        return None, "не задан CHECKO_API_KEY в .env", None
    try:
        import requests
        r = requests.get(f"{BASE}{path}", params={"key": key, **params}, timeout=20)
    except Exception as e:  # noqa: BLE001
        return None, f"сеть: {type(e).__name__}", None
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return None, f"ответ не разобрался (HTTP {r.status_code})", None
    meta = body.get("meta") or {}
    used = meta.get("today_request_count")
    status = (meta.get("status") or "").lower()
    if r.status_code != 200 or status not in ("ok", ""):
        msg = meta.get("message") or body.get("message") or f"HTTP {r.status_code}"
        if "лимит" in str(msg).lower() or r.status_code == 429:
            return None, f"суточный лимит Checko исчерпан ({DAILY_LIMIT}/сутки)", used
        return None, str(msg)[:200], used
    return body.get("data") or {}, None, used


def quota() -> dict:
    """Сколько запросов осталось сегодня. Дешёвый способ — спросить любой метод и
    посмотреть meta; специального endpoint'а для квоты у Checko нет."""
    if not _key():
        return {"ok": False, "error": "не задан CHECKO_API_KEY в .env"}
    _, err, used = _get("/company", {"inn": "7707083893"})   # Сбербанк, для проверки связи
    if err and used is None:
        return {"ok": False, "error": err}
    used = used or 0
    return {"ok": True, "used_today": used, "limit": DAILY_LIMIT,
            "left": max(0, DAILY_LIMIT - used)}


def _fio_list(block) -> list[str]:
    """Учредители/руководители → список ФИО. Структура у Checko разная (ФЛ/ЮЛ),
    поэтому разбираем мягко: чего нет — того нет."""
    out: list[str] = []
    if isinstance(block, dict):
        for grp in ("ФЛ", "ЮЛ", "РФ"):
            for item in (block.get(grp) or []):
                nm = item.get("ФИО") or item.get("НаимПолн") or item.get("НаимСокр")
                if nm:
                    out.append(nm)
    elif isinstance(block, list):
        for item in block:
            nm = (item or {}).get("ФИО") or (item or {}).get("НаимПолн")
            if nm:
                out.append(nm)
    return list(dict.fromkeys(out))


def _map_company(d: dict) -> dict:
    """Ответ Checko → колонки companies. Пишем только то, для чего есть поле."""
    out: dict = {}
    if d.get("ИНН"):
        out["inn"] = d["ИНН"]
    if d.get("ОГРН"):
        out["ogrn"] = d["ОГРН"]
    if d.get("КПП"):
        out["kpp"] = d["КПП"]
    if d.get("ДатаРег"):
        out["registration_date"] = d["ДатаРег"]
    okved = d.get("ОКВЭД") or {}
    if okved.get("Наим"):
        out["main_activity"] = f"{okved.get('Код','')} {okved['Наим']}".strip()
    addr = (d.get("ЮрАдрес") or {}).get("АдресРФ")
    if addr:
        out["address"] = addr
    ruk = (d.get("Руковод") or [{}])
    if ruk and ruk[0].get("ФИО"):
        out["director_name"] = ruk[0]["ФИО"]
        if ruk[0].get("ВидДолжн"):
            out["director_role"] = ruk[0]["ВидДолжн"].capitalize()
    founders = _fio_list(d.get("Учред"))
    if founders:
        out["founders"] = "; ".join(founders)[:500]
    if d.get("СЧР") is not None:
        out["employee_count"] = d["СЧР"]
    lic = d.get("Лиценз") or []
    if lic:
        out["licenses"] = "; ".join(
            str((l or {}).get("ВидДеят") or (l or {}).get("Номер") or "") for l in lic[:5])[:500]
    # Телефон из ЕГРЮЛ — берём первый: в карточке одно поле, остальные уйдут в заметки.
    phones = ((d.get("Контакты") or {}).get("Тел")) or []
    if phones:
        out["phone"] = phones[0]
    return out


def _risk_note(d: dict) -> str:
    """Короткая сводка о рисках и статусе — в заметки, отдельного поля под это нет."""
    bits = []
    st = (d.get("Статус") or {}).get("Наим")
    if st and st != "Действует":
        bits.append(f"⚠ статус: {st}")
    msp = (d.get("РМСП") or {}).get("Кат")
    if msp:
        bits.append(f"МСП: {msp.lower()}")
    tax = (d.get("Налоги") or {}).get("ОсобРежим") or []
    if tax:
        bits.append("режим: " + ", ".join(tax))
    cap = (d.get("УстКап") or {}).get("Сумма")
    if cap:
        bits.append(f"уставный капитал: {int(cap):,}".replace(",", " ") + " ₽")
    for flag, text in (("МассРуковод", "массовый руководитель"),
                       ("МассУчред", "массовый учредитель"),
                       ("НедобПост", "недобросовестный поставщик"),
                       ("ДисквЛица", "дисквалифицированные лица"),
                       ("Санкции", "санкции")):
        if d.get(flag):
            bits.append(f"⚠ {text}")
    phones = ((d.get("Контакты") or {}).get("Тел")) or []
    if len(phones) > 1:
        bits.append("тел. из ЕГРЮЛ: " + ", ".join(phones[:5]))
    return " · ".join(bits)


def _map_finances(fin: dict) -> dict:
    """Последний доступный год → выручка и прибыль (в тыс. руб, как в колонках)."""
    years = sorted((y for y in fin if str(y).isdigit()), reverse=True)
    for y in years:
        row = fin.get(y) or {}
        rev, prof = row.get(CODE_REVENUE), row.get(CODE_PROFIT)
        if rev is None and prof is None:
            continue
        out = {}
        if rev is not None:
            out["revenue"] = round(rev / 1000, 1)      # рубли → тыс. руб
        if prof is not None:
            out["profit"] = round(prof / 1000, 1)
        out["_year"] = y
        return out
    return {}


def _targets(company_id: int | None, limit: int) -> list[dict]:
    with database.get_conn() as conn:
        if company_id:
            rows = conn.execute("SELECT id, name, city, inn FROM companies WHERE id=?",
                                (company_id,)).fetchall()
        else:
            # Сначала те, у кого УЖЕ есть ИНН (запрос по ИНН точен), и кого ещё не
            # обогащали через Checko. Без ИНН пришлось бы искать по названию — это
            # лишний запрос и риск чужой компании, а лимит всего 100 в сутки.
            rows = conn.execute(
                "SELECT id, name, city, inn FROM companies "
                "WHERE COALESCE(inn,'')<>'' AND COALESCE(checko_checked_at,'')='' "
                "ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _save(conn, cid: int, vals: dict, note: str) -> None:
    if vals:
        sets, params = [], []
        for k, v in vals.items():
            # Заполняем пустое, заполненное руками не трогаем — как и в DaData.
            sets.append(f"{k}=COALESCE(NULLIF({k},''),?)")
            params.append(v)
        conn.execute(f"UPDATE companies SET {', '.join(sets)} WHERE id=?", (*params, cid))
    if note:
        old = (conn.execute("SELECT notes FROM companies WHERE id=?", (cid,)).fetchone() or {})["notes"]
        if note not in (old or ""):
            merged = f"{old}\n{note}".strip() if old else note
            conn.execute("UPDATE companies SET notes=? WHERE id=?", (merged[:2000], cid))
    conn.execute("UPDATE companies SET checko_checked_at=datetime('now') WHERE id=?", (cid,))


def run(company_id: int | None, limit: int, with_finances: bool = False) -> dict:
    database.init_db()
    if not _key():
        return {"ok": False, "error": "не задан CHECKO_API_KEY в .env"}
    rows = _targets(company_id, limit)
    if not rows:
        return {"ok": False, "error": "некого обогащать: нужны компании с ИНН, "
                                      "которых ещё не проверяли через Checko"}
    per_company = 2 if with_finances else 1
    done = failed = 0
    details: list[str] = []
    stopped = None
    used_now = None
    for r in rows:
        if not r.get("inn"):
            failed += 1
            details.append(f"{r['name']}: нет ИНН — сначала обогатите через ЕГРЮЛ")
            continue
        # Останавливаемся ЗАРАНЕЕ: лучше сделать меньше, чем упереться в отказ.
        if used_now is not None and used_now + per_company > DAILY_LIMIT - SAFETY_GAP:
            stopped = (f"остановился на суточном лимите Checko: израсходовано {used_now} "
                       f"из {DAILY_LIMIT}. Продолжить можно завтра")
            break
        data, err, used = _get("/company", {"inn": r["inn"]})
        if used is not None:
            used_now = used
        if err:
            if "лимит" in err.lower():
                stopped = err
                break
            failed += 1
            details.append(f"{r['name']}: {err}")
            time.sleep(PAUSE)
            continue
        vals = _map_company(data or {})
        note = _risk_note(data or {})
        fin_year = None
        if with_finances:
            fin, ferr, used2 = _get("/finances", {"inn": r["inn"]})
            if used2 is not None:
                used_now = used2
            if ferr and "лимит" in ferr.lower():
                stopped = ferr
            elif not ferr:
                fvals = _map_finances(fin or {})
                fin_year = fvals.pop("_year", None)
                vals.update(fvals)
        with database.get_conn() as conn:
            _save(conn, r["id"], vals, note)
        done += 1
        line = f"{r['name']}: {len(vals)} полей"
        if vals.get("revenue") is not None:
            line += f", выручка {vals['revenue']:,.0f} тыс".replace(",", " ")
            if fin_year:
                line += f" ({fin_year})"
        details.append(line)
        if stopped:
            break
        time.sleep(PAUSE)
    res = {"ok": True, "enriched": done, "failed": failed, "details": details[:20],
           "used_today": used_now, "limit": DAILY_LIMIT}
    if used_now is not None:
        res["left"] = max(0, DAILY_LIMIT - used_now)
    if stopped:
        res["stopped"] = stopped
    return res


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: обогащение компаний через Checko")
    p.add_argument("--company", type=int, default=None, help="одна компания (companies.id)")
    p.add_argument("--limit", type=int, default=20, help="сколько компаний за заход")
    p.add_argument("--finances", action="store_true",
                   help="добрать выручку и прибыль — это ВТОРОЙ запрос на компанию, "
                        "то есть вдвое быстрее расходует суточный лимит")
    p.add_argument("--quota", action="store_true", help="сколько запросов осталось сегодня")
    args = p.parse_args()
    try:
        res = quota() if args.quota else run(args.company, args.limit, args.finances)
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
