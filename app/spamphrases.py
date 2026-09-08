"""Default wording for the antispam check.

These are only defaults. The phrases live in settings so they can be corrected
without a new build: change the bot and its answers change with it, and no
amount of care in here would keep up with that.

They sit in their own module because both the settings store and the check
itself need them, and neither should have to import the other.

Matching is case-insensitive on a punctuation-flattened copy of the reply, so
write phrases in plain lowercase and let the code handle quotes and dashes.
"""
from __future__ import annotations

# Phrases that only appear when nothing is wrong.
DEFAULT_CLEAN = (
    "no limits",
    "free as a bird",
    "свободен от каких-либо ограничений",
    "свободен от ограничений",
    "никаких ограничений",
    "свободны как птица",
    "не наложено",
)

# Phrases that only appear when something is. Whole clauses on purpose: the
# clean Russian answer also talks about "ограничений", so anything shorter than
# a clause would match it and read a healthy account as a restricted one.
DEFAULT_LIMITED = (
    "your account is now limited",
    "your account was limited",
    "account is limited until",
    "limited until",
    "may trigger a harsh response",
    "anti-spam",
    "antispam",
    "i'm afraid i can't help",
    "i am afraid i can't help",
    "ваш аккаунт ограничен",
    "аккаунт ограничен до",
    "аккаунт сейчас ограничен",
    "мне очень жаль",
    "номера телефонов могут",
    "ограничения будут сняты",
    "ограничения были сняты",
    "антиспам",
    "боюсь, я не могу",
)
