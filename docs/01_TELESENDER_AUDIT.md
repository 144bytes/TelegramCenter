# Telegram Center — Phase 1: Telesender Audit & Evolution Map

> Source analysed: `C:\Users\144by\Desktop\Telesender\main.py` — **Telesender v1.5.2**, single file, **5729 lines**.
> Target: `C:\Users\144by\Desktop\TelegramCenter\` — same stack (Python + Tkinter/ttk + Telethon + JSON + asyncio + PyInstaller), modular, one `TelegramCenter.exe`.
> This document is the deliverable required before any new code (spec §63). Nothing is rewritten blindly — the working parts of Telesender are the foundation (spec §67).

---

## 1. Full map of current Telesender

### 1.1 File layout of the source project

```
Telesender/
├── main.py            5729 lines — everything
├── requirements.txt   telethon>=1.40,<2 · imageio-ffmpeg>=0.6 · keyboard>=0.13.5 · pyinstaller>=6
├── build.bat          PyInstaller --onefile --windowed --collect-all imageio_ffmpeg
├── install.bat        creates .venv, pip install -r requirements.txt
├── icon.ico
└── README.md          v1.5.2 — "send video notes / live locations / geo from a hotkey"
```
> Note: `requirements.txt` lists `keyboard` and README mentions global hotkeys, but the current `main.py` has **no `import keyboard`** and no hotkey code — that feature was removed. `qrcode` is imported but **missing from requirements.txt** (latent bug).

### 1.2 `main.py` internal sections (by comment banner)

| Lines (approx) | Banner | Contents |
|---|---|---|
| 1–25 | imports | stdlib + `imageio_ffmpeg`, `qrcode`, `telethon` |
| 27–237 | SETTINGS / PATHS | constants, `get_base_dir`, `get_resource_path`, `APP_DIR`, `AppSettings`, font/spacing/sidebar constants, `ease_*`, `animate` |
| 239–321 | AUTO-ADD: DATA STRUCTURES & PARSER | `UserTarget`, `AddResult`, `parse_targets` |
| 324–423 | (settings io) | key-groups, `_load_json`, `load_settings`, `save_settings`, `load_api_settings`, `save_api_settings`, `resolve_video_path`, `resolve_voice_path` |
| 425–513 | COLOR SYSTEM | luminance / mix / accent detection helpers |
| 515–533 | LOGGING | `LogBus`, global `LOG` |
| 536–1325 | TELEGRAM BACKGROUND SERVICE | `TelegramService` (loop thread + Telethon sessions + all Telegram ops) |
| 1328–1651 | TRANSLATIONS | `TRANSLATIONS` dict (`en`, `ru`) |
| 1654–1830 | AUTO-ADD SCHEDULER | `AutoAddScheduler`, `_save_auto_add_job`, `_load_auto_add_job` |
| 1833–1927 | (i18n + phone) | `translate`, `LANGUAGE_NATIVE_NAMES`, `COUNTRY_CALLING_CODES`, `split_international_phone` |
| 1929–2417 | TKINTER UI (widgets) | `ToggleSwitch`, `PremiumDropdown`, `SmartEntry`, `ColorPickerDialog` |
| 2419–2444 | (account helpers) | `session_key_for_id`, `load_accounts`, `save_accounts`, `open_folder` |
| 2446–5726 | `App` | window, theme, sidebar, all pages, all dialogs, all event wiring |
| 5729–5730 | `__main__` | `App().run()` |

### 1.3 On-disk storage (current)

Root: **`%APPDATA%\Telesender\`** (created at import time, line 68–69).

```
%APPDATA%\Telesender\
├── global_settings.json          GLOBAL_KEYS group of AppSettings
├── live_location_settings.json   LIVE_LOCATION_KEYS group
├── geo_settings.json             GEO_KEYS group
├── video_settings.json           VIDEO_KEYS group
├── voice_settings.json           VOICE_KEYS group
├── api_settings.json             {"api_id": int|null, "api_hash": str|null}
├── auto_add_job.json             {target_entity, interval, current_index, total, status}
├── cache/
│   ├── video_note_cache.mp4 + .json (signature)
│   └── voice_note_cache.ogg + .json (signature)
└── sessions/
    ├── accounts.json             {"accounts": [{key, id, phone, name}, ...]}
    ├── session_<telegram_id>.session       (Telethon SQLite)
    └── pending_<uuid>.session              (transient, pre-login; swept on cancel)
```

Design principle already present and **to be preserved**: JSON holds metadata; Telethon `.session` binary lives in its own file; never mix the two (spec §3).

### 1.4 Threading & async model (current — keep this)

```
┌────────────────────────┐        submit(coro)         ┌───────────────────────────┐
│   Tk main thread       │ ─────────────────────────▶  │  background thread        │
│   root.mainloop()      │   run_coroutine_threadsafe  │  asyncio loop.run_forever │
│                        │ ◀───────────────────────────│  (TelegramService.thread) │
│  _watch_future():      │  future.add_done_callback   │                           │
│    root.after(0, cb)   │       + root.after(0)       │  Telethon clients         │
└────────────────────────┘                             └───────────────────────────┘
         │  LogBus.q (queue.Queue)  ◀── LOG.log() from any thread
         └─ _poll_logs(): root.after(150ms) drains queue → Text widget
