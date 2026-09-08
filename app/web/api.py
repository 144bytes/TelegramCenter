"""HTTP API surface.

Every handler is a plain function taking (ctx, body, query) and returning a
JSON-serialisable value. Anything that must touch Telegram is bridged into the
asyncio loop through `ctx.run` — the HTTP thread waits, the loop does the work.

Errors: a handler raises ApiError(message) for anything the user should read.
The literal Russian message is passed through to the UI, which is why the
services raise domain errors with human wording rather than codes.
"""
from __future__ import annotations

from ..logging import LOG
from ..models.enums import GLOBAL_OWNER
from ..services.autoreply import ValidationError
from ..services.campaigns import CampaignError
from ..services.discovery import (adopt_default_api, discover,
                                  prune_missing_sessions)
from ..services.login import LoginState
from ..services.operators import OperatorError
from ..storage.settings import settable_keys

MOD = "api"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class NotFound(ApiError):
    def __init__(self, what: str = "не найдено"):
        super().__init__(what, status=404)


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
def _account(ctx, body):
    acc = ctx.storage.accounts.get(body.get("id", ""))
    if acc is None:
        raise NotFound("Аккаунт не найден")
    return acc


def _campaign(ctx, body):
    c = ctx.storage.campaigns.get(body.get("id", ""))
    if c is None:
        raise NotFound("Кампания не найдена")
    return c


def _operator(ctx, body):
    op = ctx.storage.operators.get(body.get("id", ""))
    if op is None:
        raise NotFound("Оператор не найден")
    return op


def _target(ctx, body):
    t = ctx.storage.targets.get(body.get("id", ""))
    if t is None:
        raise NotFound("Канал не найден")
    return t


def _template(ctx, body):
    t = ctx.storage.templates.get(body.get("id", ""))
    if t is None:
        raise NotFound("Шаблон не найден")
    return t


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
    acc = _account(ctx, body)
    fields = {k: v for k, v in body.items()
              if k in ("api_profile_id", "network_profile_id", "phone",
                       "first_name", "last_name", "username")}
    ctx.services.accounts.update(acc, **fields)
    if "operator_id" in body:
        ctx.services.accounts.set_operator(acc, body["operator_id"])
    ctx.services.sync_listeners()
    return ctx.state.account_view(acc)


def account_set_disabled(ctx, body, query):
    acc = _account(ctx, body)
    ctx.services.accounts.set_disabled(acc, bool(body.get("disabled")))
    ctx.services.sync_listeners()
    return ctx.state.account_view(acc)


def account_delete(ctx, body, query):
    ok = ctx.services.accounts.delete(body.get("id", ""),
                                      delete_session=bool(body.get("delete_session")))
    if not ok:
        raise NotFound("Аккаунт не найден")
    ctx.services.sync_listeners()
    return {"ok": True}


def account_probe_all(ctx, body, query):
    """Start the run and return. It walks the accounts one at a time and can
    take minutes, so the browser is not made to wait for it."""
    progress = ctx.services.checkup.start(scope="accounts")
    ctx.services.sync_listeners()
    return {"ok": True, "checkup": progress}


def operator_probe_all(ctx, body, query):
    """The same run, narrowed to operators - the records the operators page is
    about. One run at a time either way."""
    progress = ctx.services.checkup.start(scope="operators")
    ctx.services.sync_listeners()
    return {"ok": True, "checkup": progress}


def account_spam_check(ctx, body, query):
    """The antispam pass on its own, without re-probing everything."""
    if body.get("id"):
        account = _account(ctx, body)
        spam = ctx.services.spamcheck
        verdict = ctx.run(spam.check(account), timeout=spam.timeout_sec + 30)
        return {"ok": True, "state": verdict}
    return {"ok": True,
            "checkup": ctx.services.checkup.start(probe=False, spam=True)}


