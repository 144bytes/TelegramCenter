# TelegramCenter v2 — Архитектура и план миграции

Статус: **утверждено** (Фаза 3). Все открытые вопросы закрыты ответами пользователя.

---

## 1. Форма процесса

Один `TelegramCenter.exe`. Внутри — три потока:

```
main thread          ── значок в трее (меню: «Открыть» / «Завершить»)
asyncio thread       ── event loop Telethon + планировщик кампаний + автоответы
http thread pool     ── ThreadingHTTPServer на 127.0.0.1:<случайный свободный порт>
```

HTTP-поток не выполняет Telegram-операции сам: он кладёт корутину в asyncio-loop через
`asyncio.run_coroutine_threadsafe(...)` и ждёт результат. Это тот же мост, что уже работает
в v1 (`app/telegram/service.py`), поэтому логика авторизации переносится без изменений.

**Веб-фреймворк не используется.** Только `http.server.ThreadingHTTPServer` из стандартной
библиотеки. Причины: ноль новых зависимостей, размер EXE остаётся ~17 МБ, пиннинг
зависимостей тривиален, SSE на голом HTTP пишется в 30 строк.

### Жизненный цикл

| Событие | Поведение |
|---|---|
| Запуск EXE | поднимается backend, появляется значок в трее, браузер **не** открывается автоматически |
| Кнопка «Открыть» | `webbrowser.open(url_с_токеном)` |
| Закрытие вкладки браузера | backend продолжает работать, кампании идут |
| «Завершить» в трее | graceful shutdown: стоп HTTP → отмена задач → `disconnect()` клиентов → стоп loop |
| Повторный запуск EXE | обнаруживает живой инстанс по lock-файлу в `%APPDATA%` и просто открывает вкладку |

---

## 2. Безопасность локального API (§18)

Четыре независимых слоя, не только CORS:

1. **Bind только `127.0.0.1`.** Никогда `0.0.0.0`. Порт выбирается через `bind(('127.0.0.1', 0))`.
2. **Runtime-токен.** 32 случайных байта из `secrets.token_urlsafe(32)`, генерируется при
   каждом старте, живёт только в памяти процесса. Все `/api/*` требуют заголовок
   `X-TC-Token`, сравнение через `secrets.compare_digest`.
3. **Проверка `Origin`.** Если заголовок `Origin` присутствует и не равен нашему
   `http://127.0.0.1:<port>` — `403`. Требование кастомного заголовка `X-TC-Token`
   заставляет браузер делать preflight для любого кросс-origin запроса, а preflight мы
   отклоняем. Итог: чужая открытая страница не сможет дёрнуть наш API даже вслепую.
4. **Мутации только POST.** Ни один destructive-эндпоинт не доступен через `GET`,
   поэтому `<img src>` / `<form>`-атаки не работают.

Токен передаётся ровно один раз — в URL, который открывает пункт «Открыть»
(`/?t=<token>`). Страница читает его, кладёт в `sessionStorage`, и немедленно
делает `history.replaceState` чтобы убрать токен из адресной строки.

---

## 3. Модель состояния: raw vs effective

Ключевое требование §4/§5: **React только отображает, никогда не вычисляет**.

Каждая сущность хранит **raw state** — то, что реально измерено:

| Сущность | raw-поле | откуда берётся |
|---|---|---|
| `ApiProfile` | `raw_state` | проба `connect()` с этим api_id/api_hash |
| `NetworkProfile` | `raw_state` | TCP-проба прокси |
| `Account` | `raw_state` | `is_user_authorized()` / ошибки Telethon |
| `Campaign` | `raw_state` | DRAFT / SCHEDULED / RUNNING / PAUSED / DONE |
| `AutoReplyConfig` | `enabled` | ручной флаг |
| `Operator` | `raw_state` | есть ли валидный session-файл |

**Effective state** вычисляется централизованно в `app/state/manager.py` и **никогда не
пишется в JSON**. Он всегда производный:

