from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Profile:
    source: str
    external_id: str
    bio: str
    photos: list[bytes | str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScoreResult:
    score: int
    reason: str
    message: str | None = None


@dataclass
class Decision:
    profile: Profile
    score: ScoreResult
    action: str
    dry_run: bool
