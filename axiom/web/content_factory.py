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
import re
import subprocess
import sys
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import urlopen

from fastapi import APIRouter, Body, File, Form, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from integrations import content_sheet, content_writer, speech
from agent import llm

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
async def content_video_upload(file: UploadFile = File(...), title: str = Form(""), production_id: str = Form("")) -> JSONResponse:
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
    production_id = production_id.strip()
    jobs_path = base / "data" / "production-jobs.json"
    jobs = _read_json(jobs_path, {"items": []})
    job = next((x for x in jobs.get("items", []) if x.get("id") == production_id), None) if production_id else None
    if production_id and job is None:
        target.unlink(missing_ok=True)
        return JSONResponse({"error": "Задача на монтаж не найдена."}, status_code=404)
    item = {
        "sequence": sequence,
        "source": "upload",
        "channel": "Оригинальное производство" if job else "Свой ролик",
        "source_id": source_id,
        "source_url": str(job.get("source_url", "")) if job else "",
        "title": (title.strip() or str(job.get("title", "")).strip() or str(job.get("brief", {}).get("topic", "")).strip() or Path(filename).stem),
        "local_path": str(target.relative_to(base)).replace("\\", "/"),
        "status": "rendered",
        "approved": False,
        "published": False,
    }
    if job:
        item["production_id"] = job["id"]
        item["script_id"] = job["script_id"]
        job["status"] = "review_mp4"
        job["queue_sequence"] = sequence
        job["uploaded_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(jobs_path, jobs)
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


def _youtube_json(url: str) -> dict:
    """Small dependency-free client for public YouTube Data API reads."""
    try:
        with urlopen(url, timeout=18) as response:  # noqa: S310 -- Google API URL built below
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"YouTube API вернул {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Не удалось соединиться с YouTube API: {exc.reason}") from exc


