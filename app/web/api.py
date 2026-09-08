"""HTTP API surface.

Every handler is a plain function taking (ctx, body, query) and
returning something JSON-serialisable. Anything that touches Telegram
is bridged into the asyncio loop through `ctx.run`. A refusal is an
AppError - raised here or by a service - carrying a message code the
interface translates.
"""
from __future__ import annotations

from pathlib import Path
import shutil

from .. import config
from ..logging import LOG
from ..messages import AppError
from ..models.enums import GLOBAL_OWNER
from ..services.discovery import (discover, prune_missing_sessions,
                                  purge_pending_sessions)
from ..services.login import LoginState
from ..storage.settings import settable_keys
from ..util import gen_id

MOD = "api"

# What may be attached to a campaign message. Pictures only: anything
# else is a document, and that is a decision the user has not made.
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})


class ApiError(AppError):
    """A refusal from the HTTP layer itself."""


class NotFound(ApiError):
    status = 404


class Ctx:
    """What a handler is allowed to touch."""

    def __init__(self, services, storage, run):
        self.services = services
        self.storage = storage
        self.run = run          # run(coro, timeout=...) -> result, from the HTTP thread

    @property
    def state(self):
        return self.services.state


# ── lookup helpers ──────────────────────────────────────────────────────
def _lookup(repo: str, missing: str):
    """Fetch the record this request names, or answer 404 in its own words.

    Four records, one lookup: they differed only in the repository and the
    wording.
    """
    def get(ctx, body):
        record = getattr(ctx.storage, repo).get(body.get("id", ""))
        if record is None:
            raise NotFound(missing)
        return record
    return get


_account = _lookup("accounts", "err.not_found.account")
_campaign = _lookup("campaigns", "err.not_found.campaign")
_operator = _lookup("operators", "err.not_found.operator")
_target = _lookup("targets", "err.not_found.target")


# ── state ───────────────────────────────────────────────────────────────
def get_state(ctx, body, query):
    return ctx.state.snapshot()


def get_logs(ctx, body, query):
    """The buffer the app already holds, so a freshly opened tab is not blank.
    Live lines arrive over SSE."""
    try:
        limit = max(1, min(2000, int(query.get("limit", "300"))))
    except ValueError:
        limit = 300
    records = LOG.snapshot()[-limit:]
    return [{"ts": r.ts, "level": r.level, "message": r.message, "module": r.module}
            for r in records]


# ── accounts ────────────────────────────────────────────────────────────
def account_update(ctx, body, query):
    """Whatever the account's window changed - API, proxy, operator, the
    switch, its own auto-reply - saved as one change (AccountService.edit).
    Only the keys sent are touched."""
    acc = _account(ctx, body)
    ctx.services.accounts.edit(acc, body)
    ctx.services.sync_listeners()
    return ctx.state.account_view(acc)


def account_delete(ctx, body, query):
    """Delete the account, its session file and its own auto-reply.

    `keep_operator`: an operator made from it stays as a plain @username
    instead of going with it.
    """
    ok = ctx.services.accounts.delete(body.get("id", ""),
                                      keep_operator=bool(body.get("keep_operator")))
    if not ok:
        raise NotFound("err.not_found.account")
    ctx.services.sync_listeners()
    return {"ok": True}


def _probe_all(scope: str):
    """Start the check for one page's records and return at once.

    It walks them one at a time and can take minutes. One run exists at a
    time; the scope only says which records it covers.
    """
    def handler(ctx, body, query):
        progress = ctx.services.checkup.start(scope=scope)
        ctx.services.sync_listeners()
        return {"ok": True, "checkup": progress}
    return handler


def account_spam_check(ctx, body, query):
    """The antispam pass on its own, without re-probing everything."""
    return {"ok": True,
            "checkup": ctx.services.checkup.start("spam")}


def account_check(ctx, body, query):
    """The same run as "check all", narrowed to one account. A switched-off
    account is refused rather than checked: off means no traffic."""
    account = _account(ctx, body)
    progress = ctx.services.checkup.start("accounts", account_ids=[account.id])
    ctx.services.sync_listeners()
    return {"ok": True, "checkup": progress}


