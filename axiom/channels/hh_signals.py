"""Сигналы найма с hh.ru: кто из наших компаний прямо сейчас кого-то ищет.

ЗАЧЕМ. ЕГРЮЛ (DaData) даёт реквизиты — ИНН, директора, адрес. Это подтверждает, что
компания существует, но не даёт ПОВОДА написать. Вакансия — даёт: «ищем 5 менеджеров
по продажам» означает и деньги на найм, и боль, которую можно назвать в первом
сообщении. Это единственный сигнал в бесплатных источниках, который отвечает на
вопрос «почему я пишу вам именно сегодня».

ЧТО СОБИРАЕМ. По названию компании (и городу) ищем работодателя, у него —
открытые вакансии: сколько, какие, с какой зарплатой. Пишем в companies:
  hh_employer_id, hh_vacancies (сколько открыто), hh_titles (что ищут),
  hh_checked_at. Дальше это видно в карточке и годится как зацепка.

ПРО ДОСТУП. С апреля 2026 hh закрыл публичный поиск: /employers и /vacancies без
токена отдают 403 (при этом /dictionaries открыт всем — по нему и отличаем «API жив,
но нас не пустили» от «сеть легла»). Нужен ТОКЕН ПРИЛОЖЕНИЯ:

  1. dev.hh.ru/admin (нужен аккаунт работодателя) → «Регистрация нового приложения»;
  2. после модерации там же виден access_token приложения;
  3. кладём его в .env как HH_TOKEN=... и перезапускаем пульт.

Токен приложения (client_credentials) даёт доступ к чтению вакансий и работодателей —
именно то, что нужно; авторизация пользователя (OAuth-редиректы) здесь не требуется.
Без токена модуль ничего не выдумывает: честно говорит, что доступа нет.

Запуск:
    python -m channels.hh_signals --limit 10          # по компаниям без проверки
    python -m channels.hh_signals --company 21        # одну компанию
    python -m channels.hh_signals --probe             # только проверить доступ
"""
from __future__ import annotations

import argparse
import json
import time

import config
from db import database

BASE = "https://api.hh.ru"
# hh требует осмысленный User-Agent с контактом — иначе режет запросы.
UA = "AXIOM-CRM/1.0 (vassiliy.pelts@gmail.com)"


PAUSE = 0.4          # между запросами: лимитов на чтение нет, но частить незачем
TOP_TITLES = 5       # сколько названий вакансий показываем оператору


