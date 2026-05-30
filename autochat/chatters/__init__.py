"""Registry адаптеров автопереписки.

Имя должно совпадать с `DatingSource.name` — это связывает запись в
`liked_pool.source` с соответствующим Chatter'ом.
"""

from __future__ import annotations

from autochat.chatters.base import Chatter
from autochat.chatters.leonardo_tg import TelethonChatter
from autochat.chatters.leonardo_vk import VKChatter
from autochat.chatters.stub import StubChatter

_REGISTRY: dict[str, type[Chatter]] = {
    "leonardo_tg": TelethonChatter,
    "leonardo_vk": VKChatter,
    "twinby": StubChatter,
    "vk_dating": StubChatter,
}


def get_chatter_class(source_name: str) -> type[Chatter] | None:
    return _REGISTRY.get(source_name)


def all_chatter_names() -> list[str]:
    return list(_REGISTRY.keys())
