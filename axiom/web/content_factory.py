"""Раздел «Контент завод»: текстовые посты (Threads/VK/Telegram) + видео (план).

Текстовый завод читает Google-таблицу проекта Kontent-zavod-traffic-machine
(см. integrations/content_sheet.py) — Axiom здесь только витрина, не источник
правды и не пишет туда.

Видео-завод — отдельный подраздел, пока не подключен ни к какому источнику
данных (см. ROADMAP.md, волна «Контент завод»); ручка отдаёт заглушку, чтобы
пункт меню открывался и фронт не падал.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Body, File, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from integrations import content_sheet, content_writer, speech

router = APIRouter()


def _fail(e: Exception, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=code)


@router.get("/api/content/text/summary")
def content_text_summary() -> JSONResponse:
    try:
        return JSONResponse(content_sheet.summary())
    except content_sheet.ContentSheetError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001 — таблица недоступна, ключ протух и т.п.
        return JSONResponse({"error": f"Не удалось прочитать таблицу: {e}"}, status_code=502)


@router.get("/api/content/text/sources")
def content_text_sources() -> JSONResponse:
    try:
        return JSONResponse(content_sheet.sources())
    except content_sheet.ContentSheetError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"Не удалось прочитать таблицу: {e}"}, status_code=502)


@router.post("/api/content/text/write")
async def content_text_write(body: dict = Body(...)) -> JSONResponse:
    """Черновик поста: из находок дайджеста либо из своей мысли."""
    note = (body.get("note") or "").strip()
    idea = (body.get("idea") or "").strip()
    picked = body.get("sources") or []

    try:
        drafts = []
        if idea:
            drafts.append(await run_in_threadpool(content_writer.from_idea, idea, note))
        for s in picked[:5]:          # больше пяти за раз — это уже не черновик, а поток
            drafts.append(await run_in_threadpool(
                content_writer.from_source,
                (s.get("title") or "").strip(),
                (s.get("excerpt") or s.get("text") or "").strip(),
                (s.get("channel") or "").strip(), note))
        if not drafts:
            return JSONResponse({"error": "Нечего писать: отметьте находки или продиктуйте мысль."},
                                status_code=400)
        return JSONResponse({"drafts": drafts})
    except content_writer.WriterError as e:
        return _fail(e)
    except Exception as e:  # noqa: BLE001
        return _fail(e, 502)


@router.post("/api/content/text/transcribe")
async def content_text_transcribe(file: UploadFile = File(...)) -> JSONResponse:
    """Расшифровать надиктованную мысль."""
    try:
        data = await file.read()
        text = await run_in_threadpool(speech.transcribe, data, file.filename or "voice.ogg")
        return JSONResponse({"text": text})
    except speech.SpeechError as e:
        return _fail(e)
    except Exception as e:  # noqa: BLE001
        return _fail(e, 502)


@router.post("/api/content/text/queue")
async def content_text_queue(body: dict = Body(...)) -> JSONResponse:
    """Одобренный черновик — в очередь публикации."""
    try:
        res = await run_in_threadpool(
            content_sheet.add_to_queue,
            body.get("text") or "", (body.get("kind") or "").strip(),
            (body.get("platforms") or "threads,vk,tg").strip(),
            (body.get("image") or "").strip())
        return JSONResponse(res)
    except content_sheet.ContentSheetError as e:
        return _fail(e)
    except Exception as e:  # noqa: BLE001
        return _fail(e, 502)


@router.get("/api/content/text/images")
def content_text_images() -> JSONResponse:
    """Что лежит в папке с фото — чтобы выбрать картинку, а не вспоминать имя файла."""
    base = os.getenv("CONTENT_FACTORY_DIR", "")
    if not base:
        return JSONResponse({"images": [], "note": "CONTENT_FACTORY_DIR не задан"})
    folder = Path(base) / "autopost" / "images"
    if not folder.exists():
        return JSONResponse({"images": [], "note": f"нет папки {folder}"})
    names = sorted(p.name for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in
                   (".jpg", ".jpeg", ".png", ".webp", ".gif"))
    return JSONResponse({"images": names, "folder": str(folder)})


@router.get("/api/content/video/summary")
def content_video_summary() -> JSONResponse:
    return JSONResponse({"not_configured": True,
                          "message": "Видео контент завод пока не подключен к источнику данных."})
