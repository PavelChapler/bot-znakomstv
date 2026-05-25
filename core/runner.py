from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable

from config import load
from core import db
from core.models import Decision, ScoreResult
from scorer.base import Scorer
from sources.base import DatingSource

log = logging.getLogger(__name__)


@dataclass
class SessionStats:
    seen: int = 0
    liked: int = 0
    skipped: int = 0
    errors: int = 0


ProgressCb = Callable[[Decision, SessionStats], Awaitable[None]]


class SessionController:
    """Контроллер активной сессии: статистика и сигнал остановки."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.stats = SessionStats()
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


async def run_session(
    source: DatingSource,
    scorer: Scorer,
    goal: str,
    threshold: int,
    dry_run: bool,
    max_count: int,
    controller: SessionController,
    on_progress: ProgressCb,
) -> None:
    cfg = load()
    await source.start()
    try:
        for _ in range(max_count):
            if controller.stopping:
                break

            try:
                profile = await source.next_profile()
            except Exception:
                log.exception("source.next_profile failed")
                controller.stats.errors += 1
                break

            if profile is None:
                log.info("source returned no profile, stopping")
                break

            controller.stats.seen += 1

            try:
                score_result = await scorer.score(profile, goal)
            except Exception as e:
                log.exception("scorer failed")
                controller.stats.errors += 1
                score_result = ScoreResult(score=0, reason=f"ошибка scorer: {e}")

            action = "like" if score_result.score >= threshold else "skip"
            decision = Decision(
                profile=profile, score=score_result, action=action, dry_run=dry_run
            )

            # В dry-run всё равно нужно сдвигать ленту источника (иначе
            # источник просто не покажет следующую анкету). Реальные лайки
            # не отправляем — посылаем skip независимо от решения.
            try:
                if dry_run:
                    await source.skip()
                elif action == "like":
                    await source.like()
                else:
                    await source.skip()
            except Exception:
                log.exception("source action failed")
                controller.stats.errors += 1

            if action == "like":
                controller.stats.liked += 1
            else:
                controller.stats.skipped += 1

            await db.log_decision(profile, score_result, action, dry_run)

            try:
                await on_progress(decision, controller.stats)
            except Exception:
                log.exception("on_progress callback failed")

            await asyncio.sleep(
                random.uniform(cfg.throttle_min_sec, cfg.throttle_max_sec)
            )
    finally:
        try:
            await source.stop()
        except Exception:
            log.exception("source.stop failed")
