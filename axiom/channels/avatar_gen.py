"""Авто-фото профиля под пол/имя аккаунта — микс фотостока (Pexels, живые люди с
коммерческой лицензией) и синтетических ИИ-лиц (Gemini, реального человека не
существует). Специально НЕ парсим случайные фото с соцсетей/интернета без
лицензии — так к спам-заблокированному аккаунту не привяжется чужое реальное
лицо. Нужен хотя бы один из ключей PEXELS_API_KEY / GEMINI_API_KEY в .env —
без обоих шаг фото тихо пропускается (как и было).

    python -m channels.avatar_gen --ids 12,13,14
"""
from __future__ import annotations

import argparse
import base64
import random
import uuid
from pathlib import Path

import requests
from pydantic import BaseModel

import config
from channels.ru_names import gender_of
from db import database

PEXELS_QUERIES = {
    # "eastern european" — сток так помечает славянскую внешность; без этого поиск
    # по одним «businessman/businesswoman» отдаёт случайную этничность вперемешку,
    # что рвёт правдоподобие с русским именем персонажа.
    "male": ["eastern european businessman headshot studio", "russian man professional headshot plain background",
             "eastern european man corporate portrait suit"],
    "female": ["eastern european businesswoman headshot studio", "russian woman professional headshot plain background",
               "eastern european woman corporate portrait blazer"],
}

GEMINI_PROMPTS = {
    "male": "Professional headshot photo of a friendly Russian businessman in his late 20s-30s, "
            "plain neutral background, business casual attire, natural lighting, realistic photo, no text, no watermark",
    "female": "Professional headshot photo of a friendly Russian businesswoman in her late 20s-30s, "
              "plain neutral background, business casual attire, natural lighting, realistic photo, no text, no watermark",
}


def _avatars_dir() -> Path:
    d = Path(config.DB_PATH).parent / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Курируемый пул реальных лиц (отобраны вручную Василием, data/faces/{male,female}).
# Приоритетнее стока/ИИ: ставим ОДНО правдоподобное лицо под пол, а не случайного из
# Pexels. Так у аккаунта постоянное лицо, и оно заведомо годное (без водяных знаков).
_FACES_DIR = Path(config.DB_PATH).parent / "faces"


def _from_pool(gender: str) -> bytes | None:
    d = _FACES_DIR / gender
    if not d.exists():
        return None
    files = [p for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    if not files:
        return None
    p = random.choice(files)
    try:
        return _crop_top_square(p.read_bytes())   # квадрат под аватар Telegram
    except Exception:  # noqa: BLE001
        return p.read_bytes()


# Куратор-пул реальных лиц, отобранных вручную (data/faces/male|female). Приоритетнее
# стока/ИИ: ставим ОДНО правдоподобное лицо, а не случайного разного человека каждый раз.
_FACES_DIR = Path(config.DB_PATH).parent / "faces"


def _from_pool(gender: str) -> bytes | None:
    """Случайное лицо нужного пола из курируемого пула (или None, если пула нет)."""
    d = _FACES_DIR / gender
    if not d.exists():
        return None
    files = [p for p in d.iterdir()
             if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    if not files:
        return None
    p = random.choice(files)
    try:
        return _crop_top_square(p.read_bytes())   # квадрат под аватар, как у стока
    except Exception:  # noqa: BLE001 — не JPEG/битый: отдаём как есть
        return p.read_bytes()


def _crop_top_square(data: bytes) -> bytes:
    """Центрированная обрезка до квадрата (без реального распознавания лица —
    жёсткий кроп от верхнего края иногда резал подбородок на некоторых фото)."""
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(data)).convert("RGB")
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    im = im.crop((left, top, left + side, top + side))
    out = BytesIO()
    im.save(out, format="JPEG", quality=90)
    return out.getvalue()


class _PhotoCheck(BaseModel):
    ok: bool
    reason: str


def _verify_headshot(img_bytes: bytes, gender: str, name: str = "") -> bool:
    """Дёшево (Haiku) отбраковывает явный промах стока: не тот пол, групповое
    фото, во весь рост, рисунок, а также явное несоответствие внешности русскому
    имени персонажа (иначе получается нелепое сочетание, режет глаз собеседнику
    в переписке) — поиск по ключевым словам иногда мажет мимо. Без ANTHROPIC-ключа
    или при сбое проверки — не блокируем, доверяем как есть."""
    if not config.ANTHROPIC_API_KEY and not (getattr(config, "ANTHROPIC_API_KEYS", "") or ""):
        return True
    from agent import llm
    gender_ru = "мужчина" if gender == "male" else "женщина"
    name_note = f' по имени «{name}»' if name else ""
    img_b64 = base64.standard_b64encode(img_bytes).decode("ascii")
    try:
        resp = llm.call(lambda c: c.messages.parse(
            model=config.MODEL,
            max_tokens=200,
            system="Ты проверяешь фото-кандидата для делового профиля Telegram-аккаунта. "
                   "Отвечай строго по заданному формату.",
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": (
                    f"На фото должен быть один взрослый человек ({gender_ru}){name_note}, лицо "
                    "крупным планом (портрет/хедшот), деловой или деловой-повседневный стиль, "
                    "адекватный нейтральный кадр, внешность правдоподобна для человека с таким "
                    "русским именем (славянская/восточноевропейская — без бросающегося в глаза "
                    "несоответствия). НЕ подходит: не тот пол, явно другая этничность/регион, "
                    "несколько человек, во весь рост с большим фоном, рисунок/аватарка-мультик, "
                    "странный ракурс. Подходит?"
                )},
            ]}],
            output_format=_PhotoCheck,
        ))
        return resp.parsed_output.ok
    except Exception as e:  # noqa: BLE001 — сбой проверки не должен рушить весь пайплайн
        print(f"  [avatar/verify] {e}")
        return True


