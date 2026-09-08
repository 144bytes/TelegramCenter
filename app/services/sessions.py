"""Session files and the clients built on them.

One Telegram account is one session file, one key and one client, owned
by exactly one record: an account, or an independent operator. An
operator made from an account has no session of its own. What a record
points at - its API, its proxy, its operator - is checked here too.
"""
from __future__ import annotations

from ..logging import LOG
from ..messages import AppError

MOD = "sessions"

# What a record points at, and the list each one must come from.
LINKS = {"api_profile_id": "api_profiles", "network_profile_id": "network_profiles",
         "operator_id": "operators"}


def release_session(service, key: str) -> bool:
    """Disconnect and delete a session file.

    A file left on disk is picked up by start-up discovery, which rebuilds
    the record - so a deleted account would be back after a restart.
    """
    if not key:
        return False
    try:
        service.run(service.drop_session(key), timeout=10)
        return True
    except Exception as exc:  # noqa: BLE001 - the record goes either way
        LOG.warning(f"could not release session {key}: {exc}", module=MOD)
        return False


def connection_changed(owner, fields: dict) -> bool:
    """Is this edit changing how the record reaches Telegram?

    The pooled client keeps the api_id, api_hash and proxy it was built
    with, so such an edit throws it away.
    """
    return any(k in fields and getattr(owner, k) != fields[k]
               for k in ("api_profile_id", "network_profile_id"))


def checked_links(storage, values: dict) -> dict:
    """The profiles and the operator a save names, each checked to exist.

    Only the keys that were sent. An empty value means «none», which is a
    real choice. A name that does not exist is refused before anything is
    changed: twenty accounts pointing at nothing is worse than a refusal.
    """
    out = {}
    for key, repo in LINKS.items():
        if key not in values:
            continue
        value = values[key] or None
        if value is not None and getattr(storage, repo).get(value) is None:
            raise AppError("err.not_found.profile")
        out[key] = value
    return out
