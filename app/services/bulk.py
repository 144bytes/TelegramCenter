"""One action, many records.

Nothing here does any work: each action names a service method that
already exists, and this file is only the loop plus the two things a
loop must get right - one failure does not cancel the rest, and the
caller is told which ones refused and why.

Checking is deliberately not in the table: it is not a loop over
records but the one check run, which already takes a list of ids.
"""
from __future__ import annotations

from ..logging import LOG
from ..messages import AppError, msg, text_of

MOD = "bulk"


class BulkError(AppError):
    """Why a bulk action cannot start at all."""


class BulkService:
    def __init__(self, storage, services):
        self.storage = storage
        self.services = services

    # ── what each kind can do ───────────────────────────────────────────
    def _campaign_actions(self) -> dict:
        campaigns = self.services.campaigns
        return {
            "start": lambda c, v: campaigns.start(c),
            "pause": lambda c, v: campaigns.pause(c),
            "reset": lambda c, v: campaigns.reset(c),
            "delete": lambda c, v: campaigns.delete(c.id),
            "update": lambda c, v: campaigns.update(c, self._campaign_fields(v)),
        }

    @staticmethod
    def _sent(values: dict, allowed: tuple) -> dict:
        """Only the fields the user actually filled in.

        A field left alone must not become «set this to empty for all twenty
        of them», so the payload carries exactly the keys that were sent.
        """
        return {k: values[k] for k in allowed if k in values}

    def _campaign_fields(self, values: dict) -> dict:
        out = self._sent(values, ("account_id", "target_ids", "interval_min_sec",
                                  "interval_max_sec", "schedule", "messages"))
        # Channels picked replace every campaign's list; none picked keeps each.
        if not out.get("target_ids"):
            out.pop("target_ids", None)
        return out

    def _account_actions(self) -> dict:
        accounts = self.services.accounts
        return {
            "enable": lambda a, v: accounts.set_disabled(a, False),
            "disable": lambda a, v: accounts.set_disabled(a, True),
            "delete": lambda a, v: accounts.delete(
                a.id, keep_operator=bool(v.get("keep_operator"))),
            # The save the account's own window makes: a new API and a new
            # proxy arrive together, so each account reconnects once and
            # never through half of the change. A profile that does not
            # exist is refused before anything is touched.
            "update": lambda a, v: accounts.edit(a, self._sent(
                v, ("api_profile_id", "network_profile_id", "operator_id"))),
        }

    def _operator_actions(self) -> dict:
        operators = self.services.operators
        # A linked operator's API and proxy are its account's: `update`
        # sets them there, so the choice is still one setting.
        return {
            "delete": lambda o, v: operators.delete(o.id),
            "update": lambda o, v: operators.update(o, **self._sent(
                v, ("api_profile_id", "network_profile_id"))),
        }

    def _target_actions(self) -> dict:
        catalog = self.services.catalog
        return {
            "enable": lambda t, v: catalog.update_target(t, active=True),
            "disable": lambda t, v: catalog.update_target(t, active=False),
            "delete": lambda t, v: catalog.delete_target(t.id),
        }

    # ── the loop ────────────────────────────────────────────────────────
    KINDS = ("campaigns", "accounts", "operators", "targets")

    def _table(self, kind: str) -> tuple[dict, object, object]:
        """The actions, the records and how a record is named in a report."""
        if kind == "campaigns":
            return self._campaign_actions(), self.storage.campaigns, lambda c: c.name
        if kind == "accounts":
            return self._account_actions(), self.storage.accounts, lambda a: a.handle
        if kind == "operators":
            return (self._operator_actions(), self.storage.operators,
                    self.storage.operator_handle)
        if kind == "targets":
            return self._target_actions(), self.storage.targets, lambda t: t.title
        raise BulkError("err.bulk.unknown_kind", kind=kind)

    def apply(self, kind: str, ids: list[str], action: str,
              values: dict | None = None) -> dict:
        """Run one action over the named records. Never raises for one record.

        Returns {"done": n, "failed": [{"id", "name", "error"}]} so the
        interface can say «16 из 18» and name the two.
        """
        actions, repo, name_of = self._table(kind)
        handler = actions.get(action)
        if handler is None:
            raise BulkError("err.bulk.unknown_action", action=action)
        if not ids:
            raise BulkError("err.bulk.nothing_selected")
        values = values or {}

        done = 0
        failed: list[dict] = []
        for id_ in ids:
            record = repo.get(id_)
            if record is None:
                failed.append({"id": id_, "name": id_,
                               "error": msg("err.not_found.record")})
                continue
            name = str(name_of(record) or id_)
            try:
                handler(record, values)
            except AppError as exc:
                failed.append({"id": id_, "name": name, "error": exc.msg})
                continue
            except Exception as exc:  # noqa: BLE001 - one bad record, not the batch
                failed.append({"id": id_, "name": name,
                               "error": msg("raw", text=f"{type(exc).__name__}: {exc}")})
                continue
            done += 1

        LOG.info(f"{kind}/{action}: {done} done, {len(failed)} refused",
                 module=MOD)
        for row in failed:
            LOG.warning(f"{kind}/{action} {row['name']}: {text_of(row['error'])}",
                        module=MOD)
        return {"done": done, "failed": failed, "total": len(ids)}
