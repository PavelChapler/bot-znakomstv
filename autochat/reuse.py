"""Ре-активация авточата из карточки пула лайков.

Сценарий: пользователь смотрит карточку в /likes, жмёт «🤖 В авточат».
1. Резолвим chatter + peer для этого профиля.
2. Подтягиваем DM-историю с дедупом по external_msg_id.
3. Создаём или обновляем conversation:
   - нет conv → создаём active с импортированной историей.
   - active/pending → уже идёт, ничего не делаем.
   - paused → возобновляем (без отправки).
   - done/failed → сбрасываем в active, история импортирована.
4. Решаем что слать:
   - если в истории есть НАШИ сообщения → continuation («вторая попытка»).
   - если только её или пусто → opener.
5. Шлём через engine.serialized_send.
6. Возвращаем dict со статусом для UI.
"""

from __future__ import annotations

import logging
import time

from autochat import config
from autochat import db as autochat_db
from autochat.engine import AutoChatEngine
from autochat.models import Conversation

log = logging.getLogger(__name__)

HISTORY_IMPORT_LIMIT = 50
# Если её последнее сообщение свежее — это обычный respond, никакого
# «вторая попытка». Если давно или после нас тишина >12ч — continuation.
RESPOND_FRESH_SEC = 6 * 3600
LONG_GAP_SEC = 12 * 3600
# Если МЫ написали последними и прошло мало времени — не спамим, ждём.
WAIT_AFTER_OUR_SEC = 6 * 3600


