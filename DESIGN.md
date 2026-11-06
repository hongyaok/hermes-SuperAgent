# Superagent Design Notes

## 1. Core Thesis: Human Attention Is the Scarcest Resource

Inspired by DeepSeek's `dsh-superagent`, `superagent` is built around a single constraint: **take as little human attention as possible, and deliver as much verified value as possible.**

Most autonomous coding setups fail in one of two ways:

1. They burn tokens and human attention on every stray message, acknowledgement or vague discussion thread.
2. They act with unchecked autonomy — committing, pushing or opening noisy pull requests that cost humans more time to untangle than writing the change directly.

`superagent` answers both with a **deterministic, zero-token router**, **isolated, non-committing workers**, and an **observe-first operating model**. The worker can be either a Hermes subagent (the ticket path) or a headless `hermes chat` process (the chat path); in both cases the human still owns publication.

---

## 2. Two Execution Paths, One Database

| | Ticket path | Chat path |
| :--- | :--- | :--- |
| Trigger | router accepts an inbound request | a person sends a message, or the GitHub watcher opens a session |
| Worker | Hermes subagent via `ctx.subagent_lifecycle` | headless `hermes chat` subprocess |
| Launch context | only an active agent turn (tool call or cron) | any process, including the dashboard backend |
| Record | `tickets` + `events` | `chat_sessions` + `chat_messages` + `chat_events` |
| Main states | `observed`, `queued`, `running`, `review`, `failed`, `blocked` | `queued`, `running`, `review`, `observed`, `failed`, `cancelled` |
| URL surface | `/tickets`, `/ask`, `/run`, `/tick`, `/sessions` | `/chats`, `/models`, `/settings`, `/github/*` |

Both paths share one SQLite file, one settings loader (`sa_settings.py`) and one attention budget. A chat turn never creates a ticket, and a ticket never appears as a chat session; `/sessions` keeps its v0.1 meaning (ticket rows), so the two namespaces stay honest.

---

## 3. Thin Router vs. Isolated Workers

### Deterministic, zero-token router (`sa_assessor.py`)

The router (`Engine` in `sa_engine.py` + `assess()` in `sa_assessor.py`) never calls a language model to decide whether an inbound message should become work.

Every inbound message passes through fast, auditable heuristics:

- **Immediate `ignore` (0 tokens, 0 human interruption):**
  - empty or very short messages (`< 8` characters);
  - explicit opt-out markers: `[skip-agent]`, `[no-agent]`, `@agent ignore`, `/noagent`;
  - pure acknowledgements: `"thanks"`, `"thank you"`, `"thx"`, `"lgtm"`, `"looks good"`, `"+1"`, `"👍"`;
  - unaddressed comments when `allow_proactive` is `false`.
- **Route to `needs_human` → `blocked`:** design/architecture questions (`"should we"`, `"what do you think"`, `"which approach"`, `"architecture"`, `"thoughts?"`) without a concrete code change or stack trace. A worker should not guess on an open design question.
- **Route to `accept` → `observed` (observe) or `queued` (auto):**
  - direct requests (`explicit=True`) via local ask or an `@mention` from an allowed user;
  - unaddressed comments when `allow_proactive=True` that contain an action verb (`fix`, `add`, `implement`, `refactor`, `test`, `debug`, …) or code/traceback hints.

The same rules are used by `POST /ask`; no route hardcodes `accept`.

### SQLite store (`sa_store.py`, `sa_chats.py`)

All state lives in a single WAL-mode file at `<HERMES_HOME>/plugin-data/superagent/superagent.db` (via `sa_settings.data_dir()`). A fresh connection is opened per operation so CLI commands, gateway REST endpoints and agent turns never contend over thread-bound sockets.

Eight focused tables:

- `tickets` — sequential human-readable ids (`T-0001`), source metadata, requester, request text, `status`, router `verdict`/`reason`, serialized worker `handle`, `result`, `error`, timestamps.
- `events` — append-only ticket audit (`created`, `assessed`, `promoted`, `launched`, `launch_failed`, `review`, `failed`, `reply_failed`).
- `state` — key/value cursors and sequences (`ticket_seq`, `chat_seq`, `github_since`, `gh_last_poll:<repo>`).
- `seen` — ticket source dedupe (`source_key`, e.g. `github:12345`).
- `chat_sessions` — conversation identity and live state: title, prompt, status, model/provider, repo, `hermes_session_id`, source, PR fields, pid, tokens, duration, error, last event, timestamps.
- `chat_messages` — user/assistant text (20 000-char cap).
- `chat_events` — chat audit: created, queued, started, tool, tool_error, result, review, failed, cancelled, preset, github, commented (2 000-char detail cap).
- `gh_seen` — GitHub dedupe (`gh_baseline:<repo>`, `gh_comment:<repo>:<kind>:<id>`, `gh_head:<repo>:<n>:<sha>`).