```
api/proxy.effective  = raw
account.effective    = ERROR    если любая зависимость в ERROR
                     = DISABLED если account.disabled
                     = raw      иначе
campaign.effective   = BLOCKED  если account.effective != READY
                     = raw      иначе
autoreply.effective  = BLOCKED  если account.effective != READY
                     = BLOCKED  если текст содержит @operator и оператор не привязан
                     = raw      иначе
operator.effective   = WARNING  если username есть, но сессии нет (красный кружок)
```

Из этого следует инвариант, который §5 требует сделать невозможным:
`proxy=ERROR ⇒ account=ERROR ⇒ campaign=BLOCKED`. Состояние
`Proxy=ERROR + Account=READY + Campaign=RUNNING` недостижимо по построению, потому что
`effective` вычисляется на лету при каждом чтении, а не хранится.

### Issues

Вместе с effective state менеджер возвращает список проблем:

```json
{ "level": "error|warning", "code": "api_profile_missing",
  "message": "API-профиль не найден", "source": "api_profile:api_c6e0..." }
```

Фронтенд рисует **иконку-кружок** нужного цвета, текст показывается тултипом при
наведении — как просил пользователь. Модальных окон с ошибками нет.

### Граф зависимостей

```
ApiProfile ──┐
             ├──> Account ──┬──> Campaign ──> CampaignTargetResult
NetworkProfile┘             └──> AutoReplyConfig
                                        ▲
Operator ───────────────────────────────┘  (нужен, если в тексте @operator)
Target ──────────────────────> Campaign
```

Обратные рёбра (кто на меня ссылается) строятся индексом при каждом пересчёте —
удаление API-профиля мгновенно красит все зависимые аккаунты и их кампании.

---

## 4. Event Bus + SSE

```
services ──publish(Event)──> EventBus ──fanout──> [Queue per subscriber] ──> GET /api/events
```

`Event = {type, entity, id, payload, ts}`. Типы: `entity.changed`, `campaign.progress`,
`log.line`, `login.state`, `state.recomputed`.

SSE-эндпоинт `GET /api/events` держит ответ открытым, шлёт `data: {...}\n\n`, плюс
`: ping` каждые 15 секунд чтобы прокси/браузер не рвали соединение. Опроса (polling)
нет нигде.

При любом изменении сущности менеджер состояния пересчитывает граф и публикует
`state.recomputed` со свежими effective-состояниями — фронтенд просто заменяет срез.

---

## 5. Модель данных v2

### Изменения относительно v1

| Было | Стало |
|---|---|
| `Campaign.target_id: str` | `Campaign.target_ids: list[str]` |
| `Campaign.template_id` → живая связь | `Campaign.message_text` (снимок) + `source_template_id` (только происхождение) |
| `Campaign.cyclic + interval` | `Campaign.schedule: {mode, ...}` — три режима |
| глобальные `AutoReplyRule[]` | `AutoReplyConfig` на аккаунт + один глобальный дефолт |
| `Binding` | **удалено** |
| `Account.warm_state / warmup_hours / warm_*` | **удалено** |
| `Operator.capacity`, `Account` capacity | **удалено** |
| `advertising_allowed` | **удалено** |

### Campaign

```python
@dataclass
class Campaign:
    id, name, account_id
    target_ids: list[str]
    message_text: str              # снимок, независим от шаблона
    source_template_id: str | None # откуда скопировали, только для истории
    schedule: Schedule
    gap_min_sec: int = 15          # случайная пауза между каналами
    gap_max_sec: int = 40
    raw_state: str = DRAFT
    results: list[CampaignTargetResult]
    next_run_at, last_run_at, created_at
```

```python
@dataclass
class Schedule:
    mode: "ONCE" | "DAILY" | "INTERVAL"
    at: str | None            # ONCE:  ISO datetime
    times: list[str]          # DAILY: ["09:00", "18:30"]
    every_sec: int | None     # INTERVAL
```

