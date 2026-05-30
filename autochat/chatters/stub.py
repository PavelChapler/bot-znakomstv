"""Заглушка для источников без реализации (Twinby/VK Знакомства).

`can_write` всегда возвращает False — engine помечает разговор как
`failed(reason='unsupported_source')` и больше не дёргает.
"""

from __future__ import annotations

from autochat.chatters.base import Chatter
from autochat.models import ConvMessage


class StubChatter(Chatter):
    name = "stub"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def can_write(self, profile_url: str) -> tuple[bool, str | None]:
        return False, "unsupported_source"

    async def resolve_peer(self, profile_url: str) -> str | None:
        return None

    async def send(self, peer: str, text: str) -> str | None:
        return None

    async def fetch_new_replies(
        self, peer: str, after_msg_id: str | None
    ) -> list[ConvMessage]:
        return []
