from __future__ import annotations

import asyncio
import logging

from core import db
from core.bot import build_bot_and_dispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("main")


async def main() -> None:
    await db.init()
    bot, dp = build_bot_and_dispatcher()
    log.info("starting polling")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