Chat ids are `S-0001`, allocated from `state.chat_seq` inside `BEGIN IMMEDIATE`; titles come from the first prompt line (80 chars).

### Ticket lifecycle and why `review` is terminal

```text
inbound request
   │
   ├─► [verdict: ignore]      ──► ignored   (silent)
   ├─► [verdict: needs_human] ──► blocked   (surfaces in "Needs you")
   └─► [verdict: accept]
          │
          ├─► (mode: observe) ──► observed  (surfaces in "Needs you"; awaits superagent_run)
          └─► (mode: auto)    ──► queued
                                    │
                                    ▼
                                 running    (worker subagent active, guarded by watchdog)
                                    │
                        ┌───────────┴───────────┐
                        ▼                       ▼
                     review                  failed
              (changes in working      (error or watchdog
               tree; awaits human)      timeout; in "Needs you")
```

When a worker succeeds, the ticket moves to **`review`**, not `done`. `build_worker_prompt()` in `sa_executor.py` forbids `git commit`, `git push`, merging and opening pull requests: a human operator owns everything committed or published under their identity. Leaving tested modifications in the local working tree lets them inspect `git status`/`git diff`, read the summary and decide.

---

## 4. Why Two Worker Mechanisms

### Ticket workers: `ctx.subagent_lifecycle`

Ticket workers are executed only through `PluginContext.subagent_lifecycle` (`SubagentExecutor` in `sa_executor.py`), constructing a `SubagentLaunchRequest`:

- **Public, plugin-safe contract** — no brittle subprocess invocation, CLI re-entry or private agent-loop import.
- **`role="leaf"`** — a worker cannot recursively spawn sub-subagents.
- **Scoped `allowed_toolsets`** — defaults to `("file", "terminal")`: enough to edit and test, nothing unrelated.
- **`correlation_id=ticket_id`** — links the Hermes lifecycle to `T-xxxx` for status, result and cancel.
- **Watchdog** — `_reap()` cancels a `running` worker past `max_runtime_seconds` (default 1800) via `lifecycle.cancel(...)` and marks it `failed`.

`get_active_subagent_parent()` is bound only during an active agent turn (`agent/turn_facade.py` → `bind_subagent_parent`). So chat tool calls (`superagent_tick`, `superagent_run`) and cron runs (`Superagent watch`) can launch ticket workers; CLI, slash commands and REST handlers pass `launch=False` and merely queue.

### Chat workers: headless `hermes chat`

The dashboard backend is an HTTP handler: there is no agent turn to bind, so `ctx.subagent_lifecycle` cannot start work from `POST /chats`. `sa_runner.Runner` uses the supported pattern (the same one the built-in kanban dispatcher uses):

```
hermes chat -q <prompt> -Q --format stream-json --source tool \
    -t file,terminal --yolo --max-turns N [-m MODEL] [--provider P] \
    [--in REPO] [--resume SESSION]
```

- One subprocess and one daemon thread per turn; `max_workers` is a `threading.Semaphore`, so extra turns stay `queued` with their thread parked.
- The first turn sends `build_prompt(...)` (repository rules, smallest change, run tests, no commits, ≤200-word summary); resume turns send the raw user message.
- Stream-json mapping: `system` → `hermes_session_id`/model, `text` deltas → one assistant message, `tool_use` → `tool` event, `tool_result` with `is_error` → `tool_error`, `result` → tokens/duration/session id; unknown lines are ignored rather than raising.
- `stop()` kills the process tree; the runaway reaper cancels a turn older than `turn_timeout_seconds` (default 1800, `0` disables) and records whether it was `queued` or `running`.
- Chat workers follow the same publication rule as ticket workers: changes stay in the working tree; the turn ends in `review` (or `idle` when resumable), never a commit.

---

## 5. Why `observe` Is the Default

Autonomy must be earned on each repository before it is granted.

- **Tickets:** `mode: observe` records accepted requests as `observed`; nothing launches until a human calls `superagent_run` or switches to `auto`.
- **GitHub:** `github_watch_mode: observe` records PR mentions as `observed` sessions. The connect form starts in Observe; switching to Auto is an explicit act.
- For the ticket path, observe mode posts a single intake comment (`Recorded as T-xxxx (observe mode). A human can approve it from the Superagent panel.`) so collaborators know the request was seen without starting compute. The GitHub watcher records its `observed` sessions without posting.
- The first successful poll for a new repository is a **baseline**: existing comments and PR heads are marked seen without creating sessions, so connecting a busy repository does not replay its history.

