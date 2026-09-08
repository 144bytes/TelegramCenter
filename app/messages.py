"""What the app tells the user, as codes the interface translates.

A message is {"code": ..., "params": {...}}; the interface looks the code
up in its dictionaries (web/src/i18n.ts), so the backend holds no wording
in any language. Text from outside the app - a bot's reply, an error
Telegram gives no known name for - travels as code "raw" and is shown as
it came. Every code used here has a key in both dictionaries; a test
checks that.
"""
from __future__ import annotations

RAW = "raw"


def msg(code: str, **params) -> dict:
    return {"code": code, "params": params}


def raw(text: str) -> dict:
    return msg(RAW, text=str(text))


def text_of(message) -> str:
    """One line for the log: the code and what it carries, or the raw text.

    Also what the fault checks read, so a Telegram error keeps its own
    wording in `detail`.
    """
    if not message:
        return ""
    if isinstance(message, str):
        return message
    params = message.get("params") or {}
    if message.get("code") == RAW:
        return str(params.get("text", ""))
    shown = ", ".join(f"{k}={v}" for k, v in params.items()
                      if not isinstance(v, dict))
    return f"{message.get('code')}({shown})" if shown else str(message.get("code"))


class AppError(Exception):
    """A refusal the user reads: a message code and its parameters."""
    status = 400

    def __init__(self, code: str, *, status: int | None = None, **params):
        self.msg = msg(code, **params)
        if status is not None:
            self.status = status
        super().__init__(text_of(self.msg))
