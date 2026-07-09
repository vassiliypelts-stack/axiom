"""Массовая покупка прокси Proxy6.net под аккаунты — по СТРАНЕ каждого аккаунта
(гео-совпадение номер↔прокси). Один прокси на аккаунт, назначается сразу.

    python -m channels.proxy6_bulk --ids 12,13,14

Страна берётся из accounts.country (см. phone_geo.py — проставляется автоматически
по коду номера при импорте). Если у аккаунта страна не определена — пропускаем его
с понятным сообщением, а не гадаем с прокси не того гео (это и есть частая причина
банов — см. правило «номер = прокси»).
"""
from __future__ import annotations

import argparse

import phone_geo
from channels.proxy6 import Proxy6Error, buy, to_socks_url
from db import database


def run(ids: list[int], period: int, version: int) -> None:
    database.init_db()
    ok = 0
    spent_countries: dict[str, int] = {}
    total_price = 0.0
    for acc_id in ids:
        with database.get_conn() as conn:
            row = conn.execute("SELECT id, label, phone, country FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if not row:
            print(f"[skip] аккаунт #{acc_id} не найден")
            continue
        acc = dict(row)
        label = acc.get("label") or acc.get("phone") or f"#{acc_id}"
        country = acc.get("country") or phone_geo.detect(acc.get("phone"))
        if not country:
            print(f"[skip] {label}: страна не определена по номеру — прокси не куплен "
                  "(проставь страну вручную или проверь номер)")
            continue
        try:
            bought = buy(country=country, count=1, period=period, version=version)
        except Proxy6Error as e:
            print(f"[skip] {label} ({country}): {e}")
            continue
        if not bought:
            print(f"[skip] {label} ({country}): Proxy6 не вернул ни одного прокси")
            continue
        proxy_url = to_socks_url(bought[0])
        price_paid = float(bought[0].get("price") or 0)
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET proxy=? WHERE id=?", (proxy_url, acc_id))
        spent_countries[country] = spent_countries.get(country, 0) + 1
        total_price += price_paid
        ok += 1
        print(f"[ok] {label} ({country}): куплен и назначен прокси, списано {price_paid}")
    with database.get_conn() as conn:
        detail = ", ".join(f"{c}: {n}" for c, n in spent_countries.items()) or "—"
        database.add_event(
            conn, "info", f"🌐 Proxy6: куплено и назначено {ok} из {len(ids)} (списано ~{total_price:.2f})",
            f"по странам — {detail} · тип: {version} · срок: {period} дн.",
            level="good" if ok else "warn",
        )
    print(f"\nИтого куплено: {ok} из {len(ids)}, списано ~{total_price:.2f}")


def main() -> None:
    p = argparse.ArgumentParser(description="Массовая покупка прокси Proxy6.net по стране аккаунта")
    p.add_argument("--ids", required=True, help="через запятую: 1,2,3")
    p.add_argument("--period", type=int, default=30, help="на сколько дней покупать (по умолчанию 30)")
    p.add_argument("--version", type=int, default=4, help="тип: 3=IPv4 Shared, 4=IPv4, 5=MTProto, 6=IPv6")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()]
    if not ids:
        p.error("пустой список --ids")
    run(ids, args.period, args.version)


if __name__ == "__main__":
    main()
