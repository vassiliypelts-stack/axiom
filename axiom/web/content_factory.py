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
import random
import json
import subprocess
import sys
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, UploadFile
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


@router.get("/api/content/text/trends")
def content_text_trends() -> JSONResponse:
    try:
        return JSONResponse(content_sheet.trends())
    except Exception as e:  # worksheet may not exist before first collector run
        return _fail(e, 502)


@router.get("/api/content/text/plan")
def content_text_plan(weeks: int = 4) -> JSONResponse:
    try:
        return JSONResponse(content_sheet.plan(weeks))
    except Exception as e:
        return _fail(e, 502)


@router.post("/api/content/text/trends/{trend_id}/take")
async def content_text_take_trend(trend_id: str, body: dict = Body(...)) -> JSONResponse:
    """Explicit click: create a draft, then mark this source as taken."""
    try:
        draft = await run_in_threadpool(content_writer.from_source, body.get("title", ""),
            body.get("excerpt", ""), body.get("author", ""), body.get("angle", ""), body.get("link", ""), random.choice(content_writer.FORMS))
        await run_in_threadpool(content_sheet.mark_trend_taken, trend_id)
        return JSONResponse({"draft": draft})
    except Exception as e:
        return _fail(e, 502)


@router.post("/api/content/text/write")
async def content_text_write(body: dict = Body(...)) -> JSONResponse:
    """Черновик поста: из находок дайджеста либо из своей мысли."""
    note = (body.get("note") or "").strip()
    idea = (body.get("idea") or "").strip()
    picked = body.get("sources") or []

    # Форму назначаем снаружи и разную: пачку постов модель иначе пишет под
    # копирку — одна длина, одна структура, и лента читается как робот.
    forms = random.sample(content_writer.FORMS, k=len(content_writer.FORMS))
    nth = 0

    try:
        drafts = []
        if idea:
            drafts.append(await run_in_threadpool(
                content_writer.from_idea, idea, note, forms[nth % len(forms)]))
            nth += 1
        for s in picked[:5]:          # больше пяти за раз — это уже не черновик, а поток
            drafts.append(await run_in_threadpool(
                content_writer.from_source,
                (s.get("title") or "").strip(),
                (s.get("excerpt") or s.get("text") or "").strip(),
                (s.get("channel") or "").strip(), note,
                (s.get("link") or "").strip(), forms[nth % len(forms)]))
            nth += 1
        if not drafts:
            return JSONResponse({"error": "Нечего писать: отметьте находки или продиктуйте мысль."},
                                status_code=400)
        return JSONResponse({"drafts": drafts})
    except content_writer.WriterError as e:
        return _fail(e)
    except Exception as e:  # noqa: BLE001
        return _fail(e, 502)


@router.post("/api/content/text/shorten")
async def content_text_shorten(body: dict = Body(...)) -> JSONResponse:
    """Ужать черновик до лимита площадки."""
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "Пустой текст."}, status_code=400)
    try:
        return JSONResponse(await run_in_threadpool(
            content_writer.shorten, text, int(body.get("limit") or 500)))
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


@router.post("/api/content/text/upload_image")
async def content_text_upload_image(file: UploadFile = File(...)) -> JSONResponse:
    """Фото с компьютера — сразу в папку картинок контент-завода."""
    base = os.getenv("CONTENT_FACTORY_DIR", "")
    if not base:
        return JSONResponse({"error": "CONTENT_FACTORY_DIR не задан"}, status_code=400)
    folder = Path(base) / "autopost" / "images"
    folder.mkdir(parents=True, exist_ok=True)

    name = os.path.basename(file.filename or "")
    ext = Path(name).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return JSONResponse({"error": "Только jpg, png, webp или gif."}, status_code=400)

    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        return JSONResponse({"error": "Файл больше 15 МБ."}, status_code=400)

    # Одноимённый файл не затираем: та картинка может стоять у поста в очереди.
    stem = Path(name).stem or "photo"
    target = folder / f"{stem}{ext}"
    n = 2
    while target.exists():
        target = folder / f"{stem}-{n}{ext}"
        n += 1
    target.write_bytes(data)
    return JSONResponse({"name": target.name})