```python
@dataclass
class CampaignTargetResult:
    target_id: str
    status: "PENDING" | "SENT" | "FAILED" | "SKIPPED"
    error: str | None
    sent_at: str | None
    attempts: int
```

Правила исполнения (по ответам пользователя):
- цель упала → помечаем ошибку, **повторяем в следующем запуске** (`status=FAILED`,
  на следующем прогоне снова `PENDING`);
- зависимость упала посреди прогона → **останавливаем немедленно**, кампания в
  `BLOCKED`, невыполненные цели остаются `PENDING`;
- между целями — **случайная пауза** в диапазоне `[gap_min_sec, gap_max_sec]`.

### AutoReply

Один набор на аккаунт, три типа внутри:

```python
@dataclass
class AutoReplyRule:
    id: str
    kind: "FIRST_MESSAGE" | "PERIODIC" | "FAQ"
    enabled: bool
    match: str = ""              # только для FAQ
    response: str = ""
    delay_override: tuple | None = None   # обычно None → берётся диапазон аккаунта

@dataclass
class AutoReplyConfig:
    owner_id: str                # id аккаунта, либо "global" для дефолта
    enabled: bool
    delay_min_sec: int
    delay_max_sec: int
    rules: list[AutoReplyRule]
```

Ограничения: `FIRST_MESSAGE` — максимум одно активное правило, `PERIODIC` — максимум
одно, `FAQ` — от 0 до N, где N задаётся ползунком `autoreply.faq_limit` (0–10) в общих
настройках.

**Живая связь с дефолтом.** Если у аккаунта нет своего `AutoReplyConfig`, при каждом
входящем сообщении берётся текущий `owner_id="global"`. Поменяли глобальный — сразу
изменилось у всех таких аккаунтов.

**Валидация `@operator` (§11)** — в пяти точках: создание правила, редактирование,
включение, смена/отвязка оператора аккаунта, и финальная проверка прямо перед
отправкой. Если оператора нет — сохранение/активация блокируется с понятным текстом,
а перед отправкой сообщение не уходит вообще. Буквальный `@operator` не может
попасть пользователю ни при каком пути.

Задержка ответа: случайная из `[delay_min_sec, delay_max_sec]`, по умолчанию 5–10 минут
(`autoreply.default_delay_min_sec` = 300, `..._max_sec` = 600, настраивается глобально).

### Settings (новые ключи)

```
autoreply.faq_limit                = 5      # ползунок 0..10
autoreply.default_delay_min_sec    = 300
autoreply.default_delay_max_sec    = 600
campaign.default_gap_min_sec       = 15
campaign.default_gap_max_sec       = 40
ui.language                        = "ru"   # ru | en
```

---

## 6. Логин: неблокирующая машина состояний

В v1 логин был Tk-диалогом с блокирующими `wait_window`. В вебе так нельзя.

```
IDLE ──start──> CONNECTING ──> NEED_CODE ──submit_code──> NEED_PASSWORD ──> DONE
                     │              │                            │
                     └──────────────┴──────────> ERROR <─────────┘
```

Сессия логина живёт в памяти backend с TTL 5 минут, идентифицируется `login_id`.
Клиент шлёт `POST /api/login/start`, потом `POST /api/login/code`, `POST /api/login/password`,
и параллельно слушает состояние через SSE (`login.state`). Ни один HTTP-запрос не висит
дольше пары секунд. Поддерживаются три способа из v1: телефон+код, QR, bot-токен.

---

## 7. HTTP API (набросок)

```
GET  /                       bootstrap HTML (принимает ?t=)
GET  /assets/*               статика React-сборки
GET  /api/state              полный срез: сущности + effective + issues
GET  /api/events             SSE
POST /api/accounts/{id}/...  create | update | delete | enable | disable | probe
POST /api/campaigns/...      create | update | delete | start | pause | run_now
POST /api/autoreply/{owner}  save (owner = account id | "global")
POST /api/operators/...      create | update | delete | promote_from_account
POST /api/operators/{id}/send  отправка сообщения из чата оператора
POST /api/login/...          start | code | password | qr_poll | cancel
POST /api/profiles/...       api и proxy профили + probe
POST /api/targets/...        create | delete | check
GET  /api/operators/{id}/dialogs, /messages
POST /api/settings           сохранение настроек
```

