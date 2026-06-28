"""Чёрный список анкет.

Два режима матчинга:
1. ТОЧНЫЙ — по (source, external_id). У VK Знакомств external_id == dating
   user_id, не меняется при правке фото/описания/имени. Добавляется кнопкой
   «🚫 В ЧС» под анкетой (там user_id зашит в callback).
2. FUZZY (ручной) — для анкет без user_id (старые сообщения без кнопки,
   добавление по описанию). Запись с external_id='manual:<имя>:<возраст>',
   матч по совпадению имени+возраста И достаточного числа ключевых слов
   из описания. Риск ложных банов выше — поэтому требуем несколько слов.

`_recent` кэширует недавно показанные анкеты: при бане кнопкой имя/возраст
берём отсюда (в callback влезает только id).
"""

from __future__ import annotations

import logging
import re
from io import BytesIO

from PIL import Image

from core import db
from core.models import Profile

log = logging.getLogger(__name__)

_recent: dict[str, Profile] = {}
_RECENT_MAX = 300

# Сколько ключевых слов из ручной записи должно встретиться в новой анкете,
# чтобы счесть её той же (поверх совпадения имени+возраста).
_MIN_KW_HITS = 3

# Порог Хэмминга для dHash (64 бита): ≤ — «то же фото». 10 ≈ устойчиво к
# перекодированию/лёгкой правке, но не путает разные фото.
_PHOTO_THRESHOLD = 10
_DHASH_SIZE = 8  # 8x8 → 64 бита → 16 hex
_MAX_PHOTOS = 4


def dhash(image_bytes: bytes, size: int = _DHASH_SIZE) -> str | None:
    """Перцептивный difference-hash (hex). Устойчив к ресайзу/перекодированию;
    меняется при смене самого фото. None — картинку не прочитать."""
    try:
        img = Image.open(BytesIO(image_bytes)).convert("L").resize(
            (size + 1, size), Image.LANCZOS
        )
        px = img.load()
        bits = 0
        for y in range(size):
            for x in range(size):
                bits = (bits << 1) | (1 if px[x, y] < px[x + 1, y] else 0)
        return f"{bits:0{size * size // 4}x}"
    except Exception:
        log.exception("dhash failed")
        return None


def _hamming(h1: str, h2: str) -> int:
    try:
        return bin(int(h1, 16) ^ int(h2, 16)).count("1")
    except ValueError:
        return 999


def compute_hashes(photos: list) -> list[str]:
    """Хэши первых нескольких фото анкеты (только bytes)."""
    out: list[str] = []
    for p in photos[:_MAX_PHOTOS]:
        if isinstance(p, (bytes, bytearray)):
            h = dhash(bytes(p))
            if h:
                out.append(h)
    return out


def photo_match(
    image_bytes: bytes, hashes: list[str], threshold: int = _PHOTO_THRESHOLD
) -> bool:
    """Близко ли фото к любому из хэшей (по Хэммингу)."""
    h = dhash(image_bytes)
    if not h:
        return False
    return any(_hamming(h, hh) <= threshold for hh in hashes)


async def photo_hashes_for(source: str) -> list[str]:
    """Все фото-хэши всех записей ЧС источника (для матча в ленте)."""
    out: list[str] = []
    for r in await db.blacklist_list(source):
        out.extend(r.get("photo_hashes") or [])
    return out

_NAME_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_AGE_RE = re.compile(r"\b(1[4-9]|[2-9]\d)\b")          # 14..99 (2 цифры)
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{5,}")          # значимые слова ≥5 букв


def _key(source: str, external_id: str) -> str:
    return f"{source}:{external_id}"


def remember(profile: Profile) -> None:
    """Запомнить показанную анкету для бана кнопкой."""
    if not profile.external_id:
        return
    k = _key(profile.source, profile.external_id)
    _recent.pop(k, None)
    _recent[k] = profile
    while len(_recent) > _RECENT_MAX:
        _recent.pop(next(iter(_recent)))


def _name_age_from_bio(bio: str) -> tuple[str | None, int | None]:
    """Первая строка bio у vk_dating — «Имя, возраст»."""
    head = (bio or "").split("\n", 1)[0].strip()
    if not head:
        return None, None
    name, _, rest = head.partition(",")
    digits = "".join(c for c in rest if c.isdigit())
    age = int(digits) if digits else None
    return (name.strip() or None), age


def parse_description(text: str) -> tuple[str | None, int | None, list[str]]:
    """Разобрать вставленное описание анкеты на (имя, возраст, ключевые слова).
    Имя — первое слово; возраст — первое 2-значное число 14-99 (км/одиночные
    цифры игнорируются); ключевые слова — значимые слова ≥5 букв (без имени)."""
    text = (text or "").strip()
    nm = _NAME_RE.search(text)
    name = nm.group(0) if nm else None
    am = _AGE_RE.search(text)
    age = int(am.group(0)) if am else None
    name_l = (name or "").lower()
    keywords: list[str] = []
    for w in _WORD_RE.findall(text.lower()):
        if w == name_l or w in keywords:
            continue
        keywords.append(w)
        if len(keywords) >= 20:
            break
    return name, age, keywords


async def add(source: str, external_id: str, note: str = "") -> tuple[bool, str]:
    """Точный бан по кнопке. Имя/возраст/фото-хэши — из кэша показанных."""
    prof = _recent.get(_key(source, external_id))
    name, age, hashes = (None, None, [])
    if prof is not None:
        name, age = _name_age_from_bio(prof.bio)
        hashes = compute_hashes(prof.photos)
    added = await db.blacklist_add(source, external_id, name, age, hashes, note or None)
    return added, (name or external_id)


async def add_manual(
    source: str, name: str | None, age: int | None, keywords: list[str]
) -> tuple[bool, str]:
    """Ручной fuzzy-бан по описанию (без user_id)."""
    eid = f"manual:{(name or '').lower()}:{age if age is not None else ''}"
    added = await db.blacklist_add(source, eid, name, age, [], None, keywords)
    return added, (name or eid)


async def add_photos(
    source: str, photos: list, name: str | None = None, age: int | None = None
) -> tuple[bool, str]:
    """Fuzzy-бан по фото (форвард анкеты / присланное фото). Матч в ленте —
    по перцептивному хэшу, поэтому user_id не нужен."""
    hashes = compute_hashes(photos)
    if not hashes:
        return False, ""
    eid = f"photo:{hashes[0]}"
    added = await db.blacklist_add(source, eid, name, age, hashes, None)
    return added, (name or f"фото·{hashes[0][:8]}")


async def is_blacklisted(source: str, external_id: str) -> bool:
    """Только точный матч по id."""
    return await db.is_blacklisted(source, external_id)


async def match(
    source: str,
    user_id: str | None,
    name: str | None,
    age: int | None,
    text: str,
) -> bool:
    """Забанена ли анкета: точный матч по user_id ИЛИ fuzzy по ручным записям
    (имя+возраст совпали И ≥min(3,N) ключевых слов из записи есть в описании;
    если ключевых слов в записи нет — достаточно имени+возраста)."""
    if user_id and await db.is_blacklisted(source, str(user_id)):
        return True
    if not name:
        return False
    name_l = name.strip().lower()
    text_l = (text or "").lower()
    for r in await db.blacklist_manual_list(source):
        if (r["name"] or "").strip().lower() != name_l:
            continue
        if r["age"] and age and int(r["age"]) != int(age):
            continue
        kws = r["keywords"] or []
        if not kws:
            return True
        need = min(_MIN_KW_HITS, len(kws))
        hits = sum(1 for k in kws if k in text_l)
        if hits >= need:
            return True
    return False
