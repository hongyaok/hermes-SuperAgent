# Superagent (`superagent`)

`superagent` is an attention-frugal router/worker coding plugin for Hermes, inspired by DeepSeek's `dsh-superagent`.

It separates **routing** from **execution**:
- **Sources** (`LocalSource` for chat/CLI requests and `GitHubSource` for GitHub issue comment mentions) produce inbound requests.
- A **deterministic, zero-token router** (`sa_assessor.py`) classifies each request (`accept`, `ignore`, `needs_human`).
- **SQLite** (`<HERMES_HOME>/plugin-data/superagent/superagent.db`) persists tickets (`T-0001`, `T-0002`, ...), event history, source deduplication, and polling cursors.
- **Isolated Hermes worker subagents** launched via `ctx.subagent_lifecycle` perform the coding work inside your repository and leave uncommitted changes in the working tree for human review (`review` state).
- **Headless chat workers (v0.2)** — the desktop **Chat** tab starts one `hermes chat` process per turn, streams its JSON output into the session transcript, and lets you resume, watch or stop it. Chat workers follow the same rule as ticket workers: changes stay uncommitted for review.
- **GitHub PR watch (v0.3)** — connect a repository, and pull-request comments that mention the agent become sessions. `auto` mode checks each PR out as an isolated `git worktree` and launches a worker on it; `observe` (the default) only records the request.
- **Session handoff (v0.5)** — a finished session can show exactly what the worker changed (`git status`/`--stat`/patch, capped) and post one human-reviewed summary comment back to its pull request. Nothing is committed, pushed or merged.

