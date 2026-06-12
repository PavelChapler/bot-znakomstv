"""CRUD по таблицам autochat_*. Сами таблицы создаются в core/db.py:init()."""

from __future__ import annotations

import time
from typing import Any, Iterable

import aiosqlite

from autochat.models import Conversation, ConvMessage
from config import DB_PATH


def _row_to_conv(row: aiosqlite.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        source=row["source"],
        external_id=row["external_id"],
        profile_url=row["profile_url"],
        peer_id=row["peer_id"],
        state=row["state"],
        goal_prompt=row["goal_prompt"],
        style_prompt=row["style_prompt"],
        scheduled_send_ts=row["scheduled_send_ts"],
        last_activity_ts=row["last_activity_ts"],
        last_external_msg_id=row["last_external_msg_id"],
        done_reason=row["done_reason"],
        msg_count=row["msg_count"],
        manual=bool(row["manual"]),
    )


def _row_to_msg(row: aiosqlite.Row) -> ConvMessage:
    return ConvMessage(
        id=row["id"],
        conversation_id=row["conversation_id"],
        ts=row["ts"],
        role=row["role"],
        text=row["text"],
        external_msg_id=row["external_msg_id"],
    )


async def create_conversation(
    *,
    source: str,
    external_id: str,
    profile_url: str,
    goal_prompt: str,
    style_prompt: str,
    scheduled_send_ts: int,
    manual: bool = False,
) -> int | None:
    """Создать pending-диалог. None если уже есть запись с таким
    (source, external_id) — дедуп. manual=True — добавлен вручную."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO autochat_conversations"
            "(source, external_id, profile_url, peer_id, state, goal_prompt,"
            " style_prompt, scheduled_send_ts, last_activity_ts,"
            " last_external_msg_id, done_reason, msg_count, manual) "
            "VALUES(?, ?, ?, NULL, 'pending', ?, ?, ?, ?, NULL, NULL, 0, ?)",
            (
                source, external_id, profile_url,
                goal_prompt, style_prompt, scheduled_send_ts, now,
                1 if manual else 0,
            ),
        )
        await conn.commit()
        if cur.rowcount == 0:
            return None
        return cur.lastrowid


async def get_conversation(conv_id: int) -> Conversation | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM autochat_conversations WHERE id = ?", (conv_id,)
        ) as cur:
            row = await cur.fetchone()
            return _row_to_conv(row) if row else None


async def list_conversations(
    states: Iterable[str] | None = None,
    limit: int = 100,
    manual_only: bool = False,
) -> list[Conversation]:
    conds: list[str] = []
    params_list: list[Any] = []
    if states:
        states_list = list(states)
        placeholders = ",".join("?" for _ in states_list)
        conds.append(f"state IN ({placeholders})")
        params_list.extend(states_list)
    if manual_only:
        conds.append("manual = 1")
    sql = "SELECT * FROM autochat_conversations"
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY last_activity_ts DESC LIMIT ?"
    params_list.append(limit)
    params: tuple[Any, ...] = tuple(params_list)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(sql, params) as cur:
            return [_row_to_conv(r) for r in await cur.fetchall()]


async def find_due_pending(now_ts: int, limit: int = 20) -> list[Conversation]:
    """Pending, у которых пришёл срок отправить первое сообщение."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM autochat_conversations "
            "WHERE state = 'pending' AND scheduled_send_ts <= ? "
            "ORDER BY scheduled_send_ts ASC LIMIT ?",
            (now_ts, limit),
        ) as cur:
            return [_row_to_conv(r) for r in await cur.fetchall()]


async def update_state(
    conv_id: int,
    state: str,
    *,
    done_reason: str | None = None,
    peer_id: str | None = None,
    bump_activity: bool = True,
) -> None:
    sets = ["state = ?"]
    params: list[Any] = [state]
    if done_reason is not None:
        sets.append("done_reason = ?")
        params.append(done_reason)
    if peer_id is not None:
        sets.append("peer_id = ?")
        params.append(peer_id)
    if bump_activity:
        sets.append("last_activity_ts = ?")
        params.append(int(time.time()))
    params.append(conv_id)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            f"UPDATE autochat_conversations SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        await conn.commit()


