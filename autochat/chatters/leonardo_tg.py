"""Chatter для Leonardo TG: DM напрямую той девушке, чей @username
известен (Leonardo выдаёт его при mutual).

Использует shared TelegramClient — иначе конкурировал бы с
`LeonardoTGSource` за один .session файл.
"""

from __future__ import annotations

import logging
import re
from io import BytesIO

from telethon.errors import (
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserPrivacyRestrictedError,
)

from autochat import config as ac_config
from autochat.chatters.base import Chatter
from autochat.models import ConvMessage
from autochat.transcribe import transcribe_audio
from core.telethon_conn import get_shared_client

log = logging.getLogger(__name__)

# Сколько последних сообщений тянуть в fetch_new_replies — должно быть
# больше, чем она напишет за один интервал поллинга (60-90 сек).
FETCH_LIMIT = 30


class TelethonChatter(Chatter):
    name = "leonardo_tg"

    def __init__(self) -> None:
        self.client = None  # type: ignore[assignment]

    async def start(self) -> None:
        self.client = await get_shared_client()

    async def stop(self) -> None:
        # shared, не дисконнектим
        self.client = None  # type: ignore[assignment]

    async def can_write(self, profile_url: str) -> tuple[bool, str | None]:
        handle = _parse_tg_handle(profile_url)
        if not handle:
            return False, f"bad_handle: {profile_url!r}"
        if self.client is None:
            return False, "client_not_started"
        try:
            await self.client.get_entity(handle)
            return True, None
        except (UsernameNotOccupiedError, UsernameInvalidError, ValueError) as e:
            return False, f"username: {e}"
        except UserPrivacyRestrictedError:
            return False, "privacy_restricted"
        except Exception as e:
            log.exception("TG can_write %s failed", handle)
            return False, f"err: {e}"

    async def resolve_peer(self, profile_url: str) -> str | None:
        handle = _parse_tg_handle(profile_url)
        return handle

    async def send(self, peer: str, text: str) -> str | None:
        if self.client is None:
            return None
        try:
            msg = await self.client.send_message(peer, text)
            return str(msg.id) if msg else None
        except FloodWaitError as e:
            log.warning("TG send FloodWait %ds for %s", e.seconds, peer)
            raise  # engine handles globally
        except UserPrivacyRestrictedError:
            log.info("TG send to %s blocked by privacy", peer)
            return None
        except Exception:
            log.exception("TG send to %s failed", peer)
            return None

    async def fetch_new_replies(
        self, peer: str, after_msg_id: str | None
    ) -> list[ConvMessage]:
        if self.client is None:
            return []
        after_int = int(after_msg_id) if after_msg_id else None
        new: list[ConvMessage] = []
        try:
            async for m in self.client.iter_messages(peer, limit=FETCH_LIMIT):
                if after_int is not None and m.id <= after_int:
                    break  # iter_messages идёт по убыванию id
                text = await self._resolve_text(m)
                if not text:
                    continue  # стикер/неподдерживаемое медиа
                ts = int(m.date.timestamp()) if m.date else 0
                new.append(ConvMessage(
                    id=0, conversation_id=0,
                    ts=ts,
                    role="us" if m.out else "her",
                    text=text,
                    external_msg_id=str(m.id),
                ))
        except Exception:
            log.exception("TG fetch_new_replies for %s failed", peer)
            return []
        new.sort(key=lambda x: x.ts)
        return new

    async def fetch_full_history(
        self, peer: str, limit: int = 50
    ) -> list[ConvMessage]:
        if self.client is None:
            return []
        out: list[ConvMessage] = []
        try:
            async for m in self.client.iter_messages(peer, limit=limit):
                text = await self._resolve_text(m)
                if not text:
                    continue  # стикеры / нерасшифровываемое
                ts = int(m.date.timestamp()) if m.date else 0
                out.append(ConvMessage(
                    id=0, conversation_id=0,
                    ts=ts,
                    role="us" if m.out else "her",
                    text=text,
                    external_msg_id=str(m.id),
                ))
        except Exception:
            log.exception("TG fetch_full_history for %s failed", peer)
            return []
        out.sort(key=lambda x: x.ts)
        return out

    async def _resolve_text(self, m) -> str:
        """Текст реплики: либо m.message, либо расшифровка голосового.
        Возвращает «» если ничего полезного нет (стикер и т.п.)."""
        raw = m.message or ""
        if raw.strip():
            return raw
        has_voice = bool(m.voice or m.audio)
        if not has_voice:
            return ""
        if not await ac_config.is_transcribe_voice_enabled():
            return "[голосовое сообщение]"
        buf = BytesIO()
        try:
            await m.download_media(file=buf)
        except Exception:
            log.exception("TG voice download failed msg=%s", m.id)
            return "[голосовое, не скачалось]"
        audio = buf.getvalue()
        if not audio:
            return "[голосовое, пусто]"
        text = await transcribe_audio(audio, mime_type="audio/ogg")
        if not text:
            return "[голосовое, не распознано]"
        return f"[голосовое] {text}"


_TG_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def _parse_tg_handle(url: str) -> str | None:
    s = (url or "").strip().lstrip("@")
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.split("/")[0].split("?")[0]
    if not s:
        return None
    return s if _TG_HANDLE_RE.fullmatch(s) else None
