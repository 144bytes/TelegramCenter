"""Paths and app-wide constants.

All persistent user data lives in a single folder in %APPDATA%. Never next to
the EXE, never in the source tree, never in the CWD, never in sys._MEIPASS.
No backups, no media folder; a build never deletes anything here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "TelegramCenter"
APP_TITLE = "Telegram Center"
APP_VERSION = "2.0.0"
SCHEMA_VERSION = 2

HOST = "127.0.0.1"          # never 0.0.0.0


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_resource_path(*parts: str) -> Path:
    """Read-only bundled resource (the built React app, icons, ...)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")).joinpath(*parts)
    return get_base_dir().joinpath(*parts)


BASE_DIR = get_base_dir()

# Built React bundle. In development it is web/dist next to the sources.
WEB_DIR = get_resource_path("web", "dist")

# User data root. TC_APP_DIR overrides it — used by the test suite so that
# tests can never touch the real %APPDATA% folder.
APP_DIR = Path(os.environ["TC_APP_DIR"]) if os.environ.get("TC_APP_DIR") else \
    Path(os.environ.get("APPDATA", str(Path.home()))) / "TelegramCenter"

DATA_DIR = APP_DIR / "data"
SESSIONS_DIR = APP_DIR / "sessions"
CAMPAIGN_SESSIONS_DIR = SESSIONS_DIR / "campaign"
OPERATOR_SESSIONS_DIR = SESSIONS_DIR / "operators"

for _d in (APP_DIR, DATA_DIR, SESSIONS_DIR, CAMPAIGN_SESSIONS_DIR, OPERATOR_SESSIONS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
OPERATORS_FILE = DATA_DIR / "operators.json"
TARGETS_FILE = DATA_DIR / "targets.json"
TEMPLATES_FILE = DATA_DIR / "templates.json"
CAMPAIGNS_FILE = DATA_DIR / "campaigns.json"
API_PROFILES_FILE = DATA_DIR / "api_profiles.json"
NETWORK_PROFILES_FILE = DATA_DIR / "network_profiles.json"
AUTO_REPLY_FILE = DATA_DIR / "auto_reply.json"
CONVERSATIONS_FILE = DATA_DIR / "conversations.json"

# Written on startup so a second launch can find the running instance.
LOCK_FILE = APP_DIR / "instance.json"
LAST_ERROR_FILE = APP_DIR / "last_error.txt"

# One-time snapshot taken before the v1 -> v2 migration. Safe to delete by hand.
V1_BACKUP_DIR = APP_DIR / "data.v1.bak"
