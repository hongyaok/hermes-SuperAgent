# Superagent Plugin — Handover to the Next Agent

**Date:** 2026-11-06
**Version:** 0.4.0
**Repo:** `origin/main` @ the v0.4.0 release commit
**Installed as:** `<HERMES_HOME>/plugins/superagent` (on this Windows machine usually a directory junction to the working clone, so edits are live; if it is a copy, reinstall after editing)
**Status:** feature-complete through the v0.4 surfaces. **401 tests pass**, `hermes plugins validate .` is clean (manifest, capability probe, declared tools/hooks, security scan and desktop-surface check all pass), and `hermes plugins doctor --ci .` passes manifest/import/registration. The backend is exercised hermetically by the suite; the remaining optional work is a real GitHub repository run and a visual check in the Electron shell.

## 1. What this is

A Hermes native plugin with two execution paths that share one SQLite
database, one settings loader and one attention budget:

1. **Tickets (v0.1, kept).** A deterministic, zero-token router
   (`sa_assessor.py`) decides whether an inbound request is work, noise or a
   design question. Accepted tickets are run by Hermes **subagents** launched
   through `ctx.subagent_lifecycle` only from an active agent turn.
2. **Chat sessions (v0.2+, the primary desktop surface).** A user message
   starts one headless `hermes chat` **subprocess** per turn; its stream-json
   output becomes a transcript, follow-ups resume the Hermes session, and a
   live grid shows every parallel worker.

Neither path commits, pushes, merges or opens a pull request. A successful turn
ends in `review`, with the changes left uncommitted in the working tree.

Ticket lifecycle: `observed → queued → running → review` (plus `failed` /
`blocked` / `ignored` / `done`). Modes: `off` / `observe` / `auto` (default
`observe`); only `auto` launches ticket workers.

Chat statuses: `queued`, `running`, `review` (turn finished; a human should
look), `observed` (recorded GitHub request nobody launched), `failed`,
`cancelled`. `idle` is also defined in `ChatStore.STATUSES`, but the runner does
not currently emit it: every successful turn ends in `review`, and a follow-up
resumes from there.

## 2. Why chat turns are subprocesses (do not "simplify" this)

`get_active_subagent_parent()` is bound only during an active agent turn
(`agent/turn_facade.py` → `bind_subagent_parent`). The dashboard backend is an
HTTP handler: there is no turn to bind, so `ctx.subagent_lifecycle` cannot start
work from a request. The supported pattern in this Hermes version is the one
the built-in kanban dispatcher uses: spawn a headless `hermes chat` process
and read its stream-json output. `sa_runner.Runner` implements that.

Consequences:

- **Ticket workers** still launch from a tool call (`superagent_tick`,
  `superagent_run`) or a cron turn (`Superagent watch`). CLI, slash commands
  and REST handlers can inspect/queue tickets but pass `launch=False`.
- **Chat workers** launch from `POST /chats`, `POST /chats/{id}/messages` and
  the GitHub watcher, all of which are outside an agent turn. They need no
  parent session because they are ordinary `hermes chat` processes.
- `--source tool` is required: the gateway only accepts source tags it
  advertises (`cli`, `tool`, `oneshot`). `SUPERAGENT_CHAT_SOURCE` overrides it.
- `max_workers` is a `threading.Semaphore`; extra turns stay `queued` with
  their thread parked until a slot frees.
- `stop()` kills the process tree (`taskkill /T /F /PID` on Windows,
  terminate-then-kill elsewhere).

## 3. Architecture map

