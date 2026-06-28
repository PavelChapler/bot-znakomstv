"""Контракт Chatter: тонкий адаптер DM-канала источника."""

from __future__ import annotations

from abc import ABC, abstractmethod

from autochat.models import ConvMessage


class Chatter(ABC):
    name: str = ""

    @abstractmethod
    async def start(self) -> None:
        """Поднять подключение/клиент (для долгоживущей сессии)."""

    @abstractmethod
    async def stop(self) -> None:
        """Освободить ресурсы. Shared-клиенты НЕ закрывать."""

    @abstractmethod
    async def can_write(self, profile_url: str) -> tuple[bool, str | None]:
        """(можно_писать, причина_если_нет). НЕ кидать исключений."""

    @abstractmethod
    async def resolve_peer(self, profile_url: str) -> str | None:
        """Преобразовать URL/handle в стабильный peer-id для send/poll.
        None — не смогли распарсить."""

    @abstractmethod
    async def send(self, peer: str, text: str) -> str | None:
        """Отправить текст. Возвращает внешний msg_id (для дедупа) либо None."""

    @abstractmethod
    async def fetch_new_replies(
        self, peer: str, after_msg_id: str | None
    ) -> list[ConvMessage]:
        """Все новые сообщения после after_msg_id, по возрастанию ts — ОБЕИХ
        ролей: её + наши исходящие, включая отправленные ВРУЧНУЮ вне бота. По
        нашим ручным движок понимает, что мы вмешались, и не дублирует ответ.
        Объекты с conversation_id=0 — caller проставит; role уже выставлен."""

    async def fetch_full_history(
        self, peer: str, limit: int = 50
    ) -> list[ConvMessage]:
        """Все сообщения чата (us + her) по возрастанию ts. Для бэкфилла
        истории при ре-активации авточата из пула. По умолчанию пусто —
        адаптеры должны переопределить, если поддерживают."""
        return []
