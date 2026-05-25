from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

from config import DB_PATH
from core.models import Profile, ScoreResult

# kind: 'mutual' — взаимная симпатия (мы оба лайкнули),
#       'incoming' — нас лайкнули первыми (мы автолайкаем в ответ).
# UNIQUE(source, external_id) — дедуп: один и тот же msg id не пишем дважды.
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT,
    bio TEXT,
    score INTEGER NOT NULL,
    reason TEXT,
    action TEXT NOT NULL,
    dry_run INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS dismiss_cache (
    key TEXT PRIMARY KEY,
    button_text TEXT NOT NULL,
    source TEXT,
    created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS liked_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    bio TEXT,
    profile_url TEXT,
    photo_paths TEXT NOT NULL,
    discovered_ts INTEGER NOT NULL,
    viewed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_liked_pool_ts ON liked_pool(discovered_ts);
"""


async def init() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()
        await _migrate(conn)


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Идемпотентные ALTER'ы для уже существующих БД."""
    async with conn.execute("PRAGMA table_info(decisions)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if "message" not in cols:
        await conn.execute("ALTER TABLE decisions ADD COLUMN message TEXT")
        await conn.commit()


async def get_setting(key: str, default: str | None = None) -> str | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await conn.commit()


async def log_decision(
    profile: Profile, score: ScoreResult, action: str, dry_run: bool
) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO decisions(ts, source, external_id, bio, score, reason,"
            " action, dry_run, message) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                profile.source,
                profile.external_id,
                profile.bio,
                score.score,
                score.reason,
                action,
                1 if dry_run else 0,
                score.message,
            ),
        )
        await conn.commit()


async def recent_decisions(limit: int = 20) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_dismiss_button(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT button_text FROM dismiss_cache WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_dismiss_button(key: str, button_text: str, source: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO dismiss_cache(key, button_text, source, created_ts) "
            "VALUES(?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  button_text=excluded.button_text, "
            "  created_ts=excluded.created_ts",
            (key, button_text, source, int(time.time())),
        )
        await conn.commit()


async def save_liked(
    source: str,
    external_id: str,
    kind: str,
    bio: str,
    profile_url: str | None,
    photo_paths: list[str],
) -> bool:
    """Сохранить запись в пул лайков. True — записали, False — дубль."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO liked_pool"
            "(source, external_id, kind, bio, profile_url, photo_paths,"
            " discovered_ts, viewed) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 0)",
            (
                source,
                external_id,
                kind,
                bio,
                profile_url,
                json.dumps(photo_paths),
                int(time.time()),
            ),
        )
        await conn.commit()
        return cur.rowcount > 0


async def list_liked(only_unviewed: bool = False) -> list[dict[str, Any]]:
    """Все записи пула, новые сверху."""
    sql = "SELECT * FROM liked_pool"
    if only_unviewed:
        sql += " WHERE viewed = 0"
    sql += " ORDER BY discovered_ts DESC"
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(sql) as cur:
            rows = await cur.fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["photo_paths"] = json.loads(d["photo_paths"] or "[]")
                out.append(d)
            return out


async def count_liked() -> tuple[int, int, int]:
    """(всего, mutual, incoming)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT kind, COUNT(*) FROM liked_pool GROUP BY kind"
        ) as cur:
            rows = await cur.fetchall()
        by_kind = {k: c for k, c in rows}
        mutual = by_kind.get("mutual", 0)
        incoming = by_kind.get("incoming", 0)
        return mutual + incoming, mutual, incoming


async def mark_liked_viewed(pool_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE liked_pool SET viewed = 1 WHERE id = ?", (pool_id,)
        )
        await conn.commit()


async def delete_liked(pool_id: int) -> list[str]:
    """Удалить запись. Возвращает пути к фото — вызывающий удалит файлы сам."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT photo_paths FROM liked_pool WHERE id = ?", (pool_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return []
        paths = json.loads(row["photo_paths"] or "[]")
        await conn.execute("DELETE FROM liked_pool WHERE id = ?", (pool_id,))
        await conn.commit()
        return paths