```
superagent/
├── plugin.yaml            manifest (version 0.4.0) + Settings form/config schema
├── __init__.py            register(): 4 tools, /superagent slash, `hermes superagent`
│                          CLI, "Superagent watch" cron blueprint, operator skill
├── sa_schemas.py          tool schemas the model reads (ASK, TICK, STATUS, RUN)
├── sa_tools.py            tool handlers — always return JSON, never raise
├── sa_assessor.py         deterministic router: accept/observe/ignore/needs_human
├── sa_sources.py          LocalSource + GitHubSource (legacy issue-comment polling)
├── sa_store.py            ticket SQLite: tickets / events / state / seen
├── sa_engine.py           ticket Engine: ask → tick → record → launch → reap → promote
├── sa_executor.py         SubagentExecutor over ctx.subagent_lifecycle
├── sa_chats.py            chat schema + ChatStore (S-0001 ids, statuses, caps, gh_seen)
├── sa_runner.py           headless `hermes chat` Runner: argv, stream-json mapping,
│                          semaphore, redaction, repo containment, stop/reap
├── sa_settings.py         one merged settings loader + preset normalisation
├── sa_github.py           stdlib urllib GitHub client, PR prompt, PR worktree checkout
├── sa_watcher.py          GitHubWatcher: poll PRs/comments, baseline + dedupe, sessions
├── sa_diff.py             collect_diff (status/stat/patch, capped) + comment body
├── sa_cli.py              all CLI subcommands + text renderers
├── dashboard/
│   ├── manifest.json      desktop package metadata (version 0.4.0)
│   └── plugin_api.py      FastAPI router mounted at /api/plugins/superagent
├── desktop/
│   └── plugin.js          page, side pane, status chip (no build step)
├── skills/
│   └── superagent-operator/SKILL.md   operator skill `superagent:superagent-operator`
├── tests/                 401 hermetic tests
├── README.md              install, quick start, settings, surfaces, API reference
├── DESIGN.md              principles and architectural rationale
├── SPEC_V2.md             the v0.2→v0.4 build plan and where it landed
├── CHANGELOG.md           release notes (0.4.0 newest)
└── HANDOVER.md            this file
```

**Dual-mode imports are load-bearing.** `__init__.py`, `sa_engine.py`,
`sa_tools.py`, `sa_cli.py`, `sa_chats.py`, `sa_runner.py`, `sa_settings.py`,
`sa_github.py`, `sa_watcher.py` and `sa_diff.py` try `from . import …` first
and fall back to top-level imports. This lets pytest import root
`__init__.py` as `__init__` without crashing, while Hermes
(`hermes_plugins.superagent`) and tests (`sa_plugin`) use package imports. Do
not collapse these back to bare relative imports.

Key flows:

- `ask("fix X")` → assess(explicit=True) → `observed` (observe) or `queued`
  (auto).
- `tick()` → poll GitHub issue comments (if configured) → dedupe via `seen` →
  assess → record → launch queued tickets (auto only, honours `max_workers`) →
  reap finished workers; the watchdog cancels past `max_runtime_seconds`.
- `promote(T-xxxx)` → human approves `observed`/`blocked`/`failed` → `queued` →
  launch attempt.
- `POST /chats` → `ChatStore.create_session` → `Runner.launch_turn` → daemon
  thread → semaphore → subprocess → stream events → `review`/`idle`/`failed`.
- `GitHubWatcher.poll_once()` → open PRs + comments → baseline/dedupe →
  `observed` or `queued` session → optional PR worktree checkout → launch.
- `GET /chats/{id}/diff` → `sa_diff.collect_diff` → `POST
  /chats/{id}/comment` after human confirmation.

## 4. SQLite tables

One WAL-mode file: `<HERMES_HOME>/plugin-data/superagent/superagent.db`
(resolved through `sa_settings.data_dir()`). A fresh connection is opened per
operation so CLI, gateway requests and agent turns do not share a thread-bound
socket.