---

## 8. Фронтенд

React 18 + TypeScript + Vite. Никаких UI-библиотек, роутеров и state-менеджеров:
собственный хэш-роутер и один reducer поверх SSE. Зависимости — только
`react`, `react-dom`, `vite`, `@vitejs/plugin-react`, `typescript`. `package-lock.json`
коммитится, версии зафиксированы точно.

Палитра — серо-оранжевая, как просил пользователь:

```
--bg #1b1b1a   --surface #232322  --surface-2 #2c2b29  --border #3a3936
--text #eceae5 --muted #a3a09a
--accent #d97757  --accent-hover #e08a6d
--ok #5c9e6a   --warn #d9a441   --err #c2604f
```

Все ошибки и неточности — **иконка-кружок + тултип при наведении**. Никаких
модальных алертов.

Экраны: Дашборд, Аккаунты (кампании раскрываются внутри карточки аккаунта),
Кампании (глобальный список), Цели, Шаблоны, Автоответы, Операторы (чаты с
возможностью отвечать), Настройки, Логи.

Локализация: RU + EN, один словарь, переключатель в настройках.

---

## 9. План миграции реальных данных

Данные в `%APPDATA%\TelegramCenter\data\` **не удаляются**. Один раз, перед первым
запуском v2, вся папка `data\` копируется в `data.v1.bak\` — после проверки её можно
удалить вручную. Папка `sessions\` не трогается вообще ни при каких условиях.

Миграция срабатывает один раз по `settings["schema_version"] < 2`.

| Файл | Действие |
|---|---|
| `accounts.json` | удаляются поля `warm_state`, `warmup_hours`, `warm_started_at`, `warm_deadline`; остальное сохраняется как есть, включая `key` и привязки к API-профилю |
| `campaigns.json` | `target_id` → `target_ids: [target_id]`; текст шаблона копируется в `message_text` как снимок; `cyclic+interval` → `schedule.mode = INTERVAL`. Две кампании ссылаются на удалённый `acc_8697f5cf76ac` — **не удаляются**, а помечаются issue «аккаунт не найден», пользователь сам переназначит или удалит |
| `bindings.json` | сущность удалена из модели; файл переименовывается в `bindings.json.removed` (в нём одна осиротевшая запись) |
| `auto_reply.json` | два глобальных правила становятся глобальным дефолтом (`owner_id="global"`). Правило `rule_d78e5c83e2c4` содержит `@operator` → переносится, но **выключается** с issue «нет привязанного оператора». Ровно то, чего требует §11 |
| `operators.json` | у `op_0e05b3ec6bcf` поле `key` указывает на несуществующий файл сессии → `key` очищается, оператор остаётся ником (что достаточно для автоответов), рисуется красный кружок «сессия не найдена» |
| `targets.json`, `templates.json`, `api_profiles.json` | без изменений |
| `sessions/` | **не трогается** |

Осиротевшие ссылки намеренно не «чинятся» молча — они становятся видимыми issue,
потому что тихое удаление данных пользователя уже приводило к потерям.

---

## 10. Сборка

```
build.bat
 ├─ npm ci                (web/, из package-lock.json)
 ├─ npm run build         → web/dist
 ├─ pytest                (падение теста прерывает сборку)
 └─ pyinstaller --onefile → dist/TelegramCenter.exe   (web/dist внутри через --add-data)
```

Промежуточные артефакты в `build/`, в `dist/` — только EXE.
`%APPDATA%` при сборке не затрагивается ни на одном шаге.

Версии: Python 3.14.6, Node 20.20.2, npm 10.8.2. Python-зависимости пинуются
точными версиями (`==`) в `requirements.txt`.
