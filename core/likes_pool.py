"""Помощник для сохранения профилей в пул лайков.

Фото пишем файлами в data/pool_photos/<source>/<external_id>_<i>.jpg, а в
БД храним только пути. Так и таблица остаётся лёгкой, и удалить лишнее
легко вручную.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import DATA_DIR
from core import db
from core.models import Profile

log = logging.getLogger(__name__)

POOL_PHOTOS_DIR = DATA_DIR / "pool_photos"


async def save_profile(
    profile: Profile, kind: str, profile_url: str | None
) -> bool:
    """Сохранить Profile + метаданные в пул. True — записали, False — дубль."""
    photo_paths = _write_photos(profile)
    return await db.save_liked(
        source=profile.source,
        external_id=profile.external_id,
        kind=kind,
        bio=profile.bio or "",
        profile_url=profile_url,
        photo_paths=photo_paths,
    )


def _write_photos(profile: Profile) -> list[str]:
    if not profile.photos:
        return []
    dir_ = POOL_PHOTOS_DIR / profile.source
    dir_.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for i, photo in enumerate(profile.photos):
        if not isinstance(photo, (bytes, bytearray)):
            continue
        path = dir_ / f"{_safe(profile.external_id)}_{i}.jpg"
        try:
            path.write_bytes(photo)
            out.append(str(path))
        except OSError:
            log.exception("failed to write pool photo %s", path)
    return out


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (s or "x"))