async def reuse_from_pool(
    pool_id: int, engine: AutoChatEngine
) -> dict[str, object]:
    """См. модульный docstring. Возвращает {ok, action, message, conv_id}."""
    pool = await autochat_db.get_pool_entry_by_id(pool_id)
    if pool is None:
        return _err("Запись пула не найдена")
    source = pool["source"]
    external_id = pool["external_id"]
    profile_url = pool["profile_url"]
    bio = pool["bio"] or ""

    if not profile_url:
        return _err("У этой анкеты нет profile_url — нельзя написать")

    chatter = await engine.get_chatter(source)
    if chatter is None:
        return _err(f"Нет chatter'а для источника {source!r}")

    ok, reason = await chatter.can_write(profile_url)
    if not ok:
        return _err(f"Нельзя написать: {reason}")

    peer = await chatter.resolve_peer(profile_url)
    if not peer:
        return _err("Не удалось резолвить peer-id")

    conv = await autochat_db.find_conv_by_pool_key(source, external_id)
    if conv is not None and conv.state in ("active", "pending"):
        # Помечаем ручным — иначе при выключенном общем тумблере движок
        # его не обслуживает (авто-диалоги заморожены).
        await autochat_db.set_manual(conv.id)
        return {
            "ok": True,
            "action": "already_running",
            "message": (
                f"Диалог #{conv.id} уже {conv.state}. Помечен как ручной — "
                "движок ведёт его сам, даже при выключенном общем авточате."
            ),
            "conv_id": conv.id,
        }

    # Импорт DM-истории (с дедупом по external_msg_id)
    history = await chatter.fetch_full_history(peer, limit=HISTORY_IMPORT_LIMIT)

    # Создаём/обновляем conv
    goal = await config.get_goal_prompt()
    style = await config.get_style_prompt()
    if conv is None:
        conv_id = await autochat_db.create_conversation(
            source=source,
            external_id=external_id,
            profile_url=profile_url,
            goal_prompt=goal,
            style_prompt=style,
            scheduled_send_ts=int(time.time()),
            manual=True,
        )
        if conv_id is None:
            # Гонка: кто-то создал параллельно — перечитаем
            conv = await autochat_db.find_conv_by_pool_key(source, external_id)
            if conv is None:
                return _err("Не смог создать conversation")
            conv_id = conv.id
        existing_ids: set[str] = set()
    else:
        conv_id = conv.id
        existing_ids = await autochat_db.get_existing_msg_ids(conv_id)

    # Импорт новых сообщений
    imported = 0
    our_total = 0
    last_her_msg_id: str | None = None
    for m in history:
        if m.external_msg_id and m.external_msg_id in existing_ids:
            if m.role == "us":
                our_total += 1
            continue
        await autochat_db.append_message(
            conv_id, m.role, m.text, m.external_msg_id, ts=m.ts,
        )
        imported += 1
        if m.role == "us":
            our_total += 1
        elif m.role == "her":
            last_her_msg_id = m.external_msg_id

    # Точное число наших по факту (из БД, после импорта)
    full = await autochat_db.list_messages(conv_id, limit=HISTORY_IMPORT_LIMIT)
    our_count = sum(1 for x in full if x.role == "us")

    # Перевод conv в active + обновление peer/last_external_msg_id
    if last_her_msg_id is None:
        # Если в импорте её сообщений не было, оставляем прошлый last_external_msg_id
        last_her_msg_id = conv.last_external_msg_id if conv else None
    await autochat_db.reset_conv_for_reuse(
        conv_id=conv_id,
        peer_id=peer,
        last_external_msg_id=last_her_msg_id,
        new_msg_count=our_count,
    )

    # Решаем opener / respond / continuation / wait по time-gap
    history_pairs = [(m.role, m.text, m.ts) for m in full]
    now = int(time.time())
    last_us_ts = max((m.ts for m in full if m.role == "us"), default=0)
    last_her_ts = max((m.ts for m in full if m.role == "her"), default=0)
    action = _choose_action(
        our_count=our_count,
        last_us_ts=last_us_ts,
        last_her_ts=last_her_ts,
        now=now,
    )
    log.info(
        "reuse conv #%s: action=%s (our_count=%d, last_us=%ds ago, last_her=%ds ago)",
        conv_id, action,
        our_count,
        (now - last_us_ts) if last_us_ts else -1,
        (now - last_her_ts) if last_her_ts else -1,
    )

    if action == "wait":
        return {
            "ok": True,
            "action": "wait",
            "message": (
                f"Мы написали недавно (≤{WAIT_AFTER_OUR_SEC // 3600}ч назад), "
                "ничего не шлю — движок сам ответит, когда она напишет "
                "(диалог ручной, работает и при выключенном общем авточате)."
            ),
            "conv_id": conv_id,
        }

    brain = await engine.get_brain()
    if action == "opener":
        result = await brain.generate_opener(
            style=style, goal=goal,
            profile_bio=bio, profile_url=profile_url,
        )
    elif action == "respond":
        result = await brain.respond(
            style=style, goal=goal,
            profile_bio=bio, profile_url=profile_url,
            history=history_pairs,
        )
    else:  # continuation
        result = await brain.continue_conversation(
            style=style, goal=goal,
            profile_bio=bio, profile_url=profile_url,
            history=history_pairs,
        )

    if not result.reply:
        # Brain отказался — например, посчитал что цель уже достигнута
        reason = result.done_reason or "brain_no_reply"
        from autochat import db as adb
        await adb.update_state(conv_id, "done", done_reason=reason)
        return {
            "ok": True,
            "action": "brain_done",
            "message": (
                f"Brain не стал слать сообщение: {reason}. "
                f"Импортировано {imported} реплик."
            ),
            "conv_id": conv_id,
        }

    msg_id = await engine.serialized_send(chatter, peer, result.reply)
    if msg_id is None:
        from autochat import db as adb
        await adb.update_state(conv_id, "failed", done_reason="send_failed")
        return _err(f"Сообщение не отправилось (conv #{conv_id})")

    await autochat_db.append_message(conv_id, "us", result.reply, msg_id)
    await autochat_db.update_after_outgoing(conv_id, msg_id)

    return {
        "ok": True,
        "action": action,
        "message": (
            f"Готово. Импортировал {imported} реплик, отправил "
            f"{action} (conv #{conv_id})."
        ),
        "conv_id": conv_id,
        "sent_text": result.reply,
    }


def _err(msg: str) -> dict[str, object]:
    log.warning("autochat reuse: %s", msg)
    return {"ok": False, "action": "error", "message": msg, "conv_id": None}


def _choose_action(
    *, our_count: int, last_us_ts: int, last_her_ts: int, now: int
) -> str:
    """opener / respond / continuation / wait."""
    if our_count == 0:
        return "opener"
    # Кто писал последним?
    her_after_us = last_her_ts and last_her_ts >= last_us_ts
    if her_after_us:
        her_gap = now - last_her_ts
        if her_gap <= RESPOND_FRESH_SEC:
            return "respond"
        return "continuation"
    # Мы писали последними
    us_gap = now - last_us_ts if last_us_ts else 0
    if us_gap < WAIT_AFTER_OUR_SEC:
        return "wait"
    if us_gap >= LONG_GAP_SEC:
        return "continuation"
    return "continuation"
