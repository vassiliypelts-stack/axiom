"""Голосовые заготовки кампании: хранение, выбор и нативная отправка в Telegram.

Зачем. Текст в холодной личке читают как текст — от кого угодно. Голос в ответ на
реплику человека читается иначе: его записал живой человек, потратил на это время
и не побоялся быть узнанным по голосу. Это тот самый объём доверия, ради которого
всё и делается, и получить его синтезом нельзя — ухо ловит TTS быстрее, чем глаз
ловит шаблон. Поэтому здесь НЕТ синтеза речи: только заранее записанные оператором
файлы, которые система умеет доставить так, чтобы Telegram показал их родным
голосовым сообщением (кружок с волной и скоростью 2x), а не «файлом voice.ogg».

Что важно знать про формат. Голосовым Telegram считает сообщение только когда
внутри лежит OGG/OPUS и к нему приложен DocumentAttributeAudio(voice=True) вместе
с волной (waveform). Пришлёшь mp3 или ogg без атрибута — прилетит вложением с
кнопкой «скачать», и весь эффект пропадает: живые люди вложениями не разговаривают.
Конвертацию делает ffmpeg (см. ensure_voice_ogg): оператор грузит что записалось
на телефоне (m4a, mp3, wav), а на выходе всегда корректный voice.

Когда слать — решает не этот модуль, а кампания (voice_after_reply: на каком по
счёту ответе человека). Холодным первым касанием голосовые не уходят: незнакомцу
это агрессивно, а номеру стоит PeerFlood.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import config

# Папка с голосовыми заготовками кампаний (data/voice/).
VOICE_DIR = config.DB_PATH.parent / "voice"

# Предел размера: голосовое — это 10-40 секунд речи, а не лекция. 20 МБ с запасом
# перекрывают любую осмысленную запись и отсекают случайно залитое видео.
MAX_BYTES = 20 * 1024 * 1024

# Что принимаем от оператора на загрузку. Телефон пишет в m4a (iPhone) или ogg
# (Telegram), диктофоны — в mp3/wav. Всё это приводим к ogg/opus сами.
ALLOWED_EXT = {".ogg", ".oga", ".opus", ".m4a", ".mp3", ".wav", ".aac", ".mp4"}


class VoiceError(RuntimeError):
    """Не удалось подготовить голосовое (нет ffmpeg, битый файл и т.п.)."""


def ffmpeg_bin() -> str | None:
    """Путь к ffmpeg или None. Вынесено отдельно, чтобы пульт мог честно сказать
    оператору «конвертер не установлен», а не молча ронять загрузку."""
    return shutil.which("ffmpeg")


def _probe_duration(path: Path) -> int:
    """Длительность в секундах. Нужна для полосы воспроизведения: без неё Telegram
    рисует голосовое с нулевой длиной, и оно выглядит сломанным ещё до нажатия."""
    ff = shutil.which("ffprobe")
    if not ff:
        return 0
    try:
        out = subprocess.run(
            [ff, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return int(float((out.stdout or "0").strip() or 0))
    except Exception:  # noqa: BLE001
        return 0


def ensure_voice_ogg(raw: bytes, filename: str) -> tuple[bytes, int]:
    """Любая запись оператора → (ogg/opus пригодный для voice, длительность в сек).

    Перекодируем ВСЕГДА, даже если на входе уже .ogg: файл из чужого мессенджера
    бывает в vorbis, а не opus, и Telegram такой голосовым не покажет. Моно 48 кГц
    24 кбит/с — это ровно то, во что пишет сам Telegram: чужой битрейт на слух не
    отличить, но по нему отличается технический след отправителя.
    """
    ff = ffmpeg_bin()
    if not ff:
        raise VoiceError(
            "На сервере нет ffmpeg — без него запись не превратить в голосовое "
            "сообщение. Установи: sudo apt install -y ffmpeg")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / (Path(filename or "voice").name or "voice")
        dst = Path(td) / "voice.ogg"
        src.write_bytes(raw)
        try:
            proc = subprocess.run(
                [ff, "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "48000",
                 "-c:a", "libopus", "-b:a", "24k", str(dst)],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired as e:
            raise VoiceError("ffmpeg не уложился в 5 минут — файл слишком большой.") from e
        if proc.returncode != 0 or not dst.exists():
            tail = (proc.stderr or "")[-300:]
            raise VoiceError(f"ffmpeg не смог перекодировать запись: {tail}")
        return dst.read_bytes(), _probe_duration(dst)


def path_of(name: str | None) -> Path | None:
    """Путь к существующей заготовке или None (файл могли удалить с диска руками)."""
    if not name:
        return None
    p = VOICE_DIR / Path(name).name
    return p if p.exists() else None


def _waveform(duration: int) -> bytes:
    """Полоска-«волна» под голосовым.

    Telegram рисует её из 5-битных отсчётов, упакованных подряд. Реальную амплитуду
    считать незачем — её никто не сверяет с записью, но ПУСТАЯ волна даёт ровную
    прямую линию, какой не бывает у живой речи, и это единственное, чем наше
    голосовое визуально отличалось бы от записанного с телефона. Поэтому рисуем
    правдоподобный силуэт речи: неровный, с затуханием к концу фразы.
    """
    import math
    import random

    n = 63                                     # столько отсчётов кладёт сам Telegram
    vals: list[int] = []
    for i in range(n):
        base = math.sin(i / 3.0) * 0.35 + 0.55        # медленная «волна» фразы
        base *= 1.0 - 0.25 * (i / n)                  # к концу реплики голос тише
        v = base + random.uniform(-0.18, 0.18)        # неровность живой речи
        vals.append(max(0, min(31, int(v * 31))))
    bits = "".join(f"{v:05b}" for v in vals)
    bits += "0" * (-len(bits) % 8)
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))


async def send_voice(client, peer, path: Path, duration: int = 0) -> int:
    """Отправить файл как НАСТОЯЩЕЕ голосовое сообщение и вернуть id отправленного.

    Ключевое — voice=True в DocumentAttributeAudio: без него ровно тот же ogg
    приезжает вложением с кнопкой «скачать». Перед отправкой показываем статус
    «записывает голосовое» и держим паузу по длине записи: у собеседника в шапке
    диалога идёт та же надпись, что при живой записи, и голосовое появляется не
    мгновенно, а через столько секунд, сколько его «наговаривали».
    """
    import asyncio
    import random

    from telethon.tl.types import DocumentAttributeAudio

    dur = int(duration or _probe_duration(path) or 0)
    # «Записывает голосовое» + пауза примерно в длину записи (чуть меньше — человек
    # редко жмёт отправку ровно в конце фразы, и сильно дольше тоже не ждут).
    hold = max(2.0, min(float(dur or 8) * random.uniform(0.8, 1.15), 60.0))
    try:
        async with client.action(peer, "record-audio"):
            await asyncio.sleep(hold)
    except Exception:  # noqa: BLE001
        await asyncio.sleep(hold)           # статус не показался — паузу всё равно держим
    sent = await client.send_file(
        peer, str(path),
        voice_note=True,
        attributes=[DocumentAttributeAudio(duration=dur, voice=True,
                                           waveform=_waveform(dur))],
    )
    return sent.id


# Ответы, после которых голос УМЕСТЕН. Человек спросил, возразил или проявил
# интерес — ему есть что слушать. «Не интересно» и «потом» исключены: голосовое
# вдогонку отказу читается как давление и собирает жалобы.
WARM_INTENTS = {"positive", "question", "objection", "agreed"}


def _field(camp, name: str, default=0):
    """Значение колонки кампании, которой в старой базе может не быть. Сюда приходят
    и sqlite3.Row (отсутствующая колонка бросает IndexError), и обычные dict."""
    try:
        val = camp[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if val is None else val


def reached(camp, reply_no: int) -> bool:
    """Пройден ли порог кампании по счёту ответов человека.

    reply_no — какой по счёту ответ человека мы сейчас обрабатываем (1 = первый).
    Кампания задаёт порог voice_after_reply: 0 — голосовые выключены, 1 — после
    первой реплики, 2 — после второй. Порог именно «не раньше», а не «ровно на
    этом»: если на нужном шаге заготовка не ушла (не было подходящей, упал
    ffmpeg), следующий ответ человека даёт ещё одну попытку, а не закрывает тему.

    Отдельно от should_send, потому что вызывается ДО обращения к модели — когда
    намерение собеседника ещё неизвестно, а решить, показывать ли агенту список
    заготовок, уже надо.
    """
    if camp is None:
        return False
    try:
        after = int(_field(camp, "voice_after_reply", 0) or 0)
    except (TypeError, ValueError):
        return False
    return after > 0 and reply_no >= after


def should_send(camp, reply_no: int, intent: str | None) -> bool:
    """Окончательное решение: слать ли голосовое сейчас. Порог по счёту ответов
    плюс смысл последней реплики — голосовое вдогонку отказу читается как давление
    и собирает жалобы, поэтому по умолчанию уходит только заинтересованным."""
    if not reached(camp, reply_no):
        return False
    if _field(camp, "voice_only_interested", 1) and (intent or "") not in WARM_INTENTS:
        return False
    return True


def pick(conn, campaign_id: int, contact_id: int, choice: str | None = None) -> dict | None:
    """Какую заготовку отправить этому человеку — или None, если нечего.

    Уже отправленные ЭТОМУ контакту исключаем (voice_sent): повтор той же записи
    выдаёт автоматизацию мгновеннее любого шаблона в тексте. Если агент назвал
    конкретную заготовку по имени (choice) — берём её, иначе следующую по порядку.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT v.* FROM campaign_voices v "
        "WHERE v.campaign_id=? AND COALESCE(v.enabled,1)=1 AND v.file IS NOT NULL "
        "  AND v.id NOT IN (SELECT voice_id FROM voice_sent WHERE contact_id=?) "
        "ORDER BY COALESCE(v.sort_order,0), v.id", (campaign_id, contact_id),
    ).fetchall()]
    rows = [r for r in rows if path_of(r.get("file"))]
    if not rows:
        return None
    if choice:
        want = str(choice).strip().lower()
        for r in rows:
            nm = (r.get("name") or "").strip().lower()
            if nm and nm in want:
                return r
    return rows[0]


def mark_sent(conn, contact_id: int, voice_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO voice_sent (contact_id, voice_id) VALUES (?,?)",
                 (contact_id, voice_id))
