from __future__ import annotations

from core.models import Profile
from sources.base import DatingSource


class VKDatingSource(DatingSource):
    name = "vk_dating"
    title = "VK Знакомства"

    async def start(self) -> None:
        raise NotImplementedError("Адаптер VK Знакомства ещё не реализован")

    async def next_profile(self) -> Profile | None:
        raise NotImplementedError

    async def like(self) -> None:
        raise NotImplementedError

    async def skip(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        return None