def account_check(ctx, body, query):
    """The same run as "check all", narrowed to one account - including a
    switched-off one, which the bulk run leaves alone on purpose."""
    account = _account(ctx, body)
    progress = ctx.services.checkup.start(account_ids=[account.id])
    ctx.services.sync_listeners()
    return {"ok": True, "checkup": progress}


def account_promote(ctx, body, query):
    acc = _account(ctx, body)
    try:
        op = ctx.services.accounts.promote_to_operator(acc)
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    return ctx.state.operator_view(op)


def account_rescan(ctx, body, query):
    """Synchronise the local session files with the records.

    Files only - nothing here talks to Telegram. Checking whether a session
    still works is what "check all" is for, and keeping the two apart is why
    this one is instant and costs no traffic.
    """
    prune_missing_sessions(ctx.storage)
    report = discover(ctx.storage)
    report["repaired"] = adopt_default_api(ctx.storage)
    ctx.services.sync_listeners()
    return report


# ── campaigns ───────────────────────────────────────────────────────────
def campaign_create(ctx, body, query):
    try:
        c = ctx.services.campaigns.create(body)
    except CampaignError as exc:
        raise ApiError(str(exc)) from exc
    return ctx.state.campaign_view(c)


def campaign_update(ctx, body, query):
    c = _campaign(ctx, body)
    try:
        ctx.services.campaigns.update(c, body)
    except CampaignError as exc:
        raise ApiError(str(exc)) from exc
    return ctx.state.campaign_view(c)


def campaign_delete(ctx, body, query):
    if not ctx.services.campaigns.delete(body.get("id", "")):
        raise NotFound("Кампания не найдена")
    return {"ok": True}


def _campaign_action(name):
    def handler(ctx, body, query):
        c = _campaign(ctx, body)
        try:
            getattr(ctx.services.campaigns, name)(c)
        except CampaignError as exc:
            raise ApiError(str(exc)) from exc
        return ctx.state.campaign_view(c)
    return handler


# ── operators ───────────────────────────────────────────────────────────
def operator_create(ctx, body, query):
    try:
        op = ctx.services.operators.create(
            username=body.get("username", ""),
            display_name=body.get("display_name", ""),
            notes=body.get("notes", ""))
    except OperatorError as exc:
        raise ApiError(str(exc)) from exc
    return ctx.state.operator_view(op)


def operator_update(ctx, body, query):
    op = _operator(ctx, body)
    fields = {k: v for k, v in body.items()
              if k in ("username", "display_name", "notes",
                       "api_profile_id", "network_profile_id")}
    try:
        ctx.services.operators.update(op, **fields)
    except OperatorError as exc:
        raise ApiError(str(exc)) from exc
    return ctx.state.operator_view(op)


def operator_delete(ctx, body, query):
    ok = ctx.services.operators.delete(body.get("id", ""),
                                       delete_session=bool(body.get("delete_session")))
    if not ok:
        raise NotFound("Оператор не найден")
    ctx.services.sync_listeners()
    return {"ok": True}


def operator_dialogs(ctx, body, query):
    op = _operator(ctx, {"id": query.get("id", "")})
    try:
        return ctx.run(ctx.services.operators.dialogs(op), timeout=90)
    except OperatorError as exc:
        raise ApiError(str(exc)) from exc