```

- `TelegramService.submit(coro)` → `concurrent.futures.Future`.
- `App._watch_future(fut, on_done, on_error)` — the single bridge back to Tk (no polling).
- Interactive login prompts: `code_provider` / `password_provider` are run with `asyncio.to_thread(...)` inside the login coroutine; they call `App._thread_prompt`, which does `root.after(0, create_dialog)` + `threading.Event().wait()` to block that worker thread until the user submits.
- Per-session `asyncio.Lock` (`_login_lock`) serialises concurrent login attempts on the same account.

### 1.5 Navigation model (current)

Single fixed window `1060x700` (hard `minsize`). Left **sidebar** (fixed `SIDEBAR_WIDTH=212`) + **content** area. Pages are `Frame`s stacked with `place(relwidth=1, relheight=1)`; `switch_page(mode)` calls `.lift()`. Pages: `logs`, `sender` (one physical frame shared by `video`/`voice`/`live_location`/`geo`), `auto_add`, `account`, `about`, `settings`. `account` and `settings` are **rebuilt on every visit**; `about` built once. Modal dialogs are `Toplevel` with `grab_set()`; only one app-owned modal at a time (`self.modal_window`).

---

## 2. Existing classes

| Class | Line | Base | Responsibility | Verdict |
|---|---|---|---|---|
| `AppSettings` | 85 | `@dataclass` | Every persisted setting (global + live-loc + geo + video + voice) | **Keep global/appearance/i18n fields only** → `storage/settings.py`, one `settings.json`. **Drop all `live_*` / `geo_*` / `video_*` / `voice_*` fields** — the media-sender feature is removed |
| `UserTarget` | 249 | `@dataclass` | One parsed auto-add target `(raw, kind, value)` | **Keep** → `models/` |
| `AddResult` | 262 | `@dataclass` | Outcome of processing one target | **Keep** → `models/` |
| `LogBus` | 519 | — | Thread-safe log fan-out (`queue.Queue` + stdout) | **Keep + extend** (level/account/module fields, persist ring buffer) → `app/logging.py` |
| `TelegramService` | 540 | — | asyncio loop thread + multi-session Telethon manager + all Telegram operations | **Keep core, split into package** `telegram/` (see §5). Auth internals verbatim (spec §64) |
| `AutoAddScheduler` | 1658 | — | Sequential target loop inside the asyncio loop; stop/FloodWait-aware; job persistence | **Keep as template** → generalise into `services/scheduler.py` + `services/campaign_manager.py` |
| `ToggleSwitch` | 1934 | `tk.Canvas` | Animated pill toggle bound to `BooleanVar` (analytic AA render, stdlib only) | **Keep verbatim** → `ui/widgets.py` |
| `PremiumDropdown` | 2065 | `tk.Frame` | Flat dropdown field bound to `StringVar`; opens `App._open_popup_menu` | **Keep verbatim** → `ui/widgets.py` |
| `SmartEntry` | 2138 | `ttk.Entry` | Layout-independent Ctrl+A/C/V/X/Z/Y (by keycode) + local undo/redo | **Keep verbatim** → `ui/widgets.py` |
| `ColorPickerDialog` | 2251 | — | Dark-themed HSV picker `Toplevel` (replaces native Win32 dialog) | **Keep verbatim** → `ui/widgets.py` |
| `App` | 2446 | — | The entire GUI | **Dissolve** into `ui/main_window.py` + one module per page + dialogs (see §5, §8) |

No inheritance hierarchies beyond Tk base classes. No metaclasses. No global mutable state except module-level `LOG`, `BASE_DIR`, `APP_DIR`, path constants, `COUNTRY_CALLING_CODES` (sorted in place at import).

---

## 3. Existing functions

### 3.1 Module-level

| Function | Line | Keep? | Destination |
|---|---|---|---|
| `get_base_dir` | 51 | keep | `app/config.py` |
| `get_resource_path` | 57 | keep | `app/config.py` |
| `ease_out_cubic` / `ease_out_quint` | 189 / 195 | keep | `ui/anim.py` |
| `animate` | 201 | keep | `ui/anim.py` |
| `parse_targets` | 273 | keep | `services/target_import.py` |
| `_load_json` | 348 | keep (upgrade) | `storage/json_repository.py` |
| `load_settings` / `_write_group` / `save_settings` | 355 / 376 / 383 | rework | `storage/settings.py` (one file + migration shim) |
| `load_api_settings` / `save_api_settings` | 392 / 406 | rework | `storage/api_profiles.py` (now a list) |
| `resolve_video_path` / `resolve_voice_path` | 415 / 420 | **drop** | media-sender feature removed |
| `_relative_luminance` `_mix_colors` `_hex_to_rgb` `_stadium_sdf` `_readable_foreground` | 429–476 | keep | `ui/theme.py` |
| `get_windows_accent_color` / `resolve_accent_color` | 479 / 499 | keep | `ui/theme.py` |
| `_save_auto_add_job` / `_load_auto_add_job` | 1809 / 1824 | generalise | `services/scheduler.py` (per-campaign state) |
| `translate` | 1833 | keep | `app/i18n.py` |
| `split_international_phone` | 1912 | keep | `app/i18n.py` (or `ui/login_dialog.py`) |
| `session_key_for_id` | 2419 | keep | `telegram/accounts.py` |
| `load_accounts` / `save_accounts` | 2425 / 2431 | replace | `storage/accounts.py` (`AccountsRepository`) |
| `open_folder` | 2436 | keep | `app/util.py` |

### 3.2 `TelegramService` methods

| Method | Line | Verdict |
|---|---|---|
| `__init__`, `configure_api`, `has_api_credentials` | 546–580 | keep; `configure_api` becomes per-profile with a default |
| `start`, `_thread_main`, `submit` | 582–598 | **keep verbatim** (the loop-thread bridge) |
| `_session_path`, `_client_for` | 600–611 | keep; `_client_for` gains `proxy=` + per-account api profile; add a **connected-pool** so clients stay alive for health/incoming |
| `_login_lock`, `_send_fresh_code_request` | 613–652 | **keep verbatim** (documented FloodWait/`SendCodeUnavailableError` fix) |
| `set_active`, `clear_active`, `_active_client` | 654–670 | keep; "active" now = sender/tools context only, not the only client |
| `login_session`, `login_qr`, `login_bot` | 672–841 | **keep verbatim** (spec §64) |
| `get_me` | 843 | keep |
| `remove_session`, `rename_session` | 852–887 | keep |
| `dialogs`, `messages`, `_entity_name` | 889–929 | **keep + extend** for Operator Inbox (spec §65) |
| `send_video`/`_prepare_video`/`send_voice`/`_prepare_voice`/`send_live_location`/`_validate_live_location`/`_live_location_update_loop`/`send_geo`/`_validate_geo` | 931–1144 | **DELETE entirely.** New app has a new sending mechanism — a fresh `send_message(entity, text, media=None)` for campaigns/inbox, not derived from these. Drops `imageio_ffmpeg`, the `cache/` dir, `_prepare_*` FFmpeg passes |
| `get_entity_by_string` | 1148 | keep → Targets check + campaign send |
| `resolve_target`, `_resolve_phone`, `_classify_resolve_error` | 1156–1229 | keep (auto-add lineage; reusable) |
| `_is_participant`, `add_to_group` | 1231–1325 | keep (campaign inviter lineage) |

### 3.3 `AutoAddScheduler` methods

`__init__` (1688), `start` (1698), `stop` (1709), `_run` (1716) — **keep as the reference implementation** for the new campaign `Scheduler`: stop-aware `asyncio.wait_for(shield(stop_event.wait()), timeout=...)`, FloodWait pause, per-item job persistence, `on_tick`/`on_finished` callbacks hopped to Tk via `root.after`.

### 3.4 `App` methods (grouped) — all move; most survive as helpers

- **lifecycle**: `__init__`, `run`, `exit_app`(→ extend, §6), `_rebuild_main_ui`
- **i18n**: `t`
- **async glue**: `_watch_future`, `_poll_logs`, `_thread_prompt` — **keep verbatim**
- **theme**: `_apply_windows_titlebar_theme`, `_font`, `_apply_theme`, `_round_rect`, `_style_toplevel` → `ui/theme.py` (keep, spec §49)
- **widget factories**: `_make_dark_listbox`, `_make_toggle`, `_make_dropdown`, `_make_action_button` → `ui/widgets.py`
- **popup menu**: `_close_popup_menu`, `_open_popup_menu`, `_maybe_close_popup_on_click` → keep (quick account switcher, spec §17)
- **misc UI**: `_bind_text_shortcuts`, `_center_window`
- **sidebar build**: `_build_sidebar`, `_sidebar_separator`, `_make_accordion_row`, `_make_nav_row`, `_draw_nav_icon` → `ui/sidebar.py` (keep look, extend nav tree + icons)
- **page build**: `_build_page_header`, `_build_sender_group_page`, `_build_auto_add_page`, `_build_account_page`, `_build_about_page`, `_build_settings_page` → one file per new page
- **account**: `_account_label`, `_account_short_label`, `_refresh_account_combo`, `_activate_account`, `_show_account_page`, `_delete_accounts`, `_show_account_switch_menu`
- **navigation**: `switch_page`(→ nav tree), `open_sender`, `_set_global_send_busy`
- **auth**: `refresh_auth_status`, `_open_modal`, `_login_dialog`(**keep verbatim**, spec §64), `_friendly_login_error`, `login`, `_api_credentials_valid`, `_ensure_api_credentials`, `open_api_settings`
- **coords**: `_quick_coords_dialog` (tools only)
- **settings**: `_settings_section_label`, `_select_settings_section`, `_show_settings_page`, `_build_settings_page`

Sender-page behaviour lives in closures inside `_build_sender_group_page`: `load_chats`, `load_messages`, `send`, `quick_action` — will be extracted into `telegram/messages.py` + `ui/tools_page.py`.

---

## 4. What can stay (moved verbatim, behaviour unchanged)

1. **The whole Telethon auth layer** — `_send_fresh_code_request`, `login_session`, `login_qr`, `login_bot`, `_login_lock`, `set_active`, `_active_client`, `rename_session`, `remove_session`. Every docstring here documents a real bug already fixed; do not touch (spec §64).
2. **The asyncio-loop-thread + `submit` + `_watch_future` + `_thread_prompt`** non-blocking bridge (spec §48).
3. **`dialogs()` / `messages()` / `_entity_name()`** — extend into the Inbox, don't replace (spec §65).
4. **Auto-add resolver/inviter** — `get_entity_by_string`, `resolve_target`, `_resolve_phone`, `_classify_resolve_error`, `_is_participant`, `add_to_group` (reference for Targets check + campaign delivery; the actual campaign send is a new `send_message()`).
5. **`AutoAddScheduler`** stop/FloodWait/persistence pattern → the new `Scheduler`.
6. **All four custom widgets** (`ToggleSwitch`, `PremiumDropdown`, `SmartEntry`, `ColorPickerDialog`) — self-contained, keep as-is.
7. **Theme system** — color tokens, `_apply_theme`, `_round_rect`, `_style_toplevel`, `_apply_windows_titlebar_theme`, `_font`, `_center_window` (spec §49).
8. **Sidebar rendering** — `_make_nav_row`, `_make_accordion_row`, `_draw_nav_icon` (canvas-drawn glyphs, rounded highlight, accent stripe).
9. **Popup flyout + quick account switcher** — `_open_popup_menu`, `_show_account_switch_menu` (spec §17).
10. **Login dialog + API-settings dialog + country codes + `split_international_phone`** (spec §18, §64).
11. **i18n** — `translate` + `TRANSLATIONS` mechanism.
12. **`LogBus`** queue+poll model (extend the payload, keep the plumbing) (spec §46).
13. **PyInstaller approach** — `build.bat`, icon embedding, `--windowed`, `.spec` file (adapt to `--onedir`, spec §52). Drop `--collect-all imageio_ffmpeg` (no FFmpeg anymore).
14. **JSON write pattern** (`Path.write_text(json.dumps(..., indent=2, ensure_ascii=False), encoding="utf-8")`) — becomes the base of `JsonRepository` (spec §19).

---

## 5. What needs to be extracted (out of `main.py` / `App`)

Target package: **`telegram_center/app/`**.

```
models/          plain @dataclass records, zero Tk / Telethon imports
  account.py         Account
  operator.py        Operator
  campaign.py        Campaign
  target.py          Target
  template.py        Template
  conversation.py    Conversation, Client
  binding.py         Binding
  profile.py         ApiProfile, NetworkProfile
  health.py          HealthRecord
  auto_add.py        UserTarget, AddResult          (from Telesender)
  settings.py-data   AppSettings                    (from Telesender)

