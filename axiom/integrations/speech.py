"""Расшифровка голосовых — для мыслей, надиктованных на ходу.

Мысль для поста чаще приходит за рулём или между встречами, когда набирать
текст неудобно. Gemini принимает аудио напрямую и понимает русский, ключ у
проекта уже есть — отдельный STT-сервис ради этого не нужен.
"""
from __future__ import annotations

import base64
import mimetypes

import requests

import config

URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
       "gemini-2.5-flash:generateContent")

PROMPT = (
    "Расшифруй эту голосовую запись на русском языке. "
    "Отдай только текст сказанного, дословно, без пояснений и комментариев. "
    "Расставь знаки препинания и раздели на абзацы по смыслу. "
    "Слова-паразиты и самопоправки сохрани — это живая речь, а не диктант."
)

MAX_BYTES = 20 * 1024 * 1024


class SpeechError(RuntimeError):
    pass


def transcribe(data: bytes, filename: str = "voice.ogg") -> str:
    key = config.GEMINI_API_KEY
    if not key:
        raise SpeechError("GEMINI_API_KEY не задан — расшифровывать нечем.")
    if not data:
        raise SpeechError("Пустая запись.")
    if len(data) > MAX_BYTES:
        raise SpeechError("Запись длиннее 20 МБ — разбейте на части.")

    mime = mimetypes.guess_type(filename)[0] or "audio/ogg"
    try:
        r = requests.post(
            URL, params={"key": key},
            json={"contents": [{"parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": mime,
                                 "data": base64.b64encode(data).decode()}},
            ]}]},
            timeout=180,
        )
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except requests.HTTPError as e:
        body = e.response.text[:200] if e.response is not None else ""
        raise SpeechError(f"Gemini отказал: {e.response.status_code if e.response else '?'} {body}") from e
    except Exception as e:  # noqa: BLE001
        raise SpeechError(f"Не удалось расшифровать: {str(e)[:150]}") from e

    if not text:
        raise SpeechError("В записи не распознана речь.")
    return text
