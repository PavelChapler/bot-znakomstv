from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import Profile, ScoreResult


class Scorer(ABC):
    """Оценивает, насколько профиль подходит цели пользователя."""

    @abstractmethod
    async def score(
        self,
        profile: Profile,
        goal: str,
        style: str | None = None,
        gen_message_if_score_ge: int | None = None,
    ) -> ScoreResult:
        """Если style + gen_message_if_score_ge заданы и оценка пройдёт порог —
        в ScoreResult.message будет сгенерированное стартовое сообщение."""
