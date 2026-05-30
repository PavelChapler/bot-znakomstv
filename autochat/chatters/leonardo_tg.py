"""Chatter для Leonardo TG: DM напрямую той девушке, чей @username
известен (Leonardo выдаёт его при mutual).

Использует shared TelegramClient — иначе конкурировал бы с
`LeonardoTGSource` за один .session файл.
"""

from __future__ import annotations

import logging
import re

from telethon.errors import (
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserPrivacyRestrictedError,
)

from autochat.chatters.base import Chatter
from autochat.models import ConvMessage
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
                if m.out:
                    continue
                if after_int is not None and m.id <= after_int:
                    break  # iter_messages идёт по убыванию id
                ts = int(m.date.timestamp()) if m.date else 0
                new.append(ConvMessage(
                    id=0, conversation_id=0,
                    ts=ts,
                    role="her",
                    text=m.message or "",
                    external_msg_id=str(m.id),
                ))
        except Exception:
            log.exception("TG fetch_new_replies for %s failed", peer)
            return []
        new.sort(key=lambda x: x.ts)
        return new


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
