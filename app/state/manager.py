"""The single place where business status is decided.

Raw state is what we measured and stored. Effective state is derived
here on every read and never written to disk, so a campaign cannot
report RUNNING while its proxy is in ERROR: RUNNING is computed from
the account, which is computed from the proxy.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .. import config
from ..models import (
    Account, ApiProfile, AutoReplyConfig, Campaign, NetworkProfile, Operator,
    Target,
)
from ..models.enums import (
    AccountState, AutoReplyKind, CampaignState, EffectiveState, GLOBAL_OWNER,
    IssueLevel, ProbeState, SpamState,
)
from ..messages import msg, text_of
from ..util import parse_iso, until_text

# How Telegram says it does not know an api_id / api_hash pair.
API_REJECTED = "ApiIdInvalidError"

OPERATOR_TOKEN = "@operator"
_OPERATOR_RE = re.compile(r"@operator\b", re.IGNORECASE)


def mentions_operator(text: str) -> bool:
    return bool(_OPERATOR_RE.search(text or ""))


@dataclass
class Issue:
    """A problem shown in the UI as a coloured dot with a tooltip.

    The interface words it from `code` ("issue.<code>") and `params`; a
    param may itself be a message, translated first.
    """
    level: str
    code: str
    source: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def as_message(self) -> dict:
        return msg(f"issue.{self.code}", **self.params)


@dataclass
class RunBlock:
    """Why a campaign cannot send, and whether waiting will fix it.

    A transient block is a check passing through: the campaign keeps its
    schedule. A permanent one stops it and records why, as a message.
    """
    reason: dict
    transient: bool = False


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
        """An API profile has no probe of its own: its status is what the
        sessions using it found."""
        issues: list[Issue] = []
        users = [r for r in (*self.storage.accounts.all(),
                             *self.storage.operators.all())
                 if r.api_profile_id == p.id and r.key]
        rejected = [u for u in users if API_REJECTED in text_of(u.last_error)]
        if not p.enabled:
            effective = EffectiveState.DISABLED
        elif not (p.api_id and p.api_hash):
            effective = EffectiveState.ERROR
            issues.append(Issue(IssueLevel.ERROR, "api.incomplete"))
        elif rejected:
            effective = EffectiveState.ERROR
            issues.append(Issue(IssueLevel.ERROR, "api.rejected",
                                params={"error": rejected[0].last_error}))
        elif any(u.raw_state == AccountState.READY for u in users):
            effective = EffectiveState.READY
        else:
            effective = EffectiveState.UNKNOWN
        return {**p.to_dict(), "effective": effective,
                "issues": [i.to_dict() for i in issues]}

    def network_profile_view(self, p: NetworkProfile) -> dict:
        issues: list[Issue] = []
        if not p.enabled:
            effective = EffectiveState.DISABLED
        elif p.raw_state == ProbeState.ERROR:
            effective = EffectiveState.ERROR
            issues.append(Issue(IssueLevel.ERROR, "proxy.error",
                                params={"error": p.last_error}))
        elif p.raw_state == ProbeState.ONLINE:
            effective = EffectiveState.READY
        elif p.raw_state == ProbeState.CHECKING:
            effective = EffectiveState.CHECKING
        else:
            effective = EffectiveState.OFFLINE
        if p.enabled and not (p.host and p.port):
            issues.append(Issue(IssueLevel.ERROR, "proxy.incomplete"))
            effective = EffectiveState.ERROR
        return {**p.to_dict(), "effective": effective,
                "issues": [i.to_dict() for i in issues]}

    # ── accounts ────────────────────────────────────────────────────────
    # Verdicts that end an account's usefulness until the user acts, worst
    # first. What they share: waiting does not help, and what does help is
    # different for each.
    # (effective state, code on the account, code on a campaign)
    FAULTS = (
        (EffectiveState.AUTH_DEAD, "account.auth_dead",
         "campaign.account_auth_dead"),
        (EffectiveState.FROZEN, "account.frozen", "campaign.account_frozen"),
        (EffectiveState.BANNED, "account.banned", "campaign.account_banned"),
    )

    @staticmethod
    def _fault_of(owner) -> str | None:
        """Which terminal verdict stands against this account, if any.

        Asked in one place so the dot, the campaign gate and the overview row
        cannot disagree. Each verdict has its own field: a check writes
        CHECKING over raw_state on its way past.
        """
        if owner.raw_state == AccountState.AUTH_DEAD:
            return EffectiveState.AUTH_DEAD
        if getattr(owner, "frozen_at", None):
            return EffectiveState.FROZEN
        if getattr(owner, "spam_state", None) == SpamState.BLOCKED:
            return EffectiveState.BANNED
        return None

    def fault_issue(self, owner) -> Issue | None:
        """The fault above, worded for the tooltip."""
        fault = self._fault_of(owner)
        if fault is None:
            return None
        code = next(c for state, c, _ in self.FAULTS if state == fault)
        detail = (owner.frozen_detail if fault == EffectiveState.FROZEN
                  else owner.spam_detail if fault == EffectiveState.BANNED
                  else owner.last_error)
        if fault == EffectiveState.FROZEN \
                and str((detail or {}).get("code", "")).startswith("tg.Frozen"):
            detail = None       # Telegram's own error only says «frozen» again
        return Issue(IssueLevel.ERROR, code, params={"detail": detail})

    def account_issues(self, a: Account) -> list[Issue]:
        """Everything wrong with the account itself or with what it depends on."""
        issues: list[Issue] = self._connection_issues(a)

        # operator — optional, a dangling reference is a warning
        if a.operator_id:
            op = self.storage.operators.get(a.operator_id)
            if op is None:
                issues.append(Issue(IssueLevel.WARNING, "operator.missing",
                                    f"operator:{a.operator_id}"))

        if a.stop_note:
            issues.append(Issue(IssueLevel.WARNING, "account.stopped",
                                params={"note": a.stop_note}))
        if not a.key:
            issues.append(Issue(IssueLevel.WARNING, "account.no_session"))
        fault = self.fault_issue(a)
        if fault is not None:
            issues.append(fault)
        # A fault already says what is wrong and what to do about it; the raw
        # error it came from would only repeat that in Telethon's words. So
        # does a rejected API profile, on the profile's own row.
        if (fault is None and a.raw_state == AccountState.ERROR and a.last_error
                and API_REJECTED not in text_of(a.last_error)):
            issues.append(Issue(IssueLevel.ERROR, "account.error",
                                params={"error": a.last_error}))
        if a.raw_state == AccountState.RESTRICTED:
            issues.append(Issue(IssueLevel.WARNING, "account.restricted"))

        issues += self._spam_issues(a)
        if a.flood_left() > 0:
            issues.append(Issue(IssueLevel.WARNING, "account.flood_wait",
                                params={"time": until_text(
                                    parse_iso(a.flood_until))}))
        issues += self._young_issue(a)
        return issues

    def _young_issue(self, a: Account) -> list[Issue]:
        """Warn about an account added to the app only days ago.

        Telegram publishes no signup date, so this counts from when it was
        added here - the same week for a freshly bought account.
        """
        days = self.storage.settings.number("accounts.young_days")
        added = parse_iso(a.created_at) if a.created_at else None
        if days <= 0 or added is None:
            return []
        age = (datetime.now() - added).days
        if age >= days:
            return []
        return [Issue(IssueLevel.WARNING, "account.young",
                      params={"days": age, "limit": days})]

    def _spam_issues(self, owner) -> list[Issue]:
        """The antispam verdict, for an account or an operator alike.

        A limit does not stop campaigns by itself, so it is a warning: the dot
        stops being green and the tooltip says why.
        """
        state = owner.spam_state
        if state == SpamState.BLOCKED:
            # `fault_issue` already carries this one, with the wording that
            # says the account is finished rather than held back.
            return []
        code = {SpamState.LIMITED: "account.spam_limited",
                SpamState.FAILED: "account.spam_check_failed"}.get(state)
        # checked, but the bot said something we do not recognise - saying
        # nothing here would look like a clean result
        if state == SpamState.UNKNOWN and owner.spam_checked_at:
            code = "account.spam_unclear"
        if code is None:
            return []
        return [Issue(IssueLevel.WARNING, code, params={"detail": owner.spam_detail})]

    def account_effective(self, a: Account) -> str:
        issues = self.account_issues(a)
        # A terminal verdict outranks every other fault and the switch alike:
        # «Ошибка» or «Выключен» would send the user looking for the wrong
        # thing.
        fault = self._fault_of(a)
        if fault is not None:
            return fault
        if _worst(issues) == IssueLevel.ERROR:
            return EffectiveState.ERROR
        if a.disabled:
            return EffectiveState.DISABLED
        if a.raw_state == AccountState.READY and a.flood_left() > 0:
            return EffectiveState.WAITING
        return {
            AccountState.READY: EffectiveState.READY,
            AccountState.QUEUED: EffectiveState.QUEUED,
            AccountState.CHECKING: EffectiveState.CHECKING,
            AccountState.RESTRICTED: EffectiveState.RESTRICTED,
            AccountState.ERROR: EffectiveState.ERROR,
        }.get(a.raw_state, EffectiveState.OFFLINE)

    def account_usable(self, a: Account) -> bool:
        """Connected and allowed to send - now, or once a flood wait Telegram
        named runs out. What listens for messages and keeps campaigns armed."""
        return self.account_effective(a) in (EffectiveState.READY,
                                             EffectiveState.WAITING)

    def account_view(self, a: Account) -> dict:
        issues = self.account_issues(a)
        eff = self.account_effective(a)
        gap = self.operator_gap(a.id)
        return {
            **a.to_dict(),
            "handle": a.handle,
            "display_name": a.display_name,
            "effective": eff,
            "issues": [i.to_dict() for i in issues],
            "campaign_ids": [c.id for c in self.storage.campaigns_for_account(a.id)],
            "has_own_autoreply": self.storage.has_own_autoreply(a.id),
            "linked_operator_id": getattr(self.storage.linked_operator(a), "id", None),
            # the same answer for the account's own auto-reply, so the page
            # can word its hint before that config exists
            "operator_gap": gap.to_dict() if gap is not None else None,
        }

    # ── campaigns ───────────────────────────────────────────────────────
    def campaign_issues(self, c: Campaign) -> list[Issue]:
        issues: list[Issue] = []
        account = self.storage.accounts.get(c.account_id) if c.account_id else None
        if account is None:
            issues.append(Issue(IssueLevel.ERROR, "campaign.account_missing",
                                f"account:{c.account_id}"))
        else:
            acc_eff = self.account_effective(account)
            fault = next((code for state, _a, code in self.FAULTS
                          if state == acc_eff), None)
            if fault is not None:
                # As blocking as an error and worth its own wording: waiting
                # cannot help.
                issues.append(Issue(IssueLevel.ERROR, fault, f"account:{account.id}",
                                    {"account": account.handle}))
            elif not self.account_usable(account):
                level = (IssueLevel.ERROR if acc_eff in
                         (EffectiveState.ERROR, EffectiveState.DISABLED)
                         else IssueLevel.WARNING)
                issues.append(Issue(level, "campaign.account_not_ready",
                                    f"account:{account.id}",
                                    {"state": msg(f"state.{acc_eff}")}))

        if not c.has_text:
            issues.append(Issue(IssueLevel.ERROR, "campaign.no_text"))
        if not c.target_ids:
            # An error, not a warning: without channels a campaign cannot send at
            # all.
            issues.append(Issue(IssueLevel.ERROR, "campaign.no_targets"))
        for tid in c.target_ids:
            if self.storage.targets.get(tid) is None:
                issues.append(Issue(IssueLevel.WARNING, "campaign.target_missing",
                                    f"target:{tid}", {"target_id": tid}))

        # Any one of the texts may name the operator, and any one of them
        # may be the one this cycle picks.
        if any(mentions_operator(m.text) for m in c.messages):
            op = self.storage.operator_for(account) if account else None
            if op is None:
                issues.append(Issue(IssueLevel.ERROR, "operator.required"))
        return issues

    def campaign_effective(self, c: Campaign) -> str:
        issues = self.campaign_issues(c)
        if _worst(issues) == IssueLevel.ERROR:
            return EffectiveState.ERROR
        if c.raw_state in CampaignState.ACTIVE:
            account = self.storage.accounts.get(c.account_id)
            if account is None or not self.account_usable(account):
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

    def run_block(self, c: Campaign) -> "RunBlock | None":
        """Why this campaign must not send right now, or None.

        `transient` is the point of returning an object: QUEUED and CHECKING
        mean "ask again in a moment", and treating them as faults paused every
        campaign on every antispam pass. Everything else - a limit, a freeze, a
        dead session, a disabled account, a broken proxy - really does stop it.
        """
        issues = self.campaign_issues(c)
        blocking = [i for i in issues if i.level == IssueLevel.ERROR]
        if blocking:
            return RunBlock(blocking[0].as_message())
        account = self.storage.accounts.get(c.account_id)
        state = self.account_effective(account)
        if state == EffectiveState.READY:
            return None
        return RunBlock(msg("issue.campaign.account_not_ready",
                            state=msg(f"state.{state}")),
                        transient=state in EffectiveState.TRANSIENT)

    # ── auto-reply ──────────────────────────────────────────────────────
    def operator_gap(self, owner_id: str) -> Issue | None:
        """Can `@operator` be resolved for everyone this config answers for?

        The token is substituted against the account that received the message,
        never against the config holding the text. For an account's own config
        no operator means the reply cannot go out: an error. The shared default
        has no operator of its own and never could - what matters there is
        coverage, so accounts without one are a warning, not a refusal.
        One method, so the save, the switch and the dot cannot disagree.
        """
        if owner_id != GLOBAL_OWNER:
            account = self.storage.accounts.get(owner_id)
            if account is not None and self.storage.operator_for(account) is not None:
                return None
            return Issue(IssueLevel.ERROR, "operator.required")

        # Only accounts that fall back to the shared default count: one with
        # its own config never sends this text, a switched-off one never sends.
        audience = [a for a in self.storage.accounts.all()
                    if not a.disabled and not self.storage.has_own_autoreply(a.id)]
        missing = [a for a in audience if self.storage.operator_for(a) is None]
        if not missing:
            return None
        return Issue(IssueLevel.WARNING, "operator.partial_coverage",
                     params={"missing": len(missing), "total": len(audience)})

    def autoreply_issues(self, cfg: AutoReplyConfig) -> list[Issue]:
        issues: list[Issue] = []
        limit = self.storage.settings.faq_limit()
        faq = cfg.by_kind(AutoReplyKind.FAQ)
        if len(faq) > limit:
            issues.append(Issue(IssueLevel.WARNING, "autoreply.faq_over_limit",
                                params={"count": len(faq), "limit": limit}))
        for kind in AutoReplyKind.SINGLETON:
            if len(cfg.by_kind(kind)) > 1:
                issues.append(Issue(IssueLevel.ERROR, "autoreply.duplicate_singleton",
                                    params={"kind": msg(f"autoreply.kind.{kind}")}))

        # Asked once, not once per rule: five rules saying @operator are
        # still one thing to fix, and one line to say it.
        if any(r.enabled and mentions_operator(r.response) for r in cfg.rules):
            gap = self.operator_gap(cfg.owner_id)
            if gap is not None:
                issues.append(gap)

        for rule in cfg.rules:
            if not rule.enabled:
                continue
            if rule.kind == AutoReplyKind.FAQ and not rule.match.strip():
                issues.append(Issue(IssueLevel.WARNING, "autoreply.faq_no_match",
                                    f"rule:{rule.id}", {"rule_id": rule.id}))

        if cfg.delay_min_sec > cfg.delay_max_sec:
            issues.append(Issue(IssueLevel.ERROR, "autoreply.bad_delay"))
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
            if not self.account_usable(account):
                return EffectiveState.BLOCKED
        return EffectiveState.READY

    def autoreply_view(self, cfg: AutoReplyConfig) -> dict:
        gap = self.operator_gap(cfg.owner_id)
        return {
            **cfg.to_dict(),
            "effective": self.autoreply_effective(cfg),
            "issues": [i.to_dict() for i in self.autoreply_issues(cfg)],
            # whether @operator may be used in this text, and why not - the
            # page shows it as advice even when no rule mentions the token yet
            "operator_gap": gap.to_dict() if gap is not None else None,
        }

    # ── operators / targets ─────────────────────────────────────────────
    def _connection_issues(self, owner) -> list[Issue]:
        """API profile and proxy checks, shared by accounts and operators.

        An operator reaches Telegram exactly as an account does, so a broken
        profile has to read the same on both.
        """
        issues: list[Issue] = []
        if not owner.api_profile_id:
            issues.append(Issue(IssueLevel.ERROR, "api.not_assigned"))
        else:
            prof = self.storage.api_profiles.get(owner.api_profile_id)
            src = f"api_profile:{owner.api_profile_id}"
            if prof is None:
                issues.append(Issue(IssueLevel.ERROR, "api.missing", src))
            elif not prof.enabled:
                issues.append(Issue(IssueLevel.ERROR, "api.disabled", src,
                                    {"name": prof.name}))
            elif self.api_profile_view(prof)["effective"] == EffectiveState.ERROR:
                issues.append(Issue(IssueLevel.ERROR, "api.error", src,
                                    {"name": prof.name}))

        if owner.network_profile_id:
            proxy = self.storage.network_profiles.get(owner.network_profile_id)
            src = f"network_profile:{owner.network_profile_id}"
            if proxy is None:
                issues.append(Issue(IssueLevel.ERROR, "proxy.missing", src))
            elif not proxy.enabled:
                issues.append(Issue(IssueLevel.ERROR, "proxy.disabled", src,
                                    {"name": proxy.name}))
            elif self.network_profile_view(proxy)["effective"] == EffectiveState.ERROR:
                issues.append(Issue(IssueLevel.ERROR, "proxy.error", src,
                                    {"name": proxy.name, "error": proxy.last_error}))
        return issues

    def operator_issues(self, o: Operator) -> list[Issue]:
        account = self.storage.linked_account(o)
        if account is not None:
            # Everything but the notes is the account's, problems included.
            return [i for i in self.account_issues(account)
                    if i.code != "account.young"]
        issues: list[Issue] = []
        if o.stop_note:
            issues.append(Issue(IssueLevel.WARNING, "account.stopped",
                                params={"note": o.stop_note}))
        if not o.username and not o.display_name and not o.key:
            issues.append(Issue(IssueLevel.ERROR, "operator.no_name"))
        # A handle-only operator is a normal way to use one, so its connection
        # settings only start to matter once there is a session to connect.
        if o.key:
            issues += self._connection_issues(o)
            fault = self.fault_issue(o)
            if fault is not None:
                issues.append(fault)
            if fault is None and o.raw_state == AccountState.ERROR and o.last_error:
                issues.append(Issue(IssueLevel.ERROR, "account.error",
                                    params={"error": o.last_error}))
            issues += self._spam_issues(o)
        else:
            issues.append(Issue(IssueLevel.WARNING, "operator.not_logged_in"))
        return issues

    def operator_effective(self, o: Operator) -> str:
        account = self.storage.linked_account(o)
        if account is not None:
            return self.account_effective(account)
        issues = self.operator_issues(o)
        fault = self._fault_of(o) if o.key else None
        if fault is not None:
            return fault
        if _worst(issues) == IssueLevel.ERROR:
            return EffectiveState.ERROR
        if not o.key:
            return EffectiveState.OFFLINE
        return {
            AccountState.READY: EffectiveState.READY,
            AccountState.QUEUED: EffectiveState.QUEUED,
            AccountState.CHECKING: EffectiveState.CHECKING,
            AccountState.RESTRICTED: EffectiveState.RESTRICTED,
            AccountState.ERROR: EffectiveState.ERROR,
        }.get(o.raw_state, EffectiveState.OFFLINE)

    def operator_view(self, o: Operator) -> dict:
        account = self.storage.linked_account(o)
        bound = [a.id for a in self.storage.accounts.all() if a.operator_id == o.id]
        view = {
            **o.to_dict(),
            "handle": self.storage.operator_handle(o),
            "logged_in": bool((account or o).key),
            "effective": self.operator_effective(o),
            "issues": [i.to_dict() for i in self.operator_issues(o)],
            "account_ids": bound,
        }
        if account is not None:
            # shown as the operator's own, set through the account
            view.update(username=account.username or "",
                        display_name=account.display_name,
                        telegram_id=account.telegram_id,
                        api_profile_id=account.api_profile_id,
                        network_profile_id=account.network_profile_id,
                        last_check_at=account.last_check_at,
                        flood_until=account.flood_until)
        return view

    def target_view(self, t: Target) -> dict:
        issues: list[Issue] = []
        if t.last_check_error:
            issues.append(Issue(IssueLevel.WARNING, "target.unavailable",
                                params={"error": t.last_check_error}))
        if not t.username and not t.telegram_id and not t.invite:
            issues.append(Issue(IssueLevel.ERROR, "target.no_address"))
        return {
            **t.to_dict(),
            "link": t.link,
            "effective": (EffectiveState.ERROR if _worst(issues) == IssueLevel.ERROR
                          else EffectiveState.DISABLED if not t.active
                          else EffectiveState.READY),
            "issues": [i.to_dict() for i in issues],
        }

    # ── the overview list ───────────────────────────────────────────────
    # Problems a campaign has only because of its account: the account row
    # already says it, so the campaign counts towards that row.
    ACCOUNT_ECHOES = ("campaign.account_not_ready", "campaign.account_missing",
                      *(campaign_code for _s, _c, campaign_code in FAULTS))

    # Above this many accounts behind one address or one API key, the
    # setup is the problem rather than any single account. Two is normal;
    # eighteen is what the user had when Telegram started freezing them.
    CROWDING_LIMIT = 3

    def _crowding_rows(self) -> list[dict]:
        """Accounts crowded behind one proxy or one API key.

        Nothing is wrong with any one of them, which is why nothing said so:
        eighteen green dots went out through one address and Telegram froze
        them in turn. A warning, never a refusal - only the user knows what the
        accounts are for.
        """
        rows: list[dict] = []
        groups = (
            ("network_profile_id", self.storage.network_profiles,
             "account.shared_proxy"),
            ("api_profile_id", self.storage.api_profiles, "account.shared_api"),
        )
        for field_name, repo, code in groups:
            counts: dict[str, int] = {}
            for a in self.storage.accounts.all():
                if a.disabled:
                    continue
                pid = getattr(a, field_name, None)
                if pid:
                    counts[pid] = counts.get(pid, 0) + 1
            for pid, count in counts.items():
                if count < self.CROWDING_LIMIT:
                    continue
                profile = repo.get(pid)
                name = getattr(profile, "name", "") or pid
                rows.append({
                    "key": f"crowding:{code}:{pid}", "id": pid,
                    "route": "settings", "title": name, "roles": ["advice"],
                    "blocked_campaigns": 0,
                    "issues": [Issue(IssueLevel.WARNING, code, params={
                        "count": count, "name": name}).to_dict()],
                })
        return rows

    def problems(self) -> list[dict]:
        """Everything that needs attention, one row per cause.

        Built here, not in the interface: "these four complaints are one dead
        session" is a judgement about status. Three rules collapse them - a
        fault blamed on a profile is left to that profile's row, an operator
        made from an account is the account's row, and a campaign blocked only
        by its account counts towards the account's row.
        """
        rows: list[dict] = []
        by_account: dict[str, dict] = {}
        covered: set[str] = set()

        def row(id_: str, route: str, title: str, role: str,
                issues: list[dict]) -> dict:
            entry = {"key": f"{route}:{id_}", "id": id_, "route": route,
                     "title": title, "roles": [role], "issues": list(issues),
                     "blocked_campaigns": 0}
            rows.append(entry)
            return entry

        # profiles first: they are the source others point at
        for p in self.storage.api_profiles.all():
            view = self.api_profile_view(p)
            if view["issues"]:
                covered.add(f"api_profile:{p.id}")
                row(p.id, "settings", p.name or p.id, "api_profile",
                    view["issues"])
        for p in self.storage.network_profiles.all():
            view = self.network_profile_view(p)
            if view["issues"]:
                covered.add(f"network_profile:{p.id}")
                row(p.id, "settings", p.name or p.host or p.id,
                    "network_profile", view["issues"])

        def own(issues: list[Issue]) -> list[dict]:
            """What this record has to answer for itself."""
            return [i.to_dict() for i in issues if i.source not in covered]

        # An account can block campaigns while having nothing wrong with
        # itself - merely offline. Without a row of its own the problem would
        # be unsaid anywhere, so the campaigns count towards it.
        blocking = {c.account_id for c in self.storage.campaigns.all()
                    if c.account_id
                    and any(i.code in self.ACCOUNT_ECHOES
                            for i in self.campaign_issues(c))}

        for a in self.storage.accounts.all():
            issues = own(self.account_issues(a))
            if not issues and a.id in blocking:
                state = self.account_effective(a)
                issues = [Issue(IssueLevel.WARNING, "account.not_ready",
                                params={"state": msg(f"state.{state}")}).to_dict()]
            if not issues:
                continue
            by_account[a.id] = row(a.id, "accounts", a.handle, "account", issues)

        # An operator made from an account has the account's problems, and
        # the account's row already says them.
        for o in self.storage.operators.all():
            if o.linked:
                continue
            issues = self.operator_issues(o)
            if not any(i.level == IssueLevel.ERROR for i in issues):
                continue
            row(o.id, "operators", o.handle, "operator", own(issues))

        rows += self._crowding_rows()

        for c in self.storage.campaigns.all():
            issues = self.campaign_issues(c)
            echoes = [i for i in issues if i.code in self.ACCOUNT_ECHOES]
            mine = [i for i in issues if i.code not in self.ACCOUNT_ECHOES]
            account_row = by_account.get(c.account_id)
            if echoes and account_row is not None:
                account_row["blocked_campaigns"] += 1
            elif echoes:
                # the account itself is gone, so nothing upstream can say it
                mine = list(issues)
            if mine:
                row(c.id, "campaigns", c.name, "campaign", own(mine))

        return [r for r in rows if r["issues"]]

    # ── full snapshot for GET /api/state ────────────────────────────────
    def snapshot(self) -> dict:
        s = self.storage
        return {
            "accounts": [self.account_view(a) for a in s.accounts.all()],
            "campaigns": [self.campaign_view(c) for c in s.campaigns.all()],
            "operators": [self.operator_view(o) for o in s.operators.all()],
            "targets": [self.target_view(t) for t in s.targets.all()],
            "problems": self.problems(),
            "api_profiles": [self.api_profile_view(p) for p in s.api_profiles.all()],
            "network_profiles": [self.network_profile_view(p)
                                 for p in s.network_profiles.all()],
            "auto_reply": [self.autoreply_view(c) for c in s.auto_reply.all()],
            "settings": s.settings.data,
            "checkup": self.checkup_progress(),
            # Where the app keeps things, so the user does not have to know. The
            # log folder is the one worth saying: it is what to open in the
            # morning.
            "paths": {"logs": str(config.LOGS_DIR), "data": str(config.DATA_DIR)},
        }

    def checkup_progress(self) -> dict:
        """Progress of a running "check all", for the button to show."""
        runner = getattr(self, "checkup", None)
        return runner.progress() if runner is not None else {
            "running": False, "total": 0, "done": 0, "current": None,
            "spam": False, "kind": ""}
