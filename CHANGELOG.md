# Changelog

All notable changes to the `superagent` Hermes plugin. Plain bullets, newest
first.

## 0.4.0 — 2026-11-06

The first release that ships the whole surface together: a chat workspace you
can talk to, a live board of parallel workers, per-session models and presets,
one watched GitHub repository with isolated PR checkouts, and a review handoff
that never commits or pushes for you.

### Added

- **Chat sessions.** The desktop **Superagent** tab starts one headless
  `hermes chat` process per turn, streams its JSONL into a transcript that
  interleaves assistant text with tool calls and errors, and stores every
  message and audit event in SQLite. Follow-ups resume the same Hermes session;
  a turn can be stopped; a finished turn ends in `review` with its changes left
  in the working tree.
- **Live multi-session grid.** Every queued, running, review or failed chat is
  a card with status, model, a client-side elapsed timer, token count and a
  three-event mini-feed. The rail filters **All / Live / Review** without
  unmounting the open transcript, and the list polls every 2 s while a worker
  is live (8 s when quiet).
- **Per-session model.** A session records its own `model` and `provider`;
  `POST /chats/{id}/model` (and the header's model control) swaps them for
  future turns without disturbing a queued or running one.
- **Model presets.** Named `(model, provider)` pairs in three spellings
  (`name=provider:model`, plain model strings, `{name, model, provider}`),
  capped at 12 with unique names. Presets appear as composer chips, travel as
  `preset` on `POST /chats`, and are reported by `GET /models`,
  `hermes superagent models` and `hermes superagent chat --preset`.
- **Settings panel and onboarding.** The ⚙ panel edits the default
  model/provider, presets, `mode`, `repo_path`, `max_workers`, `worker_yolo`
  and `chat_max_turns` through `GET/POST /settings`; the three-step **Get
  started** card points a new user at a model, GitHub and the composer and
  remembers its dismissal.
- **GitHub connect and PR/comment watch.** Connect one repository with a
  token, an allow-list and an **Observe / Auto** mode. The watcher polls open
  PRs, turns allowed mentions into sessions, optionally starts one review
  session per new head commit, lists open PRs with **Work on this PR**, and
  keeps every seen comment/head in `gh_seen`. The first poll is a baseline, so
  connecting a busy repository does not replay its history; bots and the
  agent's own comments are ignored.
- **Isolated PR worktrees.** In `auto` mode the watcher fetches
  `refs/pull/<n>/head` into the local clone and checks it out as a detached
  `git worktree` under the plugin data directory, so the user's checkout and
  branch are never touched. A failed checkout falls back to `repo_path` with a
  warning event.
- **Session diff and human-confirmed PR comment.** A lazy **Diff** panel shows
  `git status`, `git diff --stat` (plus staged totals) and a 60 000-character
  capped patch. **Comment on PR** previews an editable summary, then posts one
  comment only after **Post**; nothing is committed, pushed, merged or edited.
- **CLI for the whole surface.** `hermes superagent chat|sessions|show|stop|
  models|settings|github|diff|comment` beside the existing `status|tick|ask|
  run|doctor`, all with `--json` and stable exit codes (`0` ok, `1` runtime,
  `2` usage). `chat` resolves presets, `github` drives the watcher without
  HTTP, and `diff`/`comment` work from the chat tables alone.
- **Completion toasts.** While the page is mounted, a chat that moves from
  `queued`/`running` to `review` or `failed` raises one `host.notify` toast
  (`<title> finished` / `<title> failed`). The first poll seeds the status map,
  so opening the page never replays old completions, and `cancelled` is
  silent.

### Changed

- **Headless `hermes chat` worker runner.** Chat turns are subprocesses, not
  subagents. Hermes only binds a subagent parent during an active agent turn
  (`bind_subagent_parent`), and the dashboard backend is an HTTP handler with
  no turn to bind, so `sa_runner.Runner` uses the supported pattern: spawn
  `hermes chat -Q --format stream-json` and read its stream. Ticket workers
  still use `ctx.subagent_lifecycle` from a tool or cron turn; the two paths
  share one database, one settings loader and one attention budget.
- **Observe by default for GitHub too.** `github_watch_mode` starts at
  `observe`: PR mentions become `observed` sessions until a human promotes one
  (or switches to `auto`). Ticket intake remains `observe` by default as well.
- **Secrets and size caps are enforced on every write.** GitHub/Slack/OpenAI
  tokens and `Authorization` values are replaced with `[redacted]` before a
  message, event, error, prompt or ticket field is stored; prompts are limited
  to 20 000 characters, events to 2 000, assistant text to 20 000, diffs to
  60 000 and posted comments to 20 000.
- **The status chip speaks for chats.** `▶ N` running chats and `◆ M`
  chats in `review`/`failed`; ticket attention is only the fallback when the
  chats query has no data. The side pane shows latest chats ahead of ticket
  rows.
- **Polling has a budget.** Chats 2 s live / 8 s quiet, an open chat
  1.5 s / 6 s, the diff 4 s only while its panel is open on a running turn, and
  GitHub 20 s only while the panel or a PR badge is visible.
- **The ticket path is kept, not replaced.** Deterministic zero-token triage,
  `superagent_ask/tick/status/run`, `/superagent`, the **Superagent watch**
  cron blueprint, `/tickets` and the ticket session grid all continue to work
  beside the chat surface.

### Fixed

- **No fallback presets.** `GET /models` and the composer no longer advertise
  the hardcoded `MODEL_PRESETS` list. With nothing configured, `presets` is
  `[]` and the chip row is hidden; `models` is the preset models in order plus
  the configured `default_model`, never a model the machine may have no
  credentials for. `hermes superagent models` follows the same rule.
- **Precise runaway-turn wording.** A cancelled turn now names the phase:
  `turn timed out after 1900s while queued (limit 1800s)` or `... while
  running ...`. Status and behaviour are unchanged.
- **Failed sessions say why.** The transcript header renders the stored
  `error` whenever a session is `failed`, and a failed live-worker card with
  no recent events shows the error's first line in its mini-feed.

## 0.3.0 — 2026-11-06

- **Chat sessions (v0.2).** The desktop Superagent tab starts one headless
  `hermes chat` process per turn, streams JSONL into a transcript, and supports
  follow-ups, resume, model changes between turns and cancel.
- **Live worker grid.** Every queued/running chat as a card with status, model,
  a client-side elapsed timer, tokens and a three-event mini-feed; rail filters
  All / Live / Review without unmounting the open transcript.
- **Model presets and settings.** Named `(model, provider)` presets in three
  spellings, a ⚙ Settings panel for default model, presets, `max_workers`,
  `chat_max_turns` and `worker_yolo`, plus composer preset chips and a
  three-step first-run onboarding card.
- **GitHub connect and PR watch (v0.3).** Connect one repository with a token
  and allow-list, choose `observe` or `auto`, watch PR mentions and new head
  commits, list open PRs and start work on one. The first poll is a baseline;
  dedupe lives in `gh_seen`.
- **Isolated PR checkouts.** `auto` fetches `refs/pull/<n>/head` and checks it
  out as a detached `git worktree` under the plugin data directory, leaving the
  user's clone and branch untouched.
- **Review handoff.** A lazy diff panel (`git status`, `--stat`, capped patch)
  and a human-confirmed PR comment with an editable preview. Nothing is
  committed, pushed, merged or posted without an explicit click.
- **CLI.** `hermes superagent chat|sessions|stop|show|models|settings|diff|comment|github|doctor`,
  all with `--json` and stable exit codes, sharing the same database, settings
  loader and worker pool as the desktop backend.
- **Ticket path kept.** Deterministic zero-token triage, `superagent_ask/tick/
  status/run` tools, `/superagent`, the **Superagent watch** cron blueprint and
  the attention pane all continue to work beside the chat surface.
- **Guardrails.** Repository containment, 20k prompt/response caps, 2k event
  caps, token redaction, `max_workers` semaphore and the runaway-turn reaper.
