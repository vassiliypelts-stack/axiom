"""Регрессия-чек: не появился ли в web/app.py роут, который открывает своё
Telethon-подключение к сессии аккаунта БЕЗ паузы слушателя (_listener_released).

ПОЧЕМУ ЭТОТ ЧЕК ВООБЩЕ НУЖЕН. Слушатель — фоновый поток пульта — держит
подключения ко всем живым сессиям разом. Любой код, который открывает ВТОРОЕ
подключение тем же ключом (build_client(...tg_session...) + client.start()),
даёт Telegram картину угона (AuthKeyDuplicatedError) — аккаунт сгорает
навсегда. Так 12.09.2026 одна забытая пауза (эндпоинт profile_setup) сожгла
19 аккаунтов, включая старые боевые, вообще не участвовавшие в операции —
слушатель держит сессии всех разом, и коллизия задевает случайные из них.

ЧТО ПРОВЕРЯЕТ. web/app.py — единственное место, где такое подключение
происходит ПРЯМО В HTTP-роуте, на живом слушателе: код в channels/*.py — это
отдельные CLI-модули, и все их запуски из app.py уже идут через
_run_capture(...) внутри `with _listener_released():` (см. account_check.py,
twofa.py, session_spare.py и т.д.) — слушатель паузится ДО старта процесса.
Опасность — именно прямой build_client()/client.start() внутри тела самого
роута, в обход этого паттерна.

Ищет по AST: функция верхнего уровня в web/app.py, в чьём теле есть вызов
build_client(...) ИЛИ client.start()/.connect() на объекте, полученном из
build_client — и в этой же функции НЕТ упоминания _listener_released.
Даёт ложные срабатывания на новых паттернах защиты — это нормально для
статического чека; смысл не пропустить забытую защиту, а не заменить обзор.

Запуск:
    python -m channels.check_listener_guard             # exit 1, если нашлись дыры
    python -m channels.check_listener_guard --json
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

APP_PY = Path(__file__).resolve().parent.parent / "web" / "app.py"


def _calls_build_client(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id == "build_client":
                return True
            if isinstance(f, ast.Attribute) and f.attr == "build_client":
                return True
    return False


def _mentions_listener_released(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == "_listener_released":
            return True
        if isinstance(n, ast.Attribute) and n.attr == "_listener_released":
            return True
    return False


def _route_decorator(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for dec in fn.decorator_list:
        # @app.get("/x") / @app.post("/x") — Call(func=Attribute(value=Name('app')))
        call = dec if isinstance(dec, ast.Call) else None
        f = call.func if call else dec
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "app":
            path = None
            if call and call.args and isinstance(call.args[0], ast.Constant):
                path = call.args[0].value
            return f"{f.attr.upper()} {path or '?'}"
    return None


def scan(path: Path = APP_PY) -> list[dict]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        route = _route_decorator(node)
        if route is None:
            continue   # не HTTP-роут — не наша забота (см. шапку модуля)
        if _calls_build_client(node) and not _mentions_listener_released(node):
            findings.append({
                "function": node.name,
                "route": route,
                "line": node.lineno,
                "why": "вызывает build_client(...) напрямую в теле роута, но в этой же "
                       "функции не упоминается _listener_released — второе подключение "
                       "к живой сессии слушателя жжёт аккаунт (AuthKeyDuplicatedError)",
            })
    return findings


def main() -> None:
    p = argparse.ArgumentParser(
        description="AXIOM: чек — нет ли в web/app.py роута с Telethon-подключением "
                    "без паузы слушателя (_listener_released)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    findings = scan()
    if args.json:
        print(json.dumps({"ok": not findings, "findings": findings}, ensure_ascii=False))
    else:
        if not findings:
            print(f"✓ чисто: все роуты в {APP_PY.name}, вызывающие build_client(...), "
                  f"паузят слушатель")
        else:
            print(f"✗ найдено {len(findings)} роут(ов) БЕЗ паузы слушателя — риск "
                  f"AuthKeyDuplicatedError (сожжённый навсегда аккаунт):\n")
            for f in findings:
                print(f"  {f['route']}  (def {f['function']}, {APP_PY.name}:{f['line']})")
                print(f"    {f['why']}\n")
        print(json.dumps({"ok": not findings, "findings": findings}, ensure_ascii=False))
    raise SystemExit(0 if not findings else 1)


if __name__ == "__main__":
    main()