async def update_after_outgoing(
    conv_id: int,
    external_msg_id: str | None,
) -> None:
    """Бамп счётчика после успешной отправки нашего сообщения."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE autochat_conversations "
            "SET msg_count = msg_count + 1, last_activity_ts = ?, "
            "    last_external_msg_id = COALESCE(?, last_external_msg_id) "
            "WHERE id = ?",
            (int(time.time()), external_msg_id, conv_id),
        )
        await conn.commit()


async def update_after_incoming(
    conv_id: int,
    last_external_msg_id: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE autochat_conversations "
            "SET last_activity_ts = ?, last_external_msg_id = ? "
            "WHERE id = ?",
            (int(time.time()), last_external_msg_id, conv_id),
        )
        await conn.commit()


async def append_message(
    conv_id: int,
    role: str,
    text: str,
    external_msg_id: str | None = None,
    ts: int | None = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO autochat_messages"
            "(conversation_id, ts, role, text, external_msg_id) "
            "VALUES(?, ?, ?, ?, ?)",
            (conv_id, ts or int(time.time()), role, text, external_msg_id),
        )
        await conn.commit()
        return cur.lastrowid or 0


async def list_messages(
    conv_id: int, limit: int = 50
) -> list[ConvMessage]:
    """Последние N сообщений в хронологическом порядке."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM ("
            "  SELECT * FROM autochat_messages "
            "  WHERE conversation_id = ? ORDER BY ts DESC LIMIT ?"
            ") ORDER BY ts ASC",
            (conv_id, limit),
        ) as cur:
            return [_row_to_msg(r) for r in await cur.fetchall()]


async def conv_exists(source: str, external_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT 1 FROM autochat_conversations "
            "WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        ) as cur:
            return await cur.fetchone() is not None


async def count_by_state() -> dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT state, COUNT(*) FROM autochat_conversations GROUP BY state"
        ) as cur:
            return {s: c for s, c in await cur.fetchall()}


async def get_pool_bio(source: str, external_id: str) -> str:
    """Достать bio из liked_pool — нужно brain'у для контекста."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT bio FROM liked_pool WHERE source = ? AND external_id = ?",
            (source, external_id),
        ) as cur:
            row = await cur.fetchone()
            return (row[0] if row else "") or ""


async def list_recent_pool_without_conv(
    since_ts: int, limit: int = 50
) -> list[dict[str, Any]]:
    """Lifecheck для recovery: записи liked_pool за последние сутки, для
    которых ещё нет autochat_conversations. Возвращает dict'ы."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT lp.source, lp.external_id, lp.profile_url, lp.bio,"
            " lp.discovered_ts "
            "FROM liked_pool lp "
            "LEFT JOIN autochat_conversations ac "
            "  ON ac.source = lp.source AND ac.external_id = lp.external_id "
            "WHERE ac.id IS NULL "
            "  AND lp.discovered_ts >= ? "
            "  AND lp.profile_url IS NOT NULL "
            "  AND lp.profile_url != '' "
            "ORDER BY lp.discovered_ts DESC LIMIT ?",
            (since_ts, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_pool_entry_by_id(pool_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT source, external_id, profile_url, bio "
            "FROM liked_pool WHERE id = ?",
            (pool_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def find_conv_by_pool_key(
    source: str, external_id: str
) -> Conversation | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM autochat_conversations "
            "WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        ) as cur:
            row = await cur.fetchone()
            return _row_to_conv(row) if row else None


async def get_existing_msg_ids(conv_id: int) -> set[str]:
    """Все external_msg_id уже сохранённых сообщений диалога — для дедупа
    при импорте истории."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT external_msg_id FROM autochat_messages "
            "WHERE conversation_id = ? AND external_msg_id IS NOT NULL",
            (conv_id,),
        ) as cur:
            return {row[0] for row in await cur.fetchall()}


async def reset_conv_for_reuse(
    conv_id: int,
    peer_id: str,
    last_external_msg_id: str | None,
    new_msg_count: int,
) -> None:
    """Сбросить done/failed-диалог в active с обновлёнными peer_id и
    last_external_msg_id (после импорта истории). Помечаем manual=1 —
    реактивация всегда ручная (кнопка «В авточат»)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE autochat_conversations "
            "SET state='active', done_reason=NULL, peer_id=?, "
            "    last_external_msg_id=COALESCE(?, last_external_msg_id), "
            "    last_activity_ts=?, msg_count=?, scheduled_send_ts=NULL, "
            "    manual=1 "
            "WHERE id = ?",
            (peer_id, last_external_msg_id, int(time.time()),
             new_msg_count, conv_id),
        )
        await conn.commit()


async def set_manual(conv_id: int) -> None:
    """Пометить диалог ведомым вручную — движок обслуживает его даже при
    выключенном общем тумблере."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE autochat_conversations SET manual = 1 WHERE id = ?",
            (conv_id,),
        )
        await conn.commit()
