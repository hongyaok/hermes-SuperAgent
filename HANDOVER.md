# Superagent Plugin — Handover to Next Agent

**Date:** 2026-11-06 (updated — third session, all core items complete)
**Project:** `C:\Users\hongy\Desktop\hermes-superagent`
**Installed as:** `C:\Users\hongy\AppData\Local\hermes\plugins\superagent` (Windows directory junction → Desktop folder; edits are live, no copy step)
**Status:** Plugin **enabled**, **41/41 tests green** from project root and `tests/`, `hermes plugins doctor --ci .` and `hermes plugins validate .` clean (16/16 checks), **live end-to-end worker run completed (`T-0001` → `review`)**, dashboard backend verified against live DB, and **all documentation (`README.md`, `DESIGN.md`, `skills/superagent-operator/SKILL.md`) written and registered.**

## 1. What this is

A Hermes native plugin implementing a DeepSeek `dsh-superagent`-style router/worker layer:

- Sources produce requests → rule-based router judges them → SQLite holds tickets → Hermes subagents do the work.
- Nothing here runs its own model. Workers are launched via the public plugin-safe API `ctx.subagent_lifecycle` (`SubagentLaunchRequest`), never via subprocess or private helpers.
- Design principle (from DeepSeek): take as little human attention as possible, give as much value as possible. Default mode is **observe**.

Ticket lifecycle: `observed → queued → running → review` (plus `failed` / `blocked` / `ignored` / `done`).
Modes: `off` / `observe` / `auto` (default `observe`). Only `auto` launches workers.

## 2. Repo guide

```
hermes-superagent/
├── plugin.yaml        manifest + Settings form (mode, repo_path, github_*, max_workers, worker_toolsets, allow_proactive, max_runtime_seconds, poll_limit)
├── __init__.py        register(): 4 tools, /superagent slash, `hermes superagent` CLI, "Superagent watch" cron blueprint (every 5 min)
├── sa_schemas.py      tool schemas the model reads (ASK, TICK, STATUS, RUN)
├── sa_tools.py        tool handlers — always return JSON string, never raise
├── sa_assessor.py     rule-based router: ACCEPT / OBSERVE / IGNORE / NEEDS_HUMAN. Deterministic, no tokens.
├── sa_sources.py      LocalSource (chat asks) + GitHubSource (REST issue-comment polling, stdlib urllib only, no `gh` needed)
├── sa_store.py        SQLite: tickets / events / state (cursor, ticket_seq) / seen (dedupe). One connection per op.
├── sa_engine.py       Engine: ask() → tick() → _record() → _launch_queued() → _reap() + watchdog + promote()
├── sa_executor.py     SubagentExecutor: build_worker_prompt() + launch/poll/cancel over ctx.subagent_lifecycle
├── sa_cli.py          `hermes superagent status|tick|ask|doctor` + text renderers shared with /superagent
├── pytest.ini         testpaths = tests (so bare `pytest` from root works)
├── conftest.py        puts the project root on sys.path (lets the dual-mode imports below resolve)
├── dashboard/
│   ├── manifest.json  {"name":"superagent", "api":"plugin_api.py", ...}
│   └── plugin_api.py  read-only FastAPI router: GET /health, GET /tickets, GET /tickets/{id}
├── desktop/
│   └── plugin.js      attention pane + status chip, polls the backend via ctx.rest every 15 s
├── skills/
│   └── superagent-operator/
│       └── SKILL.md   operator skill registered as `superagent:superagent-operator`
├── tests/             41 tests (all passing from root AND from tests/)
├── README.md          install, config reference, usage across tools/CLI/slash/cron/desktop
├── DESIGN.md          router/worker architecture, attention budget, subagent_lifecycle, observe-default
└── HANDOVER.md        this file
```

Dual-mode imports: `__init__.py`, `sa_engine.py`, `sa_tools.py`, `sa_cli.py` try `from . import …` first and fall back to top-level `import …`. This is what lets pytest import root `__init__.py` as `__init__` without crashing, while Hermes (`hermes_plugins.superagent`) and tests (`sa_plugin`) keep using proper package imports. Do not "simplify" these back to bare relative imports.

Key flows:

- `ask("fix X")` → assess(explicit=True) → `observed` (observe mode) or `queued` (auto mode).
- `tick()` → poll GitHub (if configured) → dedupe via `seen` → assess → record → `_launch_queued()` (auto only, honours `max_workers`) → `_reap()` collects finished workers → watchdog cancels workers past `max_runtime_seconds`.
- `promote(T-xxxx)` → human approves an `observed`/`blocked`/`failed` ticket → `queued` → launch attempt.
- GitHub replies: observe-mode notice on intake, finish summary on completion.

**Critical constraint (verified in hermes-agent source):** `get_active_subagent_parent()` is only bound during an agent turn (`agent/turn_facade.py` → `bind_subagent_parent`). So **workers can ONLY launch from a tool call inside a Hermes session** (chat turn or cron run). CLI (`hermes superagent`), slash command, and REST handlers can inspect/queue but `launch=False` there. The always-on path is the registered automation blueprint whose prompt calls `superagent_tick` — that runs as an agent turn, so it can launch.

## 3. Verified so far

