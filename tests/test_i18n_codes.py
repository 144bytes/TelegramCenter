"""Every message code the backend can send has wording in both languages.

The backend holds no wording of its own: it sends codes, and the
interface's dictionaries (web/src/i18n.ts) word them. A code missing there
would show up as the bare code, so this reads the backend's source for
every code it builds and checks both dictionaries have it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.models.enums import (AutoReplyKind, CampaignState, EffectiveState,
                              TargetResultStatus)
from app.telegram.errors import KNOWN, WAIT_ERRORS

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "web" / "src" / "i18n.ts"
APP = ROOT / "app"

PAIR = re.compile(r'"((?:[^"\\]|\\.)+)":\s*"((?:[^"\\]|\\.)*)"', re.S)


def _block(src: str, name: str) -> str:
    start = src.index("{", src.index(f"const {name}"))
    depth = 0
    for i in range(start, len(src)):
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start + 1:i]
    raise AssertionError(f"no end for {name}")


def _dicts() -> tuple[dict, dict]:
    src = I18N.read_text(encoding="utf-8")
    parse = lambda block: {k: json.loads(f'"{v}"')  # noqa: E731
                           for k, v in PAIR.findall(block)}
    return parse(_block(src, "ru")), parse(_block(src, "en"))


# message codes written out in the source: msg("..."), SomeError("...") and
# the HTTP layer's own refusals
LITERAL = re.compile(
    r'(?:\bmsg|Error|Conflict|NotFound|_error\(\d+,|_lookup\("\w+",)\(?\s*"'
    r'((?:err|stop|note|result|excluded|tg|delivery|spam|probe|login)'
    r'\.[A-Za-z_.]+)"')
ISSUE = re.compile(r'Issue\(\s*(?:IssueLevel\.\w+|level|code|fault),\s*"([a-z_.]+)"')
FAULT = re.compile(r'"((?:account|campaign)\.[a-z_]+)"')


def _backend_codes() -> set[str]:
    codes: set[str] = set()
    for path in APP.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        codes |= set(LITERAL.findall(src))
        codes |= {f"issue.{c}" for c in ISSUE.findall(src)}
        if path.name == "manager.py":
            # issue codes chosen from tables: faults, spam verdicts, crowding
            codes |= {f"issue.{c}" for c in FAULT.findall(src)}
    codes |= {f"tg.{name}" for name in (*KNOWN, *WAIT_ERRORS)}
    codes |= {"tg.not_found", "raw", "issue.message"}
    codes |= {f"state.{s}" for s in (*EffectiveState.ALL, *CampaignState.ALL)}
    codes |= {f"campaigns.result.{s}" for s in TargetResultStatus.ALL}
    codes |= {f"autoreply.kind.{k}" for k in AutoReplyKind.ALL}
    return codes


def test_both_dictionaries_have_the_same_keys():
    ru, en = _dicts()
    assert set(ru) == set(en)


def test_every_backend_code_has_wording_in_both_languages():
    ru, en = _dicts()
    codes = _backend_codes()
    assert len(codes) > 150, "the scan found the codes"
    missing = sorted(c for c in codes if c not in ru or c not in en)
    assert missing == []


def test_the_russian_dictionary_is_russian():
    """A Russian value with no Russian word in it is a leftover in English."""
    ru, _en = _dicts()
    allowed = {"app.name", "settings.api_id", "settings.api_hash", "media.gif",
               "raw", "issue.message", "issue.account.stopped",
               "issue.account.error", "login.via_proxy"}
    english = sorted(k for k, v in ru.items()
                     if k not in allowed and not re.search("[А-Яа-яЁё]", v))
    assert english == []