storage/         JSON persistence, one repo per file
  json_repository.py   JsonRepository  (load / save / atomic write / backup / recover)
  accounts.py          AccountsRepository            (replaces load_accounts/save_accounts)
  operators.py targets.py templates.py campaigns.py bindings.py
  clients.py conversations.py api_profiles.py network_profiles.py
  health.py logs.py settings.py auto_reply.py

telegram/        Telethon layer (TelegramService, split)
  service.py       loop thread, client pool, api/proxy wiring, start/stop
  auth.py          _send_fresh_code_request, login_session/qr/bot, _login_lock   (VERBATIM)
  accounts.py      set_active, _active_client, get_me, remove/rename_session, session_key_for_id
  dialogs.py       dialogs(), _entity_name()
  messages.py      messages() (read history for Inbox), send_message(entity, text, media=None)  ← NEW, not from Telesender
  incoming.py      NEW — per-account NewMessage handler → router
  health.py        NEW — per-account probe coroutine
  targets.py       get_entity_by_string, resolve_target, add_to_group, _is_participant

services/        orchestration, no Tk widgets (callbacks only)
  account_manager.py     lifecycle, role changes, recheck
  warmup_manager.py      NEW/WARMING/WARM transitions, elapsed/remaining/% math, live tick
  operator_manager.py    capacity accounting, bindings CRUD, FULL state
  campaign_manager.py    campaign state machine
  scheduler.py           generalised AutoAddScheduler (start/stop/pause/resume/cancel + persist)
  incoming_router.py     update → Client/Conversation → assigned operator → UI refresh
  auto_reply.py          FIRST MESSAGE / AFTER HOURS / TRANSFER / FAQ rules engine
  health_monitor.py      periodic loop, writes health.json, triggers restriction handling
  notification_manager.py  in-app toasts / status surface
  migration.py           Telesender %APPDATA% importer

