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
    # Telegram threw the auth key away; re-checking can never bring it back,
    # only signing in again. Kept apart from ERROR so the interface can offer
    # the one action that helps instead of a "check again" that cannot.
    AUTH_DEAD = "AUTH_DEAD"
    ALL = (OFFLINE, QUEUED, CHECKING, READY, RESTRICTED, ERROR, AUTH_DEAD)

    # States a check is passing through rather than a verdict it reached.
    # Nothing should be stopped over them: they say "ask again in a moment".
    TRANSIENT = (QUEUED, CHECKING)


class SpamState:
    """What Telegram's antispam bot last said about an account."""
    UNKNOWN = "UNKNOWN"        # never checked - shows nothing in the UI
    CLEAN = "CLEAN"
    LIMITED = "LIMITED"
    # Blocked for good, on user reports. Apart from LIMITED because the two
    # need opposite things from the user: a limit runs out on a date the bot
    # gives, a block never does and the account has to be replaced.
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"          # the check itself did not complete
    ALL = (UNKNOWN, CLEAN, LIMITED, BLOCKED, FAILED)
    # Verdicts that stop an account sending. One tuple, so the check, the
    # state manager and the interface cannot disagree about which they are.
    BAD = (LIMITED, BLOCKED)


class EffectiveState:
    """What the UI paints. Computed, never persisted."""
    READY = "READY"
    OFFLINE = "OFFLINE"
    QUEUED = "QUEUED"
    CHECKING = "CHECKING"
    RESTRICTED = "RESTRICTED"
    DISABLED = "DISABLED"    # выключен — тумблером или самой программой
    BLOCKED = "BLOCKED"      # сломана зависимость
    ERROR = "ERROR"
    AUTH_DEAD = "AUTH_DEAD"  # the session is finished; sign in again
    FROZEN = "FROZEN"        # Telegram froze the account
    BANNED = "BANNED"        # Telegram blocked the account for good
    UNKNOWN = "UNKNOWN"      # an API profile no account has connected with yet
    WAITING = "WAITING"      # Telegram told the account to wait (FloodWait)
    ALL = (READY, OFFLINE, QUEUED, CHECKING, RESTRICTED, DISABLED, BLOCKED,
           ERROR, AUTH_DEAD, FROZEN, BANNED, UNKNOWN, WAITING)

    # What waiting fixes by itself. A campaign must not be stopped over one:
    # a check being queued is not a broken account, nor is a flood wait.
    TRANSIENT = (QUEUED, CHECKING, WAITING)


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
    LOOP = "LOOP"            # pass after pass until stopped
    ONCE = "ONCE"            # one pass at a given date+time
    DAILY = "DAILY"          # one pass at each listed time of day
    ALL = (LOOP, ONCE, DAILY)


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