| table | holds |
| :--- | :--- |
| `tickets` | `id` (`T-0001`), `source`, `source_ref`, `repo`, `issue_number`, `requester`, `request`, `status`, `verdict`, `reason`, `mode`, `handle`, `result`, `error`, `created_at`, `updated_at`, `started_at` |
| `events` | append-only ticket audit: `ticket_id`, `kind`, `detail`, `created_at` |
| `state` | key/value: `ticket_seq`, `chat_seq`, `github_since`, `gh_last_poll:<repo>` |
| `seen` | ticket source dedupe: `source_key`, `seen_at` |
| `chat_sessions` | `id` (`S-0001`), `title`, `prompt`, `status`, `model`, `provider`, `repo`, `hermes_session_id`, `source`, `github_repo`, `pr_number`, `pr_url`, `pid`, `tokens`, `duration_ms`, `error`, `last_event`, `created_at`, `updated_at`, `started_at`, `finished_at` |
| `chat_messages` | one row per user/assistant message: `session_id`, `role`, `content` (20 000 char cap), `created_at` |
| `chat_events` | chat audit: `session_id`, `kind` (created/queued/started/tool/tool_error/result/review/failed/cancelled/preset/github/commented), `detail` (2 000 char cap), `created_at` |
| `gh_seen` | GitHub dedupe keys: `key`, `seen_at` (comment ids, PR head SHAs, per-repo baselines) |

Indices: `tickets_status`, `events_ticket`, `chat_sessions_updated`,
`chat_sessions_status`, `chat_messages_session`, `chat_events_session`,
`chat_events_created`.

Ids are readable and sequential, allocated from `state` under `BEGIN
IMMEDIATE` (`ticket_seq` / `chat_seq`). Titles come from the first prompt line
(80 chars) unless supplied.

## 5. HTTP API

Base: `/api/plugins/superagent`. All handlers return JSON and degrade instead
of raising: a missing database yields `db_exists: false` for lists.

### Health and tickets

| Method | Path | Behaviour |
| :--- | :--- | :--- |
| GET | `/health` | `{ok, version, db_exists, db_path}` |
| GET | `/tickets?limit=30` | counts, `attention` (`observed`/`blocked`/`review`/`failed`), recent tickets |
| GET | `/tickets/{id}` | full ticket + event log; 404 unknown |
| GET | `/sessions?limit=50` | enriched **ticket** rows for the side pane/grid — not chat sessions |
| POST | `/ask` | record a ticket through the same assessor rules; `400` empty/oversized |
| POST | `/run` | promote an `observed`/`blocked`/`failed` ticket to `queued` (no launch) |
| POST | `/tick` | one routing pass summary (no ticket launch from REST) |

### Chats

| Method | Path | Behaviour |
| :--- | :--- | :--- |
| GET | `/chats?limit=50&status=` | `{db_exists, sessions, counts, running}`; live rows add `recent_events` (last 3) and `elapsed_seconds` |
| POST | `/chats` | validate, create and launch a turn; `preset` optional (explicit model/provider wins); `200 {ok:false}` when the worker cannot start |
| GET | `/chats/{id}` | session + messages + events; 404 unknown |
| POST | `/chats/{id}/messages` | append the user message and resume; 409 while a turn runs; with no `hermes_session_id` a fresh chat is created |
| POST | `/chats/{id}/model` | set model/provider for future turns; 400 both empty, 409 busy, 404 unknown |
| POST | `/chats/{id}/stop` | cancel a queued/running turn; 409 when nothing is running |
| GET | `/chats/{id}/diff` | `{ok, repo, files, stat, patch, truncated, error}`; 60 000-char patch cap |
| GET | `/chats/{id}/comment-preview` | exactly what a post would send; no network, no token |
| POST | `/chats/{id}/comment` | post one human-confirmed comment to the PR; 502 on GitHub failure (redacted) |

### Settings and models

| Method | Path | Behaviour |
| :--- | :--- | :--- |
| GET | `/models` | `{default_model, default_provider, models, presets}`; `presets: []` when none configured, never a hardcoded catalogue |
| GET | `/settings` | merged public settings + `github_token_set`; never the token |
| POST | `/settings` | validate and save `mode`, `repo_path`, `default_model`, `default_provider`, `model_presets`, `max_workers` (1–8), `worker_yolo`, `chat_max_turns` (1–500); drops the Runner singleton |