---

## 6. The v0.4 Chat Workspace

### Sessions

A chat session is a durable conversation, not a ticket. `ChatStore.create_session()` stores the prompt, title, model/provider, repository and status; `Runner.launch_turn()` streams one turn into `chat_messages` and `chat_events`. A follow-up appends the user message and resumes `hermes_session_id`; when there is no resumable session, `POST /chats/{id}/messages` creates a fresh chat and returns `previous_session_id`.

Statuses are deliberately narrow: `queued` (waiting for a worker slot), `running`, `review` (a turn finished and a human should look), `observed` (recorded GitHub request), `failed`, `cancelled`. `idle` is defined in the status enum as a "finished, waiting for the next message" state, but the runner does not currently emit it: every successful turn ends in `review`, which also accepts a follow-up. Treat `idle` as reserved until the runner starts using it.

### Live grid

Every queued/running/review/failed chat is a card in the right-hand **Live workers** column: status, model, a client-side elapsed timer, tokens, and a three-event mini-feed. A card is a projection of the same `/chats` list, not a separate query, so the grid costs one poll. The rail filters All / Live / Review client-side without unmounting the open transcript; list polling is 2 s while anything runs and 8 s when quiet.

### Models, presets and settings

- A session owns `model`/`provider`; `POST /chats/{id}/model` changes them for future turns and is rejected with 409 while a turn is queued/running.
- Presets are named `(model, provider)` pairs normalised by one function (`sa_settings.normalize_presets`) for `config.yaml`, `settings.json`, the HTTP API and the CLI. Three spellings are accepted; the cap is 12 with unique names.
- An empty preset list means **no presets**, not a built-in catalogue. `GET /models` returns `presets: []` and the composer hides the chip row: offering a model the machine may have no credentials for guarantees a failed worker.
- `POST /chats` resolves `preset` after explicit `model`/`provider` and before defaults; a `preset` event records the choice.
- The ⚙ Settings panel edits mode, repository, default model/provider, presets, `max_workers`, `worker_yolo` and `chat_max_turns` through `GET/POST /settings`. The panel, CLI and backend agree because they read the same merged settings.

### Completion toasts and the status chip

The page compares consecutive `/chats` polls. A transition from `queued`/`running` to `review` or `failed` raises exactly one `host.notify` toast; the first poll only seeds the map, so mounting the page does not replay old completions, and `cancelled` is silent. The status chip speaks for chats (`▶ N` running, `◆ M` review/failed) and falls back to ticket attention only when chats have never loaded.

---

## 7. GitHub Watch

One repository, one token, one polling thread.

### Connection and settings

