from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import Profile, ScoreResult


class Scorer(ABC):
    """Оценивает, насколько профиль подходит цели пользователя."""

    @abstractmethod
    async def score(self, profile: Profile, goal: str) -> ScoreResult: ...