def _peer(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ApiError("Некорректный чат") from exc


def operator_messages(ctx, body, query):
    op = _operator(ctx, {"id": query.get("id", "")})
    peer = _peer(query.get("peer"))
    offset = _peer(query.get("offset", 0))
    try:
        return ctx.run(ctx.services.operators.messages(op, peer, offset),
                       timeout=90)
    except OperatorError as exc:
        raise ApiError(str(exc)) from exc


def operator_send(ctx, body, query):
    op = _operator(ctx, body)
    try:
        return ctx.run(ctx.services.operators.send(
            op, _peer(body.get("peer_id")), body.get("text", ""),
            reply_to=_peer(body.get("reply_to")) or None), timeout=60)
    except OperatorError as exc:
        raise ApiError(str(exc)) from exc


def operator_send_file(ctx, body, query):
    """Body arrives as a raw stream already saved to `path` by the server."""
    op = _operator(ctx, body)
    try:
        return ctx.run(ctx.services.operators.send_file(
            op, _peer(body.get("peer_id")), body["path"],
            body.get("caption", ""),
            reply_to=_peer(body.get("reply_to")) or None), timeout=600)
    except OperatorError as exc:
        raise ApiError(str(exc)) from exc


def operator_watch(ctx, body, query):
    """Listen on this operator's session while its chat is on screen."""
    op = _operator(ctx, body)
    try:
        ok = ctx.run(ctx.services.operators.watch(op), timeout=60)
    except OperatorError as exc:
        raise ApiError(str(exc)) from exc
    return {"ok": bool(ok)}


def operator_unwatch(ctx, body, query):
    op = _operator(ctx, body)
    ctx.run(ctx.services.operators.unwatch(op), timeout=30)
    return {"ok": True}


# ── auto-reply ──────────────────────────────────────────────────────────
def autoreply_save(ctx, body, query):
    owner = body.get("owner_id") or GLOBAL_OWNER
    if owner != GLOBAL_OWNER and ctx.storage.accounts.get(owner) is None:
        raise NotFound("Аккаунт не найден")
    try:
        cfg = ctx.services.autoreply.save_config(owner, body)
    except ValidationError as exc:
        raise ApiError(str(exc)) from exc
    ctx.services.sync_listeners()
    return ctx.state.autoreply_view(cfg)


def autoreply_enable(ctx, body, query):
    owner = body.get("owner_id") or GLOBAL_OWNER
    try:
        cfg = ctx.services.autoreply.set_enabled(owner, bool(body.get("enabled")))
    except ValidationError as exc:
        raise ApiError(str(exc)) from exc
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
            raise ApiError("api_id должен быть числом") from exc
    if body.get("id"):
        prof = ctx.storage.api_profiles.get(body["id"])
        if prof is None:
            raise NotFound("API-профиль не найден")
        ctx.services.profiles.update_api(prof, **fields)
    else:
        prof = ctx.services.profiles.create_api(**fields)
    return ctx.state.api_profile_view(prof)


def api_profile_delete(ctx, body, query):
    if not ctx.services.profiles.delete_api(body.get("id", "")):
        raise NotFound("API-профиль не найден")
    return {"ok": True}


def api_profile_probe(ctx, body, query):
    prof = ctx.storage.api_profiles.get(body.get("id", ""))
    if prof is None:
        raise NotFound("API-профиль не найден")
    ctx.run(ctx.services.profiles.probe_api(prof), timeout=60)
    return ctx.state.api_profile_view(prof)


def proxy_save(ctx, body, query):
    fields = {k: body[k] for k in ("name", "protocol", "host", "port", "username",
                                   "password", "enabled") if k in body}
    if "port" in fields:
        try:
            fields["port"] = int(fields["port"] or 0)
        except (TypeError, ValueError) as exc:
            raise ApiError("Порт должен быть числом") from exc
    if body.get("id"):
        prof = ctx.storage.network_profiles.get(body["id"])
        if prof is None:
            raise NotFound("Прокси-профиль не найден")
        ctx.services.profiles.update_proxy(prof, **fields)
    else:
        prof = ctx.services.profiles.create_proxy(**fields)
    return ctx.state.network_profile_view(prof)


def proxy_delete(ctx, body, query):
    if not ctx.services.profiles.delete_proxy(body.get("id", "")):
        raise NotFound("Прокси-профиль не найден")
    return {"ok": True}


def proxy_probe(ctx, body, query):
    prof = ctx.storage.network_profiles.get(body.get("id", ""))
    if prof is None:
        raise NotFound("Прокси-профиль не найден")
    ctx.run(ctx.services.profiles.probe_proxy(prof), timeout=40)
    return ctx.state.network_profile_view(prof)


# ── targets / templates ─────────────────────────────────────────────────
def target_add(ctx, body, query):
    blob = body.get("text", "")
    if "\n" in blob:
        return ctx.services.catalog.add_many(blob)
    try:
        t = ctx.services.catalog.create_target(blob, title=body.get("title", ""))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    return ctx.state.target_view(t)


def target_update(ctx, body, query):
    t = _target(ctx, body)
    fields = {k: body[k] for k in ("title", "username", "type", "active", "notes")
              if k in body}
    ctx.services.catalog.update_target(t, **fields)
    return ctx.state.target_view(t)


def target_delete(ctx, body, query):
    if not ctx.services.catalog.delete_target(body.get("id", "")):
        raise NotFound("Канал не найден")
    return {"ok": True}


def target_check(ctx, body, query):
    if body.get("id"):
        t = _target(ctx, body)
        ctx.run(ctx.services.catalog.check_target(t), timeout=90)
        return ctx.state.target_view(t)
    ctx.run(ctx.services.catalog.check_all(), timeout=600)
    return {"ok": True}


def template_save(ctx, body, query):
    try:
        if body.get("id"):
            tpl = _template(ctx, body)
            ctx.services.catalog.update_template(
                tpl, name=body.get("name", tpl.name), text=body.get("text", tpl.text))
        else:
            tpl = ctx.services.catalog.create_template(body.get("name", ""),
                                                       body.get("text", ""))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    return tpl.to_dict()


def template_delete(ctx, body, query):
    if not ctx.services.catalog.delete_template(body.get("id", "")):
        raise NotFound("Шаблон не найден")
    return {"ok": True}


# ── login ───────────────────────────────────────────────────────────────
def login_start(ctx, body, query):
    kind = body.get("kind", "phone")
    if kind not in ("phone", "qr", "bot"):
        raise ApiError("Неизвестный способ входа")
    if kind == "phone" and not body.get("phone", "").strip():
        raise ApiError("Укажите номер телефона")

    # api_id / api_hash may be typed straight into the login form instead of
    # picking a saved profile — one less round trip when adding an account.
    api_id = body.get("api_id")
    api_hash = (body.get("api_hash") or "").strip()
    if api_id not in (None, "") or api_hash:
        if api_id in (None, "") or not api_hash:
            raise ApiError("Укажите и api_id, и api_hash")
        try:
            api_id = int(api_id)
        except (TypeError, ValueError) as exc:
            raise ApiError("api_id должен быть числом") from exc

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
        raise NotFound("Сессия входа не найдена или истекла")
    return s


def login_code(ctx, body, query):
    s = _login(ctx, body)
    if s.state != LoginState.NEED_CODE:
        raise ApiError("Код сейчас не запрашивается")
    ctx.services.login.submit_code(s, body.get("code", ""))
    return s.public()


def login_password(ctx, body, query):
    s = _login(ctx, body)
    if s.state != LoginState.NEED_PASSWORD:
        raise ApiError("Пароль сейчас не запрашивается")
    ctx.services.login.submit_password(s, body.get("password", ""))
    return s.public()


def login_cancel(ctx, body, query):
    ctx.services.login.cancel(_login(ctx, body))
    return {"ok": True}


def login_status(ctx, body, query):
    s = ctx.services.login.get(query.get("login_id", ""))
    if s is None:
        raise NotFound("Сессия входа не найдена или истекла")
    return s.public()


# ── settings ────────────────────────────────────────────────────────────
# Derived, never retyped: a second hand-written list is a list that falls
# behind, and the symptom is the interface refusing a control it shows.
ALLOWED_SETTINGS = settable_keys()


def settings_save(ctx, body, query):
    values = body.get("values") or {}
    unknown = set(values) - ALLOWED_SETTINGS
    if unknown:
        raise ApiError(f"Неизвестные настройки: {', '.join(sorted(unknown))}")
    for key in ("spamcheck.clean_phrases", "spamcheck.limited_phrases"):
        if key not in values:
            continue
        raw = values[key]
        if not isinstance(raw, list):
            raise ApiError(f"{key}: нужен список фраз")
        phrases = [str(item).strip() for item in raw]
        phrases = [p for p in phrases if p]
        if len(phrases) > 100:
            raise ApiError(f"{key}: слишком много строк")
        # an empty list is allowed on purpose: it means "use the built-in
        # wording", which is a sane thing to want back
        values[key] = phrases

    if "spamcheck.bot" in values:
        bot = str(values["spamcheck.bot"] or "").strip()
        if not bot:
            raise ApiError("Укажите имя бота для проверки")
        values["spamcheck.bot"] = bot if bot.startswith("@") else f"@{bot}"
    for key, low, high in (("spamcheck.delay_sec", 0, 3600),
                           ("spamcheck.timeout_sec", 5, 300)):
        if key in values:
            try:
                number = int(values[key])
            except (TypeError, ValueError) as exc:
                raise ApiError(f"{key}: нужно число") from exc
            if not low <= number <= high:
                raise ApiError(f"{key}: допустимо от {low} до {high}")
            values[key] = number
    if "autoreply.faq_limit" in values:
        try:
            limit = int(values["autoreply.faq_limit"])
        except (TypeError, ValueError) as exc:
            raise ApiError("Лимит FAQ должен быть числом") from exc
        if not 0 <= limit <= 10:
            raise ApiError("Лимит FAQ должен быть от 0 до 10")
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
    "/api/login/status": login_status,
}

