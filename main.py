from __future__ import annotations

import asyncio
import logging

from aiogram.types import BotCommand

from core import db
from core.bot import build_bot_and_dispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("main")

BOT_COMMANDS = [
    BotCommand(command="menu", description="📋 Открыть меню"),
    BotCommand(command="likes", description="💌 Пул лайков"),
    BotCommand(command="collect_likes", description="Собрать лайки из Леонардо"),
    BotCommand(command="status", description="Статус и последние решения"),
    BotCommand(command="stop", description="Остановить активную сессию"),
    BotCommand(command="goal", description="Изменить цель"),
    BotCommand(command="threshold", description="Изменить порог"),
    BotCommand(command="dry_run", description="Переключить dry-run"),
    BotCommand(command="toggle_message", description="Сообщения on/off"),
    BotCommand(command="style", description="Стиль сообщений"),
    BotCommand(command="start", description="Перезапуск + кнопка меню"),
]


async def main() -> None:
    await db.init()
    bot, dp = build_bot_and_dispatcher()
    await bot.set_my_commands(BOT_COMMANDS)
    log.info("starting polling")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
