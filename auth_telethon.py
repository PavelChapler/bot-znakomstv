"""Одноразовая авторизация Telethon-userbot.

Бот не может ввести код подтверждения сам, поэтому первый логин делается
вручную: запусти этот скрипт, введи код из Telegram (и пароль 2FA, если
включён). После этого появится файл data/sessions/leonardo_tg.session,
и основной бот будет логиниться без вопросов.

Запуск:
    python auth_telethon.py
"""

from __future__ import annotations

from telethon.sync import TelegramClient

from config import SESSIONS_DIR, load


def main() -> None:
    cfg = load()
    if not cfg.telethon_api_id or not cfg.telethon_api_hash or not cfg.telethon_phone:
        raise SystemExit(
            "Заполни TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE в .env"
        )

    session_path = str(SESSIONS_DIR / "leonardo_tg")
    print(f"Использую сессию: {session_path}.session")
    print(f"Логинюсь по номеру: {cfg.telethon_phone}")

    # Не используем `with TelegramClient(...)` — его __enter__ дёргает start()
    # со стандартным input-промптом и игнорирует наш phone.
    client = TelegramClient(session_path, cfg.telethon_api_id, cfg.telethon_api_hash)
    client.start(phone=cfg.telethon_phone)
    try:
        me = client.get_me()
        if getattr(me, "bot", False):
            raise SystemExit(
                "ОШИБКА: сессия авторизована как BOT, а не как пользователь.\n"
                "Скорее всего, при прошлом запуске вместо номера был введён "
                "bot token.\nУдали файл сессии и запусти заново:\n"
                f"  Remove-Item '{session_path}.session*'\n"
                "  python auth_telethon.py"
            )
        print(
            f"Готово. Авторизован как: {me.first_name} (@{me.username}, id={me.id})"
        )
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
