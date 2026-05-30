"""Точка интеграции с `core/likes_pool.py`.

`save_profile` после успешной записи в liked_pool вызывает
`enqueue_async(...)`. Если фича выключена — no-op. Импорт обёрнут
в try-except в caller'е, чтобы при удалении модуля всё молча
отвалилось.
"""

from __future__ import annotations

import logging
import time

from autochat import config
from autochat import db as autochat_db

log = logging.getLogger(__name__)


async def enqueue_async(
    *,
    source: str,
    external_id: str,
    profile_url: str | None,
    bio: str,
) -> None:
    if not await config.is_enabled():
        return
    if not profile_url:
        log.debug("autochat: пустой profile_url, пропуск (%s/%s)",
                  source, external_id)
        return
    delay = await config.get_delay_sec()
    goal = await config.get_goal_prompt()
    style = await config.get_style_prompt()
    conv_id = await autochat_db.create_conversation(
        source=source,
        external_id=external_id,
        profile_url=profile_url,
        goal_prompt=goal,
        style_prompt=style,
        scheduled_send_ts=int(time.time()) + delay,
    )
    if conv_id is None:
        log.debug("autochat: conv для %s/%s уже есть, дубль", source, external_id)
    else:
        log.info(
            "autochat: pending conv id=%d (%s/%s), opener через %d сек",
            conv_id, source, external_id, delay,
        )