`sa_settings` stores the repository, token, agent login, allow-list and watch knobs in `settings.json` (the connect form's write path), layered over `config.yaml`. `sa_github.GitHubClient` is a stdlib `urllib` REST client bound to that repository: `me`, `repo_info`, `list_open_prs`, `get_pr`, `list_comments` (issue + review comments, oldest first), `post_comment`. `GitHubError` carries the HTTP status and never the token.

### Baseline, dedupe and triggers

- The first complete poll is a baseline: everything present is marked seen and creates nothing.
- `gh_seen` keys (`gh_comment:…`, `gh_head:…`, `gh_baseline:…`) survive restarts. An unseen comment is marked seen **before** it is judged, so one malformed comment cannot replay forever.
- A trigger is `@<agent-login>`, `@superagent`, `/superagent`, or a line starting `superagent:`. Bots (`*[bot]`, `github-actions`, `dependabot`) and the agent's own login are ignored.
- `github_allowed_users` is an allow-list and an empty list means **nobody**. This is a deliberate fail-closed default.
- `github_watch_new_prs` creates one review session per new head SHA; heads are tracked even while the flag is off so turning it on does not replay history.

### Observe vs auto and PR worktrees

`observe` creates an `observed` session with the PR context and stops. `auto` creates it `queued` and launches the runner. With `repo_path` set, `prepare_pr_checkout()` fetches `+refs/pull/<n>/head:refs/superagent/pr-<n>` into the local clone using a one-shot `http.extraHeader` credential, then creates a detached `git worktree` at `<data_dir>/workspaces/pr-<n>`. The user's checkout and branch are never touched. A failed checkout falls back to `repo_path` with a `github` warning event; with no usable repository the session is marked `failed`.

### Polling

One daemon thread polls every `github_poll_seconds` (minimum 5, default 90); `start_background()` is idempotent and `last_poll` is mirrored to `state['gh_last_poll:<repo>']`. `github_pr_limit` caps how many open PRs are inspected per pass. The desktop polls `/github/status` and `/github/prs` only while the panel is open or a PR badge is on screen.

---

## 8. Review Handoff: Diff and Comment

Closing the loop does not require a push.

### Diff (`sa_diff.collect_diff`)

`GET /chats/{id}/diff` runs three git commands in the session's repository: `git status --porcelain`, `git diff --stat`, and `git diff` (plus a staged section when the index has changes). Git is invoked as an **argv list with no shell**, so a path with a space or quote cannot become a command. Every failure — missing directory, not a git checkout, missing binary, timeout — returns `{ok: false, error}` instead of raising; stderr is redacted.

The patch is capped at 60 000 characters with an explicit `truncated` flag and a trailing `… diff truncated …` marker. The repository path passes through the same containment check as a worker turn, so a session cannot point the diff at an arbitrary directory. The panel is lazy: no request until it opens, and a 4 s refetch only while the turn is running.

### Comment (`sa_diff.default_comment_body` + `POST /chats/{id}/comment`)

The preview endpoint composes `Superagent finished <id> — <title>`, the last assistant message, and `Files changed: N (+X/-Y)` from the diff stat, clipped to 20 000 characters; it makes no network call and needs no token. The UI shows that body in an editable textarea and posts only when the human presses **Post**. The post calls exactly one REST endpoint (`POST /repos/{repo}/issues/{n}/comments`); there is no commit, push, merge, PR edit or review-thread reply anywhere in the flow.

---

## 9. Safety Model

The plugin is designed so that the worst failure is a bad diff a human can discard — not a leaked credential or a published change.

- **Containment.** Every repository path is resolved with `Path.resolve(strict=True)`; paths that do not exist, are not directories, contain `..`, or resolve outside `SUPERAGENT_REPO_ROOT` (default: the configured repo, plus `<data_dir>/workspaces` for PR checkouts) are rejected. Only the resolved path reaches `argv`/`cwd`.
- **Caps.** Prompts 20 000 chars, stored assistant text 20 000, event detail 2 000, diff patch 60 000, posted comment 20 000. Over-limit prompts are rejected; over-limit output is truncated before the write, so a runaway worker cannot bloat the database.
- **Secret redaction.** GitHub (`ghp_`, `gho_`, `github_pat_`), OpenAI/Slack-style keys, `Authorization:` values and bare `Bearer` tokens are replaced with `[redacted]` before messages, events, errors, prompts and ticket fields are persisted. The token itself is never returned by a route; `public_settings()` reports `github_token_set` only.
- **Observe by default.** Both ticket intake and GitHub watch start in observe. Auto is an explicit switch.
- **No auto-push.** Workers cannot commit, push, merge or open a PR; the comment endpoint posts one comment after a click. The plugin has no code path that publishes a branch.
- **Runaway guard.** A queued/running turn past `turn_timeout_seconds` is cancelled by the reaper, even after a server restart, and the reason names the phase.
- **Fail closed on GitHub.** Empty allow-list = nobody; bots and self-comments are ignored; a malformed baseline fetch leaves the baseline open for a retry rather than treating history as new.

---

## 10. Attention-Frugal UI Across Every Surface

Every user-facing surface filters tickets through `NEEDS_ATTENTION = ("observed", "blocked", "review", "failed")`:

- **CLI & slash (`sa_cli.py`):** `render_status()` prints the `"Needs you:"` section first, or `"Nothing needs you."`; ignored and routine `queued`/`running` noise never clutter it. `run <id>` promotes to `queued` (the ticket worker starts on the next agent turn); `show <id>` renders the full ticket plus audit history, or a chat transcript.
- **Automation blueprint (`__init__.py`):** `Superagent watch` calls `superagent_tick(wait=true)` and reports only `blocked`, `review`, `failed` or `observed` — otherwise one line: `'Superagent: nothing needs you.'`
- **Desktop page:** the chat rail and live grid answer "what is running / what needs me"; the diff and comment controls appear only on a finished session with a repository/PR. The chip shows only running and waiting counts.
- **Desktop side pane:** latest chats, then ticket attention, then recent tickets.
- **Completion toasts:** exactly one notification per completed/failed turn, never a replay on page load.

The ticket read model (`Engine.sessions()` / `get_ticket_detail()`) remains a pure `SELECT` layer over the ticket schema — no migration, no behavior change — so the side pane can show previews, event counts and elapsed time without touching the chat tables.
