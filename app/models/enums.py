"""String enums as plain constant classes (JSON stays human-readable).

Raw states are what we measured. Effective states are computed in
app/state/manager.py and never stored — see docs/03_ARCHITECTURE.md §3.
"""


class ProbeState:
    """Raw state of an API profile or a proxy profile."""
    UNKNOWN = "UNKNOWN"
    CHECKING = "CHECKING"
    ONLINE = "ONLINE"
    ERROR = "ERROR"
    DISABLED = "DISABLED"
    ALL = (UNKNOWN, CHECKING, ONLINE, ERROR, DISABLED)


class AccountState:
    """Raw state of a broadcast account (what Telegram told us)."""
    OFFLINE = "OFFLINE"
    QUEUED = "QUEUED"          # a check was asked for, this one's turn has not come
    CHECKING = "CHECKING"
    READY = "READY"
    RESTRICTED = "RESTRICTED"
    ERROR = "ERROR"
    ALL = (OFFLINE, QUEUED, CHECKING, READY, RESTRICTED, ERROR)


class SpamState:
    """What Telegram's antispam bot last said about an account."""
    UNKNOWN = "UNKNOWN"        # never checked - shows nothing in the UI
    CLEAN = "CLEAN"
    LIMITED = "LIMITED"
    FAILED = "FAILED"          # the check itself did not complete
    ALL = (UNKNOWN, CLEAN, LIMITED, FAILED)


class EffectiveState:
    """What the UI paints. Computed, never persisted."""
    READY = "READY"
    OFFLINE = "OFFLINE"
    QUEUED = "QUEUED"
    CHECKING = "CHECKING"
    RESTRICTED = "RESTRICTED"
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"      # a dependency is broken
    ERROR = "ERROR"
    ALL = (READY, OFFLINE, QUEUED, CHECKING, RESTRICTED, DISABLED, BLOCKED,
           ERROR)


class CampaignState:
    """Raw state of a campaign."""
    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DONE = "DONE"
    ALL = (DRAFT, SCHEDULED, RUNNING, PAUSED, DONE)
    ACTIVE = (SCHEDULED, RUNNING)


class ScheduleMode:
    ONCE = "ONCE"            # one shot at a given date+time
    DAILY = "DAILY"          # every day at the listed times
    INTERVAL = "INTERVAL"    # every N seconds
    ALL = (ONCE, DAILY, INTERVAL)


class TargetResultStatus:
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ALL = (PENDING, SENT, FAILED, SKIPPED)


class TargetType:
    CHANNEL = "CHANNEL"
    GROUP = "GROUP"
    USER = "USER"
    ALL = (CHANNEL, GROUP, USER)


class AutoReplyKind:
    FIRST_MESSAGE = "FIRST_MESSAGE"   # at most one active rule per account
    PERIODIC = "PERIODIC"             # at most one active rule per account
    FAQ = "FAQ"                       # 0..N, N from settings autoreply.faq_limit
    ALL = (FIRST_MESSAGE, PERIODIC, FAQ)
    SINGLETON = (FIRST_MESSAGE, PERIODIC)


class IssueLevel:
    ERROR = "error"
    WARNING = "warning"
    ALL = (ERROR, WARNING)


GLOBAL_OWNER = "global"   # AutoReplyConfig.owner_id for the app-wide default