def _script_section(script: str, heading: str) -> str:
    """Read a bounded editor section from the deliberately headed AI deliverable."""
    pattern = rf"(?:^|\n)\s*(?:\d+\)\s*)?{re.escape(heading)}\s*[:—-]?\s*(.*?)(?=\n\s*(?:\d+\)\s*)?[А-ЯЁA-Z][А-ЯЁA-Z /_-]{{2,}}\s*[:—-]|\Z)"
    match = re.search(pattern, script, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _youtube_channel_id(source_url: str, api_key: str) -> str:
    parsed = urlparse(source_url)
    bits = [x for x in parsed.path.split("/") if x]
    if len(bits) >= 2 and bits[0] == "channel":
        return bits[1]
    handle = next((x[1:] for x in bits if x.startswith("@")), "")
    if not handle:
        raise RuntimeError("Для YouTube-радара укажите ссылку вида youtube.com/@имя или /channel/ID.")
    endpoint = "https://www.googleapis.com/youtube/v3/channels?part=contentDetails,snippet&forHandle=" + quote(handle) + "&key=" + quote(api_key)
    items = _youtube_json(endpoint).get("items", [])
    if not items:
        raise RuntimeError("YouTube-канал не найден по этой ссылке.")
    return str(items[0]["id"])


@router.get("/api/content/video/donor-radar")
def content_video_donor_radar(days: int = 7) -> JSONResponse:
    """Return recent public YouTube uploads ranked against the donor's own baseline.

    Instagram deliberately is not scraped: its data needs an authorised Meta connection.
    """
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    days = max(1, min(15, int(days)))
    sources = _read_json(base / "config" / "sources.json", {"youtube_channels": [], "donors": []})
    donors = [x for x in sources.get("donors", []) if x.get("enabled", True)]
    donors += [{"platform": "youtube", "name": x.get("name", "Без имени"), "url": x.get("shorts_url", ""), "enabled": x.get("enabled", True)} for x in sources.get("youtube_channels", []) if x.get("enabled", True)]
    api_key = os.getenv("YOUTUBE_DATA_API_KEY", "").strip()
    result: dict = {"period_days": days, "youtube_ready": bool(api_key), "instagram_ready": False,
                    "candidates": [], "warnings": []}
    if any(x.get("platform") == "instagram" for x in donors):
        result["warnings"].append("Instagram: для метрик и свежих Reels подключите профессиональный аккаунт через Meta. Публичные страницы не сканируем.")
    youtube = [x for x in donors if x.get("platform") == "youtube"]
    if youtube and not api_key:
        result["warnings"].append("YouTube: добавьте YOUTUBE_DATA_API_KEY на сервере, чтобы радар получил последние загрузки и публичные метрики.")
        return JSONResponse(result)
    now = datetime.now(timezone.utc)
    for donor in youtube:
        try:
            channel_id = _youtube_channel_id(str(donor.get("url", "")), api_key)
            channel = _youtube_json("https://www.googleapis.com/youtube/v3/channels?part=contentDetails&id=" + quote(channel_id) + "&key=" + quote(api_key))
            uploads = channel.get("items", [])[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            page = _youtube_json("https://www.googleapis.com/youtube/v3/playlistItems?part=snippet,contentDetails&maxResults=12&playlistId=" + quote(uploads) + "&key=" + quote(api_key))
            raw = page.get("items", [])
            ids = [str(x.get("contentDetails", {}).get("videoId", "")) for x in raw if x.get("contentDetails", {}).get("videoId")]
            stats_data = _youtube_json("https://www.googleapis.com/youtube/v3/videos?part=statistics&id=" + quote(",".join(ids)) + "&key=" + quote(api_key)) if ids else {"items": []}
            stats = {str(x["id"]): x.get("statistics", {}) for x in stats_data.get("items", [])}
            all_items = []
            for row in raw:
                snippet = row.get("snippet", {})
                video_id = str(row.get("contentDetails", {}).get("videoId", ""))
                published = snippet.get("publishedAt", "")
                try:
                    published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                except ValueError:
                    continue
                views = int(stats.get(video_id, {}).get("viewCount", 0))
                age_hours = max((now - published_dt).total_seconds() / 3600, 1)
                all_items.append({"video_id": video_id, "title": snippet.get("title", "Без названия"), "url": f"https://www.youtube.com/watch?v={video_id}", "published_at": published, "views": views, "likes": int(stats.get(video_id, {}).get("likeCount", 0)), "age_hours": round(age_hours, 1), "velocity": round(views / age_hours, 1)})
            baseline = median([x["views"] for x in all_items] or [1])
            for x in all_items[:3]:
                age = now - datetime.fromisoformat(x["published_at"].replace("Z", "+00:00"))
                if age.total_seconds() <= days * 86400:
                    x.update({"platform": "youtube", "donor": donor.get("name", "YouTube"), "outlier_score": round(x["views"] / max(baseline, 1), 2), "recommended": False})
                    result["candidates"].append(x)
        except Exception as exc:  # one donor must not break the radar
            result["warnings"].append(f"{donor.get('name', 'YouTube')}: {exc}")
    if result["candidates"]:
        max(result["candidates"], key=lambda x: (x["outlier_score"], x["velocity"]))["recommended"] = True
    return JSONResponse(result)


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
    path = base / "data" / "editorial-plan.json"
    plan = _read_json(path, {"items": []})
    existing = next((x for x in plan.get("items", []) if str(x.get("id")) == str(body.get("id"))), {})
    item = {
        "id": str(body.get("id") or datetime.now(timezone.utc).strftime("video-%Y%m%d-%H%M%S-%f")),
        "title": title, "platform": str(body.get("platform", "YouTube Shorts")).strip() or "YouTube Shorts",
        "production_date": str(body.get("production_date", "")).strip(),
        "publish_date": str(body.get("publish_date", "")).strip(), "status": status,
        "source_url": str(body.get("source_url", "")).strip(),
        "caption": str(body.get("caption", existing.get("caption", ""))).strip(),
        "destination": str(body.get("destination", existing.get("destination", ""))).strip(),
        "local_path": str(body.get("local_path", existing.get("local_path", ""))).strip(),
        "video_sequence": body.get("video_sequence", existing.get("video_sequence")),
    }
    items = [x for x in plan.get("items", []) if str(x.get("id")) != item["id"]]
    items.append(item)
    path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"ok": True, "item": item})