Router-side work (polling, triage, ticket state) runs no LLM and starts no subprocess. Chat turns are explicit `hermes chat` workers launched from a user message and bounded by the caps and watchdog described in [Chat and models API](#8-chat-and-models-api-v02). By default, the plugin runs in `observe` mode so it only records tickets until you explicitly approve them or switch to `auto`.

---

## Installation & Verification

Enable the plugin and run the built-in diagnostics from the plugin directory:

```bash
hermes plugins enable superagent
hermes plugins doctor .
hermes plugins validate .
```

 Note: New agent tools (`superagent_ask`, `superagent_tick`, `superagent_status`, `superagent_run`) become available in **new** Hermes chat or cron sessions started after enabling the plugin.

---

## Configuration

Configure `superagent` via the Hermes CLI (`hermes config set plugins.entries.superagent.settings.<key> <value>`) or in **Desktop -> Settings -> Plugins -> Superagent**.

```bash
# Inspect current settings
hermes config get plugins.entries.superagent.settings

# Point workers at a local repository clone
hermes config set plugins.entries.superagent.settings.repo_path "/path/to/your/repo"

# Switch between observe (default), auto, or off
hermes config set plugins.entries.superagent.settings.mode observe
```

The same values are editable without the CLI: the Superagent page's **⚙ Settings** panel writes `<HERMES_HOME>/plugin-data/superagent/settings.json` (default model/provider, named model presets, `max_workers`, `chat_max_turns`, `worker_yolo`). The panel, `GET/POST /settings` and the CLI all read through `sa_settings.py`, so they agree on one merged view.

### Model presets

`model_presets` is a list of named `(model, provider)` pairs. Three spellings are accepted, and the loader normalises all of them to `[{name, model, provider}]`:

```yaml
model_presets:
  - fast=opencode-go:qwen/qwen3.8-27b     # name=provider:model
  - strong=z-ai/glm-5.3-flash             # provider optional
  - deepseek-v4.1-flash                   # legacy plain string: name = model
  - {name: local, model: qwen3-8b, provider: ollama}
```

At most **12** presets are kept, names must be unique (case-insensitive), and every preset needs a `model`. An empty list falls back to the built-in picker list. `GET /models` returns the names plus the flat `models` list, the composer shows one chip per preset before a chat starts, and `POST /chats` accepts `preset: "<name>"` (an explicit `model`/`provider` still wins). `hermes superagent chat --preset <name>` resolves the same catalog from the terminal.

### All Settings (`plugin.yaml`)

| Setting | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `mode` | `str` (`off` \| `observe` \| `auto`) | `observe` | `off` = do nothing; `observe` = record requests only; `auto` = launch worker subagents for accepted tickets. |
| `repo_path` | `str` | `""` | Local repository path that worker subagents should work in. |
| `github_repo` | `str` | `""` | `owner/name` repository to poll for `@mentions`. Leave empty to use local requests only. |
| `github_agent_login` | `str` | `""` | The GitHub account login the agent acts as; `@mentions` of this login become requests. |
| `github_allowed_users` | `list` | `[]` | Only these GitHub logins may request work. An empty list means nobody on GitHub is allowed. |
| `github_token` | `secret` | env `SUPERAGENT_GITHUB_TOKEN` | Fine-grained GitHub personal access token with Issues read/write permission on `github_repo`. |
| `max_workers` | `int` | `2` | Maximum number of worker subagents allowed to run in parallel (minimum `1`). |
| `worker_toolsets` | `list` | `["file", "terminal"]` | Toolsets granted to each leaf worker subagent. |
| `allow_proactive` | `bool` | `false` | Let the router pick up unaddressed GitHub comments that match an actionable verb or code/error pattern. |
| `max_runtime_seconds` | `int` | `1800` | Worker runtime limit in seconds (minimum `60`). Workers exceeding this limit are cancelled by the watchdog. |
| `poll_limit` | `int` | `30` | Maximum number of GitHub issue comments fetched per polling pass (`1`–`100`). |
| `default_model` | `str` | `""` | Model used by desktop chat sessions when the request does not pick one. Empty means the CLI default. Env: `SUPERAGENT_DEFAULT_MODEL`. |
| `default_provider` | `str` | `""` | Provider for the default chat model (for example `opencode-go`). Empty means the CLI default. Env: `SUPERAGENT_DEFAULT_PROVIDER`. |
| `model_presets` | `list` | built-in list | Models offered in the per-session picker. Empty uses the built-in list. Env: `SUPERAGENT_MODEL_PRESETS` (comma separated). |
| `worker_yolo` | `bool` | `true` | Pass `--yolo` to headless chat workers so they do not wait for approval prompts. Env: `SUPERAGENT_WORKER_YOLO`. |
| `chat_max_turns` | `int` | `120` | Maximum agent turns for one headless chat worker. Env: `SUPERAGENT_CHAT_MAX_TURNS`. |
| `github_watch_mode` | `str` (`observe` \| `auto`) | `observe` | `observe` records PR mentions as `observed` sessions only; `auto` queues and launches a worker for each one. Env: `SUPERAGENT_GITHUB_WATCH_MODE`. |
| `github_watch_new_prs` | `bool` | `false` | Start a review session whenever a watched pull request gets a new head commit. Env: `SUPERAGENT_GITHUB_WATCH_NEW_PRS`. |
| `github_poll_seconds` | `int` | `90` | Seconds between background GitHub polls (minimum `5`). Env: `SUPERAGENT_GITHUB_POLL_SECONDS`. |
| `github_pr_limit` | `int` | `10` | Maximum open pull requests checked per poll (`1`–`100`). Env: `SUPERAGENT_GITHUB_PR_LIMIT`. |
| `turn_timeout_seconds` | `int` | `1800` | A queued or running chat turn older than this is cancelled by the next reap or chat-list read. `0` disables the guard. Env: `SUPERAGENT_TURN_TIMEOUT_SECONDS`. |

### Environment overrides

The dashboard backend is loaded outside the plugin package, so it reads these variables directly. They mirror the settings above and win when both are present:

| Variable | Meaning |
| :--- | :--- |
| `SUPERAGENT_DEFAULT_MODEL` | Default chat model. |
| `SUPERAGENT_DEFAULT_PROVIDER` | Default chat provider. |
| `SUPERAGENT_MODEL_PRESETS` | Comma-separated model picker presets. |
| `SUPERAGENT_WORKER_YOLO` | `1`/`true`/`yes`/`on` to pass `--yolo`. |
| `SUPERAGENT_CHAT_MAX_TURNS` | Maximum agent turns per chat worker. |
| `SUPERAGENT_TURN_TIMEOUT_SECONDS` | Runaway-turn limit; `0` disables it. |
| `SUPERAGENT_REPO_ROOT` | Root directory every per-session repo must stay inside (defaults to `SUPERAGENT_REPO`). |
| `SUPERAGENT_REPO` | Default repository for chat workers and the containment root. |
| `SUPERAGENT_MAX_WORKERS` | Parallel chat workers per database. |
| `SUPERAGENT_WORKER_TOOLSETS` | Comma-separated toolsets for chat workers. |
| `SUPERAGENT_HERMES_BIN` | `hermes` executable used for chat workers. |
| `SUPERAGENT_CHAT_SOURCE` | Source tag passed to `hermes chat` (default `tool`). |
| `SUPERAGENT_GITHUB_REPO` | `owner/name` watched for pull requests. |
| `SUPERAGENT_GITHUB_TOKEN` | GitHub token; also the fallback for a `${...}` placeholder in `plugin.yaml`. |
| `SUPERAGENT_GITHUB_AGENT_LOGIN` | Login whose `@mention` requests work (otherwise resolved from the token). |
| `SUPERAGENT_GITHUB_ALLOWED_USERS` | Comma-separated logins allowed to request work (empty = nobody). |
| `SUPERAGENT_GITHUB_WATCH_MODE` | `observe` or `auto` for the GitHub watcher. |
| `SUPERAGENT_GITHUB_WATCH_NEW_PRS` | `1`/`true` to review every new PR head commit. |
| `SUPERAGENT_GITHUB_POLL_SECONDS` | Background poll interval (minimum `5`). |
| `SUPERAGENT_GITHUB_PR_LIMIT` | Open pull requests checked per poll (`1`–`100`). |

---

## Usage Across Surfaces

> **Important Architectural Rule:** Ticket worker subagents can **only** be launched from an active Hermes agent turn (an interactive chat session or a scheduled cron turn). `ctx.subagent_lifecycle` requires an active parent session (`bind_subagent_parent`). The CLI, slash commands, and ticket REST endpoints can inspect state and queue tickets, but they never launch ticket workers directly (`launch=False`). The v0.2 chat endpoints are separate: they start a headless `hermes chat` process, which needs no parent turn and is not a subagent.

### 1. Agent Tools (Chat Session or Cron Turn)

Inside a Hermes session, the agent has four tools in the `superagent` toolset:

- **`superagent_ask(request)`**: Record a coding request as a ticket (`observed` in `observe` mode, `queued` in `auto` mode, or `blocked` if it looks like a design question).
- **`superagent_tick(wait=false)`**: Run one full routing pass: poll GitHub (if configured), deduplicate and assess new comments, launch worker subagents for `queued` tickets (in `auto` mode, up to `max_workers`), reap finished workers into `review` or `failed`, and cancel timed-out workers. Pass `wait=true` to block until active workers finish.
- **`superagent_status(ticket_id=None)`**: Show summary counts, recent tickets, and the `"Needs you"` attention queue (`observed`, `blocked`, `review`, `failed`). Pass `ticket_id` (e.g. `"T-0001"`) to inspect full ticket details and its audit event log.
- **`superagent_run(ticket_id, wait=false)`**: Human-in-the-loop promotion. Approves an `observed`, `blocked`, or `failed` ticket, moves it to `queued`, and immediately attempts to launch a worker subagent for it.

### 2. CLI (`hermes superagent`)

Use the CLI from any terminal to inspect tickets, queue work, or check health (`--json` is supported on all subcommands):

```bash
# Show mode, running/queued counts, "Needs you" attention list, and recent tickets
hermes superagent status
hermes superagent status --json

# Queue a new request from the command line
hermes superagent ask "Add unit tests for token expiration edge cases"

# Poll sources and record new tickets (does not launch workers from CLI)
hermes superagent tick

# Approve a ticket (moves to queued; worker starts on the next agent turn)
hermes superagent run T-0001

# Show full detail and history for one ticket
hermes superagent show T-0001

# Chat: pick a named preset, or list/inspect the merged settings
hermes superagent chat "Add a regression test" --preset fast
hermes superagent models
hermes superagent settings
hermes superagent settings --json

# Review what a finished session changed, then comment on its PR
hermes superagent diff S-0001
hermes superagent comment S-0001
hermes superagent comment S-0001 --body-file review.md

# Verify configuration, repo existence, and worker lifecycle availability
hermes superagent doctor
```

| Command | Behaviour |
| :--- | :--- |
| `hermes superagent status` | Mode, running/queued counts, the "Needs you" list and recent tickets. |
| `hermes superagent tick` | One routing pass: polls sources and records tickets (never launches a worker from the CLI). |
| `hermes superagent ask "<request>"` | Record a request as a ticket. |
| `hermes superagent run <ticket-id>` | Promote an `observed`/`blocked`/`failed` ticket to `queued`. |
| `hermes superagent show <id>` | A ticket's full detail, or a chat session (`S-…`) with messages and events. |
| `hermes superagent chat "<prompt>"` | Start a headless chat turn (blocking; `--detach` returns immediately). `--preset <name>` resolves a named preset; `--model`/`--provider` still win. |
| `hermes superagent sessions` / `stop <id>` | List chat sessions / cancel a queued or running turn. |
| `hermes superagent diff <id>` | `git status` + `git diff --stat` + the (capped) patch for the session's repository; `--json` returns the full dict. `1` for an unknown session or one without a repository. |
| `hermes superagent comment <id> [--body TEXT \| --body-file PATH]` | Post one summary comment to the session's pull request and print its URL. `1` without a PR/token or when GitHub refuses, `2` for bad usage. |
| `hermes superagent models` | Default model/provider plus the preset section (`name -> provider: model`). |
| `hermes superagent settings [--json]` | Print the merged settings (the GitHub token is masked to `set`/`not set`); read-only. |
| `hermes superagent github [status\|test\|connect\|disconnect\|prs\|poll\|work <n>]` | Connect a repository, manage the watch settings, list open PRs, and start work on one (see [GitHub connect & PR watch](#9-github-connect--pr-watch-v03)). |
| `hermes superagent doctor` | Configuration, repository and worker-lifecycle checks. |

Every subcommand accepts `--json`. Exit codes: `0` ok, `1` runtime error, `2` usage error.

### 3. Slash Command (`/superagent`)

Inside Hermes chat interfaces, use `/superagent` for quick inspection and queueing:

```text
/superagent status
/superagent ask Fix off-by-one pagination bug in list_users
/superagent tick
/superagent run T-0001
/superagent help
```

Note: `/superagent run <id>` promotes the ticket to `queued`; if called outside an active agent tool turn, the worker starts on the next `superagent_tick` tool call.

### 4. Automation Blueprint (`Superagent watch`)

`superagent` registers a cron blueprint named **`Superagent watch`** (`watch`) scheduled every 5 minutes (`*/5 * * * *`).

Because cron automations execute as full Hermes agent turns, `Superagent watch` calls `superagent_tick` with `wait=true`, launches and reaps worker subagents automatically, and reports **only** tickets that require human attention (or replies `"Superagent: nothing needs you."`).

### 5. Desktop Panel & Dashboard API

The plugin ships a unified desktop extension (`desktop/plugin.js`) and a FastAPI backend (`dashboard/plugin_api.py` mounted at `/api/plugins/superagent`). Ticket routes stay read-only; the chat routes below them drive the v0.2 Chat tab:

- `GET /api/plugins/superagent/health` — plugin version and SQLite database status.
- `GET /api/plugins/superagent/tickets?limit=30` — status counts, `"Needs you"` attention list, and recent tickets.
- `GET /api/plugins/superagent/tickets/{ticket_id}` — full ticket details and event log.
- `GET /api/plugins/superagent/models` — default model/provider and the picker presets.
- `GET|POST /api/plugins/superagent/chats` — list chat sessions / start a new headless chat.
- `GET /api/plugins/superagent/chats/{session_id}` — one chat with its transcript and events.
- `POST /api/plugins/superagent/chats/{session_id}/messages` — send a follow-up and resume the Hermes session.
- `POST /api/plugins/superagent/chats/{session_id}/stop` — cancel the running turn.
- `GET /api/plugins/superagent/chats/{session_id}/diff` — git status, stat and a capped patch for the session's repository.
- `GET /api/plugins/superagent/chats/{session_id}/comment-preview` — the comment body a post would send (no network, no write).
- `POST /api/plugins/superagent/chats/{session_id}/comment` — post one human-confirmed comment to the session's pull request.

The desktop page at `/superagent` now leads with the v0.2 chat workspace (see [Desktop chat tab](#7-desktop-chat-tab-v02)) and keeps the ticket views beside it:

- **Chat tab** — a chat rail, a live transcript, and a composer with a model picker and an optional repo path. The first prompt starts a headless `hermes chat` worker; follow-ups resume it; **Stop** cancels it.
- **Attention tab** — `observed` / `blocked` / `review` / `failed` tickets that need a human decision.
- **Sessions tab** — the full sessions list (`Engine.sessions()`), with status, requester, request/result previews, event count, timestamps and elapsed time, plus `running` / `queued` counts. Selecting a row calls the ticket-detail view (`Engine.get_ticket_detail()` / `GET /tickets/{id}`).

Enable the desktop view under **Desktop -> Settings -> Plugins -> Superagent** (then reload desktop plugins) to see the status bar chip and attention panel.

### 6. Sessions Enrichment API (`Engine.sessions` / `get_ticket_detail`)

For programmatic and UI use, the engine exposes a read-only sessions layer (no behavior change to `ask` / `tick` / `promote`):

```python
engine.sessions(limit=50, status=None)   # {"sessions": [...], "counts": {...}, "running": N, "queued": N}
engine.get_ticket_detail("T-0001")       # full ticket dict + "events" list; raises KeyError when missing
```

Each session row contains `id`, `status`, `source`, `requester`, `request` (160-char preview), `reason`, `error`, `handle`, `result_preview` (200-char preview), `event_count`, `created_at` / `updated_at` / `started_at`, and `elapsed_secs` (now minus `started_at`, falling back to `created_at`). The store helpers behind it (`Store.list_tickets(status, limit)`, `Store.get_events(ticket_id)`, `Store.count_by_status()`) are pure `SELECT`s with no schema change.

---

### 7. Desktop chat tab (v0.2)

The Superagent page (`/superagent`) is chat-first. Everything in the tab is backed by `/chats` and `/models`; ticket state is shown alongside but a chat turn never creates a ticket.

- **Chat rail (left)** — the newest chats, each with status, model, age (or elapsed time while a turn runs). Selecting a row opens its transcript; **New chat** clears the composer.
- **Transcript (centre)** — the session prompt followed by `chat_messages` and `chat_events` merged in order, so assistant text interleaves with tool calls, errors and result lines. A failed turn shows the stored error.
- **Composer** — a model picker fed by `/models` (presets plus **Default model**), an optional repo path (defaults to the configured repo) and Enter-to-send / Shift+Enter newline. While a turn is queued or running the composer is disabled and **Stop** cancels the turn.
- **Chat header** — status, model/provider, tokens, duration, and **Open in Hermes** when `hermes_session_id` is known.
- **Live workers (right)** — every queued or running chat as a card, so parallel workers stay visible without opening each chat.
- **Polling** — the rail refreshes from `/chats` on a timer (faster while anything is running); the open chat refreshes from `/chats/{id}` at the same time. No WebSocket is required.

A chat turn's lifecycle is `queued` → `running` → `review` (or `failed` / `cancelled`). `review` means the worker finished and its changes are waiting in the working tree; sending another message resumes the same Hermes session when `hermes_session_id` is known.

---

### 8. Chat and models API (v0.2)

| Method | Path | Behaviour |
| :--- | :--- | :--- |
| `GET` | `/models` | `{default_model, default_provider, models, presets}` from settings/env; `presets` is `[{name, model, provider}]` and `models` stays the flat model list. |
| `GET` | `/settings` | Merged, public settings (`mode`, `repo_path`, defaults, normalised `model_presets`, worker limits, GitHub watch fields) plus `github_token_set`; never the token. |
| `POST` | `/settings` | Validate and save `mode`, `repo_path`, `default_model`, `default_provider`, `model_presets`, `max_workers` (1–8), `worker_yolo`, `chat_max_turns` (1–500); unknown keys and out-of-range values are `400`. Drops the Runner singleton so the next turn uses the new values. Returns `{ok, settings}`. |
| `GET` | `/chats?limit=50` | `{db_exists, sessions, counts, running}`; also runs the runaway watchdog before answering. |
| `POST` | `/chats` | Validate, create and launch a chat. Optional `preset` selects a named model (explicit `model`/`provider` still win; unknown preset is `400` listing the available names) and records a `preset` event. `400` on an empty/oversized prompt or an invalid repo; `200 {ok:false}` when the worker cannot start (the session is stored as `failed`). |
| `GET` | `/chats/{id}` | Session with `messages` and `events`; `404` for an unknown id. |
| `POST` | `/chats/{id}/messages` | Append the user message and resume the Hermes session. `409` while a turn is queued/running; `400` when the stored repo is no longer valid; with no `hermes_session_id` a fresh chat is created and `previous_session_id` is returned. |
| `POST` | `/chats/{id}/stop` | Cancel a queued/running turn; `409` when nothing is running. |

#### Guardrails

- **Repository containment.** Before a turn is queued, the target repo is resolved with `Path.resolve(strict=True)`. A path that does not exist, is not a directory, contains `..`, or resolves (including through a symlink/junction) outside `SUPERAGENT_REPO_ROOT` (which defaults to `SUPERAGENT_REPO`) raises `ValueError`, which the handlers turn into `400`. Only the resolved path is passed to `--in` and used as the subprocess `cwd`; the raw request string never reaches `argv`.
- **Caps.** Prompts are limited to 20 000 characters (checked by the runner and by `POST /chats` / `POST /chats/{id}/messages`), stored assistant text is clipped to 20 000, and every event detail is clipped to 2 000. Over-limit prompts are rejected with `400`; over-limit output is truncated before the write, so a runaway worker cannot bloat the database.
- **Secret redaction.** `ghp_`, `gho_` and `github_pat_` tokens, `sk-` and `xoxb-`/`xoxp-` style keys, `Authorization:` header values and bare `Bearer` tokens are replaced with `[redacted]` before chat messages, events, errors, prompts and ticket fields are persisted. The same scrub runs on the preview strings the UI reads.
- **Runaway guard.** A queued or running turn older than `turn_timeout_seconds` (env `SUPERAGENT_TURN_TIMEOUT_SECONDS`, default `1800`; `0` disables) is cancelled by `Runner.reap()`. `GET /chats` calls `reap()` before answering, so the status becomes `cancelled` and a `cancelled` event records the reason (`turn timed out after Ns (limit Ns)`) even if the server restarted and no process handle is left.
- **Consistent triage.** `POST /ask` runs the same `sa_assessor.assess(..., explicit=True)` rules as the router instead of hardcoding `accept`: a skip marker or acknowledgement is recorded as `ignored`, a design question becomes `blocked` (`needs_human`), and an actionable request becomes `observed` (`accept`). The verdict and reason are stored on the ticket and in its `assessed` event.

### 9. GitHub connect & PR watch (v0.3)

Connect one repository and Superagent watches its pull requests. A comment that mentions the agent (`@<login>`, `@superagent`, `/superagent`, or a line starting `superagent:`) from an allowed user becomes a session; when `github_watch_new_prs` is on, a new head commit on a watched PR starts a review session once per SHA.

| Method | Path | Behaviour |
| :--- | :--- | :--- |
| `GET` | `/github/status` | `{configured, repo, login, token_set, open_prs, last_poll, last_error, watching, watch_mode, watch_new_prs, allowed_users, poll_seconds, sessions_created}`; also starts the background poller (idempotent). |
| `POST` | `/github/test` | `{repo?, token?}` — validate a token/repository pair without saving. Returns `{ok, login, full_name, default_branch, private, open_pr_count}`; `400` on a bad repo or rejected token. |
| `POST` | `/github/connect` | `{repo, token?, allowed_users?, watch_mode?, watch_new_prs?, poll_seconds?}` — validate with the supplied or saved token, save the connection and the optional watch settings in one write, reset the baseline, start watching, return the status. `400` on a malformed repo/setting or an auth failure. |
| `POST` | `/github/settings` | `{allowed_users?, watch_mode?, watch_new_prs?, poll_seconds?, agent_login?}` — save watch settings without touching the repository or token; the watcher is rebuilt so the next poll uses them. `400` on an unknown mode, a `poll_seconds` outside `15`–`3600`, or a malformed login. Returns `{ok, status}` (never the token). |
| `POST` | `/github/disconnect` | Clear `github_repo` and `github_token` in `settings.json`, stop the poller. |
| `POST` | `/github/poll` | One polling pass now: `{configured, polled, created, errors}`. |
| `GET` | `/github/prs` | `{configured, prs}` — the cached open PR list. |
| `POST` | `/github/prs/{number}/work` | `{request?}` — create a session for that PR and launch a worker (an explicit human action always launches). `400` when GitHub is not connected, `404` for an unknown PR. |

**Desktop panel.** The Superagent page has a **GitHub** button in the header with a status dot (green when a repository and token are configured) and the repository's short name. It opens a full-width panel above the chat workspace:

- **Connection (left)** — repository (`owner/name`), a password field for the token (blank keeps the saved one; the placeholder says `saved token`), the comma-separated allowed-user list, and an Observe/Auto segmented control. **Connect** saves the whole form in one `POST /github/connect`; **Save settings** sends only the watch fields to `POST /github/settings`; **Poll now** runs one pass; **Disconnect** forgets the repository and token. The panel shows the resolved login, open-PR count, watching state, poll interval and the watcher's `last_error`. The token is never rendered.
- **Open pull requests (right)** — the repository's open PRs with `#number`, a two-line title, author, updated age, and **Work on this PR** (`POST /github/prs/{n}/work`). The returned session is selected immediately, so the transcript opens in the centre column.
- **PR badges** — a session created from a PR carries `pr_number`/`pr_url`, shown as a `#123` badge in the chat header, the session rail and the live-worker cards. Clicking it opens the PR in the browser.
- **Polling** — `/github/prs` refreshes every 20 s only while the panel is open; `/github/status` refreshes at the same interval only while the panel is open or a session with a PR badge is on screen. Otherwise the status is fetched once for the header dot, so the chat workspace keeps its existing polling budget.

**CLI.** `hermes superagent github ...` uses `sa_settings`/`sa_github`/`GitHubWatcher` directly (no HTTP, no dashboard needed):

```bash
# Status (default action), open PRs and one polling pass
hermes superagent github
hermes superagent github prs
hermes superagent github poll

# Validate a token/repository pair without saving anything
hermes superagent github test --repo owner/name --token <token>

# Connect (saves repo/token/login and optional watch settings)
hermes superagent github connect --repo owner/name --token <token> \
  --allowed alice,bob --watch-mode observe

# Start work on a PR (blocking turn; prints the session id and transcript)
hermes superagent github work 7 --request "add a regression test"

# Forget the repository and token
hermes superagent github disconnect
```

`--json` works on every action. Exit codes are `0` (ok), `1` (runtime or configuration error) and `2` (usage: unknown action, missing/invalid `--repo`, bad `--watch-mode`, missing PR number). The token is written to `settings.json` and never echoed.

**Settings precedence.** `DEFAULTS` (matching `plugin.yaml`) < `plugins.entries.superagent.settings` in `config.yaml` < `<HERMES_HOME>/plugin-data/superagent/settings.json` (what the connect form writes) < environment variables. A token that is empty or a `${...}` placeholder falls back to `SUPERAGENT_GITHUB_TOKEN`. `settings.json` is written atomically and chmod `600` where the platform supports it; the token is never returned by an endpoint (`token_set: true` says whether one exists).

**Watch semantics.**

- The **first successful poll is a baseline**: every existing comment and PR head is recorded as seen without creating a session, so connecting a busy repository does not replay its history.
- Dedupe lives in the `gh_seen(key, seen_at)` table, so restarts do not duplicate work. Every unseen comment is marked seen before it is judged; a malformed comment cannot replay forever.
- Bots (`*[bot]`, `github-actions`, `dependabot`) and the agent's own comments are ignored. `github_allowed_users` is an allow-list; empty means **nobody**.
- `github_watch_mode: observe` records sessions as `observed` and launches nothing. A human promotes one later (the session is a normal chat session with its PR prompt and checkout path).
- `github_watch_mode: auto` creates the session as `queued` and launches it. With `repo_path` configured, the PR is fetched into that clone (`+refs/pull/<n>/head:refs/superagent/pr-<n>`) and checked out as a detached worktree at `<data_dir>/workspaces/pr-<n>`, so the user's checkout and branch are untouched. If the checkout fails the session launches in `repo_path` with a `github` warning event; with no usable repository the session is marked `failed`.

---

### 10. Settings, presets and first-run onboarding

A new user should be able to choose models and get started without reading the docs.

**Header gear.** The ⚙ button in the Superagent header toggles a full-width **Settings** panel above the chat workspace (the same collapsible pattern as the GitHub panel). It edits:

- **Default model** — the same picker as the composer, plus **Inherit** to clear the pin (the provider is shown, or `auto (chosen by Hermes)`).
- **Model presets** — rows of `name`, `model`, `provider` with a remove button and **+ Add preset** (12 max); the hint is *"Presets appear in the composer; pick one before starting a chat."*
- **Max parallel workers** (`1–8`), **Max turns per worker** (`1–500`) and **Run workers unattended (`--yolo`)** with help text.
- **Save** posts `POST /settings` and invalidates the settings, models and chats queries so the chips and every new session agree; **Refresh** re-reads and **Cancel** closes.

**Composer preset chips.** Before a chat starts, one chip per preset sits above the textarea. Clicking a chip fills the model/provider and highlights it; the normal ModelPicker stays available for an ad-hoc choice (which clears the chip). The selected name is sent as `preset` with `POST /chats`, together with the resolved `model`/`provider`.

**First-run onboarding.** While there are no chat sessions yet, a compact **Get started** card lists three numbered steps: *Pick a model / preset* (opens Settings), *Connect a GitHub repo to watch PRs* (opens the GitHub panel; the hint states that an empty allow-list means nobody can trigger work and the agent ignores its own comments), and *Start a chat below*. Dismissing it writes `onboarded` through `ctx.storage`, so it does not come back.

**GitHub panel.** The allow-list field now carries the helper copy *"Only these users can trigger work; the agent ignores its own comments — add a teammate or a second account."*

**CLI.** `hermes superagent settings [--json]` prints the merged view (token masked to `set`/`not set`); `hermes superagent chat --preset <name>` resolves the same preset catalog and exits `2` with the available names when the preset is unknown. `hermes superagent models` prints `name -> provider: model` for every preset.

---

### 11. Review a finished session (diff + PR comment)

Closing the loop does not need a push. A finished session leaves its changes in the working tree; the plugin can show them and say what happened on the pull request, and nothing else.

**Diff button (chat header).** Visible whenever the session has a repository. The diff is *lazy*: nothing is fetched until the panel is open, and it is refetched every 4 s only while the turn is `queued`/`running` (a finished session stays still until **Refresh**). The collapsible panel sits between the transcript and the composer (`max-h-22rem`, scrolls) and shows:

- the file count, the repository and a *patch truncated* note when the cap hit;
- the `git diff --stat` block (plus a `staged:` block when the index has changes);
- one row per file with its porcelain letter (`M`, `A`, `D`, `R`, `??`, `!`); past 200 files it says how many were not listed;
- the patch itself in a monospace `<pre>`.

**Comment on PR button.** Visible when the session carries a PR number *and* a GitHub token is saved. It opens an inline confirm — never `window.confirm` — with a textarea prefilled from the preview endpoint, **Post** and **Cancel**. The button is disabled while a turn is running and nothing is posted until **Post** is pressed; the success notice links to the new comment. A user edit is never overwritten by a preview refetch.

| Method | Path | Behaviour |
| :--- | :--- | :--- |
| `GET` | `/chats/{id}/diff` | `{ok, repo, files: [{status, path}], stat, patch, truncated, error}`. `404` unknown session; `400` when the session has no repository or it no longer exists / is not a git checkout. The patch is capped at 60 000 characters and ends with `… diff truncated …`. |
| `GET` | `/chats/{id}/comment-preview` | `{ok, session_id, pr_number, github_repo, url, body, length}` — exactly what a post would send, with no network call and no token requirement. `400` when the session has no PR. |
| `POST` | `/chats/{id}/comment` | Post `{body?}` (default: the generated summary). `400` without a PR, a repository, a saved token or a readable repo; `502` when GitHub refuses (message redacted). Records a `commented` event with the comment URL and returns `{ok, url, pr_number, body_preview, session}`. |

The generated body is `Superagent finished <id> — <title>`, the last assistant message, `Files changed: N (+X/-Y)` from the diff stat, and `_Generated by Superagent._`; missing pieces are omitted and the whole thing is clipped to 20 000 characters (the assistant summary shrinks first).

**CLI.** Both commands read the chat tables directly, so they work without a dashboard or a ticket engine:

```bash
hermes superagent diff S-0001                 # stat, file list, patch (exit 1: unknown/no repo)
hermes superagent diff S-0001 --json          # {ok, session_id, diff}
hermes superagent comment S-0001              # generated summary → the PR, prints the URL
hermes superagent comment S-0001 --body "LGTM"        # your text instead
hermes superagent comment S-0001 --body-file review.md
```

`comment` exits `2` for usage (both body sources, unreadable/empty file, over 20 000 chars), `1` for an unknown session, a session without a PR, no repo/token or a GitHub failure, and `0` with the comment URL on success.

**Guardrails.** `collect_diff` runs git as an argv list with no shell and never raises: a missing directory, a plain directory, a missing `git` binary and a timeout are all `{ok: false, error}` with stderr redacted. The repository path is resolved through the same containment check as a worker turn, so a session cannot point the diff at an arbitrary directory. Commenting calls exactly one REST endpoint (`POST /repos/{repo}/issues/{n}/comments`) — no commit, push, merge or PR edit happens anywhere in this flow.

---

## Testing

Run the test suite from the repository root:

```bash
pytest -q
```

The suite is hermetic: `tests/test_runner.py` and `tests/test_hardening.py` replace `Popen` with a fake, so no real `hermes` process is spawned. `tests/test_hardening.py` covers repo containment (missing/file/dot-dot/symlink escape), secret redaction, the 20k/2k caps, every `POST /ask` triage branch, and the runaway watchdog. The v0.3 GitHub tests are also offline: `tests/test_github.py` replaces the HTTP call at the `_urlopen` seam and git at the `git_runner` parameter, `tests/test_watcher.py` uses a canned client and a fake runner, and `tests/test_github_api.py` writes settings to a throwaway `HERMES_HOME` and patches the watcher/client singletons. The handoff tests use a real temporary git repository (`tests/test_diff.py` for status/stat/patch, staged changes and truncation; `tests/test_handoff_api.py` for the endpoints; `tests/test_cli_handoff.py` for `diff`/`comment`) and fake the GitHub client, so nothing reaches the network.

Validate the plugin manifest and its registrations before shipping:

```bash
hermes plugins doctor .
hermes plugins validate .
```
