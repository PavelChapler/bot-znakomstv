"""Lazy-singleton TelegramClient на сессии Leonardo.

Используется параллельно `LeonardoTGSource` (просмотр анкет) и
`autochat.chatters.leonardo_tg.TelethonChatter` (DM-переписка). Если
открыть два независимых клиента на один и тот же `.session` файл,
Telegram отвечает `AuthKeyDuplicatedError` и одна из сессий
инвалидируется — отсюда singleton.

`start()` идемпотентен: повторные вызовы при уже подключённом клиенте
безопасны. `stop_shared_client()` зовётся из главного finally в
`main.py` для аккуратного завершения.
"""

from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient

from config import SESSIONS_DIR, load

log = logging.getLogger(__name__)

_client: TelegramClient | None = None
_lock = asyncio.Lock()


async def get_shared_client() -> TelegramClient:
    global _client
    async with _lock:
        if _client is None:
            cfg = load()
            if not cfg.telethon_api_id or not cfg.telethon_api_hash:
                raise RuntimeError(
                    "Не заполнены TELETHON_API_ID / TELETHON_API_HASH в .env"
                )
            _client = TelegramClient(
                str(SESSIONS_DIR / "leonardo_tg"),
                cfg.telethon_api_id,
                cfg.telethon_api_hash,
            )
        if not _client.is_connected():
            cfg = load()
            await _client.start(phone=cfg.telethon_phone)
            log.info("Shared Telethon client connected")
    return _client


async def stop_shared_client() -> None:
    global _client
    async with _lock:
        if _client is not None and _client.is_connected():
            try:
                await _client.disconnect()
                log.info("Shared Telethon client disconnected")
            except Exception:
                log.exception("Shared Telethon client disconnect failed")
        _client = None