def account_promote(ctx, body, query):
    acc = _account(ctx, body)
    op = ctx.services.accounts.promote_to_operator(acc)
    return ctx.state.operator_view(op)


def account_rescan(ctx, body, query):
    """Synchronise the local session files with the records.

    Files only - nothing here talks to Telegram, which is why it is instant.
    Whether a session still works is what «Проверить все» is for.
    """
    # Abandoned login files first, so the scan below never sees one.
    purge_pending_sessions(ctx.services.login.pending_keys())
    prune_missing_sessions(ctx.storage)
    report = discover(ctx.storage)
    ctx.services.sync_listeners()
    return report


# ── campaigns ───────────────────────────────────────────────────────────
def campaign_create(ctx, body, query):
    c = ctx.services.campaigns.create(body)
    return ctx.state.campaign_view(c)


def campaign_update(ctx, body, query):
    c = _campaign(ctx, body)
    ctx.services.campaigns.update(c, body)
    return ctx.state.campaign_view(c)


def campaign_delete(ctx, body, query):
    if not ctx.services.campaigns.delete(body.get("id", "")):
        raise NotFound("err.not_found.campaign")
    return {"ok": True}


def campaign_duplicate(ctx, body, query):
    """A switched-off copy of an existing campaign, ready to be edited."""
    c = _campaign(ctx, body)
    # the word goes into the copy's name, in the interface's language
    word = str(body.get("copy_word") or "").strip()
    if not word:
        raise ApiError("err.http.bad_request")
    copy = ctx.services.campaigns.duplicate(c, word)
    return ctx.state.campaign_view(copy)


def campaign_target_toggle(ctx, body, query):
    """Switch one channel off, or back on, inside one campaign.

    A tick box, not a deletion: the channel keeps its place and history.
    Other campaigns are unaffected - a ban belongs to an account.
    """
    c = _campaign(ctx, body)
    ctx.services.campaigns.set_target_excluded(
        c, str(body.get("target_id", "")), bool(body.get("excluded")))
    return ctx.state.campaign_view(c)


def _campaign_action(name):
    def handler(ctx, body, query):
        c = _campaign(ctx, body)
        getattr(ctx.services.campaigns, name)(c)
        return ctx.state.campaign_view(c)
    return handler


# ── many at once ────────────────────────────────────────────────────────
def bulk_apply(ctx, body, query):
    """One action over several records: {kind, ids, action, values}.

    One route rather than one per kind and action; every action is a
    service method that already exists. One request on purpose: twenty
    calls would be twenty snapshots and no way to report what refused.
    """
    report = ctx.services.bulk.apply(
        kind=str(body.get("kind", "")),
        ids=[str(i) for i in (body.get("ids") or [])],
        action=str(body.get("action", "")),
        values=body.get("values") or {})
    ctx.services.sync_listeners()
    return report


def bulk_check(ctx, body, query):
    """Check the selected accounts, operators or channels.

    Not part of `bulk_apply`: checking is not a loop over records but the
    one check run, which already takes a list of ids.
    """
    kind = str(body.get("kind", ""))
    ids = [str(i) for i in (body.get("ids") or [])]
    if not ids:
        raise ApiError("err.bulk.nothing_selected")
    if kind == "accounts":
        progress = ctx.services.checkup.start("accounts", account_ids=ids)
    elif kind == "operators":
        progress = ctx.services.checkup.start("operators", operator_ids=ids)
    elif kind == "targets":
        progress = ctx.services.checkup.start("targets", target_ids=ids)
    else:
        raise ApiError("err.bulk.unknown_kind", kind=kind)
    ctx.services.sync_listeners()
    return {"ok": True, "checkup": progress}


