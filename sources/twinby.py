from __future__ import annotations

from core.models import Profile
from sources.base import DatingSource


class TwinbySource(DatingSource):
    name = "twinby"
    title = "Twinby (Android)"

    async def start(self) -> None:
        raise NotImplementedError(
            "Адаптер Twinby ещё не реализован. Потребуется эмулятор Android и "
            "uiautomator2/Appium."
        )

    async def next_profile(self) -> Profile | None:
        raise NotImplementedError

    async def like(self, message: str | None = None) -> None:
        raise NotImplementedError

    async def skip(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        return None
