"""The single place where business status is decided.

Raw state is what we measured and stored. Effective state is derived here, on
every read, and is never written to disk. The frontend receives the result and
paints it; it never re-derives anything.

That is what makes the forbidden combination structurally impossible: a
campaign cannot report RUNNING while its account's proxy is in ERROR, because
"RUNNING" is not stored anywhere the UI can see — it is computed from the
account, which is computed from the proxy.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..models import (
    Account, ApiProfile, AutoReplyConfig, Campaign, NetworkProfile, Operator,
    Target,
)
from ..models.enums import (
    AccountState, AutoReplyKind, CampaignState, EffectiveState, GLOBAL_OWNER,
    IssueLevel, ProbeState, SpamState,
)

OPERATOR_TOKEN = "@operator"
_OPERATOR_RE = re.compile(r"@operator\b", re.IGNORECASE)


def mentions_operator(text: str) -> bool:
    return bool(_OPERATOR_RE.search(text or ""))


@dataclass
class Issue:
    """A problem shown in the UI as a coloured dot with a hover tooltip.

    `code` is what the frontend translates; `message` is a Russian fallback
    used in logs and when a code is unknown to the client.
    """
    level: str
    code: str
    message: str
    source: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _worst(issues: list[Issue]) -> str | None:
    if any(i.level == IssueLevel.ERROR for i in issues):
        return IssueLevel.ERROR
    if issues:
        return IssueLevel.WARNING
    return None


class StateManager:
    def __init__(self, storage) -> None:
        self.storage = storage

    # ── profiles ────────────────────────────────────────────────────────
    def api_profile_view(self, p: ApiProfile) -> dict:
        issues: list[Issue] = []
        if not p.enabled:
            effective = EffectiveState.DISABLED
        elif p.raw_state == ProbeState.ERROR:
            effective = EffectiveState.ERROR
            issues.append(Issue(IssueLevel.ERROR, "api.error",
                                p.last_error or "API-профиль недоступен",
                                params={"error": p.last_error or ""}))
        elif p.raw_state == ProbeState.ONLINE:
            effective = EffectiveState.READY
        elif p.raw_state == ProbeState.CHECKING:
            effective = EffectiveState.CHECKING
        else:
            effective = EffectiveState.OFFLINE
        if p.enabled and not (p.api_id and p.api_hash):
            issues.append(Issue(IssueLevel.ERROR, "api.incomplete",
                                "Не заполнены api_id / api_hash"))
            effective = EffectiveState.ERROR
        return {**p.to_dict(), "effective": effective,
                "issues": [i.to_dict() for i in issues]}

    def network_profile_view(self, p: NetworkProfile) -> dict:
        issues: list[Issue] = []
        if not p.enabled:
            effective = EffectiveState.DISABLED
        elif p.raw_state == ProbeState.ERROR:
            effective = EffectiveState.ERROR
            issues.append(Issue(IssueLevel.ERROR, "proxy.error",
                                p.last_error or "Прокси недоступен",
                                params={"error": p.last_error or ""}))
        elif p.raw_state == ProbeState.ONLINE:
            effective = EffectiveState.READY
        elif p.raw_state == ProbeState.CHECKING:
            effective = EffectiveState.CHECKING
        else:
            effective = EffectiveState.OFFLINE
        if p.enabled and not (p.host and p.port):
            issues.append(Issue(IssueLevel.ERROR, "proxy.incomplete",
                                "Не заполнены хост / порт"))
            effective = EffectiveState.ERROR
        return {**p.to_dict(), "effective": effective,
                "issues": [i.to_dict() for i in issues]}

    # ── accounts ────────────────────────────────────────────────────────
    def account_issues(self, a: Account) -> list[Issue]:
        """Everything wrong with the account itself or with what it depends on."""
        issues: list[Issue] = self._connection_issues(a)

        # operator — optional, a dangling reference is a warning
        if a.operator_id:
            op = self.storage.operators.get(a.operator_id)
            if op is None:
                issues.append(Issue(IssueLevel.WARNING, "operator.missing",
                                    "Привязанный оператор не найден",
                                    f"operator:{a.operator_id}"))

        if not a.key:
            issues.append(Issue(IssueLevel.WARNING, "account.no_session",
                                "Аккаунт не авторизован"))
        if a.raw_state == AccountState.ERROR and a.last_error:
            issues.append(Issue(IssueLevel.ERROR, "account.error", a.last_error,
                                params={"error": a.last_error}))
        if a.raw_state == AccountState.RESTRICTED:
            issues.append(Issue(IssueLevel.WARNING, "account.restricted",
                                "Аккаунт ограничен Telegram"))

        issues += self._spam_issues(a)
        return issues

    def _spam_issues(self, owner) -> list[Issue]:
        """The antispam verdict, for an account or an operator alike.

        A limit does not stop campaigns by itself - posting to your own
        channels still works - so it is a warning: the dot stops being green
        and the tooltip says why, alongside every other problem.
        """
        detail = getattr(owner, "spam_detail", "") or ""
        state = getattr(owner, "spam_state", SpamState.UNKNOWN)
        if state == SpamState.LIMITED:
            return [Issue(IssueLevel.WARNING, "account.spam_limited",
                          detail or "На аккаунт наложены ограничения по спаму",
                          params={"detail": detail})]
        if state == SpamState.FAILED:
            return [Issue(IssueLevel.WARNING, "account.spam_check_failed",
                          detail or "Не удалось проверить ограничения по спаму",
                          params={"detail": detail})]
        if state == SpamState.UNKNOWN and getattr(owner, "spam_checked_at", None):
            # checked, but the bot said something we do not recognise - saying
            # nothing here would look like a clean result
            return [Issue(IssueLevel.WARNING, "account.spam_unclear",
                          detail or "Непонятный ответ антиспам-бота",
                          params={"detail": detail})]
        return []

    def account_effective(self, a: Account) -> str:
        issues = self.account_issues(a)
        if _worst(issues) == IssueLevel.ERROR:
            return EffectiveState.ERROR
        if a.disabled:
            return EffectiveState.DISABLED
        return {
            AccountState.READY: EffectiveState.READY,
            AccountState.QUEUED: EffectiveState.QUEUED,
            AccountState.CHECKING: EffectiveState.CHECKING,
            AccountState.RESTRICTED: EffectiveState.RESTRICTED,
            AccountState.ERROR: EffectiveState.ERROR,
        }.get(a.raw_state, EffectiveState.OFFLINE)

    def account_view(self, a: Account) -> dict:
        issues = self.account_issues(a)
        eff = self.account_effective(a)
        return {
            **a.to_dict(),
            "handle": a.handle,
            "display_name": a.display_name,
            "effective": eff,
            "issues": [i.to_dict() for i in issues],
            "campaign_ids": [c.id for c in self.storage.campaigns_for_account(a.id)],
            "has_own_autoreply": self.storage.has_own_autoreply(a.id),
        }

    # ── campaigns ───────────────────────────────────────────────────────
    def campaign_issues(self, c: Campaign) -> list[Issue]:
        issues: list[Issue] = []
        account = self.storage.accounts.get(c.account_id) if c.account_id else None
        if account is None:
            issues.append(Issue(IssueLevel.ERROR, "campaign.account_missing",
                                "Аккаунт кампании не найден",
                                f"account:{c.account_id}"))
        else:
            acc_eff = self.account_effective(account)
            if acc_eff != EffectiveState.READY:
                level = (IssueLevel.ERROR if acc_eff in
                         (EffectiveState.ERROR, EffectiveState.DISABLED)
                         else IssueLevel.WARNING)
                issues.append(Issue(level, "campaign.account_not_ready",
                                    f"Аккаунт: {acc_eff}", f"account:{account.id}",
                                    {"state": acc_eff}))

        if not c.message_text.strip():
            issues.append(Issue(IssueLevel.ERROR, "campaign.no_text",
                                "Не задан текст сообщения"))
        if not c.target_ids:
            issues.append(Issue(IssueLevel.WARNING, "campaign.no_targets",
                                "Не выбрано ни одного канала"))
        for tid in c.target_ids:
            if self.storage.targets.get(tid) is None:
                issues.append(Issue(IssueLevel.WARNING, "campaign.target_missing",
                                    "Канал не найден", f"target:{tid}",
                                    {"target_id": tid}))

        if mentions_operator(c.message_text):
            op = self.storage.operator_for(account) if account else None
            if op is None:
                issues.append(Issue(IssueLevel.ERROR, "operator.required",
                                    "В тексте есть @operator, но оператор не привязан"))
        return issues

    def campaign_effective(self, c: Campaign) -> str:
        issues = self.campaign_issues(c)
        if _worst(issues) == IssueLevel.ERROR:
            return EffectiveState.ERROR
        if c.raw_state in CampaignState.ACTIVE:
            account = self.storage.accounts.get(c.account_id)
            if account is None or self.account_effective(account) != EffectiveState.READY:
                return EffectiveState.BLOCKED
        return c.raw_state

    def campaign_view(self, c: Campaign) -> dict:
        issues = self.campaign_issues(c)
        return {
            **c.to_dict(),
            "effective": self.campaign_effective(c),
            "issues": [i.to_dict() for i in issues],
            "sent_count": c.sent_count,
            "sent_total": c.sent_total,
            "is_recurring": c.is_recurring,
            "failed_count": c.failed_count,
            "total_count": len(c.target_ids),
        }

    def can_run(self, c: Campaign) -> tuple[bool, str | None]:
        """Gate used by the scheduler right before it sends anything."""
        issues = self.campaign_issues(c)
        blocking = [i for i in issues if i.level == IssueLevel.ERROR]
        if blocking:
            return False, blocking[0].message
        account = self.storage.accounts.get(c.account_id)
        if account is None:
            return False, "Аккаунт кампании не найден"
        if self.account_effective(account) != EffectiveState.READY:
            return False, f"Аккаунт не готов: {self.account_effective(account)}"
        return True, None

    # ── auto-reply ──────────────────────────────────────────────────────
    def autoreply_issues(self, cfg: AutoReplyConfig) -> list[Issue]:
        issues: list[Issue] = []
        account = (None if cfg.owner_id == GLOBAL_OWNER
                   else self.storage.accounts.get(cfg.owner_id))
        operator = self.storage.operator_for(account) if account else None

        limit = self.storage.settings.faq_limit()
        faq = cfg.by_kind(AutoReplyKind.FAQ)
        if len(faq) > limit:
            issues.append(Issue(IssueLevel.WARNING, "autoreply.faq_over_limit",
                                f"FAQ-правил {len(faq)} при лимите {limit}",
                                params={"count": len(faq), "limit": limit}))
        for kind in AutoReplyKind.SINGLETON:
            if len(cfg.by_kind(kind)) > 1:
                issues.append(Issue(IssueLevel.ERROR, "autoreply.duplicate_singleton",
                                    f"Правил типа {kind} больше одного",
                                    params={"kind": kind}))

        for rule in cfg.rules:
            if not rule.enabled:
                continue
            if mentions_operator(rule.response) and operator is None:
                issues.append(Issue(
                    IssueLevel.ERROR, "operator.required",
                    "В ответе есть @operator, но оператор не привязан",
                    f"rule:{rule.id}", {"rule_id": rule.id}))
            if rule.kind == AutoReplyKind.FAQ and not rule.match.strip():
                issues.append(Issue(IssueLevel.WARNING, "autoreply.faq_no_match",
                                    "У FAQ-правила не задано условие",
                                    f"rule:{rule.id}", {"rule_id": rule.id}))

        if cfg.delay_min_sec > cfg.delay_max_sec:
            issues.append(Issue(IssueLevel.ERROR, "autoreply.bad_delay",
                                "Минимальная задержка больше максимальной"))
        return issues

    def autoreply_effective(self, cfg: AutoReplyConfig) -> str:
        issues = self.autoreply_issues(cfg)
        if _worst(issues) == IssueLevel.ERROR:
            return EffectiveState.ERROR
        if not cfg.enabled:
            return EffectiveState.DISABLED
        if cfg.owner_id != GLOBAL_OWNER:
            account = self.storage.accounts.get(cfg.owner_id)
            if account is None:
                return EffectiveState.ERROR
            if self.account_effective(account) != EffectiveState.READY:
                return EffectiveState.BLOCKED
        return EffectiveState.READY

    def autoreply_view(self, cfg: AutoReplyConfig) -> dict:
        return {
            **cfg.to_dict(),
            "effective": self.autoreply_effective(cfg),
            "issues": [i.to_dict() for i in self.autoreply_issues(cfg)],
            "inherited": False,
        }

    # ── operators / targets ─────────────────────────────────────────────
    def _connection_issues(self, owner) -> list[Issue]:
        """API profile and proxy checks, shared by accounts and operators.

        An operator reaches Telegram exactly the way an account does, so a
        broken profile has to read the same on both.
        """
        issues: list[Issue] = []
        if not owner.api_profile_id:
            issues.append(Issue(IssueLevel.ERROR, "api.not_assigned",
                                "API-профиль не назначен"))
        else:
            prof = self.storage.api_profiles.get(owner.api_profile_id)
            src = f"api_profile:{owner.api_profile_id}"
            if prof is None:
                issues.append(Issue(IssueLevel.ERROR, "api.missing",
                                    "API-профиль не найден", src))
            elif not prof.enabled:
                issues.append(Issue(IssueLevel.ERROR, "api.disabled",
                                    "API-профиль отключён", src,
                                    {"name": prof.name}))
            elif self.api_profile_view(prof)["effective"] == EffectiveState.ERROR:
                issues.append(Issue(IssueLevel.ERROR, "api.error",
                                    prof.last_error or "API-профиль недоступен",
                                    src, {"name": prof.name,
                                          "error": prof.last_error or ""}))

        if owner.network_profile_id:
            proxy = self.storage.network_profiles.get(owner.network_profile_id)
            src = f"network_profile:{owner.network_profile_id}"
            if proxy is None:
                issues.append(Issue(IssueLevel.ERROR, "proxy.missing",
                                    "Прокси-профиль не найден", src))
            elif not proxy.enabled:
                issues.append(Issue(IssueLevel.ERROR, "proxy.disabled",
                                    "Прокси-профиль отключён", src,
                                    {"name": proxy.name}))
            elif self.network_profile_view(proxy)["effective"] == EffectiveState.ERROR:
                issues.append(Issue(IssueLevel.ERROR, "proxy.error",
                                    proxy.last_error or "Прокси недоступен",
                                    src, {"name": proxy.name,
                                          "error": proxy.last_error or ""}))
        return issues

    def operator_issues(self, o: Operator) -> list[Issue]:
        issues: list[Issue] = []
        # A session identifies an operator on its own, and the probe fills the
        # name in from Telegram. Calling that an error meant an operator just
        # discovered from a session file looked broken, while an account found
        # the same way was merely offline - the same situation, two verdicts.
        if not o.username and not o.display_name and not o.logged_in:
            issues.append(Issue(IssueLevel.ERROR, "operator.no_name",
                                "Не задан ни ник, ни имя"))
        # A handle-only operator is a normal way to use one, so its connection
        # settings only start to matter once there is a session to connect.
        if o.logged_in:
            issues += self._connection_issues(o)
            if o.raw_state == AccountState.ERROR and o.last_error:
                issues.append(Issue(IssueLevel.ERROR, "account.error",
                                    o.last_error, params={"error": o.last_error}))
            issues += self._spam_issues(o)
        else:
            issues.append(Issue(IssueLevel.WARNING, "operator.not_logged_in",
                                "Аккаунт не авторизован — чаты недоступны"))
        return issues

    def operator_effective(self, o: Operator) -> str:
        issues = self.operator_issues(o)
        if _worst(issues) == IssueLevel.ERROR:
            return EffectiveState.ERROR
        if not o.logged_in:
            return EffectiveState.OFFLINE
        # Same table an account uses, ERROR included: without that row a
        # failed probe with no message fell through to OFFLINE, and the two
        # kinds disagreed about the same raw state.
        return {
            AccountState.READY: EffectiveState.READY,
            AccountState.QUEUED: EffectiveState.QUEUED,
            AccountState.CHECKING: EffectiveState.CHECKING,
            AccountState.RESTRICTED: EffectiveState.RESTRICTED,
            AccountState.ERROR: EffectiveState.ERROR,
        }.get(o.raw_state, EffectiveState.OFFLINE)

    def operator_view(self, o: Operator) -> dict:
        issues = self.operator_issues(o)
        bound = [a.id for a in self.storage.accounts.all() if a.operator_id == o.id]
        return {
            **o.to_dict(),
            "handle": o.handle,
            "logged_in": o.logged_in,
            "effective": self.operator_effective(o),
            "issues": [i.to_dict() for i in issues],
            "account_ids": bound,
        }

    def target_view(self, t: Target) -> dict:
        issues: list[Issue] = []
        status = (t.last_check_status or "").upper()
        if status and not status.startswith("AVAILABLE"):
            issues.append(Issue(IssueLevel.WARNING, "target.unavailable",
                                t.last_check_status or "Канал недоступен",
                                params={"error": t.last_check_status or ""}))
        if not t.username and not t.telegram_id:
            issues.append(Issue(IssueLevel.ERROR, "target.no_address",
                                "Не задан ни username, ни ID"))
        return {
            **t.to_dict(),
            "link": t.link,
            "effective": (EffectiveState.ERROR if _worst(issues) == IssueLevel.ERROR
                          else EffectiveState.DISABLED if not t.active
                          else EffectiveState.READY),
            "issues": [i.to_dict() for i in issues],
        }

    # ── full snapshot for GET /api/state ────────────────────────────────
    def snapshot(self) -> dict:
        s = self.storage
        return {
            "accounts": [self.account_view(a) for a in s.accounts.all()],
            "campaigns": [self.campaign_view(c) for c in s.campaigns.all()],
            "operators": [self.operator_view(o) for o in s.operators.all()],
            "targets": [self.target_view(t) for t in s.targets.all()],
            "templates": [t.to_dict() for t in s.templates.all()],
            "api_profiles": [self.api_profile_view(p) for p in s.api_profiles.all()],
            "network_profiles": [self.network_profile_view(p)
                                 for p in s.network_profiles.all()],
            "auto_reply": [self.autoreply_view(c) for c in s.auto_reply.all()],
            "settings": s.settings.data,
            "checkup": self.checkup_progress(),
        }

    def checkup_progress(self) -> dict:
        """Progress of a running "check all", for the button to show."""
        runner = getattr(self, "checkup", None)
        return runner.progress() if runner is not None else {
            "running": False, "total": 0, "done": 0, "current": None,
            "spam": False}