def _from_pexels(gender: str) -> bytes | None:
    if not config.PEXELS_API_KEY:
        return None
    query = random.choice(PEXELS_QUERIES[gender])
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": 15, "orientation": "square"},
            headers={"Authorization": config.PEXELS_API_KEY}, timeout=20,
        )
        r.raise_for_status()
        photos = r.json().get("photos") or []
        if not photos:
            return None
        # Pexels не отдаёт готовый квадратный кроп — берём "portrait" (800×1200,
        # уже прицельно обрезан под вертикальный портрет) и дообрезаем до квадрата.
        # Перебираем несколько кандидатов и отсеиваем явный промах поиска Haiku-проверкой
        # (бесплатные повторные попытки — трогаем только сток, не платный Gemini).
        random.shuffle(photos)
        for photo in photos[:5]:
            try:
                img = requests.get(photo["src"]["portrait"], timeout=20)
                img.raise_for_status()
                cropped = _crop_top_square(img.content)
            except Exception as e:  # noqa: BLE001
                print(f"  [avatar/pexels] {e}")
                continue
            if _verify_headshot(cropped, gender):
                return cropped
        return None
    except Exception as e:  # noqa: BLE001 — сток недоступен, вызывающий попробует другой источник
        print(f"  [avatar/pexels] {e}")
        return None


def _from_gemini(gender: str) -> bytes | None:
    if not config.GEMINI_API_KEY:
        return None
    try:
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent",
            params={"key": config.GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": GEMINI_PROMPTS[gender]}]}]},
            timeout=40,
        )
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        for p in parts:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    except Exception as e:  # noqa: BLE001
        print(f"  [avatar/gemini] {e}")
    return None


def generate_photo(gender: str) -> tuple[bytes, str] | None:
    """Приоритет — курируемый пул лиц (data/faces); иначе сток/ИИ (какой ключ задан)."""
    pool = _from_pool(gender)
    if pool:
        return pool, "пул лиц"
    sources: list[tuple[str, "callable"]] = []
    if config.PEXELS_API_KEY:
        sources.append(("сток", _from_pexels))
    if config.GEMINI_API_KEY:
        sources.append(("ИИ-лицо", _from_gemini))
    if not sources:
        return None
    random.shuffle(sources)
    for label, fn in sources:
        data = fn(gender)
        if data:
            return data, label
    return None


def _avatar_gender(fname: str | None) -> str | None:
    """Пол, зашитый в имя авто-фото: gen_<id>_<gender>_<hash>.jpg. Старый формат
    (без пола) или ручная загрузка → None (пол неизвестен)."""
    parts = (fname or "").split("_")
    return parts[2] if len(parts) >= 4 and parts[0] == "gen" and parts[2] in ("male", "female") else None


def ensure_avatar(acc: dict) -> str | None:
    """Генерит фото профиля СТРОГО под пол имени персоны (tg_name). Если фото уже
    есть и его пол совпадает с именем — не трогает. Если пол фото НЕ совпадает с
    именем (напр. имя мужское «Егор», а стоит женское фото после перекатки имени)
    — перегенерирует, чтобы фото и имя не рассинхронились. Возвращает имя файла
    (или прежнее/None, если провайдеры не задали фото)."""
    name = (acc.get("tg_name") or acc.get("label") or "").strip()
    want = gender_of(name)                     # пол по имени персоны (может быть None)
    have = acc.get("avatar")
    # фото есть и (пол имени неизвестен ИЛИ совпадает с полом фото) → оставляем
    if have and (want is None or _avatar_gender(have) == want):
        return have
    gender = want or random.choice(["male", "female"])
    result = generate_photo(gender)
    if not result:
        return have                            # не смогли сгенерить — оставляем что было
    data, source = result
    fname = f"gen_{acc['id']}_{gender}_{uuid.uuid4().hex[:8]}.jpg"
    (_avatars_dir() / fname).write_bytes(data)
    try:
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET avatar=? WHERE id=?", (fname, acc["id"]))
    except Exception as e:  # noqa: BLE001 — файл уже на диске; не роняем весь пакетный прогон
        print(f"  [avatar/db] не записал avatar в базу для #{acc['id']}: {e}")
        return None
    print(f"  профиль: авто-фото ({source}, {gender}) -> {fname}")
    return fname


def run(ids: list[int]) -> None:
    database.init_db()
    ok = 0
    for acc_id in ids:
        with database.get_conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if not row:
            print(f"[skip] аккаунт #{acc_id} не найден")
            continue
        acc = dict(row)
        if acc.get("avatar"):
            print(f"[skip] #{acc_id} «{acc.get('label')}» — фото уже есть ({acc['avatar']})")
            continue
        fname = ensure_avatar(acc)
        if fname:
            print(f"[ok] #{acc_id} «{acc.get('tg_name') or acc.get('label')}» — {fname}")
            ok += 1
        else:
            print(f"[skip] #{acc_id}: нет PEXELS_API_KEY/GEMINI_API_KEY в .env, либо оба источника не дали фото")
    with database.get_conn() as conn:
        database.add_event(
            conn, "info", f"🖼 Авто-фото поставлено: {ok} из {len(ids)}",
            "сток+ИИ микс по полу из имени — файл сохранён, в Telegram уйдёт при следующем «Оформить»",
            level="good" if ok else "warn",
        )
    print(f"\nИтого: {ok} из {len(ids)}")


def main() -> None:
    p = argparse.ArgumentParser(description="Авто-фото профиля (сток+ИИ микс) для аккаунтов")
    p.add_argument("--ids", required=True, help="через запятую: 1,2,3")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()]
    if not ids:
        p.error("пустой список --ids")
    run(ids)


if __name__ == "__main__":
    main()
