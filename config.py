from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
DB_PATH = DATA_DIR / "bot.db"

load_dotenv(ROOT / ".env")


def _required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v else default


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_tg_ids: frozenset[int]

    telethon_api_id: int
    telethon_api_hash: str
    telethon_phone: str

    vk_access_token: str
    vk_leonardo_group: str

    vk_dating_launch_url: str
    vk_dating_agent: str
    vk_dating_city_id: int

    gemini_api_key: str
    gemini_model: str

    anthropic_api_key: str
    anthropic_model: str

    default_threshold: int
    default_goal: str
    session_max_profiles: int
    throttle_min_sec: int
    throttle_max_sec: int


_cached: Config | None = None


def load() -> Config:
    global _cached
    if _cached is not None:
        return _cached

    DATA_DIR.mkdir(exist_ok=True)
    SESSIONS_DIR.mkdir(exist_ok=True)

    raw_owner_ids = _required("OWNER_TG_ID")
    owner_ids = frozenset(
        int(s.strip()) for s in raw_owner_ids.split(",") if s.strip()
    )
    if not owner_ids:
        raise RuntimeError("OWNER_TG_ID не задан корректно")

    _cached = Config(
        bot_token=_required("BOT_TOKEN"),
        owner_tg_ids=owner_ids,
        telethon_api_id=int(os.getenv("TELETHON_API_ID") or 0),
        telethon_api_hash=os.getenv("TELETHON_API_HASH", ""),
        telethon_phone=os.getenv("TELETHON_PHONE", ""),
        vk_access_token=os.getenv("VK_ACCESS_TOKEN", ""),
        vk_leonardo_group=os.getenv("VK_LEONARDO_GROUP", "dayvinchik"),
        vk_dating_launch_url=os.getenv("VK_DATING_LAUNCH_URL", ""),
        vk_dating_agent=os.getenv("VK_DATING_AGENT", ""),
        vk_dating_city_id=_int("VK_DATING_CITY_ID", 0),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        default_threshold=_int("DEFAULT_THRESHOLD", 70),
        default_goal=os.getenv(
            "DEFAULT_GOAL",
            "Симпатичная девушка с интересными увлечениями.",
        ),
        session_max_profiles=_int("SESSION_MAX_PROFILES", 50),
        throttle_min_sec=_int("THROTTLE_MIN_SEC", 3),
        throttle_max_sec=_int("THROTTLE_MAX_SEC", 8),
    )
    return _cached
