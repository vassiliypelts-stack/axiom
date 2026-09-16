"""Раздел «Контент завод»: текстовые посты (Threads/VK/Telegram) + видео (план).

Текстовый завод читает Google-таблицу проекта Kontent-zavod-traffic-machine
(см. integrations/content_sheet.py) — Axiom здесь только витрина, не источник
правды и не пишет туда.

Видео-завод — отдельный подраздел, пока не подключен ни к какому источнику
данных (см. ROADMAP.md, волна «Контент завод»); ручка отдаёт заглушку, чтобы
пункт меню открывался и фронт не падал.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from integrations import content_sheet

router = APIRouter()


@router.get("/api/content/text/summary")
def content_text_summary() -> JSONResponse:
    try:
        return JSONResponse(content_sheet.summary())
    except content_sheet.ContentSheetError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001 — таблица недоступна, ключ протух и т.п.
        return JSONResponse({"error": f"Не удалось прочитать таблицу: {e}"}, status_code=502)


@router.get("/api/content/video/summary")
def content_video_summary() -> JSONResponse:
    return JSONResponse({"not_configured": True,
                          "message": "Видео контент завод пока не подключен к источнику данных."})
