from __future__ import annotations

from sources.base import DatingSource
from sources.leonardo_tg import LeonardoTGSource
from sources.leonardo_vk import LeonardoVKSource
from sources.twinby import TwinbySource
from sources.vk_dating import VKDatingSource

# Реестр доступных источников.
# Чтобы добавить новый сервис: реализовать DatingSource и зарегистрировать здесь.
SOURCES: dict[str, type[DatingSource]] = {
    LeonardoTGSource.name: LeonardoTGSource,
    LeonardoVKSource.name: LeonardoVKSource,
    VKDatingSource.name: VKDatingSource,
    TwinbySource.name: TwinbySource,
}


def get_source_class(name: str) -> type[DatingSource] | None:
    return SOURCES.get(name)


def all_sources() -> list[type[DatingSource]]:
    return list(SOURCES.values())
