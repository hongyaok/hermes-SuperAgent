# Changelog

All notable changes to the `superagent` Hermes plugin. Plain bullets, newest
first.

## Unreleased

- **No fallback presets.** `GET /models` and the desktop composer no longer
  advertise the hardcoded `MODEL_PRESETS` list. With nothing configured,
  `presets` is `[]` and the preset chip row is hidden; `models` is the preset
  models in order plus the configured `default_model` (never a model the
  machine may have no credentials for). The same rule now applies to
  `hermes superagent models`.
- **Completion toasts.** While the Superagent page is mounted, a chat that
  moves from `queued`/`running` to `review` or `failed` raises one
  `host.notify` toast (`<title> finished` / `<title> failed`). The first poll
  only seeds the status map, so opening the page does not replay old
  completions, and `cancelled` is silent.
- **Chat-aware status chip.** The status-bar chip shows `▶ N` running chats and
  `◆ M` chats in `review`/`failed`, falling back to ticket attention only when
  the chats query has no data at all.
- **Failed sessions say why.** The transcript header renders the stored `error`
  (prefixed `error:`) whenever a session is `failed`, and a failed worker card
  with no recent events shows the error's first line in its mini-feed.
- **Precise reap wording.** A runaway turn's reason now names the phase:
  `turn timed out after 1900s while queued (limit 1800s)` or
  `... while running ...`. Status and behaviour are unchanged.
- **Docs.** README quick start and feature list, operator skill covering the
  v0.2/v0.3 surfaces, and this changelog.

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