ui/
  theme.py anim.py widgets.py sidebar.py
  main_window.py          shell + nav tree + status bar + quick switcher + graceful shutdown
  dashboard_page.py
  accounts_page.py        unified table + New/Warming/Warm/Ready/Campaign/Operators filters
  warming_page.py         (live progress; may be a filter view of accounts_page)
  campaign_page.py        Targets / Templates / Campaigns / History sub-tabs
  operators_page.py       operator cards + reservation dropdowns
  inbox_page.py           two-pane conversations + composer + auto-reply toggle
  health_page.py
  system_page.py          API Profiles / Network Profiles / Logs
  settings_page.py        11 sections
  dialogs/
    login_dialog.py       (VERBATIM from _login_dialog)
    api_settings_dialog.py
    account_details_dialog.py   NEW
    first_run_dialog.py         NEW

app/
  config.py    paths (data/ next to exe), get_base_dir, get_resource_path, constants
  i18n.py      translate, TRANSLATIONS, COUNTRY_CALLING_CODES, split_international_phone
  logging.py   LogBus (structured)
  util.py      open_folder, small helpers
main.py        bootstrap: recover JSON → start service → start monitors → build window → mainloop
```

---

## 6. What needs to change

| # | Area | Current | Change | Why |
|---|---|---|---|---|
| 1 | Storage root | `%APPDATA%\Telesender\` | `<exe dir>\data\` + `sessions\` + `media\` + `backups\` (portable, spec §3, §52) | "copy the folder and run" |
| 2 | Settings files | 5 split JSON files | one `settings.json`; keep an importer for the old 5 | spec §47 |
| 3 | `accounts.json` schema | `{key, id, phone, name}` | full `Account` (see §9) | roles / warm / operational / profiles / operator link |
| 4 | api credentials | one global `{api_id, api_hash}` | `api_profiles.json` (list) + `account.api_profile_id` | spec §38; **note risk §10.5** |
| 5 | proxy | none | `network_profiles.json` + `account.network_profile_id`; `_client_for(..., proxy=...)` | spec §40; needs `python-socks` |
| 6 | client lifetime | `_client_for` connects on demand, nothing keeps clients alive | **connected pool**: health monitor + incoming listener keep 10–20 clients connected | spec §32, §42 |
| 7 | "active account" | the only usable client | just the sender/tools context; inbox/health/campaigns are per-row account-scoped | many accounts at once |
| 8 | `LogBus.log(text)` | one string, optional timestamp | `log(level, message, account=None, module=None)`; ring buffer persisted to `logs.json`; UI filters | spec §46 |
| 9 | Save model | global "Save" button per dialog | **auto-save on every mutation** (account/role/operator/target/template/campaign/network/api/settings) via repo `.save()` | spec §20 |
| 10 | Backups | none | `JsonRepository.save()` snapshots to `backups\<name>_<ts>.json` before overwrite, prunes to N (setting) | spec §21 |
| 11 | `exit_app` | stops scheduler, destroys root | also: stop health monitor + incoming listener, disconnect all clients, flush logs, close loop, join thread | spec §59 |
| 12 | Startup | load JSON, best-effort | validate every file, restore newest backup on parse failure, then start service/monitors, then build UI | spec §58 |
| 13 | Window | hard `1060x700`, non-resizable-smaller | larger min size, **resizable**, remember size | Inbox + wide tables |
| 14 | Nav | flat `switch_page(mode)` | grouped nav tree DASHBOARD / ACCOUNTS(6) / CAMPAIGNS(4) / OPERATORS(2) / SYSTEM(5) | spec §9 |
| 15 | Sender page (video/voice/geo/live) | primary feature | **DELETED — feature, page, service methods, settings, translations, `imageio_ffmpeg` dep, `cache/` dir all removed.** Campaigns/inbox get a brand-new `send_message(entity, text, media=None)` | new app = new mechanism (user, 2026-09-07) |
| 16 | Test Mode | none | `FakeTelegramService` with the same interface, selected by a Settings toggle | spec §56 |
| 17 | `qrcode` dependency | imported, not declared | add to `requirements.txt` | latent bug |
| 18 | Incoming updates | none | Telethon `add_event_handler(NewMessage)` on every campaign+operator client | spec §32 |

**Resolved (user, 2026-09-07):** the video-note / voice / live-location / geo feature is **removed completely**. Telesender is used only as a structural base to make designing and coding the new app faster. Nothing media-sender-related is carried over.

---

## 7. What will be added (net-new)

### 7.1 Storage files
`operators.json`, `targets.json`, `templates.json`, `campaigns.json`, `bindings.json`, `clients.json`, `conversations.json`, `api_profiles.json`, `network_profiles.json`, `health.json`, `logs.json`, `settings.json`, `auto_reply.json`.

### 7.2 Services
- **`WarmupManager`** — `warm_deadline = created_at + warmup_days`; computes elapsed / remaining / percent; a Tk `after(1000, …)` tick refreshes Warming cards live (spec §13); transitions `NEW → WARMING → WARM` when the deadline passes. WARM = "lay-off period reached", **not** an anti-spam guarantee (spec §6).
- **`HealthMonitor`** — loop every *N* s (setting, spec §42); per account: session file valid → authorized → Telegram reachable → `get_me` → api profile ok → proxy ok → derive `HEALTHY | WARNING | ERROR`; write `health.json`; on a critical Telethon exception set account `RESTRICTED`/`ERROR`, **pause linked campaigns, log, notify** — never auto-swap accounts / rotate proxies / fake activity (spec §45).
- **`OperatorManager`** — capacity vs. reserved accounting; `bindings.json` CRUD; `FULL` → disabled dropdown option (spec §28–29).
- **`CampaignManager`** — state machine `DRAFT → SCHEDULED → RUNNING → PAUSED → COMPLETED | FAILED | CANCELLED` (spec §25).
- **`Scheduler`** (generalised `AutoAddScheduler`) — start/stop/pause/resume/cancel, persist state, resume unfinished jobs on startup (spec §26).
- **`IncomingRouter`** — update → find account → find/create `Client` + `Conversation` → mark unread → route to assigned operator → push UI refresh (spec §32).
- **`AutoReplyEngine`** — rule types FIRST MESSAGE / AFTER HOURS / TRANSFER / FAQ; per-conversation manual ON/OFF override; default: hand complex dialog to a human (spec §36–37).
- **`NotificationManager`** — surfaces restrictions/errors (spec §45).
- **`migration.py`** — Telesender importer (spec §54–55, see §10).

### 7.3 UI (spec §9–37, §47)
Dashboard (live counts) · unified Accounts table · New/Warming/Warm/Ready/Campaign/Operators views · Account Details modal (`Recheck / Edit / Change Role / Assign Operator / Disable / Delete`) · Targets · Templates · Campaigns · History · Operator Inbox · Auto Reply · API Profiles (+Check) · Network Profiles (+Test) · Health · Logs · Settings (11 sections) · First-Run wizard.

### 7.4 Infra
`TelegramCenter.spec` + `build.bat` (`--onedir`, spec §52) · `requirements.txt` (`telethon>=1.40,<2`, `qrcode`, `python-socks[asyncio]`, `pyinstaller`, `pytest`) — **no `imageio-ffmpeg`, no `keyboard`** · `tests/` (spec §57) · `FakeTelegramService`.

---

## 8. New file structure

```
TelegramCenter/                         ← project root (Desktop\TelegramCenter)
│
├── main.py                             bootstrap
├── requirements.txt
├── build.bat                           PyInstaller --onedir
├── TelegramCenter.spec
├── icon.ico
├── README.md
├── docs/
│   ├── 01_TELESENDER_AUDIT.md          ← this file
│   ├── 02_JSON_SCHEMA.md
│   ├── 03_MIGRATION.md
│   └── 04_PHASE_PLAN.md
│
├── app/
│   ├── config.py  i18n.py  logging.py  util.py
│   ├── models/     account.py operator.py campaign.py target.py template.py
│   │               conversation.py binding.py profile.py health.py auto_add.py
│   ├── storage/    json_repository.py accounts.py operators.py targets.py
│   │               templates.py campaigns.py bindings.py clients.py
│   │               conversations.py api_profiles.py network_profiles.py
│   │               health.py logs.py settings.py auto_reply.py
│   ├── telegram/   service.py auth.py accounts.py dialogs.py messages.py
│   │               incoming.py health.py targets.py
│   ├── services/   account_manager.py warmup_manager.py operator_manager.py
│   │               campaign_manager.py scheduler.py incoming_router.py
│   │               auto_reply.py health_monitor.py notification_manager.py
│   │               migration.py target_import.py
│   └── ui/         theme.py anim.py widgets.py sidebar.py main_window.py
│                   dashboard_page.py accounts_page.py warming_page.py
│                   campaign_page.py operators_page.py inbox_page.py
│                   health_page.py system_page.py settings_page.py
│                   dialogs/  login_dialog.py api_settings_dialog.py
│                             account_details_dialog.py first_run_dialog.py
│
├── tests/         test_json_repository.py test_account_lifecycle.py
│                  test_warmup.py test_operator_capacity.py test_bindings.py
│                  test_target_import.py test_templates.py test_campaign_fsm.py
│                  test_backup.py test_migration.py  conftest.py (FakeTelegramService)
│
├── data/          (runtime — gitignored)   *.json
├── sessions/      (runtime — gitignored)   session_<id>.session
├── media/         (runtime — gitignored)
└── backups/       (runtime — gitignored)
```

Mapping old → new is given per-item in §2, §3, §5.

---

## 9. JSON structure

All files: UTF-8, `indent=2`, `ensure_ascii=false`, atomic write (`*.tmp` → `os.replace`), backup-before-overwrite, recover-from-newest-backup on parse failure. Top-level shape is always `{"<plural>": [ ... ]}` (or a flat object for `settings.json`).

### `settings.json`
```json
{
  "appearance": {
    "text_color": "#f2f2f2", "secondary_text_color": "#9a9a9a",
    "accent_mode": "custom", "custom_accent_color": "#ff1f79",
    "user_color": "#ff00ff", "receiver_color": "#a3e42e",
    "ui_font_scale": "Medium"
  },
  "general": { "language": "en", "show_log_time": true },
  "accounts": { "dialog_limit": 30, "message_limit": 30 },
  "warmup": { "default_days": 14 },
  "scheduler": { "default_interval_sec": 60 },
  "health": { "check_interval_sec": 60 },
  "operators": { "default_capacity": 3, "after_hours": "20:00-09:00" },
  "storage": { "keep_backups": 20, "max_messages_per_conversation": 500 },
  "notifications": { "toast": true },
  "dev": { "test_mode": false }
}
```

### `accounts.json`
```json
{"accounts": [{
  "id": "a1b2c3d4",                    // internal uuid (stable across relogin)
  "key": "session_123456789",          // session file stem (Telesender-compatible)
  "session_file": "session_123456789.session",
  "telegram_id": 123456789,
  "username": "account01",
  "phone": "+7...",
  "first_name": "Name", "last_name": "",
  "api_profile_id": "api_default",
  "network_profile_id": null,
  "role": "UNASSIGNED",                // UNASSIGNED | CAMPAIGN | OPERATOR
  "warm_state": "NEW",                 // NEW | WARMING | WARM
  "operational_state": "OFFLINE",      // OFFLINE|CHECKING|READY|WARNING|RESTRICTED|DISABLED|ERROR
  "created_at": "2026-09-07T15:14:30",
  "warm_started_at": null,
  "warm_deadline": null,
  "operator_id": null,                 // if role==CAMPAIGN, which operator account it reports to
  "last_check_at": null,
  "last_error": null
}]}
```
> `warm_state` and `operational_state` are **separate enums**, never merged (spec §6–7).
> Lifecycle: `NEW → CHECKING → WARMING → WARM → READY → CAMPAIGN|OPERATOR`; on critical error `READY → WARNING|RESTRICTED|ERROR` (spec §8).

### `operators.json`
```json
{"operators": [{
  "id": "op1", "account_id": "a1b2c3d4",
  "capacity": 3, "notes": ""
}]}
```

### `bindings.json`  (spec §29)
```json
{"bindings": [{
  "campaign_account_id": "a1b2c3d4",
  "operator_account_id": "a9x8y7z6",
  "active": true,
  "created_at": "2026-09-07T15:20:00"
}]}
```

### `targets.json`  (spec §22)
```json
{"targets": [{
  "id": "target_1", "title": "Our channel", "username": "@our_channel",
  "telegram_id": -100123, "type": "CHANNEL",           // CHANNEL | GROUP
  "active": true, "advertising_allowed": true, "notes": "",
  "last_check": null, "last_check_status": null          // "AVAILABLE" | "ERROR: ..."
}]}
```
> Campaigns may only run against targets with `advertising_allowed == true`.

### `templates.json`  (spec §24)
```json
{"templates": [{
  "id": "template_1", "name": "Standard", "text": "...",
  "media": null, "enabled": true, "created_at": "..."
}]}
```

### `campaigns.json`  (spec §25)
```json
{"campaigns": [{
  "id": "campaign_1", "name": "Campaign A",
  "account_id": "a1b2c3d4", "target_id": "target_1", "template_id": "template_1",
  "status": "DRAFT",     // DRAFT|SCHEDULED|RUNNING|PAUSED|COMPLETED|FAILED|CANCELLED
  "created_at": "...", "scheduled_at": null,
  "progress": {"sent": 0, "total": 0, "current_index": 0},
  "last_error": null
}]}
```

### `clients.json`  (spec §34)
```json
{"clients": [{
  "id": "client_1", "telegram_id": 123, "username": "username",
  "display_name": "Name", "source": "target_1",
  "status": "NEW", "notes": ""
}]}
```

### `conversations.json`  (spec §35)
```json
{"conversations": [{
  "id": "conversation_1", "client_id": "client_1",
  "campaign_account_id": "a1b2c3d4", "operator_account_id": "a9x8y7z6",
  "status": "OPEN", "unread": true, "auto_reply": true,
  "created_at": "...", "updated_at": "...",
  "messages": [ {"id": 55, "out": false, "date": "...", "text": "...", "sender": "client"} ]
}]}
```
> `messages` is a **local cache**, capped at `settings.storage.max_messages_per_conversation`; full history stays available via Telethon (spec §33).

### `api_profiles.json`  (spec §38)
```json
{"api_profiles": [{
  "id": "api_default", "name": "Main API",
  "api_id": 123, "api_hash": "...", "enabled": true,
  "last_check": null, "last_check_status": null
}]}
```

### `network_profiles.json`  (spec §40)
```json
{"network_profiles": [{
  "id": "network_1", "name": "Moscow Proxy 01",
  "protocol": "SOCKS5",                 // SOCKS5 | SOCKS4 | HTTP | MTPROTO
  "host": "...", "port": 1234, "username": "...", "password": "...",
  "enabled": true, "last_test": null, "last_latency_ms": null, "last_test_status": null
}]}
```

### `health.json`  (spec §43–44)
```json
{"health": [{
  "account_id": "a1b2c3d4", "checked_at": "...",
  "result": "HEALTHY",                  // HEALTHY | WARNING | ERROR
  "checks": {"session_valid": true, "authorized": true, "reachable": true,
            "get_me": true, "api_profile": true, "network": true},
  "detail": ""
}]}
```

### `auto_reply.json`  (spec §36)
```json
{"rules": [{
  "id": "rule_1", "name": "First message", "enabled": true,
  "trigger": "FIRST_MESSAGE",           // FIRST_MESSAGE | AFTER_HOURS | TRANSFER | FAQ
  "match": null, "response": "Здравствуйте! ...", "then_transfer_to_operator": true
}]}
```

### `logs.json`
```json
{"logs": [{"ts": "...", "level": "INFO", "account": "a1b2c3d4",
          "module": "health", "message": "..."}]}   // ring buffer, capped
