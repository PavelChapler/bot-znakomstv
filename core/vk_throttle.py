"""Глобальный троттлинг + ретрай VK API.

И likes-источник (`sources.leonardo_vk`), и autochat-chatter
(`autochat.chatters.leonardo_vk`) ходят в VK ОДНИМ user-токеном. Без общего
лимита их совокупная частота превышает лимит VK (~3 req/sec) → ошибки
code=6 (Too many requests) / code=9 (Flood control), и сессия лайков
падает («source returned no profile, stopping»).

Этот модуль:
  • сериализует ВСЕ вызовы VK с минимальным интервалом (один общий лимитер
    на оба потока — лимит у VK на токен, а не на клиент);
  • ретраит вызовы, упавшие на rate-limit (code 6/9), с backoff'ом, чтобы
    транзиентный лимит не ронял логику выше.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

VK_API_BASE = "https://api.vk.com/method"

# VK user-token: ~3 req/sec. Берём с запасом (~2.8/sec) на оба потока вместе.
_MIN_INTERVAL = 0.35
# Коды rate-limit: 6 — Too many requests per second, 9 — Flood control.
_RATE_LIMIT_CODES = (6, 9)
# Backoff между ретраями (сек). Последний элемент — потолок попыток.
_BACKOFFS = (0.5, 1.0, 2.0, 4.0)


class _Throttle:
    """Минимальный интервал между ЛЮБЫМИ VK-вызовами (общий на процесс)."""

    def __init__(self, min_interval: float) -> None:
        self._min = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._min - (now - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


_throttle = _Throttle(_MIN_INTERVAL)


async def vk_post(
    http: httpx.AsyncClient, method: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    """Throttled VK-вызов с ретраем на rate-limit.

    Возвращает распарсенный JSON (ключ 'response' при успехе или 'error'),
    либо None при сетевом сбое. Бэкофф между ретраями НЕ держит общий лок —
    другие вызовы продолжают идти своим темпом.
    """
    data: dict[str, Any] | None = None
    for attempt in range(len(_BACKOFFS) + 1):
        await _throttle.wait()
        try:
            resp = await http.post(f"{VK_API_BASE}/{method}", data=params)
            data = resp.json()
        except Exception:
            log.exception("VK call %s failed (network)", method)
            return None
        err = data.get("error") if isinstance(data, dict) else None
        if err and err.get("error_code") in _RATE_LIMIT_CODES:
            if attempt < len(_BACKOFFS):
                backoff = _BACKOFFS[attempt]
                log.warning(
                    "VK %s rate-limited (code=%s), ретрай через %.1fс",
                    method, err.get("error_code"), backoff,
                )
                await asyncio.sleep(backoff)
                continue
            log.error("VK %s всё ещё rate-limited после ретраев", method)
        return data
    return data
