"""Жизненный цикл аккаунтов: когда куплен, сколько прожил, от чего умер.

Ничего не проверяет само (это делают session_check/health/proxy_check) — только
читает то, что они УЖЕ записали в accounts (bought_at, session_alive,
session_state, session_reason, session_checked_at) и в events (лента «ban»),
и раскладывает по полочкам:

  • сколько закуплено и когда (по bought_at, группировка по дате/партии);
  • сколько ещё живо, сколько умерло, сколько archived/banned;
  • «прожил N дней» для каждого умершего — bought_at → session_checked_at
    момента смерти (первого перехода alive→не-alive, см. _death_ts);
  • ПРИЧИНА смерти — авто-категория по session_reason (см. _CAUSES), чтобы
    сразу было видно, что убивает парк: угон с двух IP, реклейм продавцом,
    бан, мёртвый прокси-канал или что-то нераспознанное;
  • свод по причинам и по возрасту смерти — где искать дыру в процессе.

Не путать с session_check.py: тот ставит вердикт ПРЯМО СЕЙЧАС (лезет в
Telegram). lifecycle_report в Telegram не ходит вообще — read-only срез по
БД, поэтому безопасен запускать когда угодно и с любой частотой.

Запуск:
    python -m channels.lifecycle_report              # текст + JSON в конце
    python -m channels.lifecycle_report --json        # только JSON (для API/cron)
    python -m channels.lifecycle_report --days 30     # закупки/смерти за последние 30 дней
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta

from db import database

# Порядок важен: проверяем сверху вниз, первое совпадение побеждает.
# AuthKeyDuplicated — самая частая и самая дорогая причина (см. CLAUDE.md/
# ПОКУПКА-АККАУНТОВ.md: 12.09.2026 так разом сгорело 19 аккаунтов), поэтому
# первая в списке, хотя по алфавиту текста ошибки могла бы попасть под 'noconn'.
_CAUSES = [
    ("auth_key_duplicated", re.compile(r"AuthKeyDuplicated", re.I),
     "Угон: сессия открыта с двух IP одновременно (свой код, не продавец) — "
     "обычно наш же процесс подключился мимо _listener_released"),
    ("reclaim", re.compile(r"не авторизован|revoked|разлогинен|terminat", re.I),
     "Реклейм продавцом: вошёл по SMS на свой номер и снёс сессию — "
     "нашего 2FA не было или не успели поставить"),
    ("banned", re.compile(r"banned|заблокир", re.I),
     "Бан самим Telegram (спам/жалобы) — не связан с угоном"),
    ("proxy_dead", re.compile(r"прокси|proxy|таймаут|timed?\s*out", re.I),
     "Канал не достучался (прокси лёг) — сессия при этом МОЖЕТ быть жива, "
     "не путать с настоящей смертью (session_alive тут обычно NULL, не 0)"),
    ("no_session", re.compile(r"нет сессии|nosess", re.I),
     "Аккаунт так и не подключили — сессии в БД никогда не было"),
]
_UNKNOWN = ("unknown", "причина не распознана — читай session_reason в карточке руками")


def _classify(reason: str | None) -> tuple[str, str]:
    reason = (reason or "").strip()
    if not reason:
        return "no_reason", "смерть зафиксирована, но текст причины пуст"
    for key, pat, hint in _CAUSES:
        if pat.search(reason):
            return key, hint
    return _UNKNOWN


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(fmt) + 2], fmt)
        except ValueError:
            continue
    return None


def _death_ts(conn, acc_id: int, fallback: str | None) -> str | None:
    """Момент смерти — первое событие type='ban' на этом аккаунте (там, где
    session_check честно отмечает переход в banned/revoked). Если событий нет
    (умер до того, как их стали писать, или смерть не через session_check),
    откатываемся на session_checked_at — грубее, но не пусто."""
    row = conn.execute(
        "SELECT ts FROM events WHERE account_id=? AND type='ban' ORDER BY id ASC LIMIT 1",
        (acc_id,)).fetchone()
    if row and row["ts"]:
        return row["ts"]
    return fallback


def build(days: int | None) -> dict:
    database.init_db()
    since = None
    if days:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, label, phone, kind, status, country, bought_at, "
            "session_alive, session_state, session_reason, session_checked_at "
            "FROM accounts WHERE COALESCE(kind,'')<>'own' AND phone IS NOT NULL"
        ).fetchall()
        accs = [dict(r) for r in rows]
        for a in accs:
            if a["session_alive"] == 0:
                a["death_at"] = _death_ts(conn, a["id"], a["session_checked_at"])
            else:
                a["death_at"] = None

    if since:
        accs = [a for a in accs if (a["bought_at"] or "") >= since or (a["death_at"] or "") >= since]

    total = len(accs)
    alive = sum(1 for a in accs if a["session_alive"] == 1)
    dead = [a for a in accs if a["session_alive"] == 0]
    unknown = total - alive - len(dead)

    # Причина + возраст на момент смерти, по каждому мёртвому.
    cause_counts: dict[str, int] = {}
    ages: list[int] = []
    dead_detail = []
    for a in dead:
        key, hint = _classify(a["session_reason"])
        cause_counts[key] = cause_counts.get(key, 0) + 1
        bought = _parse_dt(a["bought_at"])
        died = _parse_dt(a["death_at"])
        age_days = (died - bought).days if (bought and died and died >= bought) else None
        if age_days is not None:
            ages.append(age_days)
        dead_detail.append({
            "id": a["id"], "label": a["label"], "phone": a["phone"],
            "country": a["country"], "bought_at": a["bought_at"],
            "died_at": a["death_at"], "age_days": age_days,
            "cause": key, "cause_hint": hint,
            "reason_raw": (a["session_reason"] or "")[:200],
        })
    dead_detail.sort(key=lambda d: d["died_at"] or "", reverse=True)

    # Закупки по дате (bought_at усечён до дня) — «сколько и когда куплено».
    batches: dict[str, dict] = {}
    for a in accs:
        day = (a["bought_at"] or "?")[:10]
        b = batches.setdefault(day, {"date": day, "bought": 0, "alive": 0, "dead": 0})
        b["bought"] += 1
        if a["session_alive"] == 1:
            b["alive"] += 1
        elif a["session_alive"] == 0:
            b["dead"] += 1
    batch_list = sorted(batches.values(), key=lambda b: b["date"], reverse=True)

    avg_age = round(sum(ages) / len(ages), 1) if ages else None
    survival_rate = round(alive / total * 100, 1) if total else None

    return {
        "ok": True,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": days,
        "totals": {
            "total": total, "alive": alive, "dead": len(dead), "unknown": unknown,
            "survival_rate_pct": survival_rate,
        },
        "avg_lifespan_days": avg_age,
        "causes": [
            {"cause": k, "count": v, "hint": next((h for kk, _, h in _CAUSES if kk == k), _UNKNOWN[1])}
            for k, v in sorted(cause_counts.items(), key=lambda kv: -kv[1])
        ],
        "batches": batch_list,
        "dead_accounts": dead_detail,
    }


def _print_text(data: dict) -> None:
    t = data["totals"]
    print(f"=== Жизненный цикл аккаунтов ({data['generated_at']}) ===")
    print(f"Всего: {t['total']}  Живых: {t['alive']}  Мертвых: {t['dead']}  "
          f"Не проверено: {t['unknown']}  Выживаемость: {t['survival_rate_pct']}%")
    if data["avg_lifespan_days"] is not None:
        print(f"Средний срок жизни умерших: {data['avg_lifespan_days']} дн.")
    print("\n-- Причины смерти --")
    for c in data["causes"]:
        print(f"  {c['cause']:22s} {c['count']:3d}  — {c['hint']}")
    print("\n-- Закупки по датам (последние) --")
    for b in data["batches"][:15]:
        print(f"  {b['date']}: куплено {b['bought']:3d}  живых {b['alive']:3d}  мертвых {b['dead']:3d}")
    if data["dead_accounts"]:
        print("\n-- Последние смерти --")
        for d in data["dead_accounts"][:20]:
            age = f"{d['age_days']}дн" if d["age_days"] is not None else "?"
            print(f"  #{d['id']:<6} {d['label'] or d['phone']:<20} {d['cause']:<22} "
                  f"прожил {age:>6}  ({d['died_at']})")


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: аналитика жизненного цикла аккаунтов (read-only)")
    p.add_argument("--days", type=int, default=None, help="окно в днях (по умолчанию — вся история)")
    p.add_argument("--json", action="store_true", help="только JSON, без текстовой сводки")
    args = p.parse_args()
    data = build(args.days)
    if not args.json:
        _print_text(data)
        print()
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
