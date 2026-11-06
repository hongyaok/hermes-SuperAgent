# Superagent (`superagent`)

`superagent` is an attention-frugal router/worker coding plugin for Hermes, inspired by DeepSeek's `dsh-superagent`.

It separates **routing** from **execution**:
- **Sources** (`LocalSource` for chat/CLI requests and `GitHubSource` for GitHub issue comment mentions) produce inbound requests.
- A **deterministic, zero-token router** (`sa_assessor.py`) classifies each request (`accept`, `ignore`, `needs_human`).
- **SQLite** (`<HERMES_HOME>/plugin-data/superagent/superagent.db`) persists tickets (`T-0001`, `T-0002`, ...), event history, source deduplication, and polling cursors.
- **Isolated Hermes worker subagents** launched via `ctx.subagent_lifecycle` perform the coding work inside your repository and leave uncommitted changes in the working tree for human review (`review` state).

Nothing inside the plugin runs its own LLM or spawns rogue subprocesses. By default, the plugin runs in `observe` mode so it only records tickets until you explicitly approve them or switch to `auto`.

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

---

## Usage Across Surfaces

> **Important Architectural Rule:** Worker subagents can **only** be launched from an active Hermes agent turn (an interactive chat session or a scheduled cron turn). `ctx.subagent_lifecycle` requires an active parent session (`bind_subagent_parent`). The CLI, slash commands, and REST endpoints can inspect state and queue tickets, but they never launch workers directly (`launch=False`).

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

# Verify configuration, repo existence, and worker lifecycle availability
hermes superagent doctor
```

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

The plugin ships a unified desktop extension (`desktop/plugin.js`) and a read-only FastAPI backend (`dashboard/plugin_api.py` mounted at `/api/plugins/superagent`):

- `GET /api/plugins/superagent/health` — plugin version and SQLite database status.
- `GET /api/plugins/superagent/tickets?limit=30` — status counts, `"Needs you"` attention list, and recent tickets.
- `GET /api/plugins/superagent/tickets/{ticket_id}` — full ticket details and event log.

Enable the desktop view under **Desktop -> Settings -> Plugins -> Superagent** (then reload desktop plugins) to see the status bar chip and attention panel.

---

## Testing

Run the test suite from the repository root:

```bash
pytest -q
```
