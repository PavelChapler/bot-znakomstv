from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from core.models import Profile


class DatingSource(ABC):
    """Адаптер источника знакомств. Контракт для всего: TG-боты, VK,
    Android-приложения. Только UI-операции, никакой логики оценки."""

    name: str = ""
    title: str = ""

    @abstractmethod
    async def start(self) -> None:
        """Подключиться, авторизоваться, выйти на режим просмотра анкет."""

    @abstractmethod
    async def next_profile(self) -> Profile | None:
        """Получить следующую анкету. None — анкеты закончились / источник недоступен."""

    @abstractmethod
    async def like(self, message: str | None = None) -> None:
        """Лайкнуть текущую анкету и переключиться на следующую.

        Если задан `message` и источник умеет лайки с сообщением — отправляет
        его (например, в Леонардо: нажать «💌», затем послать текст). Если
        не умеет — деградирует до обычного лайка."""

    @abstractmethod
    async def skip(self) -> None:
        """Пропустить текущую анкету."""

    @abstractmethod
    async def stop(self) -> None:
        """Корректно закрыть соединение/освободить ресурсы."""

    async def scan_history_for_incoming(self) -> dict[str, Any]:
        """Опционально: пробежать историю чата и сохранить incoming-лайки
        в пул (без действий — только запись). Адаптеры переопределяют.

        Возвращает {'saved': N, 'duplicates': M, 'diag': [{...}]}.
        """
        return {"saved": 0, "duplicates": 0, "diag": []}