@router.get("/api/content/video/summary")
def content_video_summary() -> JSONResponse:
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"not_configured": True, "message": "Задайте VIDEO_FACTORY_DIR в .env Axiom.",
                             "queue": [], "counts": {}, "deepseek_ready": bool(os.getenv("DEEPSEEK_API_KEY"))})
    queue = _read_json(base / "data" / "queue-state.json", {"items": []})
    items = queue.get("items", [])
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return JSONResponse({"not_configured": False, "base": str(base), "queue": items[-12:][::-1],
                         "counts": counts, "total": len(items),
                         "deepseek_ready": bool(os.getenv("DEEPSEEK_API_KEY")),
                         "editorial_model": os.getenv("DEEPSEEK_EDITORIAL_MODEL", "deepseek-v4-pro")})


@router.post("/api/content/video/upload")
async def content_video_upload(file: UploadFile = File(...), title: str = Form("")) -> JSONResponse:
    """Загрузить собственный MP4 в очередь. Публикация здесь невозможна."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() != ".mp4":
        return JSONResponse({"error": "Загрузите MP4-файл."}, status_code=400)
    if file.content_type and file.content_type not in {"video/mp4", "application/octet-stream"}:
        return JSONResponse({"error": "Поддерживается только видео MP4."}, status_code=400)

    uploads = base / "intake" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    source_id = f"upload-{uuid.uuid4().hex[:12]}"
    target = uploads / f"{source_id}.mp4"
    try:
        with target.open("wb") as output:
            shutil.copyfileobj(file.file, output, length=1024 * 1024)
        size = target.stat().st_size
        if size == 0:
            target.unlink(missing_ok=True)
            return JSONResponse({"error": "Файл пустой."}, status_code=400)
        if size > 1024 * 1024 * 1024:
            target.unlink(missing_ok=True)
            return JSONResponse({"error": "Для первого запуска лимит файла — 1 ГБ."}, status_code=400)
    finally:
        await file.close()

    path = base / "data" / "queue-state.json"
    queue = _read_json(path, {"next_sequence": 1, "items": []})
    sequence = int(queue.get("next_sequence") or 1)
    item = {
        "sequence": sequence,
        "source": "upload",
        "channel": "Свой ролик",
        "source_id": source_id,
        "source_url": "",
        "title": (title.strip() or Path(filename).stem),
        "local_path": str(target.relative_to(base)).replace("\\", "/"),
        "status": "downloaded",
        "approved": False,
        "published": False,
    }
    items = queue.get("items", [])
    items.append(item)
    queue["items"] = items
    queue["next_sequence"] = sequence + 1
    _write_json(path, queue)
    return JSONResponse({"ok": True, "item": item})


@router.get("/api/content/video/sources")
def content_video_sources() -> JSONResponse:
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    sources = _read_json(base / "config" / "sources.json", {"youtube_channels": [], "donors": []})
    queue = _read_json(base / "data" / "queue-state.json", {"items": []})
    legacy = [
        {"platform": "youtube", "name": x.get("name", "Без имени"),
         "url": x.get("shorts_url", ""), "daily_limit": x.get("daily_limit", 0),
         "enabled": x.get("enabled", True)}
        for x in sources.get("youtube_channels", [])
    ]
    return JSONResponse({"channels": [*legacy, *sources.get("donors", [])],
                         "donors": queue.get("items", [])[::-1]})


@router.post("/api/content/video/sources")
def content_video_add_source(body: dict = Body(...)) -> JSONResponse:
    """Save a donor account. Adding it does not download or reuse any video."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    platform = str(body.get("platform", "")).strip().lower()
    name = str(body.get("name", "")).strip()
    url = str(body.get("url", "")).strip()
    if platform not in {"youtube", "instagram"}:
        return JSONResponse({"error": "Выберите YouTube или Instagram."}, status_code=400)
    if not name or not url.startswith(("https://", "http://")):
        return JSONResponse({"error": "Укажите название и корректную ссылку https://."}, status_code=400)
    if platform == "youtube" and "youtube." not in url and "youtu.be" not in url:
        return JSONResponse({"error": "Для YouTube нужна ссылка на YouTube."}, status_code=400)
    if platform == "instagram" and "instagram.com" not in url:
        return JSONResponse({"error": "Для Instagram нужна ссылка на Instagram."}, status_code=400)
    try:
        daily_limit = max(1, min(20, int(body.get("daily_limit", 3))))
    except (TypeError, ValueError):
        daily_limit = 3
    path = base / "config" / "sources.json"
    sources = _read_json(path, {"youtube_channels": [], "donors": []})
    entries = sources.setdefault("donors", [])
    if any(str(x.get("url", "")).rstrip("/") == url.rstrip("/") for x in entries):
        return JSONResponse({"error": "Этот донор уже есть в списке."}, status_code=409)
    item = {"id": f"{platform}-{uuid.uuid4().hex[:10]}", "platform": platform,
            "name": name, "url": url, "daily_limit": daily_limit, "enabled": True}
    entries.append(item)
    _write_json(path, sources)
    return JSONResponse({"ok": True, "item": item})


