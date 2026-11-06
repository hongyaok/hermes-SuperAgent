---
name: superagent-operator
description: Operating guide for running and tuning the Superagent router/worker plugin.
---

# Superagent Operator Guide

Use this guide when operating, triaging, or tuning the `superagent` router/worker plugin in Hermes.

`superagent` follows a strict attention-frugal philosophy: **take as little human attention as possible, and deliver as much verified value as possible.** The router is deterministic and zero-token; worker subagents run in isolation and never commit or push code on your behalf.

---

## 1. Start Narrow (`observe` First)

Never enable unattended execution on a repository until you have observed how the router classifies real traffic.

1. **Point at one local repository clone:**
   ```bash
   hermes config set plugins.entries.superagent.settings.repo_path "/absolute/path/to/repo"
   ```
2. **Stay in `observe` mode initially:**
   ```bash
   hermes config set plugins.entries.superagent.settings.mode observe
   ```
3. **Keep GitHub allow-lists small and explicit:**
   If polling GitHub issue comments, set `github_repo` (`owner/name`), `github_agent_login`, and restrict `github_allowed_users` to yourself or a small trusted group. Keep `allow_proactive` set to `false` so only direct `@mentions` are routed.
4. **Promote individual tickets while building trust:**
   In `observe` mode, accepted requests stop in `observed`. Use the `superagent_run` tool in a chat turn to approve and run individual tickets before switching `mode` to `auto`.

---

## 2. Tune for Less Noise

Protect both your attention budget and compute capacity:

- **Keep `max_workers` small (`1` or `2`):**
  Workers modify the working tree in `repo_path`. Unless you are using worktree isolation, running a single worker (`max_workers: 1`) prevents overlapping working-tree edits across tickets.
  ```bash
  hermes config set plugins.entries.superagent.settings.max_workers 1
  ```
- **Use skip markers when discussing without wanting action:**
  Include `[skip-agent]`, `[no-agent]`, `@agent ignore`, or `/noagent` anywhere in a comment when mentioning the agent login in a discussion where no ticket should be created.
- **Leave `allow_proactive` off (`false`) on general repositories:**
  Only enable `allow_proactive: true` on dedicated triage repositories where unaddressed comments with action verbs (`fix`, `add`, `refactor`, etc.) or stack traces are intended as agent work.
- **Let design questions stop in `blocked` (`needs_human`):**
  Requests asking `"should we..."`, `"which approach..."`, or `"what do you think..."` without code/error context are intentionally routed to `blocked`. Resolve the design choice first, then either submit a concrete `superagent_ask` or promote the ticket with `superagent_run`.

---

## 3. Own What It Says and Does

Workers are forbidden from running `git commit`, `git push`, merging, or opening pull requests. When a worker finishes (`SUCCEEDED`), the ticket moves to **`review`** and its edits remain uncommitted in `repo_path`.

Before committing or pushing under your name:

1. **Read the ticket result and event history:**
   Call `superagent_status(ticket_id="T-xxxx")` (or check the Desktop Superagent panel) to read the worker's summary of files touched, checks run, and any unresolved questions.
2. **Inspect the working tree in `repo_path`:**
   ```bash
   git -C /path/to/repo status
   git -C /path/to/repo diff
   ```
3. **Run or verify the test suite yourself** if the change touches critical paths, then commit and push under your own workflow.

---

## 4. Operational Playbook

### Daily Triage (Inside a Hermes Chat Turn)

1. **Check what needs your attention:**
   Call `superagent_status()` with no arguments. Focus on the `attention` list (`observed`, `blocked`, `review`, `failed`).
2. **Inspect a specific ticket:**
   Call `superagent_status(ticket_id="T-0001")` to view the full request, router verdict, worker summary, and event log.
3. **Approve an `observed`, `blocked`, or `failed` ticket:**
   Call `superagent_run(ticket_id="T-0001", wait=True)` to promote the ticket to `queued`, launch its worker subagent immediately, and wait for completion.
4. **Run a routing and execution pass:**
   Call `superagent_tick(wait=True)` to poll GitHub (if configured), launch queued workers (when `mode` is `auto`), wait for active workers to finish, and reap results into `review` or `failed`.
5. **Queue new work directly:**
   Call `superagent_ask(request="Fix XYZ in module.py and add a unit test")`.

### CLI Diagnostics & Inspection

From a terminal shell:

```bash
# Check configuration, repo_exists, github_configured, and workers_available
hermes superagent doctor

# Inspect current attention queue and recent tickets
hermes superagent status

# Queue a ticket from the terminal
hermes superagent ask "Add validation test for empty payload in parser.py"
```

### Troubleshooting `launch_failed` or Unstarted `queued` Tickets

- **Symptom:** Running `hermes superagent tick` or `/superagent tick` prints:
  `note: workers are launched only from a Hermes agent turn (chat or cron)`
  or a promoted ticket stays in `queued` with a `launch_failed` event.
- **Cause:** `ctx.subagent_lifecycle` requires an active parent agent session (`bind_subagent_parent`), which only exists during a Hermes chat turn or a scheduled cron automation turn (`Superagent watch`).
- **Fix:** Invoke the `superagent_tick` (with `wait=true`) or `superagent_run` **tool** from inside an active Hermes chat session, or enable the `Superagent watch` automation blueprint so a cron agent turn runs `superagent_tick(wait=true)` every 5 minutes.