```

### transient
`campaigns/<id>.job.json` — per-campaign resumable scheduler state (successor to `auto_add_job.json`).

---

## 10. Migration plan (Telesender → Telegram Center)

Source: `%APPDATA%\Telesender\`  →  Target: `<TelegramCenter.exe dir>\`
**The source is never modified or deleted (spec §54).**

### 10.1 Detection (First Run)
On startup, if `data/` has no `accounts.json`, check for any of:
`%APPDATA%\Telesender\sessions\accounts.json`, `%APPDATA%\Telesender\api_settings.json`, `%APPDATA%\Telesender\sessions\*.session`.
If found → First-Run wizard: `[Configure API] [Import existing Telesender data] [Start]`, and the prompt *"Existing Telesender data detected. Import? [Yes] [No]"* (spec §53).

### 10.2 Backup first (spec §55)
Copy the **entire** `%APPDATA%\Telesender\` tree → `backups\Telesender_backup_<YYYYMMDD_HHMMSS>\` (shutil.copytree, copy — never move).

### 10.3 Import steps
| From | To | Rule |
|---|---|---|
| `sessions\*.session` (all variants incl. `-journal`, `-wal`) | `sessions\` | `shutil.copy2`; keep `session_<id>` names; skip `pending_*` |
| `sessions\accounts.json` `{key,id,phone,name}` | `data\accounts.json` | new `Account` per entry: `key`→`key`+`session_file`, `id`→`telegram_id`, `phone`→`phone`, `name`→`first_name`; `id`(uuid)=new; `role=UNASSIGNED`, `warm_state=NEW`, `operational_state=OFFLINE`, `created_at=now`, `warm_deadline=null`, `api_profile_id="api_default"`, `network_profile_id=null`, `operator_id=null` |
| `api_settings.json` `{api_id,api_hash}` | `data\api_profiles.json` | one profile `{"id":"api_default","name":"Main API",api_id,api_hash,"enabled":true}`; every imported account points at it |
| `global_settings.json` | `data\settings.json` | carry only `language`, `ui_font_scale`, colors, `show_log_time`, `dialog_limit`, `message_limit` |
| `live_location_settings.json` / `geo_settings.json` / `video_settings.json` / `voice_settings.json` | — | **not imported** — media-sender feature removed |
| `auto_add_job.json` | — | not imported (obsolete); logged as skipped |
| `cache\` | — | not imported (no FFmpeg) |

### 10.4 Post-import enrichment
On the first `HealthMonitor` pass each imported account is connected once and `get_me()` fills the authoritative `telegram_id`, `username`, `first_name`, `last_name`. Accounts that fail to authorize → `operational_state = ERROR`, `last_error` set, surfaced in the report.

### 10.5 Report (spec §55)
```
Imported:
  Accounts:     14
  Sessions:     14
  API Profiles:  1

