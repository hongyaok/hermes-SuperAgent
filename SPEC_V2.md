# Superagent v0.2 — chat sessions and headless workers

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

Ids are readable and sequential (`S-0001`) and allocated from the existing
`state` table under `chat_seq` inside `BEGIN IMMEDIATE`. Titles come from the
first line of the prompt (80 chars) unless one is supplied.

Statuses: `queued` (waiting for a worker slot), `running`, `review` (a turn
finished; a human should look at the work), `idle` (turn finished, waiting for
the next message), `failed`, `cancelled`.

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
| GET    | `/models`                     | default model/provider + preset list |
| GET    | `/chats?limit=50`             | `{db_exists, sessions, counts, running}` |
| POST   | `/chats`                      | create + launch a chat; 400 on empty/oversized prompt, 200 `{ok:false}` when the worker cannot start (session is stored as `failed`) |
| GET    | `/chats/{id}`                 | session + messages + events; 404 unknown |
| POST   | `/chats/{id}/messages`        | append the user message and resume the Hermes session; 409 while a turn runs; with no `hermes_session_id` a fresh chat is created and `previous_session_id` returned |
| POST   | `/chats/{id}/stop`            | cancel a queued/running turn; 409 when nothing is running |

All chat routes are defensive: a missing database yields `db_exists: false`
for lists, and every handler returns JSON.

`GET /sessions` keeps its 0.1 meaning (ticket rows for the desktop sessions
grid) because `desktop/plugin.js` still reads it; chats therefore live under
`/chats` instead of replacing it. The v0.2 desktop work will read `/chats`.

The Runner is a lazy module-level singleton keyed by database path
(`_get_runner()`), so repeated requests share one semaphore and one set of
threads. Tests patch `_db_path` and `_get_runner`.

## Settings and environment

New `plugin.yaml` settings: `default_model`, `default_provider`,
`model_presets`, `worker_yolo`, `chat_max_turns`. Environment overrides with
the same meaning: `SUPERAGENT_DEFAULT_MODEL`, `SUPERAGENT_DEFAULT_PROVIDER`,
`SUPERAGENT_MODEL_PRESETS`, `SUPERAGENT_WORKER_YOLO`,
`SUPERAGENT_CHAT_MAX_TURNS`, plus `SUPERAGENT_HERMES_BIN`,
`SUPERAGENT_CHAT_SOURCE`, `SUPERAGENT_WORKER_TOOLSETS`, `SUPERAGENT_REPO`,
`SUPERAGENT_MAX_WORKERS`.

## Build plan

1. **Backend runner** — `sa_chats.py`, `sa_runner.py`, `/chats` + `/models`,
   settings, tests. *(this iteration)*
2. **Desktop chat UI** — a real composer and transcript in `desktop/plugin.js`
   on `/chats`, per-session model/provider picker from `/models`, stop button.
3. **Live session grid** — poll `/chats`, show status, tokens, elapsed time and
   the last tool event per session; click through to the transcript.
4. **GitHub backend** — connect a repo (token + allowed users), watch pull
   requests and checks, store `github_repo`/`pr_number`/`pr_url` on sessions.
5. **GitHub UI + CLI** — connect/pin a repo from the panel, `/superagent pr`
   and a CLI subcommand to open the PR for a reviewed session.
6. **CLI polish** — `hermes superagent chat`, `sessions`, `show`, `stop`,
   `models`, all with `--json`; stable exit codes.
7. **Presets and routing** — named model presets, per-session defaults, cheap
   model for triage and strong model for implementation.
8. **Robustness and security** — path checks, prompt/response caps, runaway
   watchdog, rate limits, redaction of tokens in events.
9. **UX and docs** — onboarding, empty states, README/SPEC refresh, skill
   update for operators.
10. **Final polish** — performance pass (polling, DB indexes), full test sweep,
    release notes and a 0.2.0 version bump everywhere.

## Guardrails

- stdlib only: `sqlite3`, `subprocess`, `json`, `threading`.
- No runtime state in the repository; the database stays in the plugin data
  directory.
- Existing ticket endpoints and tables are unchanged; the chat schema is
  additive and `CREATE TABLE IF NOT EXISTS`.
- Workers are launch-only: review, commit and push remain human actions.
