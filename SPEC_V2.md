# Superagent v0.2/v0.3 — chat sessions, headless workers and GitHub PR watch

Superagent 0.1 treated every request as a ticket: a rule-based router decided
whether work was worth doing, and a Hermes subagent did it. v0.2 keeps that
path and adds the missing half — a **chat you can actually talk to**, with one
live session per topic and a worker you can watch.

## Why subprocess workers

Hermes' subagent lifecycle (`agent/subagent_lifecycle.py`) only accepts a
launch from an active parent agent bound to a chat turn. The dashboard backend
is an HTTP handler: there is no turn to bind to, so `ctx.subagent_lifecycle`
cannot start work from a request. The supported pattern in this Hermes version
is the one the built-in kanban dispatcher uses: spawn a headless
`hermes chat` process and read its stream-json output. That is what
`sa_runner.Runner` does.

Workers are deliberately constrained:

- they work only inside the configured repository (`--in <repo>`);
- they never commit, push, merge or open a pull request;
- changes stay in the working tree for a human to review, which is why a
  successful turn ends in `review`, not `done`.

## Chat sessions (`sa_chats.py`)

One SQLite file (`<HERMES_HOME>/plugin-data/superagent/superagent.db`), new
additive tables next to the ticket tables:

| table           | holds                                                        |
|-----------------|--------------------------------------------------------------|
| `chat_sessions` | id, title, prompt, status, model, provider, repo, hermes_session_id, source, github_repo, pr_number, pr_url, pid, tokens, duration_ms, error, last_event, timestamps |
| `chat_messages` | one row per user/assistant message (content capped at 20 000 chars) |
| `chat_events`   | audit trail: created, queued, started, tool, tool_error, result, review, failed, cancelled (detail capped at 2 000 chars) |
| `gh_seen`       | GitHub dedupe keys (`key`, `seen_at`): comment ids, PR head SHAs, per-repo baselines |

Ids are readable and sequential (`S-0001`) and allocated from the existing
`state` table under `chat_seq` inside `BEGIN IMMEDIATE`. Titles come from the
first line of the prompt (80 chars) unless one is supplied.

Statuses: `queued` (waiting for a worker slot), `running`, `review` (a turn
finished; a human should look at the work), `idle` (turn finished, waiting for
the next message), `observed` (a recorded GitHub request nobody launched yet),
`failed`, `cancelled`.

## Runner (`sa_runner.py`)

```
hermes chat -q <prompt> -Q --format stream-json --source tool \
    -t file,terminal --yolo --max-turns N [-m MODEL] [--provider P] \
    [--in REPO] [--resume SESSION]
```

- `--source tool` is required: the gateway only accepts source tags it
  advertises (`cli`, `tool`, `oneshot`). `SUPERAGENT_CHAT_SOURCE` overrides it.
- The first turn of a session sends `build_prompt(...)` (repo rules, smallest
  change, run tests, no commits, <=200-word summary). Resume turns send the
  raw user message.
- stream-json mapping: `system` -> hermes_session_id/model, `text` deltas ->
  one assistant message, `tool_use` -> `tool` event, `tool_result` with
  `is_error` -> `tool_error`, `result` -> tokens/duration/session id, anything
  else ignored. `parse_event()` never raises on unknown lines.
- `max_workers` is enforced with a `threading.Semaphore`; extra turns stay
  `queued` with their thread parked. `stop()` kills the process tree
  (`taskkill /T /F /PID` on Windows, terminate-then-kill elsewhere).
