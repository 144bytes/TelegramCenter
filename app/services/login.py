"""Non-blocking login state machine.

The Telethon auth coroutines take `code_provider` / `password_provider`
callbacks and run in a thread, so a callback may block. Ours blocks on
an event that an HTTP POST sets: no request ever waits for the user.

    IDLE -> CONNECTING -> NEED_CODE -> NEED_PASSWORD -> DONE
                   \\-> QR_WAIT ---/          |
                    \\------------------------+--> ERROR / CANCELLED
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

from ..logging import LOG
from ..models import Account, Operator
from ..models.enums import AccountState
from ..messages import AppError, msg, text_of
from ..telegram.errors import tg_error
from ..util import gen_id, now_iso

MOD = "login"
TTL_SEC = 600
STEP_TIMEOUT_SEC = 300


class LoginConflict(AppError):
    """Why a finished login cannot be kept."""


class StepTimeout(Exception):
    """The code or the password was not typed in time."""


class LoginState:
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    QR_WAIT = "QR_WAIT"
    NEED_CODE = "NEED_CODE"
    NEED_PASSWORD = "NEED_PASSWORD"
    DONE = "DONE"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    TERMINAL = (DONE, ERROR, CANCELLED)


@dataclass
class LoginSession:
    id: str = field(default_factory=lambda: gen_id("login"))
    kind: str = "phone"              # phone | qr | bot
    as_operator: bool = False
    network_profile_id: str | None = None
    # An operator saved as a bare @username, now being signed into. The login
    # fills that record in rather than adding a second one beside it.
    target_id: str | None = None
    phone: str = ""
    state: str = LoginState.IDLE
    error: dict | None = None        # a message, see app/messages.py
    qr_url: str | None = None
    qr_modules: list[list[bool]] | None = None
    hint: dict | None = None         # e.g. «wrong code, try again», as a message
    entity_id: str | None = None     # account/operator id once DONE
    # Which proxy this sign-in is going through, as a message. Shown in the
    # dialog and written to the log, so «did the login use the proxy?»
    # has an answer.
    proxy_name: dict | None = None
    created_at: float = field(default_factory=time.time)
    key: str = ""

    def __post_init__(self) -> None:
        self._code_event = threading.Event()
        self._password_event = threading.Event()
        self._code = ""
        self._password = ""
        self._cancelled = False

    def public(self) -> dict:
        return {"login_id": self.id, "kind": self.kind, "state": self.state,
                "error": self.error, "qr_url": self.qr_url,
                "qr_modules": self.qr_modules, "hint": self.hint,
                "as_operator": self.as_operator, "entity_id": self.entity_id,
                "proxy_name": self.proxy_name}


def qr_matrix(url: str) -> list[list[bool]] | None:
    """Render the login link to a QR matrix here, in-process.

    The link is a live credential and must never go to an outside QR
    service. `get_matrix()` needs no image backend.
    """
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        return [[bool(cell) for cell in row] for row in qr.get_matrix()]
    except Exception as exc:  # noqa: BLE001
        LOG.warning(f"could not build QR matrix: {exc}", module=MOD)
        return None


class LoginService:
    def __init__(self, storage, service, bus, accounts, profiles):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.accounts = accounts
        self.profiles = profiles
        self._sessions: dict[str, LoginSession] = {}
        self._lock = threading.RLock()

    # ── plumbing ────────────────────────────────────────────────────────
    def get(self, login_id: str) -> LoginSession | None:
        self._sweep()
        with self._lock:
            return self._sessions.get(login_id)

    def _sweep(self) -> None:
        """Forget finished logins, and give up on abandoned ones.

        A dialog closed without a word left the machine waiting for a code that
        was never coming, with its temporary session file sitting in the folder.
        """
        cutoff = time.time() - TTL_SEC
        stale = []
        with self._lock:
            for lid, s in list(self._sessions.items()):
                if s.created_at >= cutoff:
                    continue
                if s.state in LoginState.TERMINAL:
                    self._sessions.pop(lid, None)
                else:
                    stale.append(s)
        for s in stale:
            LOG.info(f"login {s.id} abandoned - giving up on it", module=MOD)
            self.cancel(s)
            self.service.submit(self._discard(s))

    def _emit(self, s: LoginSession) -> None:
        self.bus.publish("login", **s.public())

    def _set(self, s: LoginSession, state: str, **fields) -> None:
        s.state = state
        for k, v in fields.items():
            setattr(s, k, v)
        self._emit(s)

    # ── start ───────────────────────────────────────────────────────────
    def start(self, kind: str, as_operator: bool = False, phone: str = "",
              bot_token: str = "", api_profile_id: str | None = None,
              api_id=None, api_hash: str = "",
              api_name: str = "",
              network_profile_id: str | None = None,
              target_id: str | None = None) -> LoginSession:
        s = LoginSession(kind=kind, as_operator=as_operator, phone=phone.strip(),
                         network_profile_id=network_profile_id or None,
                         target_id=target_id or None)
        prefix = "op_pending" if as_operator else "pending"
        s.key = f"{prefix}_{secrets.token_hex(6)}"
        with self._lock:
            self._sessions[s.id] = s

        creds = self._creds(api_profile_id, api_id, api_hash, api_name)
        if creds is not None:
            # The very first authorisation has to go through the proxy too, or the
            # account is created behind one but signed in from this machine.
            creds["proxy"], creds["proxy_error"] = self._proxy(s.network_profile_id)
            if creds["proxy_error"]:
                self._set(s, LoginState.ERROR, error=creds["proxy_error"])
                return s
            # Said out loud: whether the first authorisation went through the
            # proxy is visible only in Telegram's own notice, and by then there is
            # nothing on our side to check it against.
            s.proxy_name = self._proxy_name(s.network_profile_id)
            LOG.info(f"login via {text_of(s.proxy_name)}", module=MOD)
        if creds is None:
            self._set(s, LoginState.ERROR, error=msg("err.login.no_api"))
            return s

        self._set(s, LoginState.CONNECTING)
        self.service.submit(self._run(s, creds, bot_token))
        return s

    def _creds(self, api_profile_id: str | None, api_id=None, api_hash: str = "",
               api_name: str = ""):
        """Which api_id/api_hash this login uses, and which profile owns them.

        Keys typed into the dialog get no profile until Telegram has accepted
        them, so wrong keys leave nothing behind to clean up.
        """
        if api_id not in (None, "") and str(api_hash).strip():
            try:
                api_id = int(api_id)
            except (TypeError, ValueError):
                return None
            api_hash = str(api_hash).strip()
            existing = self.storage.api_profiles.find(
                lambda p: p.api_id == api_id and p.api_hash == api_hash)
            return {"api_id": api_id, "api_hash": api_hash,
                    "profile_id": existing.id if existing else None,
                    "name": api_name, "explicit": True}

        prof = (self.storage.api_profiles.get(api_profile_id)
                if api_profile_id else None)
        if prof is None or not prof.api_id or not prof.api_hash:
            return None
        return {"api_id": int(prof.api_id), "api_hash": prof.api_hash,
                "profile_id": prof.id, "name": "", "explicit": False}

    def _proxy_name(self, network_profile_id: str | None) -> dict:
        """How this sign-in will reach Telegram, as a message."""
        prof = (self.storage.network_profiles.get(network_profile_id)
                if network_profile_id else None)
        if prof is None:
            return msg("login.direct")
        return msg("login.via_proxy", name=prof.name or prof.host,
                   address=f"{prof.host}:{prof.port}")

    def _proxy(self, network_profile_id: str | None):
        """The Telethon proxy tuple for this login, or (None, None).

        Returns an error instead of falling back: a login that quietly ignored
        the chosen proxy would leak the real address.
        """
        if not network_profile_id:
            return None, None
        prof = self.storage.network_profiles.get(network_profile_id)
        if prof is None:
            return None, msg("err.not_found.proxy")
        proxy = prof.as_telethon_proxy()
        if proxy is None:
            return None, msg("err.login.proxy_incomplete",
                             name=prof.name or prof.host)
        return proxy, None

    # ── the blocking providers (run via asyncio.to_thread) ──────────────
    def _code_provider(self, s: LoginSession):
        def provider(error=None):
            if s._cancelled:
                raise RuntimeError("Login cancelled")
            s._code_event.clear()
            hint = msg("login.hint.bad_code") if error is not None else None
            self._set(s, LoginState.NEED_CODE, hint=hint)
            if not s._code_event.wait(timeout=STEP_TIMEOUT_SEC):
                raise StepTimeout()
            if s._cancelled:
                raise RuntimeError("Login cancelled")
            return s._code
        return provider

    def _password_provider(self, s: LoginSession):
        def provider(error=None):
            if s._cancelled:
                raise RuntimeError("Login cancelled")
            s._password_event.clear()
            hint = msg("login.hint.bad_password") if error is not None else None
            self._set(s, LoginState.NEED_PASSWORD, hint=hint)
            if not s._password_event.wait(timeout=STEP_TIMEOUT_SEC):
                raise StepTimeout()
            if s._cancelled:
                raise RuntimeError("Login cancelled")
            return s._password
        return provider

    # ── input from HTTP ─────────────────────────────────────────────────
    def submit_code(self, s: LoginSession, code: str) -> None:
        s._code = (code or "").strip()
        s._code_event.set()

    def submit_password(self, s: LoginSession, password: str) -> None:
        s._password = password or ""
        s._password_event.set()

    def cancel(self, s: LoginSession) -> None:
        s._cancelled = True
        s._code_event.set()
        s._password_event.set()
        if s.state not in LoginState.TERMINAL:
            self._set(s, LoginState.CANCELLED)

    # ── the coroutine ───────────────────────────────────────────────────
    async def _run(self, s: LoginSession, creds: dict, bot_token: str) -> None:
        try:
            if s.kind == "qr":
                def qr_provider(url):
                    self._set(s, LoginState.QR_WAIT, qr_url=url,
                              qr_modules=qr_matrix(url))
                me = await self.service.login_qr(
                    s.key, qr_provider, self._password_provider(s),
                    lambda: s._cancelled,
                    api_id=creds["api_id"], api_hash=creds["api_hash"],
                    proxy=creds.get("proxy"))
            elif s.kind == "bot":
                me = await self.service.login_bot(
                    s.key, bot_token,
                    api_id=creds["api_id"], api_hash=creds["api_hash"],
                    proxy=creds.get("proxy"))
            else:
                me = await self.service.login_session(
                    s.key, s.phone, self._code_provider(s),
                    self._password_provider(s),
                    api_id=creds["api_id"], api_hash=creds["api_hash"],
                    proxy=creds.get("proxy"))
        except Exception as exc:  # noqa: BLE001
            if s._cancelled:
                self._set(s, LoginState.CANCELLED)
            else:
                error = (msg("err.login.step_timeout")
                         if isinstance(exc, StepTimeout) else tg_error(exc))
                # "Connection to Telegram failed 5 time(s)" says nothing about the
                # proxy in front of it. Never fall back to the direct route: that is
                # the one thing the proxy exists to prevent.
                if creds.get("proxy") and isinstance(exc, (ConnectionError, OSError)):
                    error = msg("err.login.proxy_failed", proxy=s.proxy_name,
                                error=error)
                self._set(s, LoginState.ERROR, error=error)
                LOG.warning(f"login failed via {text_of(s.proxy_name)}: "
                            f"{type(exc).__name__}: {exc}", module=MOD)
            await self._discard(s)
            return

        try:
            entity = await self._persist(s, me, creds)
        except LoginConflict as exc:
            # Refused before anything was renamed or written. The login itself
            # succeeded, so it is ended in Telegram, not just forgotten here.
            self._set(s, LoginState.ERROR, error=exc.msg)
            LOG.warning(f"login refused: {exc}", module=MOD)
            await self.service.log_out(s.key)
            return
        except Exception as exc:  # noqa: BLE001
            self._set(s, LoginState.ERROR, error=tg_error(exc))
            LOG.error(f"login persist failed: {exc}", module=MOD)
            # The rename may or may not have happened, so `s.key` is whichever
            # name the file carries now. Nothing points at it either way.
            await self.service.log_out(s.key)
            return
        self._set(s, LoginState.DONE, entity_id=entity.id, hint=None)
        LOG.info(f"login done: {entity.handle}", module=MOD)

    def pending_keys(self) -> set[str]:
        """Session keys of logins still under way.

        The sweep that deletes abandoned `pending_*` files asks for this, so it
        cannot delete a login happening right now.
        """
        with self._lock:
            return {s.key for s in self._sessions.values()
                    if s.key and s.state not in LoginState.TERMINAL}

    async def _discard(self, s: LoginSession) -> None:
        """Throw away the half-finished session, file and all.

        It used to only disconnect, so a wrong password or a closed dialog left
        a `pending_*.session` nothing would ever use or remove.
        """
        try:
            await self.service.drop_session(s.key)
        except Exception:  # noqa: BLE001
            pass

    async def _persist(self, s: LoginSession, me, creds: dict):
        """Store what the login produced.

        One Telegram account, one session: signing in as an operator with an
        account already in «Аккаунты» makes a link to it, and signing in as an
        account over an independent operator turns that operator into a link.
        """
        tg_id = getattr(me, "id", None)
        profile_id = self._keep_api(s, me, creds)
        if s.as_operator:
            account = self.storage.accounts.find(lambda a: a.telegram_id == tg_id)
            if account is not None:
                return await self._link_operator(s, account)
            return await self._persist_operator(s, me, tg_id, profile_id, creds)
        return await self._persist_account(s, me, tg_id, profile_id, creds)

    def _keep_api(self, s: LoginSession, me, creds: dict) -> str:
        """The API profile id this login used, created if the keys were typed.

        Saved BEFORE touching the session: renaming reconnects the client, and
        that reconnect resolves credentials through the stored profiles.
        """
        if creds.get("profile_id") is not None:
            return creds["profile_id"]
        # Named after the phone, so the keys stay visible in the API list.
        phone = (getattr(me, "phone", "") or s.phone or "").strip()
        if phone and not phone.startswith("+"):
            phone = f"+{phone}"
        prof = self.profiles.ensure_api(
            creds["api_id"], creds["api_hash"],
            name=creds.get("name", "") or phone)
        LOG.info(f"API profile {prof.name!r} created from the login form",
                 module=MOD)
        return prof.id

    async def _link_operator(self, s: LoginSession, account: Account):
        """The operator is this account: no second session, just a link."""
        existing = self.storage.linked_operator(account)
        target = self.storage.operators.get(s.target_id) if s.target_id else None
        if existing is not None and target is not None and target.id != existing.id:
            raise LoginConflict("err.login.already_operator", name=account.handle)
        await self.service.log_out(s.key)
        op = existing or target or Operator()
        op.link(account.id)
        self.storage.operators.upsert(op)
        LOG.info(f"operator linked to account {account.handle}; the extra "
                 f"login was ended", module=MOD)
        self.bus.publish("entity.changed", entity="operator", id=op.id)
        return op

    async def _persist_operator(self, s: LoginSession, me, tg_id, profile_id,
                                creds: dict):
        final_key = f"op_{tg_id}"
        # Checked before the rename: refusing afterwards would leave a session
        # file on disk that the next rescan would pick up as a new operator.
        target = self.storage.operators.get(s.target_id) if s.target_id else None
        if target is not None:
            clash = self.storage.operators.find(
                lambda o: o.telegram_id == tg_id and o.id != target.id)
            if clash is not None:
                raise LoginConflict("err.login.already_operator", name=clash.handle)

        await self.service.rename_session(s.key, final_key)
        s.key = final_key

        existing = target or self.storage.operators.find(
            lambda o: not o.linked and (o.telegram_id == tg_id or o.key == final_key))
        if existing is None:
            # A bare @username waiting for exactly this login is adopted.
            uname = (getattr(me, "username", "") or "").lstrip("@").lower()
            if uname:
                existing = self.storage.operators.find(
                    lambda o: not o.linked and not o.key
                    and o.uname.lower() == uname)
        op = existing or Operator()
        # Telegram's name is the truthful one; a typed-in handle survives only
        # when Telegram reports none.
        op.username = getattr(me, "username", "") or op.username
        op.display_name = (op.display_name
                           or " ".join(x for x in (getattr(me, "first_name", ""),
                                                   getattr(me, "last_name", "")) if x).strip())
        op.key = final_key
        op.session_file = f"{final_key}.session"
        op.telegram_id = tg_id
        op.api_profile_id = (profile_id if creds.get("explicit")
                             else op.api_profile_id or profile_id)
        if s.network_profile_id:
            op.network_profile_id = s.network_profile_id
        op.raw_state = AccountState.READY
        op.last_error = None
        op.last_check_at = now_iso()
        self.storage.operators.upsert(op)
        self.bus.publish("entity.changed", entity="operator", id=op.id)
        return op

    async def _persist_account(self, s: LoginSession, me, tg_id, profile_id,
                               creds: dict):
        final_key = f"session_{tg_id}"
        await self.service.rename_session(s.key, final_key)
        s.key = final_key

        existing = self.storage.accounts.find(
            lambda a: a.telegram_id == tg_id or a.key == final_key)
        acc = existing or Account(created_at=now_iso())
        acc.key = final_key
        acc.session_file = f"{final_key}.session"
        acc.telegram_id = tg_id
        acc.username = getattr(me, "username", None)
        acc.phone = getattr(me, "phone", "") or s.phone
        acc.first_name = getattr(me, "first_name", "") or ""
        acc.last_name = getattr(me, "last_name", "") or ""
        # keys typed in by hand win: that is the point of typing them
        acc.api_profile_id = (profile_id if creds.get("explicit")
                              else acc.api_profile_id or profile_id)
        if s.network_profile_id:
            acc.network_profile_id = s.network_profile_id
        acc.raw_state = AccountState.READY
        acc.last_error = None
        acc.last_check_at = now_iso()
        self.storage.accounts.upsert(acc)
        self.bus.publish("entity.changed", entity="account", id=acc.id)
        await self._absorb_operator(acc)
        return acc

    async def _absorb_operator(self, account: Account) -> None:
        """An independent operator signed in as this same Telegram account
        becomes a link to it, and its own session is ended."""
        twin = self.storage.operators.find(
            lambda o: not o.linked and o.key and o.telegram_id == account.telegram_id)
        if twin is None or self.storage.linked_operator(account) is not None:
            return
        await self.service.log_out(twin.key)
        twin.link(account.id)
        self.storage.operators.upsert(twin)
        LOG.info(f"operator {account.handle} now uses the account's session; "
                 f"its own login was ended", module=MOD)
        self.bus.publish("entity.changed", entity="operator", id=twin.id)
