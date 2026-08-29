"""Пакетный парсинг: несколько чатов подряд, с паузами между ними.

ЗАЧЕМ. Оператор выбирает аккаунт, видит список его чатов и отмечает нужные. Гонять
их по одному руками неудобно, а главное — между чатами обязательна пауза: подряд
идущие GetParticipants по разным чатам с одного аккаунта и есть тот почерк, за
который Telegram выдаёт флуд-лимит. Здесь эта пауза не забывается никогда.

ЦЕЛЬ — tg_chat_id, а не ссылка: у закрытой группы ссылки может не быть вовсе, а
аккаунт внутри, и участников ему отдают. tg_parser._resolve_target по числовой цели
идёт в диалоги аккаунта и берёт полную сущность с access_hash.

ЯРЛЫК ИСТОЧНИКА. У каждого чата свой — «<база><номер чата>», чтобы в «Контактах»
подборки не слиплись в одну кучу: смысл ярлыка ровно в том, чтобы видеть, сколько
дал КОНКРЕТНЫЙ чат.

FLOODWAIT. Ловим и прекращаем заход целиком, а не идём в следующий чат: лимит висит
на аккаунте, и остальные чаты только усугубят.

Запуск:
    python -m channels.parse_batch --targets "123|456" --mode members --save
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys

from channels import tg_parser

# Пауза между ЧАТАМИ (не между запросами внутри чата — там свои паузы в tg_parser).
CHAT_PAUSE = (45.0, 120.0)


def _slug(name: str) -> str:
    """Хвост ярлыка источника: только буквы и цифры, чтобы фильтр в «Контактах» не
    спотыкался о пробелы и знаки."""
    import re
    s = re.sub(r"[^\w]+", "", (name or "").lower().replace("-", ""))
    return s[:24] or "chat"


async def run(targets: list[str], mode: str, accounts: list[int], save: bool,
              source: str | None, limit: int, scan: int, top: int,
              period_days: int | None) -> dict:
    from datetime import datetime
    stamp = datetime.now().strftime("%d%m%y")
    done, failed = 0, 0
    details: list[str] = []
    for i, target in enumerate(targets):
        # Свой ярлык на чат: иначе все подборки сольются под одним источником.
        src = f"{source}{_slug(target)}" if source else f"{stamp}chat{_slug(target)}"
        try:
            await tg_parser.run(
                target=target, mode=mode, limit=limit, scan=scan, top=top, save=save,
                account_ids=accounts or None, source=src, period_days=period_days)
            done += 1
            details.append(f"{target}: готово (источник {src})")
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            failed += 1
            details.append(f"{target}: {name}: {str(e)[:90]}")
            print(f"[batch] {target} — {name}: {str(e)[:120]}")
            # Флуд-лимит висит на АККАУНТЕ: следующий чат его только усугубит.
            if "FloodWait" in name or "flood" in str(e).lower():
                details.append("остановился: флуд-лимит на аккаунте")
                break
        if i < len(targets) - 1:
            pause = random.uniform(*CHAT_PAUSE)
            print(f"[batch] пауза {pause:.0f}с перед следующим чатом")
            await asyncio.sleep(pause)
    return {"ok": True, "parsed": done, "failed": failed, "details": details}


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: парсинг нескольких чатов подряд")
    p.add_argument("--targets", required=True,
                   help="цели через | — tg_chat_id или @username")
    p.add_argument("--mode", default="members",
                   choices=["admins", "members", "active", "all"])
    p.add_argument("--accounts", default="", help="аккаунты через запятую")
    p.add_argument("--source", default=None, help="база ярлыка источника")
    p.add_argument("--save", action="store_true")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--scan", type=int, default=2000)
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--period-days", type=int, default=None, dest="period_days")
    args = p.parse_args()
    targets = [t.strip() for t in args.targets.split("|") if t.strip()]
    accs = [int(x) for x in args.accounts.split(",") if x.strip()]
    try:
        res = asyncio.run(run(targets, args.mode, accs, args.save, args.source,
                              args.limit, args.scan, args.top, args.period_days))
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
