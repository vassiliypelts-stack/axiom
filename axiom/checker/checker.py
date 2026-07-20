"""Чекер мессенджеров: для каждого номера определяет, где есть аккаунт
(Telegram / WhatsApp / MAX). Результат пишется в книжку (has_tg/has_wa/has_max)
и выгружается в Excel с подсветкой.

Каждый канал — отдельный провайдер. Провайдеры подключаются по мере готовности
адаптеров (нужны живые аккаунты). Пока не подключён — возвращает 'unknown'.

ВАЖНО: массовая проверка номеров баноопасна. Делать порциями, прогретыми
аккаунтами, с паузами. Лимиты держим как для рассылки.
"""
from __future__ import annotations

from typing import Callable

from db import database

# Провайдер: (phone: str) -> 'yes' | 'no' | 'unknown'
Provider = Callable[[str], str]


def _unknown(_: str) -> str:
    return "unknown"


# --- Заглушки провайдеров. Заменяются реальными при подключении адаптеров. ---

def check_telegram(phone: str) -> str:
    """TG-пробив живёт в channels/phone_resolve.py — используй ЕГО, не этот интерфейс.

    Почему не здесь: сигнатура Provider — (phone) -> str, по одному номеру за вызов.
    Безопасный массовый пробив так не сделать: нужны пачки, ротация рабочих аккаунтов,
    дневной потолок на аккаунт, паузы и удаление контакта сразу после пробива. Всё это
    в phone_resolve, и он же кладёт в карточку личность (tg_user_id/@username/фото/bio),
    а не только 'yes'/'no'.
    """
    raise NotImplementedError("Массовый TG-пробив: python -m channels.phone_resolve")


def check_whatsapp(phone: str) -> str:
    """WA: isRegisteredUser/getNumberId через сессию, либо фильтр номеров WaCombo.
    Реализуется в channels/whatsapp.py (зависит от того, есть ли у WaCombo API)."""
    raise NotImplementedError("Подключи WA-адаптер (channels/whatsapp.py)")


def check_max(_: str) -> str:
    """MAX: публичной проверки номера нет → всегда unknown."""
    return "unknown"


def run_checker(
    tg: Provider = _unknown,
    wa: Provider = _unknown,
    mx: Provider = check_max,
    limit: int | None = None,
) -> int:
    """Прогоняет контакты со статусом проверки 'unknown'. Безопасно вызывать повторно.
    Передай реальные провайдеры (tg=check_telegram, wa=check_whatsapp), когда адаптеры готовы.
    """
    checked = 0
    with database.get_conn() as conn:
        q = "SELECT id, phone FROM contacts WHERE phone IS NOT NULL"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = conn.execute(q).fetchall()
        for row in rows:
            phone = row["phone"]
            res = {"has_tg": _safe(tg, phone), "has_wa": _safe(wa, phone), "has_max": _safe(mx, phone)}
            # NULLIF(?,'unknown') + COALESCE: «не знаю» НЕ затирает уже известное.
            # Раньше прогон с неподключёнными провайдерами (а они по умолчанию заглушки!)
            # проставлял 'unknown' всем подряд и стирал реальные результаты — например
            # 193 has_wa='yes' и метки из importer/import_2gis. Незнание — не факт.
            conn.execute(
                "UPDATE contacts SET "
                "has_tg=COALESCE(NULLIF(?,'unknown'),has_tg), "
                "has_wa=COALESCE(NULLIF(?,'unknown'),has_wa), "
                "has_max=COALESCE(NULLIF(?,'unknown'),has_max), "
                "checked_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
                (res["has_tg"], res["has_wa"], res["has_max"], row["id"]),
            )
            checked += 1
    return checked


def _safe(provider: Provider, phone: str) -> str:
    try:
        return provider(phone)
    except NotImplementedError:
        return "unknown"
    except Exception:
        return "unknown"


if __name__ == "__main__":
    # Демо без аккаунтов: проставит unknown везде (TG/WA не подключены).
    database.init_db()
    n = run_checker()
    print(f"Проверено контактов: {n} (TG/WA пока не подключены -> unknown). "
          f"Дальше: python -m checker.export_excel")