### GitHub

| Method | Path | Behaviour |
| :--- | :--- | :--- |
| GET | `/github/status` | connection/watch status; lazily starts the poller |
| POST | `/github/test` | validate `{repo?, token?}` without saving; returns masked identity |
| POST | `/github/connect` | validate, save repo/token/watch settings, reset baseline, start watching |
| POST | `/github/settings` | save allow-list/mode/new-PR flag/poll interval/login; rebuilds the watcher |
| POST | `/github/disconnect` | clear repo + token, stop polling |
| POST | `/github/poll` | one pass: `{configured, polled, created, errors}` |
| GET | `/github/prs` | cached open PR list |
| POST | `/github/prs/{number}/work` | create + launch a session for that PR (explicit human action) |

`GET /sessions` keeps its v0.1 meaning (ticket rows). Chat sessions live under
`/chats`; do not merge the two namespaces.

## 6. Desktop UI (`desktop/plugin.js`)

Four registrations: `page` at `/superagent`, `nav` sidebar entry, `pane`
(right dock, 300 px) and `chip` in `statusBar.right`.

**Page (`SuperagentPage`), top to bottom:**

- **Header** — title, live counts (`N running · M waiting · Q queued`), ⚙
  Settings, GitHub button (status dot + repo), **New chat**, **Refresh**.
- **Settings panel** — collapsible, fed by `/settings` and `/models`; edits
  default model/provider (with **Inherit**), preset rows (max 12), max workers,
  max turns, `--yolo`; **Save** → `POST /settings`, then invalidates settings,
  models and chats.
- **GitHub panel** — collapsible; connection form on the left
  (`POST /github/connect`, `/github/settings`, `/github/poll`,
  `/github/disconnect`) and open PRs with **Work on this PR** on the right.
- **Onboarding card** — three steps while there are no sessions; dismissal is
  stored through `ctx.storage` (`onboarded`).
- **Body** — `SessionsRail` (All / Live / Review filter, `/chats`),
  `ChatCenter` (transcript merging `chat_messages` + `chat_events`;
  header with status/model/tokens/duration, PR badge, **Open in Hermes**,
  **Pop out** tile, **Diff**, **Comment on PR**; `DiffPanel`;
  `CommentBox` with editable preview and explicit **Post**; composer with
  preset chips, `ModelPicker`, repo path, Enter/Shift+Enter, **Stop**), and
  `LiveWorkers` (cards for queued/running/review/failed with 1 s client-side
  elapsed timers and a three-event mini-feed).
- **Side pane (`SuperagentPane`)** — `/tickets` + `/chats`: latest chats,
  "Needs you" ticket attention, recent tickets.
- **Status chip (`SuperagentChip`)** — `▶ N` running chats, `◆ M`
  `review`/`failed`; ticket attention only when the chats query has no data.

Polling budget: list 2 s live / 8 s quiet; open chat 1.5 s / 6 s; tickets 15 s;
diff 4 s only while open on a running turn; GitHub status/PRs 20 s only while
the panel or a PR badge is visible; models stale for 60 s. No WebSocket.

## 7. CLI surface

`hermes superagent <command> [--json]`:

| Command | Behaviour |
| :--- | :--- |
| `status` | mode, running/queued counts, "Needs you" list, recent tickets |
| `tick` | one routing pass; records tickets but never launches from the CLI |
| `ask "<request>"` | record a request as a ticket |
| `run <ticket-id>` | promote an `observed`/`blocked`/`failed` ticket to `queued` |
| `show <id>` | ticket detail, or a chat session (`S-…`) with messages/events |
| `chat "<prompt>" [--preset P] [--model M] [--provider P] [--repo R] [--title T] [--detach]` | start a headless turn (blocking unless `--detach`) |
| `sessions [--limit N]` | list chat sessions, newest first |
| `stop <session-id>` | cancel a queued/running turn |
| `models` | default model/provider plus the normalised preset list |
| `settings` | merged settings, token masked to `set`/`not set`; read-only |
| `github [status\|test\|connect\|disconnect\|prs\|poll\|work <n>]` | drive the watcher without HTTP |
| `diff <session-id>` | git status/stat/patch for the session repository |
| `comment <session-id> [--body TEXT \| --body-file PATH]` | post one summary comment to the session's PR |
| `doctor` | configuration, repository and worker-lifecycle checks |

