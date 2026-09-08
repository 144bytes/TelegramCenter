# Build status — 2026-09-07

The app is runnable end to end. `python -m pytest` → **11 passed**; headless
navigation smoke over all 20 pages → OK; real + Test Mode launch → OK;
**PyInstaller onefile build → `dist\TelegramCenter.exe` (17 MB), launches OK**.

## Round 2 changes (user feedback)

1. **Full Russian localization** — every page title, column, button, menu,
   dialog and the status bar now go through `app/i18n.py` (ru default, en table).
2. **Accounts toolbar** — `Recheck` button removed. `Обновить и проверить`
   reloads the page *and* runs a health recheck on every account in the current
   filtered view.
3. **Settings** rebuilt Telesender-style: a left rounded section list (accent
   stripe on the active one) + swappable right panels; choices use the custom
   `PremiumDropdown` flyout and `ToggleSwitch`, not ttk combobox/checkbox.
4. **Sidebar account selector removed.** "Active" account is now implicit
   (first READY, else first) and only used for target/API checks.
5. **Cyclic campaigns** — per campaign: `cyclic` + `interval min/max` seconds.
   A cyclic campaign re-sends after a random delay in that range and stays
   RUNNING (shown as `sent` count) until paused/cancelled.
6. **Right-click menu on account rows** (any account list): account settings,
   start/stop/edit warming (NEW → params dialog in **hours**; WARMING → edit or
   stop), change role, assign operator, assign proxy, assign API profile,
   enable/disable, delete. `Open` button dropped — double-click opens details.
7. **`@operator` substitution** — in a template / operator reply / auto-reply,
   the token `@operator` is replaced on send with the `@username` of the
   operator bound to that campaign account (`app/services/mentions.py`).
8. **UI fixes** — sidebar scrolls only when it doesn't fit and is clamped (no
   over-scroll / no top gap); every table now uses `build_tree` with fixed
   column widths, one stretch column and consistent left anchor.
