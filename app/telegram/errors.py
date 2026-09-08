"""Turn Telethon exceptions into something a person can act on.

The raw class names ("PhoneNumberInvalidError") tell the user nothing about
what to do next, and they are what would otherwise reach the login dialog.
"""
from __future__ import annotations

TABLE = {
    "PasswordHashInvalidError": "Неверный облачный пароль. Попробуйте ещё раз.",
    "PhoneCodeInvalidError": "Неверный код. Попробуйте ещё раз.",
    "PhoneCodeExpiredError": "Код истёк — запросите новый.",
    "PhoneNumberInvalidError": "Некорректный номер телефона.",
    "PhoneNumberBannedError": "Этот номер заблокирован в Telegram.",
    "ApiIdInvalidError": "Telegram отклонил пару api_id / api_hash.",
    "AuthKeyUnregisteredError": "Сессия больше не действительна — войдите заново.",
    "SessionRevokedError": "Сессия отозвана — войдите заново.",
    "TimeoutError": "Telegram не ответил вовремя.",
}


def friendly_login_error(exc: Exception) -> str:
    name = exc.__class__.__name__
    if name in TABLE:
        return TABLE[name]
    if name == "FloodWaitError":
        seconds = getattr(exc, "seconds", None)
        if seconds:
            minutes = int(seconds) // 60
            when = f"{minutes} мин" if minutes else f"{int(seconds)} сек"
            return f"Слишком много попыток. Повторите через {when}."
        return "Слишком много попыток. Повторите позже."
    message = str(exc)
    if len(message) > 200:
        message = message[:200].rstrip() + "…"
    return f"{name}: {message}" if message else name