def campaign_upload(ctx, body, query):
    """Store a picture for a campaign message and say where it went.

    Copied into the app's own media folder: a campaign runs for weeks, and
    a file moved out of Downloads would break it halfway through. The
    server's temporary file is deleted the moment this returns.
    """
    source = Path(body.get("path", ""))
    name = Path(body.get("name", "") or source.name).name
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ApiError("err.upload.pictures_only",
                       types=", ".join(sorted(IMAGE_SUFFIXES)))
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    target = config.MEDIA_DIR / f"{gen_id('img')}{suffix}"
    shutil.copyfile(source, target)
    LOG.info(f"attachment stored: {target.name}", module=MOD)
    return {"path": str(target), "name": name}


# ── operators ───────────────────────────────────────────────────────────
def operator_create(ctx, body, query):
    op = ctx.services.operators.create(
        username=body.get("username", ""),
        display_name=body.get("display_name", ""),
        notes=body.get("notes", ""))
    return ctx.state.operator_view(op)


def operator_update(ctx, body, query):
    op = _operator(ctx, body)
    fields = {k: v for k, v in body.items()
              if k in ("username", "display_name", "notes",
                       "api_profile_id", "network_profile_id")}
    ctx.services.operators.update(op, **fields)
    return ctx.state.operator_view(op)


def operator_delete(ctx, body, query):
    ok = ctx.services.operators.delete(body.get("id", ""))
    if not ok:
        raise NotFound("err.not_found.operator")
    ctx.services.sync_listeners()
    return {"ok": True}


def operator_dialogs(ctx, body, query):
    op = _operator(ctx, {"id": query.get("id", "")})
    return ctx.run(ctx.services.operators.dialogs(op), timeout=90)