Exit codes: `0` ok, `1` runtime/config error, `2` usage. Every subcommand
accepts `--json`.

## 8. Settings sources and precedence

`sa_settings.load_settings()` is the single source of truth for the dashboard,
the CLI and the runner:

```
DEFAULTS (mirrors plugin.yaml)
  < plugins.entries.superagent.settings in <HERMES_HOME>/config.yaml
  < <HERMES_HOME>/plugin-data/superagent/settings.json   (written by the panels)
  < environment variables
```

- `settings.json` also holds the GitHub token written by the connect form;
  writes are temp-file + `os.replace`, chmod 600 where supported, unknown keys
  rejected.
- The merged result is cached 5 s, keyed by both file mtimes plus an
  environment fingerprint; `save_settings()`/`clear_cache()` invalidate it.
- An empty or `${...}` token falls back to `SUPERAGENT_GITHUB_TOKEN`.
- `public_settings()` replaces the token with `github_token_set: bool`; no
  HTTP route or CLI `settings` output ever prints it.
- Environment overrides: `SUPERAGENT_MODE`, `SUPERAGENT_REPO`,
  `SUPERAGENT_REPO_ROOT`, `SUPERAGENT_MAX_WORKERS`,
  `SUPERAGENT_WORKER_TOOLSETS`, `SUPERAGENT_WORKER_YOLO`,
  `SUPERAGENT_CHAT_MAX_TURNS`, `SUPERAGENT_TURN_TIMEOUT_SECONDS`,
  `SUPERAGENT_DEFAULT_MODEL`, `SUPERAGENT_DEFAULT_PROVIDER`,
  `SUPERAGENT_MODEL_PRESETS`, `SUPERAGENT_HERMES_BIN`,
  `SUPERAGENT_CHAT_SOURCE`, `SUPERAGENT_GITHUB_*`.
- Presets accept `name=provider:model`, plain model strings, or
  `{name, model, provider}` objects; max 12, unique names (case-insensitive),
  every preset needs a model.

## 9. Tests and verification

```bash
# from the repository root
python -m pytest -q                 # 401 passed
hermes plugins validate .           # manifest + registrations
hermes plugins doctor --ci .        # CI-safe doctor
hermes plugins doctor .             # human-readable detail

# optional live checks
hermes superagent doctor
hermes superagent status --json
```

The suite is hermetic: `tests/test_runner.py` and `tests/test_hardening.py`
replace `Popen` with a fake, so no real `hermes` process is spawned.
`tests/test_settings.py` and `tests/test_github_api.py` point `HERMES_HOME` at
a throwaway directory. GitHub tests replace `_urlopen` and the `git_runner`
seam; diff tests use a real temporary git repository. `tests/test_release.py`
asserts that `plugin.yaml`, `dashboard/manifest.json`, the newest CHANGELOG
release and `dashboard/plugin_api.py` `VERSION` agree, and that the manifest
description mentions sessions — so a release bump cannot drift silently.

## 10. Install and reload

1. **Enable the plugin**
   ```bash
   hermes plugins enable superagent
   ```
   The gateway live-reloads registration, but new agent tools only appear in
   **new** chat/cron sessions.
2. **Desktop half** — Desktop → Settings → Plugins → Superagent (enable), then
   ⌘K → **Reload desktop plugins** if the tab, pane or chip is missing.
   This reload is enough for `desktop/plugin.js` changes.