- Every turn ends by setting `finished_at`, clearing `pid` and recording an
  event; a non-zero exit becomes `failed` with the best available error
  (result `error`, then stderr without the CLI's session trailer).

## HTTP surface (`dashboard/plugin_api.py`)

| method | path                          | behaviour |
|--------|-------------------------------|-----------|
| GET    | `/models`                     | default model/provider + configured presets; `presets` is `[]` when none are set and `models` is preset models in order plus the default model (never a hardcoded list) |
| GET    | `/chats?limit=50&status=`      | `{db_exists, sessions, counts, running}`; live rows add `recent_events` (last 3, newest first, detail 160 chars) and `elapsed_seconds` (running/queued, else `null`); `status=running,queued` narrows rows only |
| POST   | `/chats`                      | create + launch a chat; 400 on empty/oversized prompt, 200 `{ok:false}` when the worker cannot start (session is stored as `failed`) |
| GET    | `/chats/{id}`                 | session + messages + events; 404 unknown |
| POST   | `/chats/{id}/messages`        | append the user message and resume the Hermes session; 409 while a turn runs; with no `hermes_session_id` a fresh chat is created and `previous_session_id` returned |
| POST   | `/chats/{id}/model`           | set `{model, provider}` for future turns; 400 when both are empty, 409 while queued/running, 404 unknown |
| POST   | `/chats/{id}/stop`            | cancel a queued/running turn; 409 when nothing is running |
| GET    | `/chats/{id}/diff`            | `{ok, repo, files:[{status,path}], stat, patch, truncated, error}` from `git status`/`diff --stat`/`diff`; patch capped at 60 000 chars with a trailing marker; 404 unknown, 400 no repo / repo gone |
| GET    | `/chats/{id}/comment-preview`  | the body `POST /comment` would send (`{ok, body, length, pr_number, github_repo, url}`); no network, no token needed; 400 without a PR |
| POST   | `/chats/{id}/comment`         | post `{body?}` (default: `sa_diff.default_comment_body`) to the PR; 400 no PR/repo/token, 502 on a GitHub failure (redacted); records a `commented` event with the URL |

All chat routes are defensive: a missing database yields `db_exists: false`
for lists, and every handler returns JSON.

`GET /sessions` keeps its 0.1 meaning (ticket rows for the desktop sessions
grid) because `desktop/plugin.js` still reads it; chats therefore live under
`/chats` instead of replacing it. The v0.2 desktop work will read `/chats`.

The Runner is a lazy module-level singleton keyed by database path
(`_get_runner()`), so repeated requests share one semaphore and one set of
threads. Tests patch `_db_path` and `_get_runner`.

## GitHub watch (v0.3)

Three modules add a GitHub path without touching the ticket schema:

- **`sa_settings.py`** — one loader for the dashboard and the CLI. Layers:
  `DEFAULTS` (mirroring `plugin.yaml`) < `plugins.entries.superagent.settings`
  in the Hermes `config.yaml` < `<data_dir>/settings.json` (written by the
  connect form) < environment. The merged result is cached for 5 s keyed by
  both file mtimes and the environment. `save_settings()` merges a patch into
  `settings.json` with a temp file + `os.replace` and chmod 600 where supported;
  unknown keys are rejected. An empty or `${...}` token falls back to
  `SUPERAGENT_GITHUB_TOKEN`; the token is never returned by `public_settings()`
  or an HTTP route.
- **`sa_github.py`** — stdlib `urllib` REST client bound to one repository:
  `me`, `repo_info`, `list_open_prs`, `get_pr`, `list_comments` (issue +
  review comments merged, oldest first), `post_comment`. `GitHubError` carries
  the HTTP status and never the token. `is_trigger()` accepts `@<login>`,
  `@superagent`, `/superagent`, or a `superagent:` line; `is_bot()` knows the
  `[bot]` suffix plus `github-actions`/`dependabot`.
  `prepare_pr_checkout()` fetches `+refs/pull/<n>/head:refs/superagent/pr-<n>`
  into the local clone with a one-shot `http.extraHeader` credential, then
  creates a detached `git worktree add --detach <workspace>/pr-<n>`; the git
  call is injectable for tests. `build_pr_prompt()` writes the PR context, the
  triggering comment, the checkout path, the `git diff origin/<base>...HEAD`
  hint, the no-commit rules and the 200-word summary request.
- **`sa_watcher.py`** — `GitHubWatcher(store, runner, settings_getter,
  client_factory)`. `poll_once()` fetches the open PRs, reads every PR's
  comments, and acts only on unseen items. The first successful poll for a
  repository is a **baseline**: it marks every comment and PR head seen without
  creating sessions. Later, an unseen comment from an allowed human that
  triggers is marked seen and becomes a session; a new PR head becomes one
  review session per SHA when `github_watch_new_prs` is on. In `observe` mode
  sessions are `observed`; in `auto` mode they are `queued`, the PR checkout is
  prepared when `repo_path` is set, and the runner is launched with the
  checkout (falling back to `repo_path`, or failing the session). A single
  daemon thread polls every `github_poll_seconds`; `start_background()` is
  idempotent and `last_poll` is mirrored to `state['gh_last_poll:<repo>']`.
  Dedupe keys: `gh_baseline:<repo>`, `gh_comment:<repo>:<kind>:<id>`,
  `gh_head:<repo>:<n>:<sha>`.

HTTP surface (`dashboard/plugin_api.py`):

| method | path | behaviour |
|--------|------|-----------|
| GET    | `/github/status` | watcher status; lazily starts the poller |
| POST   | `/github/test` | `{repo?, token?}` -> masked `{login, full_name, default_branch, private, open_pr_count}` |
| POST   | `/github/connect` | `{repo, token?}` -> validate, save, reset the baseline, return status |
| POST   | `/github/settings` | `{allowed_users?, watch_mode?, watch_new_prs?, poll_seconds?, agent_login?}` -> validate + save watch settings, rebuild the watcher, return `{ok, status}` |
| POST   | `/github/disconnect` | clear repo + token, stop polling |
| POST   | `/github/poll` | one pass: `{configured, polled, created, errors}` |
| GET    | `/github/prs` | `{configured, prs}` |
| POST   | `/github/prs/{number}/work` | `{request?}` -> create + launch a session for that PR |

`_get_runner()` and `/models` read `load_settings()`, so plugin-config
`default_model`, `default_provider`, `max_workers`, `worker_yolo` and
`chat_max_turns` apply; environment variables still win. The runner's allowed
roots include `<data_dir>/workspaces` so a GitHub checkout can be passed to
`--in` and resumed from the chat panel.

## Settings and environment

New `plugin.yaml` settings: `default_model`, `default_provider`,
`model_presets`, `worker_yolo`, `chat_max_turns`, and for GitHub
`github_watch_mode`, `github_watch_new_prs`, `github_poll_seconds`,
`github_pr_limit`. Environment overrides with the same meaning:
`SUPERAGENT_DEFAULT_MODEL`, `SUPERAGENT_DEFAULT_PROVIDER`,
`SUPERAGENT_MODEL_PRESETS`, `SUPERAGENT_WORKER_YOLO`,
`SUPERAGENT_CHAT_MAX_TURNS`, `SUPERAGENT_GITHUB_WATCH_MODE`,
`SUPERAGENT_GITHUB_WATCH_NEW_PRS`, `SUPERAGENT_GITHUB_POLL_SECONDS`,
`SUPERAGENT_GITHUB_PR_LIMIT`, `SUPERAGENT_GITHUB_REPO`,
`SUPERAGENT_GITHUB_TOKEN`, `SUPERAGENT_GITHUB_AGENT_LOGIN`,
`SUPERAGENT_GITHUB_ALLOWED_USERS`, plus `SUPERAGENT_HERMES_BIN`,
`SUPERAGENT_CHAT_SOURCE`, `SUPERAGENT_WORKER_TOOLSETS`, `SUPERAGENT_REPO`,
`SUPERAGENT_MAX_WORKERS`.

`model_presets` defaults to `[]`. An empty list is not replaced by a built-in
catalogue: `GET /models` returns `presets: []` and the composer hides its chip
row, so a user is never offered a model id the machine may have no credentials
for. The flat `models` list still names the configured `default_model`.

## Build plan

1. **Backend runner** — `sa_chats.py`, `sa_runner.py`, `/chats` + `/models`,
   settings, tests. *(done)*
2. **Desktop chat UI** — a real composer and transcript in `desktop/plugin.js`
   on `/chats`, per-session model/provider picker from `/models`, stop button.
   *(done)*
3. **Live session grid** — poll `/chats`, show status, tokens, elapsed time and
   the last tool event per session; click through to the transcript. *(done:
   per-card 3-event feed with 1s client timers, All/Live/Review rail filter,
   in-chat model switch via `POST /chats/{id}/model`)*
4. **GitHub backend** — connect a repo (token + allowed users), watch pull
   requests and comments, store `github_repo`/`pr_number`/`pr_url` on sessions,
   and check a PR out as an isolated worktree. *(done: `sa_settings.py`,
   `sa_github.py`, `sa_watcher.py`, `/github/*`)*
5. **GitHub UI + CLI** — a **GitHub** header button (status dot + repo), a
   connect/settings panel with the open-PR list and "Work on this PR", `#123`
   PR badges on the transcript, rail and worker cards, and
   `hermes superagent github status|test|connect|disconnect|prs|poll|work`.
   *(done: `/github/settings` validates the allow-list, mode and 15–3600 s
   poll interval; the PR list polls at 20 s only while the panel is open and
   the status only while the panel or a PR badge is visible. The CLI uses
   `sa_settings`/`sa_github`/`GitHubWatcher` directly — no HTTP — and `work`
   creates a `github_pr` session, prepares the PR checkout when `repo_path` is
   set, launches blocking and prints the session/transcript id.)*
6. **CLI polish** — `hermes superagent chat`, `sessions`, `show`, `stop`,
   `models`, all with `--json`; stable exit codes.
7. **Presets and routing** — named model presets, per-session defaults, cheap
   model for triage and strong model for implementation. *(done for presets +
   defaults: `sa_settings.normalize_presets` accepts
   `name=provider:model` strings, plain model strings and
   `{name, model, provider}` objects; `GET/POST /settings` expose and validate
   the eight panel-editable keys; `GET /models` returns `presets` alongside the
   flat `models` list; `POST /chats` resolves `preset` (explicit model/provider
   wins) and records a `preset` event; the desktop page has a ⚙ Settings panel,
   composer preset chips and a dismissible three-step onboarding card; the CLI
   has `chat --preset` and a read-only `settings` command. Triage routing —
   cheap model for assessment, strong model for implementation — remains
   future work.)*
8. **Robustness and security** — path checks, prompt/response caps, runaway
   watchdog, rate limits, redaction of tokens in events. *(The handoff adds one
   more read path: `sa_diff.collect_diff` runs git without a shell, never
   raises, redacts stderr, caps the patch at 60 000 characters and resolves the
   session repository through the same containment check as a worker turn;
   `POST /chats/{id}/comment` caps the body at 20 000, never echoes the token
   and never commits, pushes or edits the PR.)*
9. **UX and docs** — onboarding, empty states, README/SPEC refresh, skill
   update for operators. *(Done: the three-step first-run card, honest empty
   presets (no chip row and no hardcoded fallback catalogue), one completion
   toast per `queued`/`running` → `review`/`failed` transition, a chat-aware
   status-bar chip, the failed session's error in the transcript header and
   worker mini-feed, and phase-accurate reap wording (`while queued` /
   `while running`). The README now opens with a five-minute quick start and a
   feature list, the operator skill covers every v0.2/v0.3 surface plus the
   ticket path, and `CHANGELOG.md` records 0.3.0.)*
10. **Final polish** — performance pass (polling, DB indexes), full test sweep,
    release notes and a version bump everywhere. *(Done: list polling 2 s while
    live / 8 s quiet, detail 1.5 s / 6 s, diff 4 s only while its panel is open
    on a running turn, GitHub 20 s only while its panel or a PR badge is
    visible; `chat_sessions`, `chat_messages` and `chat_events` indexes;
    `CHANGELOG.md` and `plugin.yaml`/`VERSION` at 0.3.0; full pytest sweep
    green. Triage routing — a cheap model for assessment and a strong one for
    implementation — remains future work.)*

## Guardrails

- stdlib only: `sqlite3`, `subprocess`, `json`, `threading`, `urllib`
  (PyYAML is an optional import used only to read `config.yaml`).
- No runtime state in the repository; the database stays in the plugin data
  directory.
- Existing ticket endpoints and tables are unchanged; the chat schema is
  additive and `CREATE TABLE IF NOT EXISTS`.
- Workers are launch-only: review, commit and push remain human actions.