- `hermes plugins doctor .`, `hermes plugins doctor --ci .`, and `hermes plugins validate .` (from project root) → OK: manifest parses, import + registration pass, **4 tools, 0 hooks, 1 skill (`superagent-operator`)**, 16/16 validation checks pass (including security scan), exit 0.
- **41/41 tests pass from project root** (`pytest -q`) and from `tests/` — the old "run from tests/ only" workaround is no longer needed.
- Dashboard backend loads standalone under the Hermes venv and is verified against the live SQLite DB (`C:\Users\hongy\AppData\Local\hermes\plugin-data\superagent\superagent.db`): routes `GET /health`, `GET /tickets`, `GET /tickets/T-0001` confirmed; `health` with no DB returns `db_exists: false` instead of erroring.
- `desktop/plugin.js` passes the real SDK surface check (`desktop_surface_hits` = none) — no forbidden imports, no hardcoded colors, polling via `ctx.rest` + `useQuery`.
- Plugin **enabled** (`hermes plugins enable superagent` → in `plugins.enabled`; gateway live-reloaded, tools deferred to new sessions).
- Live CLI works against the real DB: `hermes superagent status` (mode=auto, `T-0001` in `review` under `"Needs you"`) and `hermes superagent doctor` (`repo_exists: True`, `workers_available: True`, `max_workers: 1`).
- **First live end-to-end worker run completed (`T-0001` → `review`):** Executed via a real Hermes agent turn (`hermes chat -Q --yolo --provider opencode-go -m deepseek-v4.1-flash -q ...`), which bound `bind_subagent_parent(self)`, called `superagent_status` and `superagent_tick(wait=True)`, launched real leaf subagent `sa-0-90936e1a` (`correlation_id=T-0001`), created `test_hello.py` in `C:\Users\hongy\Desktop\superagent-scratch`, ran `pytest -q` (`1 passed`), cleaned up cache dirs, left `test_hello.py` uncommitted in the working tree as required, and moved `T-0001` to **`review`** with full 4-event history (`created` → `assessed` → `launched` → `review`).
- **Docs & Operator Skill written:** `README.md`, `DESIGN.md`, and `skills/superagent-operator/SKILL.md` are in place and tested.
- **Manual promotion in `observe` mode fixed:** Promoting a ticket outside an agent turn (e.g. `/superagent run T-xxxx`) sets `status="queued", mode="manual"` and now launches on the next `superagent_tick` even when `settings.mode == "observe"`, and allows re-running via `superagent_run`.
- Coverage: assessor rules (thanks/skip-markers/design-questions/actionable/proactive-on-off/short/stack-trace), store CRUD + seq ids + state + dedupe + events, engine (observe records w/o launch, auto launches→review, max_workers cap, promote + manual-mode observe launch, doctor github token check, watchdog cancel, launch-failure keeps queued, GitHub dedupe), GitHub parsing (mention/allow-list/bot+self skip/no-mention skip/config error/reply path), tools + registration + slash commands (ask/status/tick/run/error containment/skill registration/docs presence), plus dashboard backend (health/list/detail/404/missing-DB).

## 4. Remaining optional items

1. **See the panel in the Electron UI** (when the user opens Hermes Desktop): Desktop → Settings → Plugins → enable Superagent → ⌘K → Reload desktop plugins. The backend API (`dashboard/plugin_api.py`) is already serving `T-0001` (`status: "review"`) from the live DB, and `desktop/plugin.js` is validated and ready for the Electron shell's unified-package materializer.
2. *(Optional)* **Live GitHub test.** `GitHubSource` is unit-tested with mocks; to run against a real GitHub repo, configure `github_repo`, `github_agent_login`, `github_allowed_users`, and `SUPERAGENT_GITHUB_TOKEN`, comment `@<agent> fix …` on an issue, and run a tick.

## 5. Useful commands

```powershell
# tests (from project root — no workaround needed anymore)
cd C:\Users\hongy\Desktop\hermes-superagent
C:\Users\hongy\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -m pytest -q

# doctor (from project root)
cd C:\Users\hongy\Desktop\hermes-superagent
hermes plugins doctor .
hermes plugins doctor --ci .

# live state (CLI: inspect/queue only — cannot launch workers)
hermes superagent status
hermes superagent doctor
hermes superagent ask "what to do"

# settings live here (NOT plugins.superagent)
hermes config get plugins.entries.superagent.settings
hermes config set plugins.entries.superagent.settings.mode auto

# in a NEW chat session (has the tools): superagent_status, then superagent_tick wait=true
```

## 6. Things NOT to do

- Don't run workers from CLI/REST/slash — they can't (no parent session); queue there, launch from agent turns.
- Don't commit/push from workers — the worker prompt forbids it; results stay in the working tree for human review.
- Don't rename root `__init__.py`, don't collapse the try/except dual-mode imports back to bare relative imports (breaks `pytest` from root), don't write runtime state into the plugin dir (DB lives in `<HERMES_HOME>/plugin-data/superagent/` via `plugin_data_dir`), don't read `.env`/tokens directly (use `get_secret` path per catalog rules when wiring the secret through).
- Don't `pip install` anything for the GitHub source — it's stdlib-only by design.
- Don't hand-copy `desktop/plugin.js` into `desktop-plugins/` — the Electron shell copies the unified-package half itself; a second copy will drift.
- Never restart the gateway from inside a session it hosts — enabling already live-reloaded; if a restart is ever needed, hand the user the command.