# Routes whose body is a raw file rather than JSON. The server streams the
# bytes to a temp file and calls these with {"id", "peer_id", "name",
# "caption", "path"}.
UPLOAD_ROUTES = {
    "/api/operators/send_file": operator_send_file,
}

POST_ROUTES = {
    "/api/accounts/update": account_update,
    "/api/accounts/set_disabled": account_set_disabled,
    "/api/accounts/delete": account_delete,
    "/api/accounts/check": account_check,
    "/api/accounts/probe_all": account_probe_all,
    "/api/accounts/spam_check": account_spam_check,
    "/api/accounts/promote": account_promote,
    "/api/accounts/rescan": account_rescan,

    "/api/campaigns/create": campaign_create,
    "/api/campaigns/update": campaign_update,
    "/api/campaigns/delete": campaign_delete,
    "/api/campaigns/start": _campaign_action("start"),
    "/api/campaigns/pause": _campaign_action("pause"),
    "/api/campaigns/run_now": _campaign_action("run_now"),
    "/api/campaigns/reset": _campaign_action("reset"),

    "/api/operators/create": operator_create,
    "/api/operators/update": operator_update,
    "/api/operators/delete": operator_delete,
    "/api/operators/send": operator_send,
    "/api/operators/probe_all": operator_probe_all,
    "/api/operators/watch": operator_watch,
    "/api/operators/unwatch": operator_unwatch,

    "/api/autoreply/save": autoreply_save,
    "/api/autoreply/enable": autoreply_enable,
    "/api/autoreply/reset": autoreply_reset,

    "/api/profiles/api/save": api_profile_save,
    "/api/profiles/api/delete": api_profile_delete,
    "/api/profiles/api/probe": api_profile_probe,
    "/api/profiles/proxy/save": proxy_save,
    "/api/profiles/proxy/delete": proxy_delete,
    "/api/profiles/proxy/probe": proxy_probe,

    "/api/targets/add": target_add,
    "/api/targets/update": target_update,
    "/api/targets/delete": target_delete,
    "/api/targets/check": target_check,

    "/api/templates/save": template_save,
    "/api/templates/delete": template_delete,

    "/api/login/start": login_start,
    "/api/login/code": login_code,
    "/api/login/password": login_password,
    "/api/login/cancel": login_cancel,

    "/api/settings": settings_save,
}