@router.post("/api/content/video/plan/{item_id}/approve")
def content_video_plan_approve(item_id: str) -> JSONResponse:
    """Explicit approval of title/caption before a card enters the publish queue."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    path = base / "data" / "editorial-plan.json"
    plan = _read_json(path, {"items": []})
    item = next((x for x in plan.get("items", []) if x.get("id") == item_id), None)
    if item is None:
        return JSONResponse({"error": "Карточка публикации не найдена."}, status_code=404)
    if not str(item.get("caption", "")).strip():
        return JSONResponse({"error": "Сначала добавьте описание к ролику."}, status_code=400)
    item["status"] = "scheduled"
    item["approved_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(path, plan)
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


@router.post("/api/content/video/generate-script")
def content_video_generate_script(body: dict = Body(...)) -> JSONResponse:
    """Explicit paid call: generate a reviewable short-video script, never publish."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    topic = str(body.get("topic", "")).strip()
    if not topic:
        return JSONResponse({"error": "Укажите тему ролика."}, status_code=400)
    model = os.getenv("DEEPSEEK_EDITORIAL_MODEL", "deepseek:deepseek-chat")
    if ":" not in model:
        model = f"deepseek:{model}"
    if not llm.available(model):
        return JSONResponse({"error": "DeepSeek не подключён: добавьте ключ в .env Axiom."}, status_code=400)
    brief = {key: str(body.get(key, "")).strip() for key in ("topic", "audience", "goal", "format", "cta", "source_url", "donor_analysis")}
    prompt = f"""Создай оригинальный сценарий вертикального ролика на русском. Не копируй источник буквально.
Тема: {brief['topic']}
Аудитория: {brief['audience']}
Цель: {brief['goal']}
Формат: {brief['format']}
CTA: {brief['cta']}
Вывод разбора донора (используй только как механизм, не копируй текст или структуру буквально): {brief['donor_analysis'] or 'нет'}
Верни строго по разделам: 1) ХУК 0–3 сек, 2) СЦЕНАРИЙ ОЗВУЧКИ с таймкодами до 45 сек, 3) МОНТАЖНОЕ ТЗ: AI-аватар, скринкаст, B-roll, субтитры и переходы для каждого блока, 4) ЗАГОЛОВОК, 5) ОПИСАНИЕ для YouTube Shorts и Instagram Reels, 6) ФИНАЛЬНЫЙ CTA, 7) ПЕРВЫЙ ОТВЕТ В ЛС человеку, который написал кодовое слово. Не копируй формулировки источника.
"""
    try:
        script = llm.text(model, system="Ты редактор коротких видео. Пиши конкретно, честно и без обещаний результата.",
                          messages=[{"role": "user", "content": prompt}], max_tokens=1100, timeout=90)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Не удалось получить сценарий: {exc}"}, status_code=502)
    path = base / "data" / "scripts.json"
    saved = _read_json(path, {"items": []})
    item = {"id": f"script-{uuid.uuid4().hex[:12]}", "created_at": datetime.now(timezone.utc).isoformat(),
            "brief": brief, "script": script, "model": model, "status": "review"}
    saved.setdefault("items", []).append(item)
    _write_json(path, saved)
    return JSONResponse({"ok": True, "item": item, "script": script})


@router.post("/api/content/video/analyze-donor")
def content_video_analyze_donor(body: dict = Body(...)) -> JSONResponse:
    """Explicit editorial analysis of a donor link; never downloads or copies it."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    url = str(body.get("url", "")).strip()
    notes = str(body.get("notes", "")).strip()
    if not url.startswith(("https://", "http://")):
        return JSONResponse({"error": "Добавьте ссылку на конкретный ролик донора."}, status_code=400)
    model = os.getenv("DEEPSEEK_EDITORIAL_MODEL", "deepseek:deepseek-chat")
    if ":" not in model:
        model = f"deepseek:{model}"
    if not llm.available(model):
        return JSONResponse({"error": "DeepSeek не подключён."}, status_code=400)
    prompt = f"""Разбери донорский короткий ролик только по описанию и заметкам ниже. Не выдумывай, что видел ролик; неизвестное помечай «нужна проверка». Не копируй фразы и сценарий.