Failed:
  account_07   reason: session not authorized
```

### 10.6 Risks / open questions
1. **Per-account API profile vs. existing sessions** — a `.session` authorized under one `api_id` can misbehave if later bound to a *different* `api_id`. Mitigation: all imported accounts keep `api_default`; changing an account's API profile shows a warning.
2. **Connected pool of 10–20 clients** — Telesender only ever keeps one connected. Health + incoming require a persistent pool; must handle reconnect/backoff and clean shutdown (spec §59).
3. **Proxy support** needs `python-socks[asyncio]`; PyInstaller `--onedir` must bundle it; `--onefile` + SQLite `.session` file locking is why spec §52 prefers `--onedir`.
4. **`_thread_prompt` blocks an asyncio worker thread** by design (interactive login). It must never be reachable from health/incoming/campaign code paths.
5. **Live UI refresh** from `HealthMonitor` / `IncomingRouter` / `WarmupManager` must always marshal through `root.after(0, …)`.
6. **`qrcode` missing from `requirements.txt`** in Telesender — fix in the new project.
7. **Window is currently a hard 1060×700** — Inbox and the accounts table need a larger, resizable window.
8. **Telethon 1.x pin** — keep `telethon>=1.40,<2` (v2 has breaking API changes); do not upgrade during the port.

---

## 11. Suggested phase order (maps spec §61 onto this codebase)

| Phase | Deliverable | Depends on |
|---|---|---|
| **1** | *this document* | — |
| 2 | Scaffold package; move `theme` / `widgets` / `anim` / `i18n` / `LogBus` / `config` verbatim; `JsonRepository` + `SettingsRepository` + `AccountsRepository`; app boots to an empty shell with the new nav tree; **no behaviour change** | 1 |
| 3 | Move `TelegramService` into `telegram/` package verbatim; login dialog + API settings dialog working; add/switch/delete account working exactly as today | 2 |
| 4 | `Account` model + lifecycle (`NEW/WARMING/WARM/READY`) + `WarmupManager` + Accounts table + New/Warming/Warm/Ready views + Account Details modal | 3 |
| 5 | Roles (`UNASSIGNED/CAMPAIGN/OPERATOR`) + role change UI | 4 |
| 6 | `OperatorManager` + `bindings.json` + operator cards + reservation dropdowns | 5 |
| 7 | Targets (manual + TXT/JSON import/export + Check) — reuses `get_entity_by_string` | 3 |
| 8 | Templates | 2 |
| 9 | Campaigns + generalised `Scheduler` (start/stop/pause/resume/cancel, resume on startup) | 7, 8 |
| 10 | Incoming listener + `IncomingRouter` + `clients.json` / `conversations.json` | 3 |
| 11 | Operator Inbox (two-pane + composer) + Auto Reply + manual override | 10, 6 |
| 12 | `HealthMonitor` + Health tab + restriction handling (pause campaign + notify) | 3, 9 |
| 13 | API Profiles (+Check) + Network Profiles (+Test, proxy wiring in `_client_for`) | 3 |
| 14 | Dashboard (live counts) + Notifications + Logs tab | 4–13 |
| 15 | First-Run wizard + Telesender migration importer | 2, 3 |
| 16 | `TelegramCenter.spec` + `build.bat` (`--onedir`) → `dist\TelegramCenter\TelegramCenter.exe` | all |

After every phase (spec §62): run app, run `pytest`, check imports, check JSON round-trips, check UI, check Telegram mocks — only then move on.

---

*End of Phase 1 deliverable. Awaiting go-ahead for Phase 2 (scaffold + verbatim moves, no behaviour change).*
