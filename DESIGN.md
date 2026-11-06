# Superagent Design Notes

## 1. Core Thesis: Human Attention Is the Scarcest Resource

Inspired by DeepSeek's `dsh-superagent`, `superagent` is built around a single constraint: **take as little human attention as possible, and deliver as much verified value as possible.**

Most autonomous coding setups fail in one of two ways:
1. They burn tokens and human attention on every stray message, acknowledgement, or vague discussion thread.
2. They act with unchecked autonomy—committing, pushing, or opening noisy pull requests that cost humans more time to untangle than writing the change directly.

`superagent` solves both problems by pairing a **deterministic, zero-token router** with **isolated, non-committing worker subagents** and an **observe-first operating model**.

---

## 2. Thin Router vs. Isolated Workers

### Deterministic, Zero-Token Router (`sa_assessor.py`)

The router (`Engine` in `sa_engine.py` + `assess()` in `sa_assessor.py`) never calls a language model to decide whether an inbound message should become work.

Instead, every inbound message passes through fast, auditable heuristics:
- **Immediate `ignore` (0 tokens, 0 human interruption):**
  - Empty or very short messages (`< 8` characters).
  - Explicit opt-out skip markers: `[skip-agent]`, `[no-agent]`, `@agent ignore`, `/noagent`.
  - Pure acknowledgements: `"thanks"`, `"thank you"`, `"thx"`, `"lgtm"`, `"looks good"`, `"+1"`, `"👍"`.
  - Unaddressed comments when `allow_proactive` is `false`.
- **Route to `needs_human` -> `blocked`:**
  - Messages matching design or architectural questions (`"should we"`, `"what do you think"`, `"which approach"`, `"architecture"`, `"thoughts?"`) without concrete code or stack traces. Rather than letting a worker guess on an open design question, the router stops immediately and surfaces the ticket to a human.
- **Route to `accept` -> `observed` (in `observe` mode) or `queued` (in `auto` mode):**
  - Direct requests (`explicit=True`) via local ask or `@mention` from an allowed user.
  - Unaddressed comments when `allow_proactive=True` that contain an explicit action verb (`fix`, `add`, `implement`, `refactor`, `test`, `debug`, etc.) or code/traceback hints.

### SQLite Ticket Store & Deduplication (`sa_store.py`)

All state lives in a single WAL-mode SQLite file at `<HERMES_HOME>/plugin-data/superagent/superagent.db` (resolved via `plugin_data_dir("superagent")`). A fresh connection is opened per operation so CLI commands, gateway REST endpoints, and agent turns never contend over thread-bound sockets.

The schema has four focused tables:
- `tickets`: Sequential human-readable IDs (`T-0001`, `T-0002`, ...), source metadata (`local` or `github`), requester, request text, current `status`, router `verdict` and `reason`, serialized worker `handle`, `result` summary, `error`, and timestamps.
- `events`: Append-only per-ticket audit trail (`created`, `assessed`, `promoted`, `launched`, `launch_failed`, `review`, `failed`, `reply_failed`).
- `state`: Key/value cursor and sequence storage (`ticket_seq` for atomic ID generation, `github_since` ISO timestamp for incremental GitHub polling).
- `seen`: Source deduplication table keyed by `source_key` (e.g., `github:12345`) so no comment is ever routed twice.

### Ticket Lifecycle & Why `review` Is the Terminal Worker State

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

When a worker subagent succeeds (`SUCCEEDED`), the ticket transitions to **`review`**, not `done`.

Furthermore, `build_worker_prompt()` in `sa_executor.py` strictly forbids workers from running `git commit`, `git push`, merging, or opening pull requests:
- **Why?** A human operator owns everything committed or published under their identity. Leaving minimal, tested modifications in the local working tree (`repo_path`) lets the human inspect `git status` and `git diff`, verify the 200-word worker summary, and decide whether to commit or discard.

---

## 3. Why `ctx.subagent_lifecycle` (`SubagentLaunchRequest`)

Workers are executed exclusively through `PluginContext.subagent_lifecycle` (`SubagentExecutor` in `sa_executor.py`), constructing a `SubagentLaunchRequest`:

- **Public, plugin-safe contract:** Avoids brittle subprocess invocations, CLI re-entry hacks, or importing private internal agent loops.
- **`role="leaf"`:** Guarantees that worker subagents cannot recursively spawn sub-subagents.
- **Scoped `allowed_toolsets`:** Defaults to `("file", "terminal")`, giving workers just enough capability to edit code and run tests without exposing unrelated integrations.
- **`correlation_id=ticket_id`:** Links the Hermes subagent lifecycle directly back to `T-xxxx` for status polling (`lifecycle.status`), result collection (`lifecycle.result`), and watchdog cancellation (`lifecycle.cancel`).
- **Watchdog safety (`max_runtime_seconds`):** During `_reap()`, any `running` worker whose elapsed time exceeds `max_runtime_seconds` (default 1800s) is cancelled via `lifecycle.cancel(handle, reason="superagent watchdog")` and marked `failed`.

### Why Workers Only Launch Inside an Agent Turn

In Hermes, `get_active_subagent_parent()` is bound only during an active agent turn (`agent/turn_facade.py` -> `bind_subagent_parent`). Therefore:
- **Chat tool calls** (`superagent_tick`, `superagent_run`) and **scheduled cron runs** (`Superagent watch`) have an active parent session and can launch workers.
- **CLI commands** (`hermes superagent ...`), **slash commands** (`/superagent ...`), and **dashboard REST endpoints** execute outside an agent turn. They pass `launch=False` (or leave promoted tickets in `queued` if launch fails) so tickets are safely recorded and picked up on the next agent turn.

---

## 4. Why `observe` Mode Is the Default

Autonomy must be earned on each repository before it is granted.

Starting in `observe` mode gives operators zero-risk visibility into the router:
1. You can connect a repository or issue stream and watch which comments become `observed`, which stop as `blocked` (`needs_human`), and which are `ignored`.
2. On GitHub, `observe` mode posts a single concise intake comment (`"Recorded as T-xxxx (observe mode). A human can approve it from the Superagent panel."`) so collaborators know their request was seen without kicking off unreviewed compute.
3. Any `observed` ticket can be promoted individually with `superagent_run(ticket_id="T-xxxx")` while keeping the global mode in `observe`.
4. Once routing rules and allow-lists match your expectations, switching to `auto` is a single setting change.

---

## 5. Attention-Frugal UI Across Every Surface

Every user-facing surface filters tickets through `NEEDS_ATTENTION = ("observed", "blocked", "review", "failed")`:

- **CLI & Slash (`sa_cli.py`):** `render_status()` prints the `"Needs you:"` section first, or `"Nothing needs you."` when the attention queue is empty. `ignored` and routine `queued`/`running` noise never clutter the top section.
- **Automation Blueprint (`__init__.py`):** `Superagent watch` instructs the cron turn to call `superagent_tick(wait=true)` and report **only** tickets in `blocked`, `review`, `failed`, or `observed`—or reply with a single line: `'Superagent: nothing needs you.'`
- **Desktop Panel & Status Chip (`desktop/plugin.js` + `dashboard/plugin_api.py`):** The backend opens SQLite in read-only URI mode (`file:...?mode=ro`) and separates `attention` tickets from `recent` tickets so the desktop chip and side panel highlight only items waiting on a human decision.