Ссылка: {url}
Заметки пользователя: {notes or 'нет'}
Верни компактно по разделам: ТЕМА; HOOK первых 3 секунд; СТРУКТУРА удержания; ВИЗУАЛ/МОНТАЖ; CTA/ВОРОНКА; ЧТО БЕРЁМ КАК МЕХАНИКУ; ЧТО НЕЛЬЗЯ КОПИРОВАТЬ; ОРИГИНАЛЬНЫЙ УГОЛ для Axiom."""
    try:
        analysis = llm.text(model, system="Ты редактор-аналитик коротких видео. Анализируй механику, а не копируй чужое произведение.", messages=[{"role": "user", "content": prompt}], max_tokens=1000, timeout=90)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Не удалось разобрать донора: {exc}"}, status_code=502)
    path = base / "data" / "donor-analyses.json"
    saved = _read_json(path, {"items": []})
    item = {"id": f"donor-{uuid.uuid4().hex[:12]}", "created_at": datetime.now(timezone.utc).isoformat(), "url": url, "notes": notes, "analysis": analysis, "model": model}
    saved.setdefault("items", []).append(item)
    _write_json(path, saved)
    return JSONResponse({"ok": True, "item": item})


@router.post("/api/content/video/analyze-donor-file")
async def content_video_analyze_donor_file(file: UploadFile = File(...), source_url: str = Form(""), visual_notes: str = Form("")) -> JSONResponse:
    """Transcribe an authorised donor MP4 before analysis; it never republishes the source."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    filename = Path(file.filename or "donor.mp4").name
    if Path(filename).suffix.lower() != ".mp4":
        return JSONResponse({"error": "Для фактического разбора загрузите MP4."}, status_code=400)
    incoming = base / "intake" / "donor-analysis"
    incoming.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    video_path = incoming / f"donor-{token}.mp4"
    try:
        with video_path.open("wb") as output:
            shutil.copyfileobj(file.file, output, length=1024 * 1024)
    finally:
        await file.close()
    if not video_path.exists() or video_path.stat().st_size == 0:
        video_path.unlink(missing_ok=True)
        return JSONResponse({"error": "Файл пустой."}, status_code=400)
    if video_path.stat().st_size > 500 * 1024 * 1024:
        video_path.unlink(missing_ok=True)
        return JSONResponse({"error": "Лимит MP4 для разбора — 500 МБ."}, status_code=400)
    transcript_path = base / "artifacts" / "transcripts" / f"donor-{token}.json"
    try:
        result = await run_in_threadpool(subprocess.run,
            [sys.executable, "-m", "app.media.transcribe", str(video_path), str(transcript_path), "--model", "base"],
            cwd=base, capture_output=True, text=True, encoding="utf-8", timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return JSONResponse({"error": f"Не удалось запустить расшифровку: {exc}"}, status_code=502)
    if result.returncode != 0 or not transcript_path.exists():
        return JSONResponse({"error": f"Расшифровка не завершилась: {(result.stderr or result.stdout).strip()[-700:]}"}, status_code=502)
    transcript_payload = _read_json(transcript_path, {})
    transcript = " ".join(str(x.get("text", "")).strip() for x in transcript_payload.get("segments", [])).strip()
    if not transcript:
        return JSONResponse({"error": "В речи ролика не найден текст. Добавьте визуальные заметки и разберите ссылкой."}, status_code=400)
    model = os.getenv("DEEPSEEK_EDITORIAL_MODEL", "deepseek:deepseek-chat")
    if ":" not in model:
        model = f"deepseek:{model}"
    if not llm.available(model):
        return JSONResponse({"error": "DeepSeek не подключён."}, status_code=400)
    origin = source_url.strip() or f"локальный MP4: {filename}"
    notes = f"ФАКТИЧЕСКАЯ РАСШИФРОВКА: {transcript}\nВИЗУАЛЬНЫЕ ЗАМЕТКИ: {visual_notes.strip() or 'не добавлены'}"
    prompt = f"""Разбери донорский короткий ролик по фактической расшифровке и визуальным заметкам. Не выдумывай невидимые кадры. Не копируй фразы и сценарий.
Источник: {origin}
{notes}
Верни компактно по разделам: ТЕМА; HOOK первых 3 секунд; СТРУКТУРА удержания; ВИЗУАЛ/МОНТАЖ; CTA/ВОРОНКА; ЧТО БЕРЁМ КАК МЕХАНИКУ; ЧТО НЕЛЬЗЯ КОПИРОВАТЬ; ОРИГИНАЛЬНЫЙ УГОЛ для Axiom."""
    try:
        analysis = await run_in_threadpool(llm.text, model, system="Ты редактор-аналитик коротких видео. Анализируй механику, а не копируй чужое произведение.", messages=[{"role": "user", "content": prompt}], max_tokens=1000, timeout=90)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Не удалось разобрать донора: {exc}"}, status_code=502)
    path = base / "data" / "donor-analyses.json"
    saved = _read_json(path, {"items": []})
    item = {"id": f"donor-{uuid.uuid4().hex[:12]}", "created_at": datetime.now(timezone.utc).isoformat(), "url": source_url.strip(), "notes": visual_notes.strip(), "analysis": analysis, "model": model, "transcript_path": str(transcript_path.relative_to(base)), "transcript": transcript}
    saved.setdefault("items", []).append(item)
    _write_json(path, saved)
    return JSONResponse({"ok": True, "item": item, "analysis": analysis})


@router.get("/api/content/video/donor-analyses")
def content_video_donor_analyses() -> JSONResponse:
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    return JSONResponse({"items": _read_json(base / "data" / "donor-analyses.json", {"items": []}).get("items", [])[-12:][::-1]})


@router.get("/api/content/video/scripts")
def content_video_scripts() -> JSONResponse:
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    scripts = _read_json(base / "data" / "scripts.json", {"items": []})
    return JSONResponse({"items": scripts.get("items", [])[-20:][::-1]})


@router.get("/api/content/video/production-jobs")
def content_video_production_jobs() -> JSONResponse:
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    jobs = _read_json(base / "data" / "production-jobs.json", {"items": []})
    return JSONResponse({"items": jobs.get("items", [])[-20:][::-1]})


@router.post("/api/content/video/scripts/{script_id}/approve-production")
def content_video_approve_script_for_production(script_id: str, body: dict = Body(default={})) -> JSONResponse:
    """Human gate between original editorial output and any montage work."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    scripts_path = base / "data" / "scripts.json"
    scripts = _read_json(scripts_path, {"items": []})
    script = next((x for x in scripts.get("items", []) if x.get("id") == script_id), None)
    if script is None:
        return JSONResponse({"error": "Сценарий не найден."}, status_code=404)
    jobs_path = base / "data" / "production-jobs.json"
    jobs = _read_json(jobs_path, {"items": []})
    existing = next((x for x in jobs.get("items", []) if x.get("script_id") == script_id), None)
    if existing:
        return JSONResponse({"ok": True, "item": existing, "already_exists": True})
    script["status"] = "approved_for_production"
    script["approved_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(scripts_path, scripts)
    chosen_title = str(body.get("title", "")).strip()
    job = {
        "id": f"production-{uuid.uuid4().hex[:12]}", "script_id": script_id,
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "waiting_mp4",
        "brief": script.get("brief", {}), "script": script.get("script", ""), "title": chosen_title,
        "source_url": script.get("brief", {}).get("source_url", ""),
    }
    jobs.setdefault("items", []).append(job)
    _write_json(jobs_path, jobs)
    return JSONResponse({"ok": True, "item": job})


@router.post("/api/content/video/queue/{sequence}/approve")
def content_video_approve(sequence: int) -> JSONResponse:
    """Explicit human approval. It never sends video to a social network."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    path = base / "data" / "queue-state.json"
    queue = _read_json(path, {"items": []})
    for item in queue.get("items", []):
        if int(item.get("sequence", -1)) == sequence:
            if not item.get("local_path"):
                return JSONResponse({"error": "Сначала загрузите готовый MP4 ролик."}, status_code=400)
            item["approved"] = True
            item["status"] = "approved"
            _write_json(path, queue)
            return JSONResponse({"ok": True, "item": item})
    return JSONResponse({"error": "Ролик не найден."}, status_code=404)


@router.post("/api/content/video/queue/{sequence}/prepare-publish")
def content_video_prepare_publish(sequence: int) -> JSONResponse:
    """Create editable publication cards; never uploads to social platforms."""
    base = _video_factory_dir()
    if base is None:
        return JSONResponse({"error": "Задайте VIDEO_FACTORY_DIR в .env Axiom."}, status_code=400)
    queue = _read_json(base / "data" / "queue-state.json", {"items": []})
    video = next((x for x in queue.get("items", []) if int(x.get("sequence", -1)) == sequence), None)
    if not video or not video.get("approved") or not video.get("local_path"):
        return JSONResponse({"error": "Нужен одобренный загруженный MP4."}, status_code=400)
    path = base / "data" / "editorial-plan.json"
    plan = _read_json(path, {"items": []})
    title = str(video.get("title") or "Новый ролик")
    script = ""
    if video.get("production_id"):
        jobs = _read_json(base / "data" / "production-jobs.json", {"items": []})
        job = next((x for x in jobs.get("items", []) if x.get("id") == video.get("production_id")), {})
        script = str(job.get("script", ""))
    generated_title = _script_section(script, "ЗАГОЛОВОК")
    generated_caption = _script_section(script, "ОПИСАНИЕ")
    if generated_title:
        title = generated_title.splitlines()[0][:120]
    caption = generated_caption or f"{title}\n\nНапиши «КЛУБ» в комментариях — пришлю видео с AI-клубом изнутри."
    created = []
    for platform in ("YouTube Shorts", "Instagram Reels"):
        if any(int(x.get("video_sequence", -1)) == sequence and x.get("platform") == platform for x in plan.get("items", [])):
            continue
        card = {"id": f"publish-{uuid.uuid4().hex[:12]}", "video_sequence": sequence, "title": title,
                "caption": caption, "platform": platform, "production_date": "", "publish_date": "",
                "status": "review", "source_url": "", "local_path": video["local_path"],
                "destination": "https://studio.youtube.com/" if platform == "YouTube Shorts" else "https://www.instagram.com/"}
        plan.setdefault("items", []).append(card)
        created.append(card)
    _write_json(path, plan)
    return JSONResponse({"ok": True, "items": created})


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
