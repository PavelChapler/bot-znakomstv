from __future__ import annotations

import logging
from io import BytesIO

from PIL import Image

log = logging.getLogger(__name__)


def extract_video_frames(video_bytes: bytes, n: int = 3) -> list[bytes]:
    """Достаёт до n равноотстоящих кадров из видео и возвращает их в JPEG.

    Если imageio не установлен или видео не открывается — возвращает [];
    вызывающий код должен с этим жить (например, пропустить анкету)."""
    if not video_bytes:
        return []

    try:
        import imageio.v2 as imageio  # type: ignore
    except ImportError:
        log.warning(
            "imageio не установлен; кадры из видео не извлекаются. "
            "Поставь зависимости: pip install -r requirements.txt"
        )
        return []

    out: list[bytes] = []
    try:
        reader = imageio.get_reader(BytesIO(video_bytes), "ffmpeg")
    except Exception:
        log.exception("не удалось открыть видео")
        return out

    try:
        try:
            total = reader.count_frames()
        except Exception:
            total = None

        if total and total > 0:
            # n кадров по серединам равных сегментов длины видео
            indices = sorted({int(total * (i + 0.5) / n) for i in range(n)})
            for idx in indices:
                try:
                    out.append(_frame_to_jpeg(reader.get_data(idx)))
                except Exception:
                    log.exception("не удалось извлечь кадр %d", idx)
        else:
            # count_frames может не работать на некоторых форматах —
            # тогда просто берём первые n.
            for i, frame in enumerate(reader):
                if i >= n:
                    break
                try:
                    out.append(_frame_to_jpeg(frame))
                except Exception:
                    log.exception("не удалось закодировать кадр %d", i)
    finally:
        try:
            reader.close()
        except Exception:
            pass

    return out


def _frame_to_jpeg(frame) -> bytes:
    img = Image.fromarray(frame)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()