9. **Excel-like column sorting** — click any header to sort asc/desc (▲/▼).
10. **Storage moved to `%APPDATA%\TelegramCenter\`** (like Telesender). Nothing
    is written next to the exe. `TC_APP_DIR` env var overrides it (tests).
11. **Build** → `--onefile`; `build.bat` wipes `build\` and `dist\` first and
    deletes the `build\` work dir afterwards, leaving only `dist\TelegramCenter.exe`.
    `console=False`; logging guards a missing/again-non-UTF8 stdout.

## Round 7 changes (user feedback + bug fixes)

1. **"Сделать оператором" now duplicates**, it doesn't move: the session file is
   *copied* into `sessions\operators`, an Operator record is created, and the
   broadcast account stays exactly where it was (idempotent — a second call
   returns the existing operator).
2. **Автоответы are a separate tab under Кампании again** (Шаблоны and
   Автоответы are two pages sharing one `ListEditor` base).
3. **Операторы / Диалоги keeps its state** — switching tabs no longer reloads
   or clears the open chat; the selected conversation is restored by chat id.
4. **Аккаунты list has an "Ошибка" column**, same as Кампании.
5. **Fixed:** deleting an API profile left the account looking green. The row
   was tinted by the *warm-state* tag, which outranked the operational tag in
   ttk. Row tinting is gone entirely (see 6), and the account now shows ERROR
   plus the reason in the error column.
6. **Only the status is coloured, never the whole row.** Every table
   (accounts, campaigns, targets, health, API and network profiles) now shows a
   small anti-aliased colour dot in a narrow leading column; all row text keeps
   the normal colour.
7. **Fixed:** several auto-replies with the same trigger. Matching rules are now
   collected and **one is chosen at random**, so rules with the same FAQ keyword
   act as variants instead of only the first one ever firing. Rules with an
   empty response are ignored.

Also in this pass: error/info pop-ups replaced by in-app status-bar messages
(only destructive actions still confirm); status messages no longer clear each
other early; `PhotoImage`s are created against their widget; `PopupMenu` guards
against a dead interpreter; path constants resolve at call time instead of
import time (this was silently pinning session folders to the first-imported
path); dependency-forced errors are tracked in `Account.dep_error` instead of
matching on message text, so a real Telegram error is never cleared by mistake.

## Round 6 changes (user feedback)

1. **"Прогрев" column** shows only on the *Прогрев* tab.
2. Warming on/off/settings live only in the account right-click menu — removed
   from the account-settings dialog.
3. **Empty flyout lists** never pop a Windows dialog — the list just shows a
   single `None` row.
4. **Settings is an in-window page again** with a left section list (same look
   as the rest). Modals are only for *Add account* now.
5. `Операторы / Диалоги` — panes equal height, no redundant "Диалоги" label.
6. **Make operator from a broadcast account** — right-click → *Сделать
   оператором* (moves the session to `sessions\operators`).
7. **Fractional warm-up** — "0.1" or "0,1" hours (≈6 min); shown as "15 мин" /
   "3 ч".
8. **Templates + Auto-replies = one page** (`Шаблоны`, two tabs). Template =
   message to a target; auto-reply = answer to a client; campaign = template +
   target (+ account).
9. **Targets table** = Название · Ссылка · Тип · Доступность, plus a
   *Проверить все* button.
10. Campaigns nav group: **Кампании · Цели · История · Шаблоны**.
11-12. Empty pick lists → `None` only (see 3).
13. Accounts tabs = **Новые · Прогрев · Готовы** only.
14. **Account ⇄ campaign are bound like a DB** (`services/reconcile.py`):
    - account API profile / proxy unavailable, disabled or failing → account
      **ERROR** immediately, and every campaign on it → **ERROR** (red).
    - account stopped (disabled) → its campaigns → **STOPPED** (yellow).
    - account healthy again → its campaigns → **ACTIVE** (green).
    Re-evaluated on every account change, health sweep, and API-check /
    proxy-test. The scheduler skips held campaigns.

## Round 5 changes (user feedback)

1. **Nothing in `%APPDATA%\TelegramCenter` is ever deleted** — Telesender-style.
   The folder holds exactly `data\` (JSON) + `sessions\`. No backups, no `.bak`
   files, no `media\`. Missing folder → defaults; existing folder → used as-is.
   A build only touches `build\` / `dist\`.
2. **`sessions\` split in two:** `sessions\campaign\` (broadcast accounts) and
   `sessions\operators\` (operator sessions). Keys route by prefix
   (`session_*` / `pending_*` → campaign, `op_*` / `op_pending_*` → operators).
3. **No "Import sessions" button.** Drop `*.session` files into the right
   sub-folder and they appear on next launch (or on *Обновить и проверить*) —
   `services/discovery.py` scans both folders and adds unknown sessions as
   *New* accounts / operators.
4. **Settings is a modal window** (`SettingsDialog`), opened from the sidebar
   "Настройки" like Telesender's API-settings dialog — no left section list; a
   **section dropdown at the top**, fields below, one *Применить* button.
   `MainWindow.open_modal()` mirrors Telesender's single-app-modal +
   `navigate()` blocks while a modal is open.
5. **Operators: "Входящие" removed.** Operators do nothing automatic. A
   logged-in operator is only a history viewer. OPERATORS group is now
   **Операторы** (manage) + **Диалоги** (`operators.chats` — Telesender-style
   pane: operator picker → left chat list → right message history). The
   campaign-account auto-reply is a background `AutoResponder` (no inbox UI);
   `conversations.json` / `clients.json` removed.

## Round 4 changes (user feedback)

1. **Flyout popups toggle.** Clicking the same field again closes the popup.
   Picking a value no longer rebuilds the whole account-settings window — only
   that one field's label updates.
2. **Role removed.** There are two account kinds only: **broadcast accounts**
   (everything under ACCOUNTS + all campaign features) and **operators**
   (a separate list). No role selector anywhere.
3. **Operators are their own entity.** New **`Операторы`** page (first item of
   the OPERATORS group): add an operator by `@username` (for redirect /
   `@operator` substitution) **or** log in as that account. A logged-in operator
   gets an **`Открыть чаты`** viewer — its dialogs + history, like Telesender.
   Capacity limits how many broadcast accounts point at one operator.
4. **Warm / Ready merged.** The confusing split is gone — the WARM state is
   labelled **`Готовы`**. Lifecycle is now `NEW → Прогрев → Готовы`. The
   `Кампания` view = broadcast accounts used by at least one campaign.
   Health/operational state stays as a column + the Health page.
5. **`%APPDATA%` sessions folder is portable** — `sessions\` copied between
   machines shows the accounts as *New*; the manual `Импорт сессий` button does
   the same from any folder.

## Round 3 changes (user feedback)

1. **Account right-click** trimmed → `Настройки аккаунта`, start/edit warming,
   enable/disable, delete. Role / operator / network / API are now **flyout
   pickers inside the account-settings dialog** (Telesender-style), each listing
   the available operators / networks / API profiles.
2. **Stop warming** moved into the warming-settings dialog (Danger button); the
   standalone menu item is gone.
3. **Sidebar Campaigns group reordered** → Цели · Кампании · История · Шаблоны ·
   Автоответы.
4. **Auto Reply moved from Operators to Campaigns** — it configures how a
   *campaign* account answers a client and redirects to `@operator`. Operator
   accounts are the manual second line; `IncomingRouter` now sets
   `conversation.auto_reply = (account.role == CAMPAIGN)`.
5. **No Telesender auto-detection.** `migration.py` → `import_sessions_folder()`;
   a manual **`Импорт сессий`** button on the Accounts page copies `*.session`
   files from any folder and adds them as **New / Unassigned**. Sessions are
   portable — copying the `sessions\` folder between machines is the whole sync.
6. **`%APPDATA%\TelegramCenter\` cleaned up** — only `data\` + `sessions\` now
   (no `media\`, no `backups\`). `JsonRepository` keeps one rolling
   `<name>.json.bak` next to each file instead of a backups directory. A rebuild
   never touches this folder.

## Original 16-phase status

## Done (all 16 phases, first pass)

| Phase | Status | Notes |
|---|---|---|
| 1 Audit | ✅ | `docs/01_TELESENDER_AUDIT.md` |
| 2 Scaffold + verbatim moves | ✅ | `app/` package; theme, widgets (ToggleSwitch / SmartEntry / ColorPicker), anim, i18n, LogBus, JsonRepository (+ backups + recovery + lock) |
| 3 Telegram layer | ✅ | `TelegramService` ported (auth verbatim), per-account API/proxy resolver, `LoginDialog` (phone / QR / bot), API-profile dialog |
| 4 Account lifecycle | ✅ | `Account` model, `WarmupManager` (live 15 s tick), Accounts table + New/Warming/Warm/Ready/Campaign filters, `AccountDetailsDialog` |
| 5 Roles | ✅ | UNASSIGNED / CAMPAIGN / OPERATOR + change-role dialog |
| 6 Operator bindings | ✅ | `OperatorManager`, `bindings.json`, capacity/FULL, reservation dialog, Operators cards page |
| 7 Targets | ✅ | manual + TXT/JSON import + JSON export + Check (resolve entity) |
| 8 Templates | ✅ | list + editor + duplicate |
| 9 Campaigns + Scheduler | ✅ | FSM DRAFT→…→COMPLETED/FAILED/CANCELLED, `Scheduler` (queue, FloodWait wait, resume-on-start), New-campaign dialog |
| 10 Incoming | ✅ | `IncomingRouter` + `AutoReplyEngine`, `clients.json` / `conversations.json` |
| 11 Operator Inbox | ✅ | two-pane + composer + per-conversation auto-reply toggle; Conversations list; Auto Reply rules editor |
| 12 Health Monitor | ✅ | periodic sweep, `health.json`, Health page, restriction → pause campaigns + notify |
| 13 API / Network profiles | ✅ | API Profiles (+Check), Network Profiles (+Test via socket connect), proxy wired into `_client_for` |
| 14 Dashboard / Notifications / Logs | ✅ | live stat cards, notification hub + status-bar toast, Logs page with level filter |
| 15 First run + migration | ✅ | `FirstRunDialog`, `migration.py` — non-destructive copy of `%APPDATA%\Telesender` sessions + accounts + api + a few settings |
| 16 PyInstaller | ✅ | `TelegramCenter.spec` (`--onedir`), `build.bat`, `install.bat`, `run.bat` |

## Known limitations / next passes

- **Campaign semantics** are minimal: one post of the template text to the
  target, then COMPLETED. Recurring / multi-recipient campaigns are not modelled
  yet (`total` is always 1).
- **Incoming router / auto-reply** are wired and unit-safe but only lightly
  exercised — needs a live account pair to validate end to end.
- **API "Check"** piggybacks on the active account resolving `telegram`; it does
  not independently validate an arbitrary profile's credentials.
- **Appearance settings** re-apply ttk styles live but existing widgets keep old
  colours until restart.
- `python-socks[asyncio]` + `pysocks` must be installed for proxy support
  (already in `requirements.txt`; bundled by the spec).
- No `numpy`/`PIL` — excluded in the spec to keep the exe small.
- Tk is required at runtime (bundled with standard CPython on Windows).

## Run

```
install.bat      # once
run.bat          # launch
.venv\Scripts\pytest -q
build.bat        # -> dist\TelegramCenter\TelegramCenter.exe
```

Toggle **Settings → Development → Test Mode** (restart) to click through
everything with a mock Telegram layer.