def _headers() -> dict:
    """Заголовки запроса. Токен приложения из .env (HH_TOKEN) — без него поиск
    закрыт: с апреля 2026 hh отвечает 403 на неавторизованные запросы."""
    h = {"User-Agent": UA, "Accept": "application/json"}
    token = (getattr(config, "HH_TOKEN", "") or "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(path: str, params: dict) -> tuple[dict | None, str | None]:
    """(данные, ошибка). Ошибку возвращаем текстом — она попадёт оператору как есть."""
    try:
        import requests
        r = requests.get(f"{BASE}{path}", params=params, headers=_headers(), timeout=12)
    except Exception as e:  # noqa: BLE001
        return None, f"сеть: {type(e).__name__}"
    if r.status_code == 403:
        if not (getattr(config, "HH_TOKEN", "") or "").strip():
            return None, ("нужен токен приложения hh: dev.hh.ru/admin → «Регистрация "
                          "нового приложения» → скопировать access_token в .env как "
                          "HH_TOKEN (с апреля 2026 поиск без токена закрыт)")
        return None, "hh.ru отдал 403 — токен есть, но доступ к методу закрыт (проверь права приложения)"
    if r.status_code == 401:
        return None, "hh.ru отдал 401 — токен неверный или истёк, обнови HH_TOKEN в .env"
    if r.status_code == 404:
        return None, "не найдено"
    if r.status_code != 200:
        return None, f"hh.ru ответил {r.status_code}"
    try:
        return r.json(), None
    except Exception:  # noqa: BLE001
        return None, "ответ не разобрался"


def probe() -> dict:
    """Пускают ли нас в поиск. Отдельная команда, чтобы не гадать при пустых итогах."""
    _, err_dict = _get("/dictionaries", {})
    _, err_search = _get("/employers", {"text": "тест", "per_page": 1})
    return {"ok": err_search is None,
            "has_token": bool((getattr(config, "HH_TOKEN", "") or "").strip()),
            "dictionaries": "доступен" if not err_dict else err_dict,
            "search": "доступен" if not err_search else err_search}


def _find_employer(name: str, city: str | None) -> tuple[dict | None, str | None]:
    data, err = _get("/employers", {"text": name, "only_with_vacancies": "true",
                                    "per_page": 5})
    if err:
        return None, err
    items = (data or {}).get("items") or []
    if not items:
        return None, None
    # Совпадение по значимым словам: hh, как и DaData, отдаёт ближайшее, а не точное.
    want = _words(name)
    for e in items:
        if want & _words(e.get("name")):
            return e, None
    return None, None


def _words(x: str | None) -> set:
    import re
    junk = {"ооо", "оао", "зао", "ао", "ип", "пао", "компания", "групп", "group", "llc"}
    return {w for w in re.findall(r"\w+", (x or "").lower().replace("ё", "е").replace("-", " "))
            if w not in junk and len(w) > 2}


def _vacancies(employer_id: str) -> tuple[list, str | None]:
    data, err = _get("/vacancies", {"employer_id": employer_id, "per_page": 20})
    if err:
        return [], err
    out = []
    for v in (data or {}).get("items") or []:
        sal = v.get("salary") or {}
        out.append({
            "name": v.get("name"),
            "area": (v.get("area") or {}).get("name"),
            "salary_from": sal.get("from"),
            "salary_to": sal.get("to"),
            "published_at": v.get("published_at"),
            "url": v.get("alternate_url"),
        })
    return out, None


def _is_blocking(err: str) -> bool:
    """Ошибка доступа, при которой обход надо прекратить: она одинакова для ВСЕХ
    компаний, и перебирать остальные — только тратить время и плодить пустые записи.
    Проверять подстроку «403» нельзя: текст про отсутствующий токен её не содержит."""
    low = (err or "").lower()
    return "403" in low or "401" in low or "токен" in low


def _targets(company_id: int | None, limit: int) -> list[dict]:
    with database.get_conn() as conn:
        if company_id:
            rows = conn.execute("SELECT id, name, city FROM companies WHERE id=?",
                                (company_id,)).fetchall()
        else:
            # Сначала те, кого ещё не проверяли: повторный обход тех же — пустая трата.
            rows = conn.execute(
                "SELECT id, name, city FROM companies WHERE COALESCE(name,'')<>'' "
                "AND COALESCE(hh_checked_at,'')='' ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _save(company_id: int, emp: dict | None, vacs: list) -> None:
    titles = "; ".join(dict.fromkeys(v["name"] for v in vacs if v.get("name")))[:500]
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE companies SET hh_employer_id=?, hh_vacancies=?, hh_titles=?, "
            "hh_url=?, hh_checked_at=datetime('now') WHERE id=?",
            ((emp or {}).get("id"), len(vacs), titles or None,
             (emp or {}).get("alternate_url"), company_id))


def run(company_id: int | None, limit: int) -> dict:
    database.init_db()
    rows = _targets(company_id, limit)
    if not rows:
        return {"ok": False, "error": "некого проверять: нет компаний без отметки hh"}
    found = checked = 0
    blocked = None
    details = []
    for r in rows:
        emp, err = _find_employer(r["name"], r.get("city"))
        if err and _is_blocking(err):
            blocked = err            # дальше идти смысла нет — доступ закрыт целиком
            break
        checked += 1
        vacs: list = []
        if emp:
            vacs, verr = _vacancies(emp["id"])
            if verr and _is_blocking(verr):
                blocked = verr
                break
        _save(r["id"], emp, vacs)
        if vacs:
            found += 1
            details.append(f"{r['name']}: {len(vacs)} вакансий — "
                           + ", ".join(v["name"] for v in vacs[:TOP_TITLES] if v.get("name")))
        time.sleep(PAUSE)
    res = {"ok": not blocked, "checked": checked, "with_vacancies": found,
           "details": details[:15]}
    if blocked:
        res["error"] = blocked + ". Проверка остановлена — записывать пустые данные не стали"
    return res


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: сигналы найма с hh.ru по компаниям")
    p.add_argument("--company", type=int, default=None, help="одна компания (companies.id)")
    p.add_argument("--limit", type=int, default=20, help="сколько компаний за заход")
    p.add_argument("--probe", action="store_true", help="только проверить доступ к hh.ru")
    args = p.parse_args()
    try:
        res = probe() if args.probe else run(args.company, args.limit)
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