def _peer(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ApiError("err.chat.bad_peer") from exc


def operator_messages(ctx, body, query):
    op = _operator(ctx, {"id": query.get("id", "")})
    peer = _peer(query.get("peer"))
    offset = _peer(query.get("offset", 0))
    return ctx.run(ctx.services.operators.messages(op, peer, offset),
                   timeout=90)


def operator_send(ctx, body, query):
    op = _operator(ctx, body)
    return ctx.run(ctx.services.operators.send(
        op, _peer(body.get("peer_id")), body.get("text", ""),
        reply_to=_peer(body.get("reply_to")) or None), timeout=60)


def operator_send_file(ctx, body, query):
    """Body arrives as a raw stream already saved to `path` by the server."""
    op = _operator(ctx, body)
    return ctx.run(ctx.services.operators.send_file(
        op, _peer(body.get("peer_id")), body["path"],
        body.get("caption", ""),
        reply_to=_peer(body.get("reply_to")) or None), timeout=600)


def operator_enter(ctx, body, query):
    """The operator opened a chat: online, and its messages read.

    Presence for an operator follows the person, not the send: nothing here
    knows when an operator is present except the operator.
    """
    op = _operator(ctx, body)
    ctx.run(ctx.services.operators.enter_chat(op, _peer(body.get("peer_id"))),
            timeout=30)
    return {"ok": True}


def operator_typing(ctx, body, query):
    """The operator is typing right now.

    Sent from the chat as they type, so the other side sees it while it is
    happening.
    """
    op = _operator(ctx, body)
    ctx.run(ctx.services.operators.typing(op, _peer(body.get("peer_id"))),
            timeout=30)
    return {"ok": True}


def operator_leave(ctx, body, query):
    """The operator closed the chat."""
    op = _operator(ctx, body)
    ctx.run(ctx.services.operators.leave_chat(op), timeout=30)
    return {"ok": True}


def operator_watch(ctx, body, query):
    """Listen on this operator's session while its chat is on screen."""
    op = _operator(ctx, body)
    ok = ctx.run(ctx.services.operators.watch(op), timeout=60)
    return {"ok": bool(ok)}


def operator_unwatch(ctx, body, query):
    op = _operator(ctx, body)
    ctx.run(ctx.services.operators.unwatch(op), timeout=30)
    return {"ok": True}


# ── auto-reply ──────────────────────────────────────────────────────────
def autoreply_save(ctx, body, query):
    owner = body.get("owner_id") or GLOBAL_OWNER
    if owner != GLOBAL_OWNER and ctx.storage.accounts.get(owner) is None:
        raise NotFound("err.not_found.account")
    cfg = ctx.services.autoreply.save_config(owner, body)
    ctx.services.sync_listeners()
    return ctx.state.autoreply_view(cfg)


def autoreply_reset(ctx, body, query):
    ctx.services.autoreply.reset_to_global(body.get("owner_id", ""))
    ctx.services.sync_listeners()
    return {"ok": True}


# ── profiles ────────────────────────────────────────────────────────────
def api_profile_save(ctx, body, query):
    fields = {k: body[k] for k in ("name", "api_id", "api_hash", "enabled")
              if k in body}
    if "api_id" in fields and fields["api_id"] not in (None, ""):
        try:
            fields["api_id"] = int(fields["api_id"])
        except (TypeError, ValueError) as exc:
            raise ApiError("err.api.id_not_number") from exc
    if body.get("id"):
        prof = ctx.storage.api_profiles.get(body["id"])
        if prof is None:
            raise NotFound("err.not_found.api")
        ctx.services.profiles.update_api(prof, **fields)
    else:
        prof = ctx.services.profiles.create_api(**fields)
    return ctx.state.api_profile_view(prof)


def api_profile_delete(ctx, body, query):
    if not ctx.services.profiles.delete_api(body.get("id", "")):
        raise NotFound("err.not_found.api")
    return {"ok": True}


def proxy_save(ctx, body, query):
    fields = {k: body[k] for k in ("name", "protocol", "host", "port", "username",
                                   "password", "enabled") if k in body}
    if "port" in fields:
        try:
            fields["port"] = int(fields["port"] or 0)
        except (TypeError, ValueError) as exc:
            raise ApiError("err.proxy.port_not_number") from exc
    if body.get("id"):
        prof = ctx.storage.network_profiles.get(body["id"])
        if prof is None:
            raise NotFound("err.not_found.proxy")
        ctx.services.profiles.update_proxy(prof, **fields)
    else:
        prof = ctx.services.profiles.create_proxy(**fields)
    return ctx.state.network_profile_view(prof)


def proxy_delete(ctx, body, query):
    if not ctx.services.profiles.delete_proxy(body.get("id", "")):
        raise NotFound("err.not_found.proxy")
    return {"ok": True}


def proxy_probe(ctx, body, query):
    prof = ctx.storage.network_profiles.get(body.get("id", ""))
    if prof is None:
        raise NotFound("err.not_found.proxy")
    ctx.run(ctx.services.profiles.probe_proxy(prof), timeout=40)
    return ctx.state.network_profile_view(prof)


# ── targets ─────────────────────────────────────────────────────────────
def target_add(ctx, body, query):
    blob = body.get("text", "")
    if "\n" in blob:
        return ctx.services.catalog.add_many(blob)
    t = ctx.services.catalog.create_target(blob, title=body.get("title", ""))
    return ctx.state.target_view(t)


def target_update(ctx, body, query):
    t = _target(ctx, body)
    fields = {k: body[k] for k in ("title", "username", "type", "active", "notes")
              if k in body}
    ctx.services.catalog.update_target(t, **fields)
    return ctx.state.target_view(t)


def target_delete(ctx, body, query):
    if not ctx.services.catalog.delete_target(body.get("id", "")):
        raise NotFound("err.not_found.target")
    return {"ok": True}


def target_check(ctx, body, query):
    """Start the run and return, exactly as the accounts page does.

    Checking every channel takes as long as checking every account, and it
    used to happen inside this request with the browser waiting.
    """
    ids = [body["id"]] if body.get("id") else None
    if ids and ctx.storage.targets.get(ids[0]) is None:
        raise NotFound("err.not_found.target")
    progress = ctx.services.checkup.start("targets", target_ids=ids)
    return {"ok": True, "checkup": progress}


# ── login ───────────────────────────────────────────────────────────────
def login_start(ctx, body, query):
    kind = body.get("kind", "phone")
    if kind not in ("phone", "qr", "bot"):
        raise ApiError("err.login.unknown_kind")
    if kind == "phone" and not body.get("phone", "").strip():
        raise ApiError("err.login.no_phone")

    # api_id / api_hash may be typed straight into the login form instead of
    # picking a saved profile — one less round trip when adding an account.
    api_id = body.get("api_id")
    api_hash = (body.get("api_hash") or "").strip()
    if api_id not in (None, "") or api_hash:
        if api_id in (None, "") or not api_hash:
            raise ApiError("err.login.api_pair")
        try:
            api_id = int(api_id)
        except (TypeError, ValueError) as exc:
            raise ApiError("err.api.id_not_number") from exc

    s = ctx.services.login.start(
        kind=kind, as_operator=bool(body.get("as_operator")),
        network_profile_id=body.get("network_profile_id") or None,
        target_id=body.get("target_id") or None,
        phone=body.get("phone", ""), bot_token=body.get("bot_token", ""),
        api_profile_id=body.get("api_profile_id"),
        api_id=api_id, api_hash=api_hash,
        api_name=(body.get("api_name") or "").strip())
    return s.public()


def _login(ctx, body):
    s = ctx.services.login.get(body.get("login_id", ""))
    if s is None:
        raise NotFound("err.login.gone")
    return s


def login_code(ctx, body, query):
    s = _login(ctx, body)
    if s.state != LoginState.NEED_CODE:
        raise ApiError("err.login.no_code_asked")
    ctx.services.login.submit_code(s, body.get("code", ""))
    return s.public()


def login_password(ctx, body, query):
    s = _login(ctx, body)
    if s.state != LoginState.NEED_PASSWORD:
        raise ApiError("err.login.no_password_asked")
    ctx.services.login.submit_password(s, body.get("password", ""))
    return s.public()


def login_cancel(ctx, body, query):
    ctx.services.login.cancel(_login(ctx, body))
    return {"ok": True}


# ── settings ────────────────────────────────────────────────────────
# Derived, never retyped: a hand-written second list falls behind, and
# the symptom is the interface refusing a control it shows.
ALLOWED_SETTINGS = settable_keys()


def settings_save(ctx, body, query):
    values = body.get("values") or {}
    unknown = set(values) - ALLOWED_SETTINGS
    if unknown:
        raise ApiError("err.settings.unknown", keys=", ".join(sorted(unknown)))
    for key in ("spamcheck.clean_phrases", "spamcheck.limited_phrases",
                "spamcheck.blocked_phrases"):
        if key not in values:
            continue
        raw = values[key]
        if not isinstance(raw, list):
            raise ApiError("err.settings.not_a_list", key=key)
        phrases = [str(item).strip() for item in raw]
        phrases = [p for p in phrases if p]
        if len(phrases) > 100:
            raise ApiError("err.settings.too_many_lines", key=key)
        # an empty list is allowed on purpose: it means "use the built-in
        # wording", which is a sane thing to want back
        values[key] = phrases

    if "spamcheck.bot" in values:
        bot = str(values["spamcheck.bot"] or "").strip()
        if not bot:
            raise ApiError("err.settings.no_bot")
        values["spamcheck.bot"] = bot if bot.startswith("@") else f"@{bot}"
    for key, low, high in (("spamcheck.delay_sec", 0, 3600),
                           ("spamcheck.timeout_sec", 5, 300),
                           ("campaign.auto_join_delay_sec", 0, 3600),
                           ("campaign.start_delay_sec", 0, 86400),
                           ("campaign.default_interval_min_sec", 0, 86400),
                           ("campaign.default_interval_max_sec", 0, 86400),
                           ("accounts.probe_interval_min_sec", 60, 86400),
                           ("accounts.probe_interval_max_sec", 60, 86400),
                           ("accounts.young_days", 0, 365),
                           ("campaign.error_stop_percent", 0, 100),
                           ("campaign.error_stop_min", 0, 1000),
                           ("campaign.reaction_percent", 0, 100),
                           ("campaign.refusals_before_off", 0, 100)):
        if key in values:
            try:
                number = int(values[key])
            except (TypeError, ValueError) as exc:
                raise ApiError("err.settings.not_a_number", key=key) from exc
            if not low <= number <= high:
                raise ApiError("err.settings.out_of_range", key=key, low=low, high=high)
            values[key] = number
    settings = ctx.storage.settings
    low = values.get("campaign.default_interval_min_sec",
                     settings.get("campaign.default_interval_min_sec"))
    high = values.get("campaign.default_interval_max_sec",
                      settings.get("campaign.default_interval_max_sec"))
    if low > high:
        raise ApiError("Интервал «от» больше, чем «до»")
    if "autoreply.faq_limit" in values:
        try:
            limit = int(values["autoreply.faq_limit"])
        except (TypeError, ValueError) as exc:
            raise ApiError("err.settings.not_a_number", key="autoreply.faq_limit") from exc
        if not 0 <= limit <= 10:
            raise ApiError("err.settings.out_of_range", key="autoreply.faq_limit", low=0, high=10)
        values["autoreply.faq_limit"] = limit
    ctx.storage.settings.update(values)
    ctx.services.recompute()
    LOG.info(f"settings updated: {', '.join(sorted(values))}", module=MOD)
    return ctx.storage.settings.data


# ── routing table ───────────────────────────────────────────────────────
GET_ROUTES = {
    "/api/state": get_state,
    "/api/logs": get_logs,
    "/api/operators/dialogs": operator_dialogs,
    "/api/operators/messages": operator_messages,
}

# Routes whose body is a raw file rather than JSON. The server streams
# it to a temp file and calls these with {id, peer_id, name, caption,
# path}.
UPLOAD_ROUTES = {
    "/api/operators/send_file": operator_send_file,
    "/api/campaigns/upload": campaign_upload,
}

POST_ROUTES = {
    "/api/accounts/update": account_update,
    "/api/accounts/delete": account_delete,
    "/api/accounts/check": account_check,
    "/api/accounts/probe_all": _probe_all("accounts"),
    "/api/accounts/spam_check": account_spam_check,
    "/api/accounts/promote": account_promote,
    "/api/accounts/rescan": account_rescan,

    "/api/campaigns/create": campaign_create,
    "/api/campaigns/update": campaign_update,
    "/api/campaigns/delete": campaign_delete,
    "/api/campaigns/duplicate": campaign_duplicate,
    "/api/campaigns/start": _campaign_action("start"),
    "/api/campaigns/pause": _campaign_action("pause"),
    "/api/campaigns/reset": _campaign_action("reset"),
    "/api/campaigns/target_toggle": campaign_target_toggle,

    "/api/operators/create": operator_create,
    "/api/operators/update": operator_update,
    "/api/operators/delete": operator_delete,
    "/api/operators/send": operator_send,
    "/api/operators/probe_all": _probe_all("operators"),
    "/api/operators/watch": operator_watch,
    "/api/operators/enter": operator_enter,
    "/api/operators/typing": operator_typing,
    "/api/operators/leave": operator_leave,
    "/api/operators/unwatch": operator_unwatch,

    "/api/autoreply/save": autoreply_save,
    "/api/autoreply/reset": autoreply_reset,

    "/api/profiles/api/save": api_profile_save,
    "/api/profiles/api/delete": api_profile_delete,
    "/api/profiles/proxy/save": proxy_save,
    "/api/profiles/proxy/delete": proxy_delete,
    "/api/profiles/proxy/probe": proxy_probe,

    "/api/targets/add": target_add,
    "/api/targets/update": target_update,
    "/api/targets/delete": target_delete,
    "/api/targets/check": target_check,

    "/api/login/start": login_start,
    "/api/login/code": login_code,
    "/api/login/password": login_password,
    "/api/login/cancel": login_cancel,

    "/api/bulk/apply": bulk_apply,
    "/api/bulk/check": bulk_check,

    "/api/settings": settings_save,
}
