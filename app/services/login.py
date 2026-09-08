"""Non-blocking login state machine.

The Telethon auth coroutines are the ones ported verbatim from v1 — they take
`code_provider` / `password_provider` callbacks and are run through
`asyncio.to_thread`, so a callback is allowed to block. We exploit that: the
callback blocks on a threading.Event that an HTTP POST sets.

The result is that no HTTP request ever waits for the user. The browser starts
a login, watches `login` events over SSE, and posts the code when the machine
says it needs one.

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
from ..telegram.errors import friendly_login_error
from ..util import gen_id, now_iso

MOD = "login"
TTL_SEC = 600
STEP_TIMEOUT_SEC = 300


class LoginConflict(RuntimeError):
    """Message meant to be shown to the user as-is."""


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
    error: str | None = None
    qr_url: str | None = None
    qr_modules: list[list[bool]] | None = None
    hint: str | None = None          # e.g. "неверный код, попробуйте ещё раз"
    entity_id: str | None = None     # account/operator id once DONE
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
                "as_operator": self.as_operator, "entity_id": self.entity_id}


def qr_matrix(url: str) -> list[list[bool]] | None:
    """Render the login link to a QR matrix here, in-process.

    The link is a live credential, so it must never be handed to an outside
    QR-rendering service. `get_matrix()` needs no image backend, so this adds
    no dependency beyond the qrcode package already in requirements.
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
        cutoff = time.time() - TTL_SEC
        with self._lock:
            for lid, s in list(self._sessions.items()):
                if s.created_at < cutoff and s.state in LoginState.TERMINAL:
                    self._sessions.pop(lid, None)

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
            # The very first authorisation has to go through the proxy too -
            # otherwise the account is created behind one but was signed in
            # from this machine's own address, which is exactly the trail the
            # proxy exists to avoid.
            creds["proxy"], creds["proxy_error"] = self._proxy(s.network_profile_id)
            if creds["proxy_error"]:
                self._set(s, LoginState.ERROR, error=creds["proxy_error"])
                return s
        if creds is None:
            self._set(s, LoginState.ERROR,
                      error="Не настроен API-профиль (api_id / api_hash)")
            return s

        self._set(s, LoginState.CONNECTING)
        self.service.submit(self._run(s, creds, bot_token))
        return s

    def _creds(self, api_profile_id: str | None, api_id=None, api_hash: str = "",
               api_name: str = ""):
        """Which api_id/api_hash this login uses, and which profile owns them.

        Keys typed straight into the login dialog do not get a profile yet:
        `profile_id` stays None and the profile is created in `_persist`, only
        once Telegram has actually accepted them. Wrong keys therefore leave
        nothing behind to clean up.
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

        prof = (self.storage.api_profiles.get(api_profile_id) if api_profile_id
                else self.storage.default_api_profile())
        if prof is None or not prof.api_id or not prof.api_hash:
            return None
        return {"api_id": int(prof.api_id), "api_hash": prof.api_hash,
                "profile_id": prof.id, "name": "", "explicit": False}

    def _proxy(self, network_profile_id: str | None):
        """The Telethon proxy tuple for this login, or (None, None).

        Returns an error message instead of silently falling back: a login
        that quietly ignored the chosen proxy would leak the real address.
        """
        if not network_profile_id:
            return None, None
        prof = self.storage.network_profiles.get(network_profile_id)
        if prof is None:
            return None, "Выбранный прокси не найден"
        proxy = prof.as_telethon_proxy()
        if proxy is None:
            return None, f"У прокси {prof.name or prof.host!r} не задан хост или порт"
        return proxy, None

    # ── the blocking providers (run via asyncio.to_thread) ──────────────
    def _code_provider(self, s: LoginSession):
        def provider(error=None):
            if s._cancelled:
                raise RuntimeError("Login cancelled")
            s._code_event.clear()
            hint = None
            if error is not None:
                hint = "Неверный код, попробуйте ещё раз"
            self._set(s, LoginState.NEED_CODE, hint=hint)
            if not s._code_event.wait(timeout=STEP_TIMEOUT_SEC):
                raise TimeoutError("Код не введён вовремя")
            if s._cancelled:
                raise RuntimeError("Login cancelled")
            return s._code
        return provider

    def _password_provider(self, s: LoginSession):
        def provider(error=None):
            if s._cancelled:
                raise RuntimeError("Login cancelled")
            s._password_event.clear()
            hint = "Неверный пароль, попробуйте ещё раз" if error is not None else None
            self._set(s, LoginState.NEED_PASSWORD, hint=hint)
            if not s._password_event.wait(timeout=STEP_TIMEOUT_SEC):
                raise TimeoutError("Пароль не введён вовремя")
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
                self._set(s, LoginState.ERROR, error=friendly_login_error(exc))
                LOG.warning(f"login failed: {type(exc).__name__}: {exc}", module=MOD)
            await self._discard(s)
            return

        try:
            entity = await self._persist(s, me, creds)
        except LoginConflict as exc:
            # Refused before anything was renamed or written, so there is
            # nothing half-done to clean up beyond the temporary session.
            self._set(s, LoginState.ERROR, error=str(exc))
            LOG.warning(f"login refused: {exc}", module=MOD)
            await self._discard(s)
            return
        except Exception as exc:  # noqa: BLE001
            self._set(s, LoginState.ERROR, error=f"{type(exc).__name__}: {exc}")
            LOG.error(f"login persist failed: {exc}", module=MOD)
            return
        self._set(s, LoginState.DONE, entity_id=entity.id, hint=None)
        LOG.info(f"login done: {entity.handle}", module=MOD)

    async def _discard(self, s: LoginSession) -> None:
        try:
            await self.service.remove_session(s.key)
        except Exception:  # noqa: BLE001
            pass

    async def _persist(self, s: LoginSession, me, creds: dict):
        """Rename the temporary session to its final name and store the record."""
        tg_id = getattr(me, "id", None)
        final_key = (f"op_{tg_id}" if s.as_operator else f"session_{tg_id}")

        # Save the keys BEFORE touching the session. Renaming reconnects the
        # client under its new name, and that reconnect resolves credentials
        # through the stored profiles — the login's own credentials are already
        # out of scope by then. With a brand-new install and nothing saved yet,
        # doing this afterwards fails with "API ID / API Hash are not
        # configured" on the very first account.
        profile_id = creds.get("profile_id")
        if profile_id is None:
            # Name it after the phone it was entered for. The profile shows up
            # in the API list like any other, so the keys stay visible and
            # editable instead of being buried in the account record.
            phone = (getattr(me, "phone", "") or s.phone or "").strip()
            if phone and not phone.startswith("+"):
                phone = f"+{phone}"
            prof = self.profiles.ensure_api(
                creds["api_id"], creds["api_hash"],
                name=creds.get("name", "") or phone, verified=True)
            profile_id = prof.id
            LOG.info(f"API profile {prof.name!r} created from the login form",
                     module=MOD)

        # Checked before the rename: refusing afterwards would leave a session
        # file on disk that the next rescan would pick up as a new operator.
        target = (self.storage.operators.get(s.target_id)
                  if s.as_operator and s.target_id else None)
        if target is not None:
            clash = self.storage.operators.find(
                lambda o: o.telegram_id == tg_id and o.id != target.id)
            if clash is not None:
                raise LoginConflict(
                    f"Этот аккаунт уже сохранён как оператор {clash.handle}")

        await self.service.rename_session(s.key, final_key)
        s.key = final_key

        if s.as_operator:
            existing = target or self.storage.operators.find(
                lambda o: o.telegram_id == tg_id or o.key == final_key)
            if existing is None:
                # Signed in as an account someone had already written down as
                # a bare @username: that record was waiting for exactly this,
                # so it is filled in instead of a near-duplicate appearing
                # beside it. Only sessionless records are adopted - a handle
                # that is already signed in belongs to somebody else.
                uname = (getattr(me, "username", "") or "").lstrip("@").lower()
                if uname:
                    existing = self.storage.operators.find(
                        lambda o: not o.logged_in and o.uname.lower() == uname)
            op = existing or Operator()
            # The record now is this account, so the name Telegram gives is the
            # truthful one. A typed-in handle only survives when Telegram
            # reports none.
            op.username = getattr(me, "username", "") or op.username
            op.display_name = (op.display_name
                               or " ".join(x for x in (getattr(me, "first_name", ""),
                                                       getattr(me, "last_name", "")) if x).strip())
            op.key = final_key
            op.session_file = f"{final_key}.session"
            op.telegram_id = tg_id
            # Everything a broadcast account gets, an operator gets. Leaving
            # these out is what made a freshly signed-in operator show up with
            # no API profile and as "not connected" until something probed it.
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
        return acc
