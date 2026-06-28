from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject, User

from autochat.handlers import router as autochat_router
from config import load
from core.handlers import blacklist, goal, likes, menu, sources, status

log = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Глушит любые апдейты не от владельца. Single-user деплой."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        cfg = load()
        user: User | None = data.get("event_from_user")
        if user is not None and user.id not in cfg.owner_tg_ids:
            log.warning("blocked update from foreign user_id=%s", user.id)
            return None
        return await handler(event, data)


def build_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    cfg = load()
    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(OwnerOnlyMiddleware())
    dp.include_router(menu.router)
    dp.include_router(goal.router)
    dp.include_router(sources.router)
    dp.include_router(status.router)
    dp.include_router(likes.router)
    dp.include_router(blacklist.router)
    dp.include_router(autochat_router)
    return bot, dp
