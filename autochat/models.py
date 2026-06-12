"""Доменные модели автопереписки."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ConvState = Literal["pending", "active", "paused", "done", "failed"]
Role = Literal["us", "her"]


@dataclass
class Conversation:
    id: int
    source: str
    external_id: str
    profile_url: str
    peer_id: str | None
    state: ConvState
    goal_prompt: str
    style_prompt: str
    scheduled_send_ts: int | None
    last_activity_ts: int
    last_external_msg_id: str | None
    done_reason: str | None
    msg_count: int
    # True — диалог добавлен вручную (кнопка «В авточат»). Такие движок
    # обслуживает даже при выключенном общем тумблере; авто-диалоги при
    # OFF заморожены.
    manual: bool = False


@dataclass
class ConvMessage:
    id: int
    conversation_id: int
    ts: int
    role: Role
    text: str
    external_msg_id: str | None