@router.get("/api/content/video/plan")
def content_video_plan() -> JSONResponse:
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    plan = _read_json(base / "data" / "editorial-plan.json", {"items": []})
    return JSONResponse({"items": plan.get("items", [])})


@router.post("/api/content/video/plan")
def content_video_plan_save(body: dict = Body(...)) -> JSONResponse:
    """Save a manually approved production/publication calendar entry."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    title = str(body.get("title", "")).strip()
    if not title:
        return JSONResponse({"error": "Укажите название материала."}, status_code=400)
    allowed_statuses = {"brief", "production", "review", "scheduled", "published"}
    status = str(body.get("status", "brief"))
    if status not in allowed_statuses:
        return JSONResponse({"error": "Неизвестный статус плана."}, status_code=400)
    item = {
        "id": str(body.get("id") or datetime.now(timezone.utc).strftime("video-%Y%m%d-%H%M%S-%f")),
        "title": title, "platform": str(body.get("platform", "YouTube Shorts")).strip() or "YouTube Shorts",
        "production_date": str(body.get("production_date", "")).strip(),
        "publish_date": str(body.get("publish_date", "")).strip(), "status": status,
        "source_url": str(body.get("source_url", "")).strip(),
    }
    path = base / "data" / "editorial-plan.json"
    plan = _read_json(path, {"items": []})
    items = [x for x in plan.get("items", []) if str(x.get("id")) != item["id"]]
    items.append(item)
    path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"ok": True, "item": item})


@router.post("/api/content/video/editorial-pack")
def content_video_editorial_pack(body: dict = Body(...)) -> JSONResponse:
    """Create a local editorial pack. This never calls DeepSeek or publishes a video."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    required = ("topic", "audience", "goal", "format", "cta")
    brief = {key: str(body.get(key, "")).strip() for key in required}
    missing = [key for key, value in brief.items() if not value]
    if missing:
        return JSONResponse({"error": f"Заполните: {', '.join(missing)}"}, status_code=400)
    source_url = str(body.get("source_url", "")).strip()
    if source_url:
        brief["source_url"] = source_url
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    briefs = base / "intake" / "editorial-briefs"
    briefs.mkdir(parents=True, exist_ok=True)
    brief_path = briefs / f"brief-{stamp}.json"
    brief_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.spike.cli", "editorial-pack", str(brief_path), "--output", str(base / "artifacts" / "editorial")],
            cwd=base, capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return JSONResponse({"error": f"Не удалось собрать пакет: {exc}"}, status_code=502)
    if result.returncode != 0:
        return JSONResponse({"error": result.stderr.strip() or "Сборка пакета завершилась с ошибкой."}, status_code=502)
    manifest = json.loads(result.stdout)
    return JSONResponse({"ok": True, "brief": str(brief_path), "manifest": manifest,
                         "note": "Созданы задания для 11 скиллов. DeepSeek ещё не запускался."})


def _video_factory_dir() -> Path | None:
    value = os.getenv("VIDEO_FACTORY_DIR", "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.exists() else None


def _read_json(path: Path, fallback: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