3. **Backend changes need a gateway restart.** Python modules
   (`dashboard/plugin_api.py` and every `sa_*.py`) are imported by the gateway
   process; a route or handler edit is not visible until:
   ```bash
   hermes gateway restart
   ```
   Check with `hermes gateway status`. Never restart the gateway from inside a
   session it hosts — hand the user the command.
4. **Installed copy** — if `<HERMES_HOME>/plugins/superagent` is a junction to
   this checkout, edits are already live. If it is a copy, reinstall before
   reloading.
5. **No database migration step.** The chat schema is additive and
   `CREATE TABLE IF NOT EXISTS`; the plugin creates it on first use.

## 11. Known limitations

- **Workers never commit, push, merge or open a PR.** They edit the working
  tree and surface as `review` (or `idle` when resumable); that is deliberate,
  not an unfinished feature.
- **The GitHub allow-list is the security boundary.** Empty
  `github_allowed_users` means **nobody** can trigger work. Bots
  (`*[bot]`, `github-actions`, `dependabot`) and the agent's own login are
  ignored, so testing with the same account that owns the token will produce
  no sessions.
- **One watched repository and one token per plugin install.** Polling covers
  open PRs only, capped by `github_pr_limit`.
- **PR comments land in the PR conversation**, via the issue-comments
  endpoint; the plugin does not reply inside review threads or edit existing
  comments.
- **Gateway restart is required for backend route changes**; the desktop
  **Reload desktop plugins** action only reloads `plugin.js`.
- **Windows UTF-8.** Persisted text is UTF-8 and subprocess/git output is
  decoded UTF-8 with replacement. Tools that emit a legacy OEM code page can
  produce `�` in captured output; keep repository paths ASCII where possible
  or run with `PYTHONUTF8=1`.
- **`max_workers` is an in-process semaphore.** One gateway process is
  assumed; two gateways against the same database would each allow N workers.
- **Ticket workers still need an agent turn.** CLI/REST/slash only queue; the
  `Superagent watch` cron or a chat tool call does the launching.
- **`idle` is a reserved chat status.** It is defined in
  `sa_chats.STATUSES` but the runner never emits it; every successful turn is
  `review`. Follow-ups work from `review`, so there is no functional gap.
- **Chat UI is poll-based**, not token-streaming; the transcript updates on
  the next poll, and messages are read back capped at 200 per session.
- **No cheap-model triage routing yet.** Presets/defaults exist, but the router
  itself remains deterministic and token-free.
- **PR worktrees accumulate** under `<data_dir>/workspaces/pr-<n>`; nothing
  prunes them automatically.

## 12. Things not to do

- Do not collapse the dual-mode `try: from . import … / except ImportError`
  imports. They let the same modules load as a package (Hermes), top-level
  (dashboard/pytest) and `sa_plugin` (tests).
- Do not write runtime state into the repository. The database, settings,
  workspaces and logs belong under `<HERMES_HOME>/plugin-data/superagent/`.
- Do not return the GitHub token from a route, event, error or CLI settings
  output. Use `public_settings()` / `github_token_set`; scrub with
  `redact_secrets` before persisting.
- Do not replace `/chats` with `/sessions` (or vice versa). `/sessions` is the
  v0.1 ticket read model the side pane still consumes; chats are `/chats`.
- Do not launch ticket workers from the CLI, REST or slash commands. They have
  no parent turn; queue and let `superagent_tick`/`superagent_run` launch from
  an agent turn.
- Do not add commit/push/merge behaviour to a worker prompt or the comment
  flow. The human owns publication.
- Do not hand-copy `desktop/plugin.js` into `desktop-plugins/`; the Electron
  shell materialises the package itself and a second copy will drift.
- Do not add third-party dependencies to `sa_github.py` — it is stdlib
  `urllib` by design.
- Do not weaken repository containment, the 20k/2k/60k caps or secret
  redaction to make a test easier.
- Do not bump one version location alone; `tests/test_release.py` enforces the
  plugin/manifest/changelog/backend agreement.
