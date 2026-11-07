/**
 * Superagent desktop surface — a chat-first workspace over headless chats.
 *
 * Data path: ctx.rest('/chats'), '/chats/{id}', '/chats/{id}/diff',
 * '/chats/{id}/comment-preview', '/models', '/settings', POST /chats,
 * POST /chats/{id}/messages, POST /chats/{id}/model, POST /chats/{id}/stop,
 * PATCH /chats/{id} (rename), POST /chats/{id}/archive (archive/unarchive),
 * DELETE /chats/{id} (row + transcript only — never files), and
 * POST /chats/{id}/comment, POST /settings → dashboard/plugin_api.py. The list
 * query polls at 2s while a worker runs and 8s when the board is quiet; the
 * open transcript polls at 1.5s while its own turn is live. The diff panel is
 * lazy (only while open) and refetches at 4s only while the turn is running.
 * Every mutation invalidates both so the rail, the transcript, and the worker
 * strip agree on the next paint. The per-card elapsed timer ticks on local
 * state only — it must never add a request to those budgets.
 *
 * Housekeeping lives behind a per-row ⋯ that only appears on hover/focus: a
 * rename is an inline editor in the row, archive keeps the row (dimmed, tagged)
 * and makes the composer read-only, and delete asks once before removing the
 * database rows. `?archived=all` is the "Show archived" filter, remembered in
 * ctx.storage.
 *
 * Layout: one 44px header row, then a 16rem session rail beside a chat column.
 * Live workers are a collapsible strip inside the chat column (under the
 * transcript, above the composer), and GitHub/Settings are mutually exclusive
 * overlay drawers — neither can reflow or resize the conversation.
 *
 * The composer is one rounded box, not a stack of rows: an auto-growing field
 * (two rows, eight at most) over one meta row carrying preset/model, the repo
 * for new chats and send/stop. Drafts persist per target (`draft:<id>` and
 * `draft:new`, debounced), a send paints the user's text as an optimistic
 * bubble until the server's own row arrives, and Enter / Cmd+Enter send while
 * Shift+Enter breaks the line. The backend's 8 000-character cap is the
 * contract; the counter only warns.
 *
 * The ticket pane and the status-bar chip stay (they answer "does a human need
 * to look at anything?"); the route page is chat-only.
 *
 * The transcript renders assistant answers through the app's own Markdown
 * surface (feature-detected; older shells keep pre-wrap), folds runs of audit
 * events into a collapsible Activity block, and stamps messages and events in
 * local HH:MM. Bubbles are memoized on primitive props so a 1.5s poll does not
 * re-typeset every answer on the page.
 *
 * Plain ESM, loaded uncompiled — UI is jsx()/jsxs() calls, not JSX syntax.
 * Only @hermes/plugin-sdk, react and react/jsx-runtime resolve here. The SDK is
 * imported as a namespace: a shell that predates a component (the model
 * catalog menu, StatusDot) leaves that binding undefined and the page degrades
 * instead of failing to link.
 */

import * as SDK from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { memo, useEffect, useMemo, useRef, useState } from 'react'

const {
  Tip, StatusDot, DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
  ModelCatalogMenu, ModelMenuCloseContext, cn, host,
  useQuery, useMutation, useQueryClient, ROUTES_AREA, SIDEBAR_NAV_AREA,
  // Transcript niceties. Optional: a shell that predates them leaves the
  // binding undefined and every use below is feature-detected.
  MessageTextContent, CopyButton, DisclosureCaret,
  // Session housekeeping: the app's own confirm dialog when this shell has
  // one; older shells get a two-step "Confirm delete" instead.
  ConfirmDialog,
} = SDK

const ID = 'superagent'

// Polling budgets. A live worker is worth 2s; a quiet board is not.
const LIST_POLL_RUNNING = 2000
const LIST_POLL_QUIET = 8000
// Typing in the rail is cheap; a search round-trip per keystroke is not.
const SEARCH_DEBOUNCE_MS = 250
const DETAIL_POLL_RUNNING = 1500
const DETAIL_POLL_QUIET = 6000
const TICKET_POLL = 15000
// GitHub is a secondary surface: its status/PR list is only worth a request
// while the panel is open or a session on screen carries a PR badge.
const GITHUB_POLL = 20000
// An open diff panel on a *running* session: fast enough to watch files land,
// slow enough that `git diff` never becomes the busiest thing on the machine.
const DIFF_POLL = 4000

// The live-workers column shows chats that are doing something (or that a
// human should look at). Idle/cancelled chats stay in the rail only.
const LIVE_STATUSES = ['running', 'queued', 'review', 'failed']
const MAX_WORKERS = 8

// Chat statuses plus the ticket statuses the pane still renders.
const ICONS = {
  observed: '·', queued: '…', running: '▶', review: '◆', blocked: '!',
  failed: '×', done: '✓', ignored: '–', idle: '·', cancelled: '–',
}

const STATUS_TONES = {
  running: 'good', queued: 'muted', review: 'warn',
  failed: 'bad', idle: 'muted', cancelled: 'muted',
}

// Set in register(); the data hooks below need them.
let rest = null
let storage = null
let openExternal = null

// -- storage (feature-detected: older hosts may not ship ctx.storage) -------

function readStored(key, fallback) {
  try {
    if (storage && typeof storage.get === 'function') {
      const value = storage.get(key, fallback)
      return typeof value === 'string' ? value : fallback
    }
  } catch (error) {
    // Storage is a nicety; a broken key must never break the page.
  }
  return fallback
}

function writeStored(key, value) {
  try {
    if (storage && typeof storage.set === 'function') storage.set(key, value)
  } catch (error) {
    // Same: losing a remembered model is not worth an error card.
  }
}

// -- formatting helpers -----------------------------------------------------

function clip(text, max) {
  const value = String(text == null ? '' : text)
  return value.length <= max ? value : value.slice(0, Math.max(1, max - 1)) + '…'
}

/** Middle-out clip for paths: keeps the drive/root and the segment that names
 *  the repo, which is the part a reader is actually looking for. */
function clipMiddle(text, max) {
  const value = String(text == null ? '' : text)
  if (value.length <= max) return value
  const keep = Math.max(1, Math.floor((max - 1) / 2))
  return `${value.slice(0, keep)}…${value.slice(-keep)}`
}

function isBusy(session) {
  return !!session && (session.status === 'running' || session.status === 'queued')
}

/** Relative age of an epoch-seconds timestamp: 'now' / '4m' / '3h' / '2d'. */
function fmtAge(seconds) {
  if (!seconds) return ''
  const delta = Date.now() / 1000 - seconds
  if (!isFinite(delta)) return ''
  if (delta < 45) return 'now'
  if (delta < 3600) return `${Math.max(1, Math.round(delta / 60))}m`
  if (delta < 86400) return `${Math.round(delta / 3600)}h`
  return `${Math.round(delta / 86400)}d`
}

/** The same age for a GitHub ISO-8601 timestamp (e.g. a PR's updated_at). */
function fmtIsoAge(value) {
  const stamp = Date.parse(String(value || ''))
  if (!isFinite(stamp)) return ''
  return fmtAge(stamp / 1000)
}

/** Wall-clock HH:MM (24h) for one epoch-seconds stamp.
 *
 * Deliberately not toLocaleTimeString: the transcript is a fixed-width column
 * and a locale that appends AM/PM or a non-Latin digit set would jitter it.
 * Local getters, so the clock matches the user's wall clock. */
function fmtClock(seconds) {
  const stamp = Number(seconds)
  if (!isFinite(stamp) || stamp <= 0) return ''
  const date = new Date(stamp * 1000)
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

/** Compact duration from milliseconds: '12s' / '3m 04s' / '1h 12m'. */
function fmtDuration(ms) {
  if (!ms || !isFinite(ms) || ms < 0) return ''
  const total = Math.round(ms / 1000)
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return total % 60 ? `${minutes}m ${total % 60}s` : `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

/** Same shape as fmtDuration but from seconds, and 0 renders as '0s'. */
function fmtSeconds(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0))
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return total % 60 ? `${minutes}m ${total % 60}s` : `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

/** Seconds the current turn has been alive, recomputed against `nowMs`.
 *
 * The server sends `elapsed_seconds` for live rows, but a value frozen at the
 * last poll would make the timer stutter; ticking from `started_at` locally
 * keeps it smooth without a request. A queued turn has no start of its own, so
 * its wait is measured from `created_at`.
 */
function liveElapsedSeconds(session, nowMs) {
  if (!session) return 0
  const start = session.started_at ||
    (session.status === 'queued' ? session.created_at : 0)
  if (start) return Math.max(0, Math.floor(nowMs / 1000 - start))
  const reported = Number(session.elapsed_seconds)
  return isFinite(reported) && reported > 0 ? Math.floor(reported) : 0
}

/** Re-render once a second while `active`; purely local, never a refetch. */
function useNow(active) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return undefined
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active])
  return now
}

/** Elapsed for a live turn, duration for a finished one, age otherwise. */
function timingLabel(session) {
  if (!session) return ''
  if (isBusy(session)) {
    const start = session.started_at || session.updated_at || session.created_at
    return start ? fmtDuration(Date.now() - start * 1000) : ''
  }
  if (session.duration_ms) return fmtDuration(session.duration_ms)
  if (session.started_at && session.finished_at) {
    return fmtDuration((session.finished_at - session.started_at) * 1000)
  }
  return fmtAge(session.updated_at || session.created_at)
}

/** The composer's label: `provider: model`, the model alone, or the default. */
function modelLabel(model, provider) {
  const name = String(model || '').trim()
  const vendor = String(provider || '').trim()
  if (!name) return 'Default model'
  return vendor ? `${vendor}: ${name}` : name
}

/** A session's fixed model, or the lowercase rail copy for "nothing pinned". */
function sessionModelLabel(session) {
  if (!session || !String(session.model || '').trim()) return 'default model'
  return modelLabel(session.model, session.provider)
}

function firstLine(text) {
  for (const line of String(text || '').split('\n')) {
    const trimmed = line.trim()
    if (trimmed) return trimmed
  }
  return ''
}

function sessionTitle(session) {
  if (!session) return 'Chat'
  const title = String(session.title || '').trim()
  if (title) return title
  const line = firstLine(session.prompt)
  return line ? clip(line, 72) : session.id
}

/**
 * Toast once per finished turn.
 *
 * `previous` is the status map from the last poll, or null before the first
 * one. The first pass only seeds it, so merely opening the page never replays
 * completions that happened while it was closed. A chat that goes
 * queued/running -> review/failed announces itself; `cancelled` is silent
 * because the user (or the runaway guard) already stopped it deliberately.
 * Returns the map the next poll should compare against.
 */
function announceCompletions(previous, sessions) {
  const next = new Map()
  const list = Array.isArray(sessions) ? sessions : []
  for (const session of list) next.set(session.id, String(session.status || ''))
  if (!previous) return next
  const notify = host && typeof host.notify === 'function' ? host.notify : null
  if (!notify) return next
  for (const session of list) {
    const before = previous.get(session.id)
    const after = String(session.status || '')
    if (before !== 'queued' && before !== 'running') continue
    if (after !== 'review' && after !== 'failed') continue
    const title = sessionTitle(session)
    try {
      notify({
        kind: after === 'failed' ? 'error' : 'info',
        message: after === 'failed' ? `${title} failed` : `${title} finished`,
      })
    } catch (error) {
      // A host without working toasts must not break polling.
    }
  }
  return next
}

function openUrl(url) {
  const target = String(url || '').trim()
  if (!target) return
  try {
    if (typeof openExternal === 'function') return void openExternal(target)
    if (typeof window !== 'undefined' && typeof window.open === 'function') {
      window.open(target, '_blank', 'noopener')
    }
  } catch (error) {
    // A blocked popup is not worth an error card.
  }
}

/** Save a generated document the shell already returned: the response *is*
 *  the file, so no round-trip or filesystem permission is needed. A shell
 *  without Blob just does nothing — the transcript stays on screen. */
function downloadMarkdown(filename, text) {
  try {
    const blob = new Blob([String(text || '')], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = String(filename || 'superagent-chat.md')
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) {
    // Nothing to clean up: the export simply was not saved.
  }
}

function errorText(error) {
  if (!error) return ''
  return String((error && error.message) || error)
}

// -- data hooks -------------------------------------------------------------

function fetchTickets() {
  return rest ? rest('/tickets') : Promise.reject(new Error('backend not ready'))
}

function fetchChats(archived, query) {
  const suffix = archived === 'all' ? '&archived=all' : ''
  const needle = String(query || '').trim()
  const term = needle ? `&q=${encodeURIComponent(needle)}` : ''
  return rest ? rest(`/chats?limit=50${suffix}${term}`) : Promise.reject(new Error('backend not ready'))
}

function fetchChat(id) {
  return rest ? rest(`/chats/${encodeURIComponent(id)}`) : Promise.reject(new Error('backend not ready'))
}

/** What this session changed: git status, stat and a capped patch. */
function fetchChatDiff(id) {
  return rest ? rest(`/chats/${encodeURIComponent(id)}/diff`) : Promise.reject(new Error('backend not ready'))
}

/** The comment body POST /comment would send (no network, no write). */
function fetchCommentPreview(id) {
  return rest ? rest(`/chats/${encodeURIComponent(id)}/comment-preview`) : Promise.reject(new Error('backend not ready'))
}

/** The transcript as Markdown; the caller turns it into a download. */
function fetchChatExport(id) {
  return rest ? rest(`/chats/${encodeURIComponent(id)}/export`) : Promise.reject(new Error('backend not ready'))
}

function fetchModels() {
  return rest ? rest('/models') : Promise.reject(new Error('backend not ready'))
}

function fetchSettings() {
  return rest ? rest('/settings') : Promise.reject(new Error('backend not ready'))
}

/** Aggregated chat activity: totals, a daily series, per-model rows. */
function fetchUsage(days) {
  const span = Math.max(1, Math.min(Number(days) || 14, 90))
  return rest ? rest(`/usage?days=${span}`) : Promise.reject(new Error('backend not ready'))
}

function fetchGithubStatus() {
  return rest ? rest('/github/status') : Promise.reject(new Error('backend not ready'))
}

function fetchGithubPrs() {
  return rest ? rest('/github/prs') : Promise.reject(new Error('backend not ready'))
}

function useTickets() {
  return useQuery({
    queryKey: [ID, 'tickets'], queryFn: fetchTickets,
    refetchInterval: TICKET_POLL, retry: false,
  })
}

/** Session list. `archived` is the server-side visibility mode: 'hide' (the
 *  default board) or 'all' (the rail's "Show archived"). The mode is part of
 *  the query key, so toggling the filter is a different, cached board. */
function useChats(archived = 'hide') {
  return useQuery({
    queryKey: [ID, 'chats', archived], queryFn: () => fetchChats(archived), retry: false,
    refetchInterval: (query) => {
      const data = query.state.data
      return data && (data.running || 0) > 0 ? LIST_POLL_RUNNING : LIST_POLL_QUIET
    },
  })
}

/** Server-side search. The query key carries the term, so a late response for
 *  an older term can never overwrite the newest one; a blank term disables the
 *  query and the rail falls back to the plain list. Results are not polled —
 *  a finished chat does not change what it said. */
function useChatSearch(term, archived = 'hide') {
  return useQuery({
    queryKey: [ID, 'chats', 'search', archived, term],
    queryFn: () => fetchChats(archived, term),
    enabled: !!term,
    retry: false,
  })
}

function useChat(id) {
  return useQuery({
    queryKey: [ID, 'chat', id], queryFn: () => fetchChat(id), enabled: !!id, retry: false,
    refetchInterval: (query) =>
      isBusy(query.state.data && query.state.data.session) ? DETAIL_POLL_RUNNING : DETAIL_POLL_QUIET,
  })
}

/** Lazy: the diff is fetched only while its panel is open. A live turn can
 *  still be writing files, so an open panel refetches every 4s; a finished
 *  session leaves the panel alone until Refresh is pressed. */
function useChatDiff(id, enabled, running) {
  return useQuery({
    queryKey: [ID, 'chat', id, 'diff'], queryFn: () => fetchChatDiff(id),
    enabled: !!id && !!enabled, retry: false,
    refetchInterval: enabled && running ? DIFF_POLL : false,
  })
}

function useModels() {
  return useQuery({
    queryKey: [ID, 'models'], queryFn: fetchModels, staleTime: 60000, retry: false,
  })
}

/** Only fetched while the Usage drawer is open: it is a report, not a poll. */
function useUsage(enabled, days = 14) {
  return useQuery({
    queryKey: [ID, 'usage', days], queryFn: () => fetchUsage(days),
    enabled: !!enabled, staleTime: 30000, retry: false,
  })
}

/** Merged settings, fetched only while the settings panel is open. */
function useSettings(enabled) {
  return useQuery({
    queryKey: [ID, 'settings'], queryFn: fetchSettings, enabled, retry: false,
    staleTime: 5000,
  })
}

/** GitHub connection state. One fetch on mount; polls only while `polling`. */
function useGithubStatus(polling) {
  return useQuery({
    queryKey: [ID, 'github'], queryFn: fetchGithubStatus, retry: false,
    staleTime: 10000, refetchInterval: polling ? GITHUB_POLL : false,
  })
}

/** Open PRs are fetched only while the panel is open (20s while it is). */
function useGithubPrs(enabled) {
  return useQuery({
    queryKey: [ID, 'github', 'prs'], queryFn: fetchGithubPrs, enabled, retry: false,
    refetchInterval: enabled ? GITHUB_POLL : false,
  })
}

function invalidateGithub(client) {
  client.invalidateQueries({ queryKey: [ID, 'github'] })
}

function invalidateChats(client, id) {
  client.invalidateQueries({ queryKey: [ID, 'chats'] })
  client.invalidateQueries({ queryKey: [ID, 'chat'] })
  if (id) client.invalidateQueries({ queryKey: [ID, 'chat', id] })
}

// -- small shared bits ------------------------------------------------------

function StatusGlyph({ status, pulse }) {
  return jsx('span', {
    'aria-hidden': 'true',
    className: cn('shrink-0 text-(--ui-text-quaternary)',
      pulse && status === 'running' && 'animate-pulse text-(--ui-text-secondary)'),
    children: ICONS[status] || '?',
  })
}

function ChatDot({ status }) {
  if (StatusDot) return jsx(StatusDot, { tone: STATUS_TONES[status] || 'muted' })
  return jsx('span', {
    'aria-hidden': 'true',
    className: 'inline-block shrink-0 text-(--ui-text-quaternary)',
    children: '•',
  })
}

/** `#123` chip that opens the pull request in the browser.
 *
 * A span (role=link), not a button: the rail rows and worker cards are
 * buttons themselves and a nested <button> is invalid DOM. */
function PrBadge({ session, className }) {
  const number = session && session.pr_number
  if (!number) return null
  const url = String((session && session.pr_url) || '')
  const open = (event) => {
    if (event) {
      event.stopPropagation()
      event.preventDefault()
    }
    openUrl(url)
  }
  return jsx('span', {
    role: 'link',
    tabIndex: 0,
    title: url ? `Open PR #${number} on GitHub` : `PR #${number}`,
    onClick: open,
    onKeyDown: (event) => {
      if (event && (event.key === 'Enter' || event.key === ' ')) open(event)
    },
    className: cn('inline-flex shrink-0 items-center rounded border border-(--ui-stroke-secondary)',
      'px-1 text-[0.6875rem] tabular-nums text-(--ui-text-tertiary)',
      'hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)', className),
    children: `#${number}`,
  })
}

/** GitHub status dot: green when a repo + token are configured. */
function GithubDot({ configured }) {
  if (StatusDot) return jsx(StatusDot, { tone: configured ? 'good' : 'muted' })
  return jsx('span', {
    'aria-hidden': 'true',
    className: cn('inline-block shrink-0 leading-none',
      configured ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-quaternary)'),
    children: '●',
  })
}

// One 28px square icon button for the header. The tooltip carries the label:
// the chrome has room for one primary action (New chat) and two glyphs.
const HEADER_ICON = cn(
  'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded',
  'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)',
)

function HeaderIconButton({ label, tip, active, onClick, buttonRef, children }) {
  return jsx(Tip, {
    label: tip || label,
    children: jsx('button', {
      type: 'button',
      ref: buttonRef,
      onClick,
      title: tip || label,
      'aria-label': label,
      'aria-expanded': !!active,
      className: cn(HEADER_ICON, active && 'bg-(--chrome-action-hover) text-(--ui-text-secondary)'),
      children,
    }),
  })
}

/** GitHub as a glyph: its status dot is the whole affordance, the tip names
 *  the repo. Keeps the header to a single 44px line. */
function GithubHeaderButton({ open, status, onClick, buttonRef }) {
  const configured = !!(status && status.configured)
  const repo = configured ? String(status.repo || '') : ''
  const tip = configured
    ? `GitHub: ${repo || 'connected'} — connection and open pull requests`
    : 'GitHub — connect a repository to watch pull requests'
  return jsx(HeaderIconButton, {
    label: 'GitHub', tip, active: open, onClick, buttonRef,
    children: jsx(GithubDot, { configured }),
  })
}

function ModeButton({ label, active, onClick }) {
  return jsx('button', {
    type: 'button',
    'aria-pressed': !!active,
    onClick,
    className: cn('rounded px-1.5 py-0.5 text-[0.6875rem]',
      active
        ? 'bg-(--chrome-action-hover) text-(--ui-text-secondary)'
        : 'text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)'),
    children: label,
  })
}

/** Comma separated input -> trimmed non-empty strings (the server dedupes). */
function splitList(text) {
  return String(text || '')
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
}

function GhostButton({
  children, onClick, disabled, title, className,
  'aria-label': ariaLabel, 'aria-expanded': ariaExpanded,
}) {
  return jsx('button', {
    type: 'button', onClick, disabled, title,
    'aria-label': ariaLabel, 'aria-expanded': ariaExpanded,
    className: cn('rounded px-2 py-1 text-xs text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)',
      disabled && 'opacity-50', className),
    children,
  })
}

function ErrorCard({ title, error, onRetry }) {
  return jsxs('div', {
    className: 'flex flex-col gap-1.5 rounded border border-(--ui-stroke-secondary) p-2.5 text-xs',
    children: [
      jsx('div', { key: 't', className: 'text-(--ui-text-secondary)', children: title || 'Superagent backend unavailable.' }),
      jsx('div', { key: 'e', className: 'break-words text-(--ui-text-quaternary)', children: errorText(error) || 'unknown error' }),
      jsx(GhostButton, { key: 'r', className: 'self-start', onClick: onRetry, children: 'Retry' }),
    ],
  })
}

// -- sessions rail ----------------------------------------------------------

// How long the fallback "Confirm delete" stays armed (the SDK dialog, when the
// shell has one, is the real confirm step).
const DELETE_ARM_MS = 3000

/** One line in the row's ⋯ menu. Plain buttons: the SDK menu portals its
 *  content out of the row, so a nested-button row would still be invalid. */
function RailMenuItem({ label, onSelect }) {
  return jsx('button', {
    type: 'button',
    role: 'menuitem',
    onClick: (event) => {
      // Never let a menu click reach the row under it: selection must not move.
      event.stopPropagation()
      event.preventDefault()
      onSelect()
    },
    className: cn('flex w-full min-w-0 items-center rounded px-2 py-1 text-left text-xs',
      'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'),
    children: label,
  })
}

/** SessionRow: a pointer row plus a hover/focus-only ⋯ menu.
 *
 *  The row is a div wrapping a button because the ⋯ trigger is a second
 *  control: a nested <button> is invalid DOM. The ⋯ sits exactly where the age
 *  is and the age fades out under it, so nothing shifts on hover. Rename swaps
 *  the whole row for an inline input (Enter saves, Esc cancels, blur saves when
 *  the text changed); archive toggles archived_at; delete asks first — through
 *  the SDK ConfirmDialog when the shell exports one, otherwise this button
 *  becomes "Confirm delete" for three seconds.
 */
function SessionRow({
  session, selected, onSelect, onRename, onArchive, onDelete, onDeleteAsk, opensUp, match,
}) {
  const archived = !!session.archived_at
  const [menuOpen, setMenuOpen] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState('')
  const [armed, setArmed] = useState(false)
  const armTimer = useRef(null)
  // Enter, Esc and blur can all arrive for one edit; only the first settles it.
  const settled = useRef(false)
  const age = session.status === 'running'
    ? timingLabel(session)
    : fmtAge(session.updated_at || session.created_at)
  // One line per chat: title and age. Model and repo live in the tooltip so a
  // 16rem rail stays scannable at a glance.
  const tooltip = [sessionTitle(session), sessionModelLabel(session), String(session.repo || '').trim()]
    .filter(Boolean)
    .join(' · ')

  useEffect(() => () => {
    if (armTimer.current) clearTimeout(armTimer.current)
  }, [])

  const startRename = () => {
    settled.current = false
    setTitle(sessionTitle(session))
    setRenaming(true)
  }

  const cancelRename = () => {
    settled.current = true
    setRenaming(false)
  }

  const saveRename = () => {
    if (settled.current) return
    settled.current = true
    const next = String(title || '').trim()
    setRenaming(false)
    if (!next || next === sessionTitle(session)) return
    onRename(session.id, next)
  }

  const requestDelete = () => {
    if (ConfirmDialog) {
      onDeleteAsk(session)
      return
    }
    setArmed(true)
    if (armTimer.current) clearTimeout(armTimer.current)
    armTimer.current = setTimeout(() => setArmed(false), DELETE_ARM_MS)
  }

  const confirmDelete = () => {
    if (armTimer.current) clearTimeout(armTimer.current)
    setArmed(false)
    onDelete(session.id)
  }

  const items = [
    jsx(RailMenuItem, {
      key: 'rename', label: 'Rename',
      onSelect: () => { setMenuOpen(false); startRename() },
    }),
    jsx(RailMenuItem, {
      key: 'archive', label: archived ? 'Unarchive' : 'Archive',
      onSelect: () => { setMenuOpen(false); onArchive(session.id, !archived) },
    }),
    jsx(RailMenuItem, {
      key: 'delete', label: 'Delete',
      onSelect: () => { setMenuOpen(false); requestDelete() },
    }),
  ]

  const sdkMenu = !!(DropdownMenu && DropdownMenuTrigger && DropdownMenuContent)
  const trigger = jsx('button', {
    type: 'button',
    'aria-label': `Chat actions for ${sessionTitle(session)}`,
    'aria-haspopup': 'menu',
    'aria-expanded': !!menuOpen,
    title: 'Rename, archive or delete',
    onClick: sdkMenu
      ? (event) => event.stopPropagation()
      : (event) => { event.stopPropagation(); setMenuOpen((value) => !value) },
    className: cn('inline-flex h-5 w-5 items-center justify-center rounded leading-none',
      'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)',
      menuOpen && 'bg-(--chrome-action-hover) text-(--ui-text-secondary)'),
    children: '⋯',
  })
  const more = sdkMenu
    ? jsxs(DropdownMenu, {
        open: menuOpen, onOpenChange: setMenuOpen,
        children: [
          jsx(DropdownMenuTrigger, { key: 'trigger', asChild: true, children: trigger }),
          jsx(DropdownMenuContent, {
            key: 'content', align: 'end', className: 'w-40 p-1', children: items,
          }),
        ],
      })
    : jsxs('div', {
        className: 'relative',
        // A legacy shell has no portalled menu, and the list scrolls: close on
        // focus loss / Escape, and open upwards near the bottom of the rail so
        // the list's overflow cannot clip the three items.
        onBlur: (event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) setMenuOpen(false)
        },
        onKeyDown: (event) => {
          if (event.key === 'Escape') setMenuOpen(false)
        },
        children: [
          trigger,
          menuOpen
            ? jsx('div', {
                role: 'menu',
                'aria-label': 'Chat actions',
                className: cn('absolute right-0 z-30 flex w-40 flex-col gap-0.5 rounded border',
                  'border-(--ui-stroke-secondary) bg-background p-1 shadow-md',
                  opensUp ? 'bottom-6' : 'top-6'),
                children: items,
              })
            : null,
        ],
      })

  return jsxs('div', {
    'data-archived': archived ? '1' : undefined,
    className: cn('group relative flex w-full min-w-0 items-center rounded',
      selected ? 'bg-(--chrome-action-hover)' : 'hover:bg-(--chrome-action-hover)'),
    children: [
      renaming
        ? jsxs('div', {
            key: 'edit',
            className: 'flex w-full min-w-0 items-center gap-1.5 px-2 py-1',
            children: [
              jsx(StatusGlyph, { key: 'g', status: session.status }),
              jsx('input', {
                key: 'i',
                type: 'text',
                value: title,
                autoFocus: true,
                maxLength: 200,
                'aria-label': 'Rename chat',
                onMouseDown: (event) => event.stopPropagation(),
                onClick: (event) => event.stopPropagation(),
                onChange: (event) => setTitle(event.target.value),
                onKeyDown: (event) => {
                  event.stopPropagation()
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    saveRename()
                  } else if (event.key === 'Escape') {
                    event.preventDefault()
                    cancelRename()
                  }
                },
                onBlur: saveRename,
                className: cn('min-w-0 flex-1 rounded border border-(--ui-stroke-secondary)',
                  'bg-transparent px-1 py-0.5 text-xs text-(--ui-text-secondary) focus:outline-none'),
              }),
            ],
          })
        : jsxs('button', {
            key: 'row',
            type: 'button',
            onClick: () => onSelect(session.id),
            title: tooltip,
            'aria-current': selected ? 'true' : undefined,
            className: cn('flex w-full min-w-0 items-baseline gap-1.5 rounded px-2 py-1 text-left',
              archived && 'opacity-60'),
            children: [
              jsx(StatusGlyph, { key: 'g', status: session.status, pulse: true }),
              jsx('span', {
                key: 't', className: 'flex min-w-0 flex-1 flex-col',
                children: [
                  jsx('span', {
                    key: 'title', className: 'truncate text-xs text-(--ui-text-secondary)',
                    children: sessionTitle(session),
                  }),
                  // Why the chat matched: only while a search is active, so a
                  // normal rail row stays one line.
                  match
                    ? jsx('span', {
                        key: 'match',
                        title: match,
                        className: 'truncate text-[0.6875rem] leading-4 text-(--ui-text-quaternary)',
                        children: match,
                      })
                    : null,
                ],
              }),
              archived
                ? jsx('span', {
                    key: 'tag',
                    className: cn('shrink-0 rounded border border-(--ui-stroke-secondary) px-1',
                      'text-[0.625rem] leading-4 text-(--ui-text-quaternary)'),
                    children: 'archived',
                  })
                : null,
              jsx('span', {
                key: 'a',
                className: cn('shrink-0 text-[0.6875rem] tabular-nums text-(--ui-text-quaternary)',
                  'group-hover:opacity-0 group-focus-within:opacity-0'),
                children: age,
              }),
            ],
          }),
      renaming
        ? null
        : armed
          ? jsx('button', {
              key: 'confirm',
              type: 'button',
              onClick: (event) => { event.stopPropagation(); confirmDelete() },
              className: cn('absolute right-0.5 top-1/2 z-10 -translate-y-1/2 rounded border',
                'border-(--ui-stroke-secondary) bg-(--ui-bg-primary) px-1 py-0.5',
                'text-[0.6875rem] text-(--ui-text-secondary) shadow-sm'),
              children: 'Confirm delete',
            })
          : jsx('div', {
              key: 'more',
              className: cn('absolute right-0.5 top-1/2 flex -translate-y-1/2 items-center',
                'opacity-0 transition-opacity focus-within:opacity-100',
                'group-hover:opacity-100 group-focus-within:opacity-100',
                // An open menu must stay visible even if the pointer leaves and
                // the browser did not focus the trigger on click.
                menuOpen && 'opacity-100'),
              children: more,
            }),
    ],
  })
}

const RAIL_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'live', label: 'Live' },
  { id: 'review', label: 'Review' },
]

/** Segment predicates. Filtering never clears the selection: the page keeps
 *  `selectedId`, so a hidden session still has its transcript on screen. */
function railFilterMatch(session, filter) {
  if (filter === 'live') return isBusy(session)
  if (filter === 'review') return session.status === 'review' || session.status === 'failed'
  return true
}

const RAIL_EMPTY = {
  all: 'No chats yet.',
  live: 'Nothing running.',
  review: 'Nothing to review.',
}

/** True under the md breakpoint (768px). Only used to fold the mobile rail
 *  after a pick; the layout itself is CSS-driven, so a wrong answer is benign. */
function isNarrowViewport() {
  try {
    if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
      return !!window.matchMedia('(max-width: 767px)').matches
    }
  } catch (error) {
    // Older shells: assume a wide window.
  }
  return false
}

function RailFilter({ value, onChange }) {
  return jsx('div', {
    role: 'group',
    'aria-label': 'Filter chats',
    className: 'flex shrink-0 items-center gap-0.5',
    children: RAIL_FILTERS.map((option) => jsx('button', {
      key: option.id,
      type: 'button',
      'aria-pressed': value === option.id,
      onClick: () => onChange(option.id),
      className: cn('rounded px-1.5 py-0.5 text-[0.6875rem]',
        value === option.id
          ? 'bg-(--chrome-action-hover) text-(--ui-text-secondary)'
          : 'text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)'),
      children: option.label,
    })),
  })
}

function SessionsRail({
  sessions, selectedId, onSelect, onRename, onArchive, onDelete,
  showArchived, onToggleArchived, archivedCount,
}) {
  const [filter, setFilter] = useState('all')
  const [query, setQuery] = useState('')
  // The debounced term is what the server sees; `query` stays instant so the
  // input never feels laggy.
  const [term, setTerm] = useState('')
  useEffect(() => {
    const handle = setTimeout(() => setTerm(String(query || '').trim()), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(handle)
  }, [query])
  const search = useChatSearch(term, showArchived ? 'all' : 'hide')
  const searching = !!term
  const source = searching ? ((search.data && search.data.sessions) || []) : sessions
  // The delete confirm step: the SDK dialog needs to live above the row that
  // opened it (a row can unmount as soon as the list refreshes).
  const [pendingDelete, setPendingDelete] = useState(null)
  // Under `md` the rail folds into a 'Chats' section above the chat; picking a
  // chat closes it so the transcript gets the screen. md+ ignores this state.
  const [open, setOpen] = useState(() => !selectedId)
  const shown = useMemo(
    () => source.filter((session) => railFilterMatch(session, filter)),
    [source, filter],
  )

  const choose = (id) => {
    onSelect(id)
    if (isNarrowViewport()) setOpen(false)
  }

  const removeNow = (id) => {
    setPendingDelete(null)
    onDelete(id)
  }

  const noChats = source.length === 0
  const emptyText = searching
    ? (search.isFetching && !search.data ? 'Searching…' : 'No matching chats.')
    : showArchived
      ? (noChats ? 'No archived chats.' : 'No matching archived chats.')
      : noChats
        ? 'No chats yet.'
        : RAIL_EMPTY[filter]
  const countLabel = searching
    ? `${shown.length} match${shown.length === 1 ? '' : 'es'}`
    : shown.length === sessions.length
      ? `${sessions.length} chat${sessions.length === 1 ? '' : 's'}`
      : `${shown.length} of ${sessions.length}`
  const showToggle = !!showArchived || (Number(archivedCount) || 0) > 0

  return jsxs('div', {
    className: 'flex min-h-0 w-full flex-col gap-2 md:w-[16rem] md:shrink-0',
    children: [
      jsxs('button', {
        key: 'fold',
        type: 'button',
        'aria-expanded': open,
        'aria-label': 'Chats',
        onClick: () => setOpen((value) => !value),
        className: cn('flex h-7 w-full items-center gap-1.5 rounded px-2 text-xs md:hidden',
          'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'),
        children: [
          jsx(Caret, { key: 'c', open }),
          jsx('span', { key: 'l', className: 'font-medium', children: 'Chats' }),
          jsx('span', {
            key: 'n', className: 'ml-auto text-[0.6875rem] tabular-nums text-(--ui-text-quaternary)',
            children: sessions.length ? String(sessions.length) : '',
          }),
        ],
      }),
      jsxs('div', {
        key: 'body',
        className: cn('flex min-h-0 flex-col gap-2', open ? 'flex' : 'hidden md:flex'),
        children: [
          jsx('input', {
            key: 'search',
            type: 'search',
            value: query,
            placeholder: 'Search chats',
            'aria-label': 'Search chats',
            autoComplete: 'off',
            onChange: (event) => setQuery(event.target.value),
            className: cn('w-full min-w-0 rounded border border-(--ui-stroke-secondary) bg-transparent',
              'px-2 py-1 text-xs text-(--ui-text-secondary)',
              'placeholder:text-(--ui-text-quaternary) focus:outline-none'),
          }),
          jsx(RailFilter, { key: 'filter', value: filter, onChange: setFilter }),
          jsx('div', {
            key: 'list',
            className: 'flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto pr-1',
            children: shown.length
              ? shown.map((session, index) => jsx(SessionRow, {
                  key: session.id, session, selected: session.id === selectedId,
                  onSelect: choose, onRename, onArchive, onDelete,
                  onDeleteAsk: setPendingDelete,
                  match: searching ? session.match_snippet : '',
                  // The last two rows open their menu upwards so the list's
                  // overflow cannot clip it in a shell without a portal.
                  opensUp: index >= shown.length - 2,
                }))
              : jsx('div', { className: 'px-2 py-1 text-xs text-(--ui-text-tertiary)', children: emptyText }),
          }),
          jsxs('div', {
            key: 'foot',
            className: cn('flex shrink-0 items-center justify-between gap-1 px-2',
              'text-[0.6875rem] tabular-nums text-(--ui-text-quaternary)'),
            children: [
              jsx('span', { key: 'n', className: 'min-w-0 truncate', children: countLabel }),
              showToggle
                ? jsx('button', {
                    key: 'archived',
                    type: 'button',
                    'aria-pressed': !!showArchived,
                    onClick: onToggleArchived,
                    title: 'Include archived chats in the rail',
                    className: cn('shrink-0 rounded px-1 py-0.5',
                      showArchived
                        ? 'bg-(--chrome-action-hover) text-(--ui-text-secondary)'
                        : 'hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)'),
                    children: showArchived
                      ? 'Hide archived'
                      : `Show archived${Number(archivedCount) ? ` (${archivedCount})` : ''}`,
                  })
                : null,
            ],
          }),
        ],
      }),
      ConfirmDialog
        ? jsx(ConfirmDialog, {
            key: 'delete',
            open: !!pendingDelete,
            onClose: () => setPendingDelete(null),
            onConfirm: () => { if (pendingDelete) removeNow(pendingDelete.id) },
            title: 'Delete this chat?',
            description: pendingDelete
              ? `“${sessionTitle(pendingDelete)}” and its transcript are deleted for good. Files, worktrees and pull requests are not touched.`
              : '',
            confirmLabel: 'Delete',
            destructive: true,
            dismissOnConfirm: true,
          })
        : null,
    ],
  })
}

// -- transcript -------------------------------------------------------------

/** Prompt bubble first (the opening turn is not a chat_messages row), then
 *  messages + tool events merged on created_at. */
function buildTranscript(session) {
  if (!session) return []
  const items = []
  // An empty turn is not a bubble: a blank prompt would otherwise paint an
  // empty card and hide the "No transcript yet" state.
  if (String(session.prompt || '').trim()) {
    items.push({
      key: 'prompt', kind: 'message', role: 'user',
      content: session.prompt || '', created_at: session.created_at || 0,
    })
  }
  for (const message of session.messages || []) {
    if (!String(message.content || '').trim()) continue
    items.push({
      key: `m${message.id}`, kind: 'message', role: message.role,
      content: message.content || '', created_at: message.created_at || 0,
    })
  }
  for (const event of session.events || []) {
    items.push({
      key: `e${event.id}`, kind: 'event', eventKind: event.kind,
      detail: event.detail || '', created_at: event.created_at || 0,
    })
  }
  items.sort((a, b) => a.created_at - b.created_at)
  return items
}

/** Fold runs of consecutive audit events into one Activity block.
 *
 * Messages break a run, so the event stream still reads in wall-clock order:
 * tool, tool, assistant answer, tool, tool. A single event also becomes an
 * Activity block — a lone "bash: ls" line outside the fold would look like a
 * different kind of thing from the same line inside it. */
function groupTranscript(items) {
  const blocks = []
  let run = null
  for (const item of items) {
    if (item.kind !== 'event') {
      run = null
      blocks.push({ key: item.key, kind: 'message', role: item.role, content: item.content, created_at: item.created_at })
      continue
    }
    if (!run) {
      run = { key: `a${item.key}`, kind: 'activity', events: [], created_at: item.created_at }
      blocks.push(run)
    }
    run.events.push(item)
  }
  return blocks
}

/** 'Assistant' / 'User' — capitalized role, never the raw enum. */
function roleLabel(role) {
  const value = String(role || '').trim()
  if (!value) return 'Assistant'
  return value.charAt(0).toUpperCase() + value.slice(1)
}

/** Assistant text: the app's own Markdown pipeline when this shell ships it.
 *  Older shells keep the plain pre-wrap rendering (no lost content). */
function MessageBody({ text }) {
  const value = String(text == null ? '' : text)
  if (!value) return null
  if (MessageTextContent) return jsx(MessageTextContent, { text: value })
  return jsx('div', { className: 'whitespace-pre-wrap break-words', children: value })
}

/** Disclosure glyph: the chrome caret when exported, a text twin otherwise. */
function Caret({ open }) {
  if (DisclosureCaret) return jsx(DisclosureCaret, { open })
  return jsx('span', { 'aria-hidden': 'true', className: 'shrink-0', children: open ? '▾' : '▸' })
}

/** One-line `kind: detail` summary for a collapsed Activity block. */
function eventSummary(event) {
  const kind = String((event && event.eventKind) || '').trim()
  const detail = String((event && event.detail) || '').replace(/\s+/g, ' ').trim()
  if (!detail) return kind
  return clip(`${kind}: ${detail}`, 160)
}

/** Per-answer header: who said it, when, and a copy affordance.
 *  The timestamp is the message's own created_at, not the poll time. */
function MessageMeta({ role, createdAt, content }) {
  const clock = fmtClock(createdAt)
  return jsxs('div', {
    className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem] text-(--ui-text-quaternary)',
    children: [
      jsx('span', { key: 'r', className: 'font-medium text-(--ui-text-tertiary)', children: roleLabel(role) }),
      clock ? jsx('span', { key: 't', className: 'tabular-nums', children: clock }) : null,
      CopyButton
        ? jsx(CopyButton, { key: 'c', text: String(content == null ? '' : content), appearance: 'icon', label: 'Copy message' })
        : null,
    ],
  })
}

const Bubble = memo(function Bubble({ role, content, createdAt }) {
  const mine = role === 'user'
  const body = String(content == null ? '' : content)
  const clock = fmtClock(createdAt)
  // User turns stay verbatim: Markdown in a prompt is the user's text, not a
  // document to typeset. Only the assistant answer renders as Markdown.
  if (mine) {
    return jsxs('div', {
      className: 'flex max-w-[85%] min-w-0 flex-col items-end gap-0.5 self-end',
      children: [
        jsx('div', {
          key: 'b',
          className: cn('min-w-0 rounded-lg bg-(--chrome-action-hover) px-3 py-1.5',
            'whitespace-pre-wrap break-words text-sm leading-relaxed text-(--ui-text-secondary)'),
          children: body,
        }),
        clock
          ? jsx('span', {
              key: 't', className: 'text-[0.625rem] tabular-nums text-(--ui-text-quaternary)',
              children: clock,
            })
          : null,
      ],
    })
  }
  // An answer is a document, not a card: no border, just generous line-height
  // so a long reply reads like the main chat. Code blocks keep their own fill.
  return jsxs('div', {
    className: 'flex w-full max-w-[92%] min-w-0 flex-col gap-1 self-start',
    children: [
      jsx(MessageMeta, { key: 'h', role, createdAt, content: body }),
      jsx('div', {
        key: 'b',
        className: 'min-w-0 max-w-full text-sm leading-[1.7] text-(--ui-text-secondary)',
        children: jsx(MessageBody, { text: body }),
      }),
    ],
  })
})

/** One audit line: `kind: detail`, monospace, dim, stamped HH:MM. */
function EventRow({ item }) {
  const detail = String(item.detail || '')
  const clock = fmtClock(item.created_at)
  return jsxs('div', {
    className: 'flex min-w-0 items-baseline gap-1.5 pl-1 text-[0.6875rem] text-(--ui-text-quaternary)',
    children: [
      jsx('span', { key: 'k', className: 'shrink-0 font-mono', children: item.eventKind }),
      jsx('span', { key: 'c', className: 'shrink-0', children: ':' }),
      detail
        ? jsx('span', {
            key: 'd', title: detail,
            className: 'min-w-0 flex-1 truncate font-mono',
            children: clip(detail.replace(/\s+/g, ' '), 160),
          })
        : null,
      clock ? jsx('span', { key: 't', className: 'shrink-0 tabular-nums', children: clock }) : null,
    ],
  })
}

/** A run of audit events, collapsible. Live turns open by default (the work
 *  is what you are watching); a finished turn collapses to its last line so
 *  the answer is not buried under tool noise. */
function Activity({ events, running, sessionId }) {
  const [open, setOpen] = useState(!!running)
  const wasRunning = useRef(!!running)
  const latest = events[events.length - 1]
  const clock = fmtClock(latest && latest.created_at)
  const count = events.length

  // A poll can flip the status while the block stays mounted; follow it, but
  // never override a click made while the status is unchanged.
  useEffect(() => {
    if (wasRunning.current === !!running) return
    wasRunning.current = !!running
    setOpen(!!running)
  }, [running])

  return jsxs('div', {
    className: 'flex w-full min-w-0 flex-col gap-0.5',
    children: [
      jsxs('button', {
        key: 'head',
        type: 'button',
        'aria-expanded': open,
        'aria-label': `Activity, ${count} ${count === 1 ? 'step' : 'steps'}`,
        onClick: () => setOpen((value) => !value),
        className: cn('flex w-full min-w-0 items-center gap-1.5 self-start rounded px-1 py-0.5',
          'text-[0.6875rem] text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover)'),
        children: [
          jsx(Caret, { key: 'caret', open }),
          jsx('span', {
            key: 'label', className: 'shrink-0 font-medium',
            children: `Activity · ${count} ${count === 1 ? 'step' : 'steps'}`,
          }),
          open || !latest
            ? null
            : jsx('span', {
                key: 'summary',
                className: 'min-w-0 flex-1 truncate text-left text-(--ui-text-quaternary)',
                children: eventSummary(latest),
              }),
          clock
            ? jsx('span', { key: 'clock', className: 'ml-auto shrink-0 pl-2 tabular-nums text-(--ui-text-quaternary)', children: clock })
            : null,
        ],
      }),
      open
        ? jsx('div', {
            key: 'body',
            className: 'flex flex-col gap-0.5',
            children: events.map((event) => jsx(EventRow, { key: `${sessionId}:${event.key}`, item: event })),
          })
        : null,
    ],
  })
}

/** Skeleton-ish placeholder while a live turn has not produced an answer yet. */
function WaitingCard({ session }) {
  return jsxs('div', {
    className: 'flex max-w-[85%] min-w-0 flex-col gap-1.5 self-start',
    children: [
      jsxs('div', {
        key: 'meta',
        className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
        children: [
          jsx('span', { key: 's', className: 'font-medium', children: session.status || 'running' }),
          jsx('span', { key: 'd', children: '·' }),
          jsx('span', { key: 'm', className: 'min-w-0 truncate', children: sessionModelLabel(session) }),
        ],
      }),
      jsx('div', {
        key: 't', className: 'text-sm text-(--ui-text-secondary)',
        children: 'Waiting for the first response…',
      }),
      jsxs('div', {
        key: 'sk', className: 'flex flex-col gap-1 pt-0.5', 'aria-hidden': 'true',
        children: [
          jsx('span', { key: 'a', className: 'h-1.5 w-40 animate-pulse rounded bg-(--chrome-action-hover)' }),
          jsx('span', { key: 'b', className: 'h-1.5 w-28 animate-pulse rounded bg-(--chrome-action-hover)' }),
          jsx('span', { key: 'c', className: 'h-1.5 w-32 animate-pulse rounded bg-(--chrome-action-hover)' }),
        ],
      }),
    ],
  })
}

function Transcript({ session, optimistic }) {
  const items = useMemo(() => buildTranscript(session), [session])
  const blocks = useMemo(() => groupTranscript(items), [items])
  const boxRef = useRef(null)
  const stickRef = useRef(true)
  const [away, setAway] = useState(false)
  const sessionId = (session && session.id) || ''
  const tail = items.length ? items[items.length - 1].key : 'empty'
  const running = isBusy(session)
  const hasAnswer = items.some((item) => item.kind === 'message' && item.role !== 'user')
  const waiting = !!session && !hasAnswer && (session.status === 'queued' || session.status === 'running')
  // An optimistic turn is a local bubble until the server's own user row
  // lands. Counting matching rows means sending the same text twice still
  // shows a bubble the second time, and never paints two for one send.
  const pending = optimistic && optimistic.text ? optimistic : null
  const pendingShown = pending
    ? items.filter((item) => item.kind === 'message' && item.role === 'user' &&
        String(item.content || '').trim() === pending.text).length
    : 0
  const showPending = !!pending && pendingShown <= (Number(pending.seen) || 0)

  // Switching chats starts at the newest line; polling never moves the box
  // unless the reader was already at the bottom.
  useEffect(() => { stickRef.current = true; setAway(false) }, [sessionId])
  useEffect(() => {
    const el = boxRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [sessionId, items.length, tail, showPending])

  const scrollToLatest = () => {
    stickRef.current = true
    setAway(false)
    const el = boxRef.current
    if (el) el.scrollTop = el.scrollHeight
  }

  const body = blocks.length
    ? blocks.map((block) => (block.kind === 'activity'
        ? jsx(Activity, {
            key: `${sessionId}:${block.key}`, events: block.events, running, sessionId,
          })
        : jsx(Bubble, {
            key: `${sessionId}:${block.key}`, role: block.role, content: block.content,
            createdAt: block.created_at,
          })))
    : []

  if (waiting) body.push(jsx(WaitingCard, { key: 'waiting', session }))
  if (showPending) {
    body.push(jsxs('div', {
      key: 'pending',
      'data-pending': '1',
      className: 'flex max-w-[85%] min-w-0 flex-col items-end gap-0.5 self-end',
      children: [
        jsx('div', {
          key: 'b',
          className: cn('min-w-0 rounded-lg bg-(--chrome-action-hover) px-3 py-1.5',
            'whitespace-pre-wrap break-words text-sm leading-relaxed text-(--ui-text-secondary) opacity-70'),
          children: pending.text,
        }),
        jsx('span', {
          key: 't', className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: 'Sending…',
        }),
      ],
    }))
  }
  if (!body.length) {
    body.push(jsx('div', {
      key: 'empty', className: 'text-xs text-(--ui-text-tertiary)', children: 'No transcript yet.',
    }))
  }

  return jsxs('div', {
    className: 'relative flex min-h-0 flex-1 flex-col',
    children: [
      jsx('div', {
        key: 'scroll',
        ref: boxRef,
        onScroll: () => {
          const el = boxRef.current
          if (!el) return
          const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 96
          stickRef.current = atBottom
          setAway(!atBottom)
        },
        className: cn('flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-1 py-1'),
        children: body,
      }),
      away
        ? jsx('button', {
            key: 'jump',
            type: 'button',
            onClick: scrollToLatest,
            className: cn('absolute bottom-3 right-3 inline-flex items-center gap-1 rounded-full border',
              'border-(--ui-stroke-secondary) bg-background px-2.5 py-1 text-[0.6875rem]',
              'text-(--ui-text-secondary) shadow-sm hover:bg-(--chrome-action-hover)'),
            children: '↓ Jump to latest',
          })
        : null,
    ],
  })
}

// -- model picker -----------------------------------------------------------

function PresetSelect({ value, models, onChange }) {
  const presets = (models && models.models) || []
  const current = value.model || ''
  const options = current && presets.indexOf(current) === -1 ? [current].concat(presets) : presets
  return jsxs('select', {
    value: current,
    onChange: (event) => onChange({ model: event.target.value, provider: '' }),
    className: cn('max-w-[14rem] rounded border border-(--ui-stroke-secondary) bg-transparent',
      'px-1.5 py-1 text-[0.6875rem] text-(--ui-text-secondary)'),
    children: [
      jsx('option', { key: 'default', value: '', children: 'Default model' }),
      ...options.map((name) => jsx('option', { key: name, value: name, children: name })),
    ],
  })
}

/** DropdownMenu + the app's own ModelCatalogMenu driven by a detached
 *  controller (the kanban pattern): the menu renders, we own what a pick
 *  means. Falls back to a preset <select> on shells without the catalog. */
function ModelPicker({ value, models, onChange, disabled }) {
  const [open, setOpen] = useState(false)
  if (!DropdownMenu || !DropdownMenuTrigger || !DropdownMenuContent || !ModelCatalogMenu) {
    return jsx(PresetSelect, { value, models, onChange })
  }
  const controller = {
    allowSpeed: false,
    applyPreset: (preset, row) => onChange({ model: row.model, provider: row.provider }),
    current: { effort: '', fast: false, model: value.model, provider: value.provider },
    presetFor: () => ({}),
    select: (model, provider) => {
      onChange({ model, provider })
      setOpen(false)
    },
    setOptions: (patch, row) => {
      if (patch.effort === undefined) return
      onChange({ model: row.model, provider: row.provider })
    },
  }
  const menu = jsx(ModelCatalogMenu, {
    controller, includeMoa: false,
    request: host && typeof host.request === 'function' ? host.request : undefined,
  })
  const body = ModelMenuCloseContext && ModelMenuCloseContext.Provider
    ? jsx(ModelMenuCloseContext.Provider, { value: () => setOpen(false), children: menu })
    : menu

  return jsxs(DropdownMenu, {
    open, onOpenChange: setOpen,
    children: [
      jsx(DropdownMenuTrigger, {
        key: 'trigger', asChild: true,
        children: jsxs('button', {
          type: 'button', disabled,
          className: cn('flex max-w-[14rem] items-center gap-1 rounded border border-(--ui-stroke-secondary)',
            'px-1.5 py-1 text-[0.6875rem] text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)',
            disabled && 'opacity-50'),
          children: [
            jsx('span', { key: 'l', className: 'min-w-0 truncate', children: modelLabel(value.model, value.provider) }),
            jsx('span', { key: 'c', className: 'shrink-0 text-(--ui-text-quaternary)', children: '▾' }),
          ],
        }),
      }),
      jsx(DropdownMenuContent, { key: 'content', align: 'start', className: 'w-72 p-0', children: body }),
    ],
  })
}

/** Existing chats keep their model, but a small button swaps it for the next
 *  turn via POST /chats/{id}/model. Same ModelPicker, opened on demand. */
function SessionModelControl({ session, models, onChange, pending }) {
  const [editing, setEditing] = useState(false)
  const busy = isBusy(session)
  const disabled = busy || pending
  if (!editing) {
    return jsxs('span', {
      className: 'flex min-w-0 items-center gap-1',
      children: [
        jsx('span', { key: 'l', className: 'min-w-0 truncate', children: sessionModelLabel(session) }),
        jsx(GhostButton, {
          key: 'b',
          className: 'shrink-0',
          disabled,
          title: busy
            ? 'The model can change when this turn finishes'
            : 'Change the model for future turns of this chat',
          onClick: () => setEditing(true),
          children: pending ? 'Saving…' : 'Change model',
        }),
      ],
    })
  }
  return jsx(ModelPicker, {
    value: { model: session.model || '', provider: session.provider || '' },
    models,
    disabled: pending,
    onChange: (next) => {
      setEditing(false)
      if (onChange) onChange(next)
    },
  })
}

// -- settings panel ---------------------------------------------------------

const MAX_PRESET_ROWS = 12
const SETTINGS_HINT = 'Presets appear in the composer; pick one before starting a chat.'

function blankPreset() {
  return { name: '', model: '', provider: '' }
}

function presetRowsFrom(settings) {
  const rows = settings && Array.isArray(settings.model_presets) ? settings.model_presets : []
  return rows.map((preset) => ({
    name: String((preset && preset.name) || ''),
    model: String((preset && preset.model) || ''),
    provider: String((preset && preset.provider) || ''),
  }))
}

/** Compact token label for a report: 1234567 -> "1.2M". */
function shortTokens(value) {
  const number = Number(value) || 0
  if (number >= 1e6) return `${(number / 1e6).toFixed(1)}M`
  if (number >= 1e3) return `${(number / 1e3).toFixed(1)}k`
  return String(Math.round(number))
}

/** Read-only activity report: five figures, 14 daily bars, and the models
 *  that did the work. Nothing here polls — a report that refetches while you
 *  read it is noise, so it loads once when the drawer opens. */
function UsagePanel({ usage, loading, error, onRetry }) {
  if (error) {
    return jsx(ErrorCard, { title: 'Could not load usage.', error, onRetry })
  }
  if (loading || !usage) {
    return jsx('div', {
      className: 'p-3 text-xs text-(--ui-text-tertiary)', children: 'Loading usage…',
    })
  }
  const totals = usage.totals || {}
  const counts = totals.counts || {}
  const series = usage.series || []
  const models = usage.models || []
  const peak = Math.max(1, ...series.map((day) => Number(day.tokens) || 0))
  const stats = [
    ['Chats', String(totals.sessions || 0)],
    ['Turns', String(totals.turns || 0)],
    ['Tokens', shortTokens(totals.tokens)],
    ['Review', String(counts.review || 0)],
    ['Failed', String(counts.failed || 0)],
  ]
  return jsxs('div', {
    className: 'flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 text-xs',
    children: [
      jsx('div', {
        key: 'stats', className: 'flex flex-wrap gap-2',
        children: stats.map(([label, value]) => jsxs('div', {
          key: label,
          className: cn('flex min-w-[5rem] flex-col gap-0.5 rounded border',
            'border-(--ui-stroke-secondary) px-2 py-1.5'),
          children: [
            jsx('span', {
              key: 'l',
              className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)',
              children: label,
            }),
            jsx('span', {
              key: 'v', className: 'text-sm tabular-nums text-(--ui-text-secondary)',
              children: value,
            }),
          ],
        })),
      }),
      jsxs('div', {
        key: 'chart', className: 'flex flex-col gap-1',
        children: [
          jsx('span', {
            key: 'h',
            className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)',
            children: `Tokens · last ${usage.days || series.length} days`,
          }),
          jsx('div', {
            key: 'bars', className: 'flex h-16 items-end gap-0.5',
            children: series.map((day) => {
              const tokens = Number(day.tokens) || 0
              return jsx('div', {
                key: day.date,
                title: `${day.date}: ${day.sessions || 0} chat(s) · ${tokens.toLocaleString()} tokens`,
                className: cn('flex-1 rounded-t',
                  tokens ? 'bg-(--accent) opacity-60' : 'bg-(--ui-stroke-secondary)'),
                style: {
                  height: `${Math.max(tokens ? 4 : 1, Math.round((tokens / peak) * 100))}%`,
                },
              })
            }),
          }),
          jsxs('div', {
            key: 'axis',
            className: 'flex justify-between text-[0.625rem] text-(--ui-text-quaternary)',
            children: [
              jsx('span', { key: 'a', children: series.length ? series[0].date : '' }),
              jsx('span', {
                key: 'b', children: series.length ? series[series.length - 1].date : '',
              }),
            ],
          }),
        ],
      }),
      models.length
        ? jsxs('div', {
            key: 'models', className: 'flex flex-col gap-1',
            children: [
              jsx('span', {
                key: 'h',
                className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)',
                children: 'By model',
              }),
              ...models.slice(0, 8).map((row) => jsxs('div', {
                key: `${row.model}|${row.provider}`,
                className: cn('flex items-baseline justify-between gap-2 rounded px-1 py-0.5',
                  'hover:bg-(--chrome-action-hover)'),
                children: [
                  jsx('span', {
                    key: 'm',
                    className: 'min-w-0 truncate font-mono text-[0.6875rem] text-(--ui-text-secondary)',
                    children: row.model || 'unknown',
                  }),
                  jsx('span', {
                    key: 'n',
                    className: 'shrink-0 tabular-nums text-[0.6875rem] text-(--ui-text-quaternary)',
                    children: `${row.sessions || 0} · ${shortTokens(row.tokens)}`,
                  }),
                ],
              })),
            ],
          })
        : null,
    ],
  })
}

/** Full-width settings surface: defaults, named presets and worker limits.
 *
 * Everything is local state until Save; the effect below reseeds from the
 * server whenever the saved values change (including after a save), so a
 * Cancel/close never leaves stale text behind on the next open.
 */
function SettingsPanel({ settings, models, loading, error, onRetry }) {
  const queryClient = useQueryClient()
  const [model, setModel] = useState(() => ({
    model: (settings && settings.default_model) || '',
    provider: (settings && settings.default_provider) || '',
  }))
  const [rows, setRows] = useState(() => presetRowsFrom(settings))
  const [maxWorkers, setMaxWorkers] = useState(() => Number(settings && settings.max_workers) || 2)
  const [maxTurns, setMaxTurns] = useState(() => Number(settings && settings.chat_max_turns) || 120)
  const [yolo, setYolo] = useState(() => !(settings && settings.worker_yolo === false))
  const [notice, setNotice] = useState('')

  const signature = useMemo(() => JSON.stringify({
    model: (settings && settings.default_model) || '',
    provider: (settings && settings.default_provider) || '',
    presets: presetRowsFrom(settings),
    workers: Number(settings && settings.max_workers) || 2,
    turns: Number(settings && settings.chat_max_turns) || 120,
    yolo: !(settings && settings.worker_yolo === false),
  }), [settings])

  useEffect(() => {
    if (!settings) return
    setModel({
      model: settings.default_model || '',
      provider: settings.default_provider || '',
    })
    setRows(presetRowsFrom(settings))
    setMaxWorkers(Number(settings.max_workers) || 2)
    setMaxTurns(Number(settings.chat_max_turns) || 120)
    setYolo(settings.worker_yolo !== false)
  }, [signature]) // eslint-disable-line react-hooks/exhaustive-deps

  const edited = () => setNotice('')

  const updateRow = (index, key, value) => {
    edited()
    setRows((current) => current.map((row, position) => (
      position === index ? { ...row, [key]: value } : row
    )))
  }

  const save = useMutation({
    mutationFn: () => rest('/settings', {
      method: 'POST',
      body: {
        default_model: String(model.model || '').trim(),
        default_provider: String(model.provider || '').trim(),
        model_presets: rows
          .map((row) => ({
            name: String(row.name || '').trim(),
            model: String(row.model || '').trim(),
            provider: String(row.provider || '').trim(),
          }))
          .filter((row) => !!row.model),
        max_workers: Number(maxWorkers) || 1,
        chat_max_turns: Number(maxTurns) || 1,
        worker_yolo: !!yolo,
      },
    }),
    onSuccess: (result) => {
      if (result && result.ok === false) {
        setNotice(String(result.error || result.detail || 'could not save settings'))
        return
      }
      setNotice('Saved.')
      queryClient.invalidateQueries({ queryKey: [ID, 'settings'] })
      queryClient.invalidateQueries({ queryKey: [ID, 'models'] })
      invalidateChats(queryClient)
    },
  })

  const failure = notice && notice !== 'Saved.'
    ? notice
    : (errorText(save.error) || errorText(error))
  const saved = notice === 'Saved.'

  return jsxs('section', {
    'aria-label': 'Settings',
    className: 'flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3',
    children: [
      jsxs('div', {
        key: 'head', className: 'flex flex-wrap items-center justify-between gap-2 text-[0.6875rem]',
        children: [
          jsx('span', {
            key: 'h', className: 'min-w-0 flex-1 truncate text-(--ui-text-quaternary)',
            children: 'Defaults apply to new chats and worker launches.',
          }),
          jsxs('div', {
            key: 'actions', className: 'flex shrink-0 items-center gap-1',
            children: [
              jsx(GhostButton, {
                key: 'save', disabled: save.isPending || loading, onClick: () => save.mutate(),
                children: save.isPending ? 'Saving…' : 'Save',
              }),
              jsx(GhostButton, {
                key: 'refresh', disabled: loading,
                onClick: () => { setNotice(''); onRetry() },
                children: 'Refresh',
              }),
            ],
          }),
        ],
      }),
      saved || failure
        ? jsx('div', {
            key: 'notice',
            className: cn('truncate text-[0.6875rem]', saved ? 'text-(--ui-text-tertiary)' : 'text-(--ui-text-quaternary)'),
            title: failure || 'Saved.',
            children: saved ? 'Saved.' : failure,
          })
        : null,
      loading && !settings
        ? jsx('div', { key: 'loading', className: 'px-1 py-2 text-xs text-(--ui-text-tertiary)', children: 'Loading settings…' })
        : jsxs('div', {
            key: 'columns', className: 'flex min-h-0 flex-col gap-4',
            children: [
              jsxs('div', {
                key: 'defaults', className: 'flex min-w-0 flex-col gap-2',
                children: [
                  jsx('div', {
                    key: 'label', className: 'text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-quaternary)',
                    children: 'Default model',
                  }),
                  jsxs('div', {
                    key: 'picker', className: 'flex flex-wrap items-center gap-1',
                    children: [
                      jsx(ModelPicker, {
                        value: model, models, disabled: save.isPending,
                        onChange: (next) => {
                          edited()
                          setModel({ model: next.model || '', provider: next.provider || '' })
                        },
                      }),
                      model.model
                        ? jsx(GhostButton, {
                            key: 'clear', title: 'Clear the default (let Hermes choose)',
                            onClick: () => { edited(); setModel({ model: '', provider: '' }) },
                            children: 'Inherit',
                          })
                        : null,
                    ],
                  }),
                  jsxs('div', {
                    key: 'provider', className: 'flex flex-wrap items-center gap-2 text-[0.6875rem] text-(--ui-text-quaternary)',
                    children: [
                      jsx('span', { key: 'p', children: model.provider ? `provider: ${model.provider}` : 'provider: auto (chosen by Hermes)' }),
                      jsx('span', { key: 'n', children: 'No default means the CLI picks.' }),
                    ],
                  }),
                  jsxs('label', {
                    key: 'workers', className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: [
                      jsx('span', { key: 'l', className: 'min-w-0 flex-1', children: 'Max parallel workers' }),
                      jsx('input', {
                        key: 'i', type: 'number', min: 1, max: 8, value: maxWorkers,
                        'aria-label': 'Max parallel workers',
                        onChange: (event) => { edited(); setMaxWorkers(event.target.value) },
                        className: 'w-16 rounded border border-(--ui-stroke-secondary) bg-transparent px-1.5 py-1 text-[0.6875rem] text-(--ui-text-secondary) focus:outline-none',
                      }),
                    ],
                  }),
                  jsxs('label', {
                    key: 'turns', className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: [
                      jsx('span', { key: 'l', className: 'min-w-0 flex-1', children: 'Max turns per worker' }),
                      jsx('input', {
                        key: 'i', type: 'number', min: 1, max: 500, value: maxTurns,
                        'aria-label': 'Max turns per worker',
                        onChange: (event) => { edited(); setMaxTurns(event.target.value) },
                        className: 'w-16 rounded border border-(--ui-stroke-secondary) bg-transparent px-1.5 py-1 text-[0.6875rem] text-(--ui-text-secondary) focus:outline-none',
                      }),
                    ],
                  }),
                  jsxs('label', {
                    key: 'yolo', className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: [
                      jsx('input', {
                        key: 'i', type: 'checkbox', checked: !!yolo,
                        'aria-label': 'Run workers unattended',
                        onChange: (event) => { edited(); setYolo(!!event.target.checked) },
                        className: 'accent-(--ui-accent)',
                      }),
                      jsx('span', { key: 'l', className: 'min-w-0 flex-1', children: 'Run workers unattended (--yolo)' }),
                    ],
                  }),
                  jsx('div', {
                    key: 'yolo-help', className: 'text-[0.6875rem] leading-snug text-(--ui-text-quaternary)',
                    children: 'Skips approval prompts inside a turn; workers still never commit, push or merge. 1–8 workers, 1–500 turns.',
                  }),
                ],
              }),
              jsxs('div', {
                key: 'presets', className: 'flex min-h-0 min-w-0 flex-1 flex-col gap-1',
                children: [
                  jsxs('div', {
                    key: 'label', className: 'flex items-baseline justify-between gap-2',
                    children: [
                      jsx('span', {
                        key: 'l', className: 'text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-quaternary)',
                        children: 'Model presets',
                      }),
                      jsx('span', {
                        key: 'n', className: 'text-[0.6875rem] tabular-nums text-(--ui-text-quaternary)',
                        children: `${rows.length}/${MAX_PRESET_ROWS}`,
                      }),
                    ],
                  }),
                  jsx('div', {
                    key: 'hint', className: 'text-[0.6875rem] leading-snug text-(--ui-text-quaternary)',
                    children: SETTINGS_HINT,
                  }),
                  rows.length
                    ? jsx('div', {
                        key: 'rows', className: 'flex min-h-0 flex-col gap-1 overflow-y-auto pr-1',
                        children: rows.map((row, index) => jsxs('div', {
                          key: `preset-${index}`, className: 'flex min-w-0 items-center gap-1',
                          children: [
                            jsx('input', {
                              key: 'name', type: 'text', value: row.name, placeholder: 'name',
                              'aria-label': `Preset ${index + 1} name`, autoComplete: 'off',
                              onChange: (event) => updateRow(index, 'name', event.target.value),
                              className: cn(GITHUB_FIELD, 'w-24 shrink-0'),
                            }),
                            jsx('input', {
                              key: 'model', type: 'text', value: row.model, placeholder: 'model',
                              'aria-label': `Preset ${index + 1} model`, autoComplete: 'off',
                              onChange: (event) => updateRow(index, 'model', event.target.value),
                              className: cn(GITHUB_FIELD, 'min-w-0 flex-1'),
                            }),
                            jsx('input', {
                              key: 'provider', type: 'text', value: row.provider, placeholder: 'provider',
                              'aria-label': `Preset ${index + 1} provider`, autoComplete: 'off',
                              onChange: (event) => updateRow(index, 'provider', event.target.value),
                              className: cn(GITHUB_FIELD, 'w-28 shrink-0'),
                            }),
                            jsx(GhostButton, {
                              key: 'remove', className: 'shrink-0', title: 'Remove this preset',
                              onClick: () => {
                                edited()
                                setRows((current) => current.filter((_, position) => position !== index))
                              },
                              children: '×',
                            }),
                          ],
                        })),
                      })
                    : jsx('div', {
                        key: 'empty', className: 'px-1 py-2 text-xs text-(--ui-text-tertiary)',
                        children: 'No presets yet — use the model picker, or add one here.',
                      }),
                  jsx('div', {
                    key: 'add', className: 'flex items-center gap-2',
                    children: jsx(GhostButton, {
                      disabled: rows.length >= MAX_PRESET_ROWS,
                      onClick: () => { edited(); setRows((current) => current.concat([blankPreset()])) },
                      children: '+ Add preset',
                    }),
                  }),
                ],
              }),
            ],
          }),
    ],
  })
}

// -- composer ---------------------------------------------------------------

// Drafts belong to a target, never to the page: one bucket per chat plus one
// for the new-chat box, so switching chats cannot bleed text between them.
const DRAFT_NEW_KEY = 'draft:new'
const DRAFT_DEBOUNCE = 300
// The box grows with its text up to eight rows, then scrolls. Fixed numbers
// keep the height deterministic even when computed styles are unavailable.
const COMPOSER_ROWS = 2
const COMPOSER_MAX_ROWS = 8
const COMPOSER_ROW_PX = 24
const COMPOSER_PAD_PX = 16
const COMPOSER_MIN_PX = COMPOSER_ROWS * COMPOSER_ROW_PX + COMPOSER_PAD_PX
const COMPOSER_MAX_PX = COMPOSER_MAX_ROWS * COMPOSER_ROW_PX + COMPOSER_PAD_PX
// The backend cap (8000) is the contract; the counter warns, it never clips.
const COMPOSER_MAX_CHARS = 8000
const COMPOSER_WARN_CHARS = 6400
const COMPOSER_COUNTER_AT = 1000
const RECENT_REPO_LIMIT = 5
const RECENT_REPO_LIST_ID = 'superagent-recent-repos'

function draftKeyFor(sessionId) {
  return sessionId ? `draft:${sessionId}` : DRAFT_NEW_KEY
}

/** Last repos this person sent work into. A datalist, not a picker: the field
 *  stays free text and the browser only offers what was used before. */
function readRecentRepos() {
  const raw = readStored('recentRepos', '')
  if (!raw) return []
  try {
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((entry) => typeof entry === 'string' && entry.trim())
      .slice(0, RECENT_REPO_LIMIT)
  } catch (error) {
    // A corrupt list is not worth an error card; the field still works.
    return []
  }
}

function rememberRepo(repo) {
  const value = String(repo || '').trim()
  if (!value) return readRecentRepos()
  const next = [value]
    .concat(readRecentRepos().filter((entry) => entry !== value))
    .slice(0, RECENT_REPO_LIMIT)
  writeStored('recentRepos', JSON.stringify(next))
  return next
}

/** Grows the textarea to its content, capped at eight rows; past the cap the
 *  box scrolls instead of pushing the transcript off the page. */
function fitTextarea(el) {
  if (!el || !el.style) return
  el.style.height = 'auto'
  const full = Number(el.scrollHeight) || 0
  const next = Math.min(Math.max(full, COMPOSER_MIN_PX), COMPOSER_MAX_PX)
  el.style.height = `${next}px`
  el.style.overflowY = full > COMPOSER_MAX_PX ? 'auto' : 'hidden'
}

/** `provider: model` for a preset (mirrors the backend's label). */
function presetLabelText(preset) {
  const name = String((preset && preset.model) || '')
  const provider = String((preset && preset.provider) || '')
  if (provider && name) return `${provider}: ${name}`
  return name || provider
}

/** One chip means one preset: the name, plus a ✕ that hands the model back to
 *  the picker. With no preset picked, a native select is the way in. */
function PresetControl({ presets, active, disabled, onPick, onClear }) {
  const list = Array.isArray(presets) ? presets : []
  if (!list.length) return null
  if (active) {
    const preset = list.find((entry) => String((entry && entry.name) || '') === active)
    return jsxs('span', {
      'data-preset': active,
      title: preset ? presetLabelText(preset) : active,
      className: cn('inline-flex max-w-[12rem] min-w-0 shrink-0 items-center gap-0.5 rounded',
        'border border-(--ui-stroke-secondary) bg-(--chrome-action-hover) px-1.5 py-0.5',
        'text-[0.6875rem] text-(--ui-text-secondary)'),
      children: [
        jsx('span', { key: 'n', className: 'min-w-0 truncate', children: active }),
        jsx('button', {
          key: 'x', type: 'button', disabled,
          'aria-label': `Clear preset ${active}`, title: 'Clear preset',
          onClick: onClear,
          className: cn('shrink-0 rounded px-0.5 leading-none text-(--ui-text-quaternary)',
            'hover:text-(--ui-text-secondary) disabled:opacity-50'),
          children: '×',
        }),
      ],
    })
  }
  return jsxs('select', {
    value: '', disabled, 'aria-label': 'Model preset',
    onChange: (event) => {
      const picked = list.find((entry) => String((entry && entry.name) || '') === event.target.value)
      if (picked) onPick(picked)
    },
    className: cn('max-w-[9rem] shrink-0 rounded border border-(--ui-stroke-secondary) bg-transparent',
      'px-1 py-0.5 text-[0.6875rem] text-(--ui-text-quaternary)'),
    children: [
      jsx('option', { key: 'none', value: '', children: 'Preset…' }),
      ...list.map((preset) => jsx('option', {
        key: String((preset && preset.name) || ''),
        value: String((preset && preset.name) || ''),
        children: String((preset && preset.name) || ''),
      })),
    ],
  })
}

/** One calm box: the textarea, then a meta row that carries the model/preset,
 *  the repo (new chats only) and the send/stop action. Nothing else is
 *  bordered — an empty composer is one 64px field plus one 30px row. */
function Composer(props) {
  const { mode, session, draft, onDraft, onSend, pending, error,
    model, onModelChange, repo, onRepoChange, models, inputRef,
    onSessionModel, modelPending, presets, presetName, onPreset, onPresetClear,
    onStop, stopPending, recentRepos, onUnarchive } = props
  const busy = isBusy(session)
  // An archived chat is history: the transcript stays readable, the box does
  // not pretend it can send. Unarchive (from here or the row's ⋯) restores it.
  const archived = mode === 'session' && !!(session && session.archived_at)
  const text = String(draft == null ? '' : draft)
  const canSend = !!text.trim() && !pending && !busy && !archived
  // The one sentence that explains a disabled Send, in priority order.
  const reason = archived
    ? 'Archived — unarchive to continue'
    : pending
      ? 'Sending…'
      : busy
        ? 'Waiting for this turn to finish'
        : !text.trim() ? 'Type a message' : ''
  const placeholder = archived
    ? 'Archived — unarchive to continue'
    : mode === 'session'
      ? (busy ? 'Waiting for this turn to finish…' : 'Reply to this chat…')
      : 'Describe the work…'
  const repos = Array.isArray(recentRepos) ? recentRepos : []
  const sentRef = useRef(false)

  // Auto-grow on every value change: the field is as tall as its text (up to
  // eight rows), so the page never jumps when a draft starts.
  useEffect(() => {
    fitTextarea(inputRef && inputRef.current)
  }, [draft, mode])

  // After a send, focus stays in the box — once it is enabled again.
  useEffect(() => {
    if (!sentRef.current || pending || busy) return
    sentRef.current = false
    const el = inputRef && inputRef.current
    if (el && typeof el.focus === 'function' && typeof document !== 'undefined' && document.activeElement !== el) {
      el.focus()
    }
  }, [pending, busy, draft])

  // Leaving the chat (or closing the new-chat box) drops a pending refocus:
  // a long turn must never pull the keyboard back minutes later.
  useEffect(() => { sentRef.current = false }, [mode, session && session.id])

  const send = () => {
    sentRef.current = true
    onSend()
  }

  const left = mode === 'session'
    ? [
        archived && typeof onUnarchive === 'function'
          ? jsx(GhostButton, {
              key: 'unarchive', className: 'h-6 shrink-0 px-1.5 py-0',
              onClick: onUnarchive, title: 'Restore this chat so it can continue',
              children: 'Unarchive',
            })
          : jsx(SessionModelControl, {
              key: 'model', session, models, pending: modelPending, onChange: onSessionModel,
            }),
      ]
    : [
        jsx(PresetControl, {
          key: 'preset', presets, active: presetName, disabled: pending,
          onPick: onPreset, onClear: onPresetClear,
        }),
        jsx(ModelPicker, { key: 'picker', value: model, models, disabled: pending, onChange: onModelChange }),
        jsxs('span', {
          key: 'repo', className: 'flex min-w-0 flex-1 basis-[9rem] items-center',
          children: [
            jsx('input', {
              key: 'i', type: 'text', value: repo, list: RECENT_REPO_LIST_ID,
              placeholder: 'repo path (optional)', 'aria-label': 'Repository path',
              onChange: (event) => onRepoChange(event.target.value),
              className: cn('min-w-0 flex-1 bg-transparent px-1 py-0.5 text-[0.6875rem]',
                'text-(--ui-text-secondary) placeholder:text-(--ui-text-quaternary)',
                'focus:outline-none disabled:opacity-60'),
            }),
            jsx('datalist', {
              key: 'd', id: RECENT_REPO_LIST_ID,
              children: repos.map((entry, index) => jsx('option', { key: `${index}:${entry}`, value: entry })),
            }),
          ],
        }),
      ]

  const counter = text.length > COMPOSER_COUNTER_AT
    ? jsx('span', {
        key: 'count',
        'data-over': text.length > COMPOSER_WARN_CHARS ? '1' : '0',
        title: `${text.length} of ${COMPOSER_MAX_CHARS} characters`,
        className: cn('shrink-0 tabular-nums',
          text.length > COMPOSER_WARN_CHARS
            ? 'text-(--ui-text-secondary)'
            : 'text-(--ui-text-quaternary)'),
        children: `${text.length} / ${COMPOSER_MAX_CHARS}`,
      })
    : null

  const sendButton = jsxs('button', {
    type: 'button', disabled: !canSend, onClick: send,
    'aria-label': 'Send', title: reason || 'Send this message (Enter)',
    className: cn('inline-flex h-6 shrink-0 items-center gap-1 rounded bg-(--chrome-action-hover)',
      'px-2 text-[0.6875rem] font-medium text-(--ui-text-secondary) hover:opacity-90',
      !canSend && 'opacity-40'),
    children: [
      jsx('span', { key: 'g', 'aria-hidden': 'true', children: '↑' }),
      jsx('span', { key: 'l', className: 'hidden sm:inline', children: pending ? 'Sending…' : 'Send' }),
    ],
  })

  return jsxs('div', {
    'data-composer': mode,
    className: cn('flex shrink-0 flex-col overflow-hidden rounded-lg border',
      'border-(--ui-stroke-secondary) bg-(--ui-bg-primary)'),
    children: [
      error
        ? jsx('div', {
            key: 'err', role: 'alert',
            className: 'px-3 pt-2 text-[0.6875rem] text-(--ui-text-secondary)',
            children: error,
          })
        : null,
      jsx('textarea', {
        key: 'input', ref: inputRef, rows: COMPOSER_ROWS, value: draft,
        disabled: pending || busy || archived, placeholder,
        'aria-label': archived
          ? 'Archived chat (read-only)'
          : mode === 'session' ? 'Reply to this chat' : 'Describe the work',
        onChange: (event) => onDraft(event.target.value),
        onKeyDown: (event) => {
          if (event.key === 'Escape') {
            // Escape gives the keyboard back; it never clears the draft.
            if (inputRef && inputRef.current) inputRef.current.blur()
            return
          }
          if (event.key !== 'Enter') return
          // An IME candidate window owns Enter; never send mid-composition.
          if (event.nativeEvent && event.nativeEvent.isComposing) return
          if (event.shiftKey) return
          // Enter, Ctrl+Enter and Cmd+Enter send. Only this box listens, so no
          // global shortcut is claimed anywhere else on the page.
          event.preventDefault()
          if (!canSend) return
          send()
        },
        className: cn('min-h-16 w-full resize-none bg-transparent px-3 pt-2 pb-1',
          'text-sm leading-relaxed text-(--ui-text-secondary)',
          'placeholder:text-(--ui-text-quaternary) focus:outline-none disabled:opacity-60'),
      }),
      jsxs('div', {
        key: 'meta',
        className: cn('flex min-w-0 flex-wrap items-center gap-1.5 px-2 pt-0.5 pb-1.5',
          'text-[0.6875rem] text-(--ui-text-quaternary)'),
        children: [
          ...left,
          jsxs('div', {
            key: 'act', className: 'ml-auto flex shrink-0 items-center gap-1',
            children: [
              counter,
              busy && typeof onStop === 'function'
                ? jsx(GhostButton, {
                    key: 'stop', className: 'h-6 shrink-0 px-1.5 py-0',
                    onClick: onStop, disabled: stopPending,
                    title: 'Stop this turn', 'aria-label': 'Stop this turn',
                    children: stopPending ? 'Stopping…' : 'Stop',
                  })
                : null,
              jsx(Tip, { key: 'send', label: reason || 'Send this message (Enter)', children: sendButton }),
            ],
          }),
        ],
      }),
    ],
  })
}

// -- handoff: diff panel + comment confirm ----------------------------------

// A file's porcelain state as the one or two letters git users expect.
const DIFF_LETTERS = {
  modified: 'M', added: 'A', deleted: 'D', renamed: 'R', copied: 'C',
  untracked: '??', conflict: '!', typechange: 'T', changed: '*',
}
// A 5 000-file status is not a review: show the head and say how much is left.
const DIFF_FILE_ROWS = 200

/** Collapsible "what did this session change" panel, between transcript and
 *  composer. The query is owned by ChatCenter (the header needs its count);
 *  this component only renders it. */
function DiffPanel({ session, query, onClose }) {
  const data = (query && query.data) || {}
  const files = Array.isArray(data.files) ? data.files : []
  const shown = files.slice(0, DIFF_FILE_ROWS)
  const hidden = files.length - shown.length
  const failed = (data && data.ok === false && data.error) || ''
  const error = failed || errorText(query && query.error)
  const fetching = !!(query && query.isFetching)

  return jsxs('div', {
    className: cn('flex max-h-[22rem] min-h-0 flex-col gap-1.5 overflow-y-auto rounded border',
      'border-(--ui-stroke-secondary) p-2'),
    children: [
      jsxs('div', {
        key: 'head', className: 'flex shrink-0 flex-wrap items-center justify-between gap-2 text-[0.6875rem]',
        children: [
          jsxs('div', {
            key: 'm', className: 'flex min-w-0 flex-wrap items-baseline gap-2',
            children: [
              jsx('span', { key: 't', className: 'text-(--ui-text-secondary)', children: 'Diff' }),
              jsx('span', {
                key: 'n', className: 'text-(--ui-text-quaternary)',
                children: `${files.length} file${files.length === 1 ? '' : 's'}`,
              }),
              data.truncated
                ? jsx('span', { key: 'tr', className: 'text-(--ui-text-quaternary)', children: 'patch truncated' })
                : null,
              session && session.repo
                ? jsx('span', {
                    key: 'r', className: 'min-w-0 max-w-[20rem] truncate font-mono text-(--ui-text-quaternary)',
                    children: session.repo,
                  })
                : null,
            ],
          }),
          jsxs('div', {
            key: 'a', className: 'flex shrink-0 items-center gap-1',
            children: [
              jsx(GhostButton, {
                key: 'refresh', onClick: () => query && query.refetch(), disabled: fetching,
                children: fetching ? 'Refreshing…' : 'Refresh',
              }),
              jsx(GhostButton, { key: 'close', onClick: onClose, children: 'Close' }),
            ],
          }),
        ],
      }),
      query && query.isLoading
        ? jsx('div', { key: 'load', className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'Reading the diff…' })
        : null,
      error
        ? jsx('div', {
            key: 'err', className: 'break-words text-[0.6875rem] text-(--ui-text-quaternary)',
            children: error,
          })
        : null,
      !error && data.ok && !files.length
        ? jsx('div', {
            key: 'none', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
            children: 'No changes in this repository.',
          })
        : null,
      shown.length
        ? jsxs('div', {
            key: 'files', className: 'flex shrink-0 flex-col gap-0.5',
            children: [
              ...shown.map((file, index) => jsxs('div', {
                key: `f${index}`,
                className: 'flex min-w-0 items-baseline gap-1.5 font-mono text-[0.6875rem]',
                children: [
                  jsx('span', {
                    key: 's', className: 'w-5 shrink-0 text-(--ui-text-quaternary)',
                    children: DIFF_LETTERS[file.status] || '?',
                  }),
                  jsx('span', {
                    key: 'p', className: 'min-w-0 truncate text-(--ui-text-tertiary)',
                    children: file.path,
                  }),
                ],
              })),
              hidden > 0
                ? jsx('div', {
                    key: 'more', className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
                    children: `… ${hidden} more file${hidden === 1 ? '' : 's'}`,
                  })
                : null,
            ],
          })
        : null,
      data.stat
        ? jsx('pre', {
            key: 'stat',
            className: cn('shrink-0 overflow-x-auto whitespace-pre font-mono text-[0.6875rem]',
              'leading-[1.35] text-(--ui-text-quaternary)'),
            children: String(data.stat),
          })
        : null,
      data.patch
        ? jsx('pre', {
            key: 'patch',
            className: cn('min-h-0 shrink-0 overflow-x-auto whitespace-pre font-mono text-[0.6875rem]',
              'leading-[1.35] text-(--ui-text-quaternary)'),
            children: String(data.patch),
          })
        : null,
    ],
  })
}

/** Inline confirm (never window.confirm): the generated summary is editable,
 *  Post is the only thing that talks to GitHub, and the URL is shown after. */
function CommentBox({ session, onClose }) {
  const queryClient = useQueryClient()
  const preview = useQuery({
    queryKey: [ID, 'chat', session.id, 'comment'],
    queryFn: () => fetchCommentPreview(session.id),
    retry: false,
  })
  const [text, setText] = useState('')
  const [edited, setEdited] = useState(false)

  // Prefill from the server once; a preview refetch must never overwrite what
  // the user typed.
  useEffect(() => {
    if (edited || !preview.data) return
    if (typeof preview.data.body === 'string') setText(preview.data.body)
  }, [preview.data, edited])

  const post = useMutation({
    mutationFn: () => rest(`/chats/${encodeURIComponent(session.id)}/comment`, {
      method: 'POST', body: { body: text },
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [ID, 'chat', session.id] })
      queryClient.invalidateQueries({ queryKey: [ID, 'chats'] })
    },
  })

  const posted = post.data && post.data.ok ? post.data : null
  const canPost = !!String(text || '').trim() && !post.isPending
  const error = errorText(post.error) || errorText(preview.error) ||
    (post.data && post.data.ok === false && post.data.error) || ''

  return jsxs('div', {
    className: cn('flex max-h-[22rem] min-h-0 flex-col gap-1.5 overflow-y-auto rounded border',
      'border-(--ui-stroke-secondary) p-2'),
    children: [
      jsxs('div', {
        key: 'head', className: 'flex shrink-0 flex-wrap items-center justify-between gap-2 text-[0.6875rem]',
        children: [
          jsx('span', {
            key: 't', className: 'text-(--ui-text-secondary)',
            children: `Comment on PR #${session.pr_number}`,
          }),
          jsx('span', {
            key: 's', className: 'text-(--ui-text-quaternary)',
            children: 'Nothing is posted until you press Post.',
          }),
        ],
      }),
      preview.isLoading
        ? jsx('div', { key: 'load', className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'Preparing the summary…' })
        : jsx('textarea', {
            key: 'body', rows: 7, value: text,
            onChange: (event) => { setEdited(true); setText(event.target.value) },
            placeholder: 'What should the pull request hear?',
            className: cn('min-h-24 w-full resize-y rounded border border-(--ui-stroke-secondary)',
              'bg-transparent px-2 py-1.5 font-mono text-[0.6875rem] text-(--ui-text-secondary)',
              'placeholder:text-(--ui-text-quaternary) focus:outline-none disabled:opacity-60'),
          }),
      error
        ? jsx('div', {
            key: 'err', className: 'break-words text-[0.6875rem] text-(--ui-text-quaternary)',
            children: error,
          })
        : null,
      posted
        ? jsxs('div', {
            key: 'ok', className: 'flex flex-wrap items-center gap-2 text-[0.6875rem] text-(--ui-text-secondary)',
            children: [
              jsx('span', { key: 'm', children: 'Comment posted.' }),
              posted.url
                ? jsx('button', {
                    key: 'open', type: 'button', onClick: () => openUrl(posted.url),
                    className: 'text-(--ui-text-secondary) underline',
                    children: 'Open on GitHub',
                  })
                : null,
            ],
          })
        : null,
      jsxs('div', {
        key: 'actions', className: 'flex shrink-0 items-center gap-1',
        children: [
          jsx(GhostButton, {
            key: 'post', onClick: () => { if (canPost) post.mutate() }, disabled: !canPost,
            children: post.isPending ? 'Posting…' : 'Post',
          }),
          jsx(GhostButton, {
            key: 'cancel', onClick: onClose, disabled: post.isPending, children: 'Cancel',
          }),
        ],
      }),
    ],
  })
}

// -- chat center ------------------------------------------------------------

/** Secondary chat actions behind one ⋯: the SDK menu when this shell has it,
 *  a plain popover list otherwise. Neither is a visible button, so the header
 *  keeps at most Stop / Diff / Open. */
function OverflowItem({ item }) {
  return jsx('button', {
    type: 'button',
    role: 'menuitem',
    disabled: !!item.disabled,
    onClick: item.onSelect,
    className: cn('flex w-full min-w-0 items-center rounded px-2 py-1 text-left text-xs',
      'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)',
      item.disabled && 'opacity-50'),
    children: item.label,
  })
}

function OverflowMenu({ items, label }) {
  const [open, setOpen] = useState(false)
  if (!items || !items.length) return null
  const trigger = jsx('button', {
    type: 'button',
    'aria-label': label || 'More actions',
    'aria-haspopup': 'menu',
    'aria-expanded': !!open,
    title: label || 'More actions',
    onClick: () => setOpen((value) => !value),
    className: cn('inline-flex h-6 w-6 items-center justify-center rounded leading-none',
      'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)',
      open && 'bg-(--chrome-action-hover) text-(--ui-text-secondary)'),
    children: '⋯',
  })
  const entries = items.map((item) => jsx(OverflowItem, { key: item.id, item }))
  if (DropdownMenu && DropdownMenuTrigger && DropdownMenuContent) {
    return jsxs(DropdownMenu, {
      open, onOpenChange: setOpen,
      children: [
        jsx(DropdownMenuTrigger, { key: 'trigger', asChild: true, children: trigger }),
        jsx(DropdownMenuContent, { key: 'content', align: 'end', className: 'w-52 p-1', children: entries }),
      ],
    })
  }
  return jsxs('div', {
    className: 'relative',
    children: [
      trigger,
      open
        ? jsx('div', {
            role: 'menu',
            'aria-label': label || 'More actions',
            className: cn('absolute right-0 top-7 z-30 flex w-52 flex-col gap-0.5 rounded border',
              'border-(--ui-stroke-secondary) bg-background p-1 shadow-md'),
            children: entries,
          })
        : null,
    ],
  })
}

/** Two lines and three actions: title + status dot, then model · repo · PR.
 *  Comment-on-PR and the workspace tile live behind the ⋯ menu. */
function ChatHeader({ session, tile, diff, comment, retry, exportMd }) {
  const canOpen = !!session.hermes_session_id && !!(host && typeof host.openSession === 'function')
  const canTile = !tile && !!(host && typeof host.openWorkspace === 'function')
  const repo = String(session.repo || '').trim()
  const detail = String(session.error || '').trim()

  const overflow = []
  if (!tile && comment) {
    overflow.push({
      id: 'comment',
      label: comment.open ? 'Close the comment box' : `Comment on PR #${session.pr_number}`,
      disabled: comment.disabled,
      onSelect: comment.onToggle,
    })
  }
  if (exportMd) {
    overflow.push({
      id: 'export',
      label: exportMd.pending ? 'Preparing export…' : 'Export as Markdown',
      disabled: exportMd.pending,
      onSelect: exportMd.onExport,
    })
  }
  if (canTile) {
    overflow.push({ id: 'tile', label: 'Open in a workspace tile', onSelect: () => openChatTile(session) })
  }

  return jsxs('div', {
    className: 'flex items-start justify-between gap-2',
    children: [
      jsxs('div', {
        key: 'meta', className: 'flex min-w-0 flex-col gap-0.5',
        children: [
          jsxs('div', {
            key: 'title', className: 'flex min-w-0 items-center gap-1.5',
            children: [
              jsx(ChatDot, { key: 'dot', status: session.status }),
              jsx('span', {
                key: 't', title: sessionTitle(session),
                className: 'min-w-0 truncate text-sm font-medium text-(--ui-text-secondary)',
                children: sessionTitle(session),
              }),
              // Archived is a state, not a badge of honour: one quiet pill is
              // enough, and the composer explains what to do about it.
              session.archived_at
                ? jsx('span', {
                    key: 'arch',
                    title: 'Archived: read-only until you unarchive it',
                    className: cn('shrink-0 rounded border border-(--ui-stroke-secondary) px-1',
                      'text-[0.625rem] font-normal leading-4 text-(--ui-text-quaternary)'),
                    children: 'Archived',
                  })
                : null,
            ],
          }),
          jsxs('div', {
            key: 'line', className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem] text-(--ui-text-quaternary)',
            children: [
              jsx('span', { key: 'm', className: 'shrink-0', children: sessionModelLabel(session) }),
              repo
                ? jsxs('span', {
                    key: 'r', className: 'flex min-w-0 items-center gap-1.5',
                    children: [
                      jsx('span', { key: 'd', className: 'shrink-0', children: '·' }),
                      jsx('span', {
                        key: 'p', title: repo,
                        className: 'min-w-0 max-w-[18rem] truncate font-mono',
                        children: clipMiddle(repo, 48),
                      }),
                    ],
                  })
                : null,
              session.pr_number
                ? jsx(PrBadge, { key: 'pr', session })
                : (session.pr_url
                    ? jsx('button', {
                        key: 'prl', type: 'button', onClick: () => openUrl(session.pr_url),
                        className: 'shrink-0 text-(--ui-text-secondary) underline',
                        children: 'Pull request',
                      })
                    : null),
            ],
          }),
          detail || session.status === 'failed'
            ? jsx('span', {
                key: 'err',
                title: detail || 'No error text was recorded for this turn.',
                className: 'min-w-0 truncate text-[0.6875rem] text-(--ui-text-quaternary)',
                children: detail ? `error: ${clip(detail, 200)}` : 'failed: no error recorded',
              })
            : null,
        ],
      }),
      jsxs('div', {
        key: 'actions', className: 'flex shrink-0 flex-wrap items-center justify-end gap-1',
        children: [
          // Only a stopped turn offers this: the prompt is already stored, so
          // the click relaunches it instead of asking the user to retype it.
          retry ? jsx(GhostButton, {
            key: 'retry',
            title: 'Run the last prompt again in this chat',
            disabled: retry.pending,
            onClick: () => retry.onRetry(),
            children: retry.pending ? 'Retrying…' : 'Retry',
          }) : null,
          diff ? jsx(GhostButton, {
            key: 'diff',
            title: 'Show what this session changed in its repository',
            'aria-expanded': !!diff.open,
            onClick: diff.onToggle,
            children: diff.open ? 'Diff ▴' : (diff.count ? `Diff (${diff.count})` : 'Diff'),
          }) : null,
          canOpen ? jsx(GhostButton, {
            key: 'open',
            title: 'Open this chat in Hermes',
            onClick: () => { host.openSession(session.hermes_session_id).catch(() => {}) },
            children: 'Open',
          }) : null,
          jsx(OverflowMenu, { key: 'more', items: overflow, label: 'More actions' }),
        ],
      }),
    ],
  })
}

function ChatCenter(props) {
  const { summary, detail, canComment, sessions, onSelect, banner, optimistic, retry, exportMd, ...composerProps } = props
  const session = (detail && detail.data && detail.data.session) || summary
  const sessionId = session && session.id
  const busy = isBusy(session)
  const [diffOpen, setDiffOpen] = useState(false)
  const [commentOpen, setCommentOpen] = useState(false)
  // Unconditional hooks: `enabled` stays false until the panel is opened.
  const diffQuery = useChatDiff(sessionId, diffOpen, busy)

  // Switching chats closes both panels: neither belongs to the next session.
  useEffect(() => {
    setDiffOpen(false)
    setCommentOpen(false)
  }, [sessionId])

  if (!session) {
    return jsxs('div', {
      className: 'flex min-h-0 min-w-0 flex-1 flex-col gap-3',
      children: [
        banner ? jsx('div', { key: 'banner', className: 'shrink-0', children: banner }) : null,
        jsx('div', {
          key: 'empty',
          className: 'flex min-h-0 flex-1 items-center justify-center p-4 text-center text-xs text-(--ui-text-tertiary)',
          children: 'Start a chat — pick a model and describe the work.',
        }),
        jsx(LiveWorkerStrip, { key: 'workers', sessions, selectedId: null, onSelect }),
        jsx(Composer, { key: 'composer', mode: 'new', ...composerProps }),
      ],
    })
  }

  const hasRepo = !!String(session.repo || '').trim()
  const diffCount = diffQuery.data && Array.isArray(diffQuery.data.files)
    ? diffQuery.data.files.length
    : 0
  const diffAction = hasRepo
    ? { open: diffOpen, count: diffCount, onToggle: () => setDiffOpen((value) => !value) }
    : null
  const commentAction = session.pr_number && canComment
    ? {
        open: commentOpen, disabled: busy,
        onToggle: () => setCommentOpen((value) => !value),
      }
    : null

  return jsxs('div', {
    className: 'flex min-h-0 min-w-0 flex-1 flex-col gap-3',
    children: [
      jsx(ChatHeader, {
        key: 'head', session,
        diff: diffAction, comment: commentAction, retry, exportMd,
      }),
      banner ? jsx('div', { key: 'banner', className: 'shrink-0', children: banner }) : null,
      detail && detail.isError
        ? jsx(ErrorCard, {
            key: 'err', title: `Could not load ${session.id}.`, error: detail.error,
            onRetry: () => detail.refetch(),
          })
        : jsx(Transcript, { key: 'transcript', session, optimistic }),
      diffOpen && hasRepo
        ? jsx(DiffPanel, {
            key: 'diff', session, query: diffQuery, onClose: () => setDiffOpen(false),
          })
        : null,
      commentOpen && commentAction
        ? jsx(CommentBox, {
            key: 'comment', session, onClose: () => setCommentOpen(false),
          })
        : null,
      jsx(LiveWorkerStrip, { key: 'workers', sessions, selectedId: sessionId, onSelect }),
      jsx(Composer, { key: 'composer', mode: 'session', session, ...composerProps }),
    ],
  })
}

/** Read-only transcript in a main-area tile (newer shells). The composer
 *  stays on the page; older shells never see the ⧉ button. */
function ChatTile({ sessionId }) {
  const detail = useChat(sessionId)
  const session = detail.data && detail.data.session
  if (!session) {
    return jsx('div', { className: 'p-4 text-xs text-(--ui-text-tertiary)', children: 'Loading chat…' })
  }
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-2 p-3',
    children: [
      jsx(ChatHeader, { key: 'head', session, tile: true }),
      jsx(Transcript, { key: 'transcript', session }),
    ],
  })
}

function openChatTile(session) {
  try {
    host.openWorkspace(`superagent-chat-${session.id}`, {
      title: sessionTitle(session),
      render: () => jsx(ChatTile, { sessionId: session.id }),
    })
  } catch (error) {
    // Older shells: the button is hidden by the same typeof check.
  }
}

// -- live workers (a strip under the transcript, not a third column) --------

/** The last thing this worker did, on one line. A failed card's first
 *  question is "why", so its stored error wins when no feed arrived. */
function lastEventText(session) {
  const events = Array.isArray(session.recent_events) ? session.recent_events.filter(Boolean) : []
  const latest = events.length ? events[0] : null
  let text = ''
  if (latest) {
    const kind = String(latest.kind || '').trim()
    const detail = String(latest.detail || '').replace(/\s+/g, ' ').trim()
    text = detail ? `${kind}: ${detail}` : kind
  }
  if (!text) {
    const failure = session.status === 'failed' ? firstLine(session.error) : ''
    text = failure || String(session.last_event || '').replace(/\s+/g, ' ').trim()
  }
  if (text) return clip(text, 120)
  if (session.status === 'queued') return 'Queued — waiting for a slot'
  if (session.status === 'review') return 'Finished — ready to review'
  if (session.status === 'failed') return 'Failed'
  return ''
}

function WorkerCard({ session, selected, onSelect }) {
  const busy = isBusy(session)
  // Client-only ticker: it repaints this card every second and touches no
  // query, so polling budgets stay exactly as configured.
  const now = useNow(busy)
  const timing = busy
    ? (session.status === 'queued'
        ? `queued ${fmtSeconds(liveElapsedSeconds(session, now))}`
        : fmtSeconds(liveElapsedSeconds(session, now)))
    : timingLabel(session)
  const event = lastEventText(session)
  return jsxs('button', {
    type: 'button',
    onClick: () => onSelect(session.id),
    title: `${sessionTitle(session)} · ${sessionModelLabel(session)}`,
    'aria-current': selected ? 'true' : undefined,
    className: cn('flex h-[76px] w-56 shrink-0 flex-col gap-1 rounded border border-(--ui-stroke-secondary)',
      'p-2 text-left hover:bg-(--chrome-action-hover)',
      selected && 'bg-(--chrome-action-hover)'),
    children: [
      jsx('span', {
        key: 't', className: 'w-full truncate text-xs text-(--ui-text-secondary)',
        children: sessionTitle(session),
      }),
      jsxs('span', {
        key: 'm', className: 'flex w-full min-w-0 items-center gap-1 text-[0.6875rem] text-(--ui-text-quaternary)',
        children: [
          jsx('span', {
            key: 'dot',
            className: cn('flex shrink-0 items-center', session.status === 'running' && 'animate-pulse'),
            children: jsx(ChatDot, { status: session.status }),
          }),
          jsx('span', { key: 'model', className: 'min-w-0 flex-1 truncate', children: sessionModelLabel(session) }),
          jsx('span', { key: 'e', className: 'shrink-0 tabular-nums', children: timing }),
        ],
      }),
      jsx('span', {
        key: 'ev', title: event,
        className: 'w-full truncate text-[0.6875rem] leading-snug text-(--ui-text-quaternary)',
        children: event,
      }),
    ],
  })
}

/** Collapsible strip under the transcript: a 28px header with a chevron and
 *  one dot per live worker, then a horizontally scrolling row of cards.
 *
 *  It opens itself while anything is running or queued and folds when the
 *  board goes quiet; a manual toggle wins until the next transition and is
 *  remembered in storage so the next visit starts where the user left it. */
function LiveWorkerStrip({ sessions, selectedId, onSelect }) {
  const live = sessions
    .filter((session) => !session.archived_at && LIVE_STATUSES.indexOf(session.status) !== -1)
    .slice(0, MAX_WORKERS)
  const active = live.some((session) => isBusy(session))
  const [open, setOpen] = useState(() => active || readStored('workersOpen', '') === '1')
  const wasActive = useRef(active)

  useEffect(() => {
    if (wasActive.current === active) return
    wasActive.current = active
    setOpen(active)
  }, [active])

  // Nothing live and nobody asked to see the empty state: no strip at all.
  if (!live.length && !open) return null

  const toggle = () => setOpen((value) => {
    const next = !value
    writeStored('workersOpen', next ? '1' : '0')
    return next
  })

  return jsxs('div', {
    className: 'flex min-w-0 shrink-0 flex-col gap-1.5',
    children: [
      jsxs('button', {
        key: 'head',
        type: 'button',
        'aria-expanded': open,
        'aria-label': `Live workers, ${live.length}`,
        onClick: toggle,
        className: cn('flex h-7 w-full items-center gap-1.5 rounded px-1 text-[0.6875rem]',
          'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)'),
        children: [
          jsx(Caret, { key: 'caret', open }),
          jsx('span', {
            key: 'label', className: 'shrink-0 font-medium',
            children: live.length ? `Live workers · ${live.length}` : 'Live workers',
          }),
          jsx('span', {
            key: 'dots', className: 'flex min-w-0 items-center gap-1 pl-0.5',
            children: live.slice(0, 6).map((session) =>
              jsx(ChatDot, { key: session.id, status: session.status })),
          }),
        ],
      }),
      open
        ? jsx('div', {
            key: 'body',
            className: 'flex min-w-0 gap-2 overflow-x-auto pb-1',
            children: live.length
              ? live.map((session) => jsx(WorkerCard, {
                  key: session.id, session,
                  selected: session.id === selectedId,
                  onSelect,
                }))
              : jsx('div', {
                  className: 'px-1 py-2 text-[0.6875rem] text-(--ui-text-quaternary)',
                  children: 'No live workers.',
                }),
          })
        : null,
    ],
  })
}

// -- GitHub panel -----------------------------------------------------------

const GITHUB_FIELD = cn('min-w-0 rounded border border-(--ui-stroke-secondary) bg-transparent',
  'px-1.5 py-1 text-[0.6875rem] text-(--ui-text-secondary)',
  'placeholder:text-(--ui-text-quaternary) focus:outline-none')

/** One open PR: number, two-line title, author, age, and a work button. */
function GithubPrRow({ pr, onWork, pending }) {
  const number = pr && pr.number
  const title = String((pr && pr.title) || '').trim() || '(untitled)'
  return jsxs('div', {
    className: 'flex min-w-0 items-start gap-2 rounded px-1 py-1 hover:bg-(--chrome-action-hover)',
    children: [
      jsx('button', {
        key: 'n', type: 'button', onClick: () => openUrl(pr && pr.html_url),
        title: 'Open this pull request on GitHub',
        className: 'shrink-0 font-mono text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)',
        children: `#${number}`,
      }),
      jsxs('div', {
        key: 'body', className: 'flex min-w-0 flex-1 flex-col gap-0.5',
        children: [
          jsx('span', {
            key: 'title', className: 'line-clamp-2 break-words text-xs text-(--ui-text-secondary)',
            title, children: title,
          }),
          jsxs('span', {
            key: 'meta', className: 'flex min-w-0 items-baseline gap-1.5 text-[0.6875rem] text-(--ui-text-quaternary)',
            children: [
              jsx('span', { key: 'a', className: 'min-w-0 truncate', children: (pr && pr.author) || 'unknown' }),
              jsx('span', { key: 'd', children: '·' }),
              jsx('span', { key: 't', className: 'shrink-0 tabular-nums', children: fmtIsoAge(pr && pr.updated_at) || '—' }),
            ],
          }),
        ],
      }),
      jsx(GhostButton, {
        key: 'work', className: 'shrink-0 self-center', disabled: pending,
        title: 'Start a Superagent worker on this pull request',
        onClick: () => onWork(number),
        children: pending ? 'Starting…' : 'Work on this PR',
      }),
    ],
  })
}

/** Connect/settings form on the left, the open PR list on the right. */
function GithubPanel({ status, prs, prsLoading, onSelectSession }) {
  const queryClient = useQueryClient()
  const configured = !!(status && status.configured)
  const tokenSet = !!(status && status.token_set)
  const statusRepo = (status && status.repo) || ''
  const statusAllowed = status && Array.isArray(status.allowed_users)
    ? status.allowed_users.join(', ')
    : ''
  const statusMode = (status && status.watch_mode) || 'observe'

  const [repo, setRepo] = useState('')
  const [token, setToken] = useState('')
  const [allowed, setAllowed] = useState('')
  const [mode, setMode] = useState('observe')
  const [notice, setNotice] = useState('')

  // Seed from the server when a value changes; typing never changes these
  // inputs' server values, so a poll cannot overwrite an edit in progress.
  useEffect(() => { setRepo(statusRepo) }, [statusRepo])
  useEffect(() => { setAllowed(statusAllowed) }, [statusAllowed])
  useEffect(() => { setMode(statusMode) }, [statusMode])

  const connect = useMutation({
    mutationFn: () => rest('/github/connect', {
      method: 'POST',
      body: {
        repo: String(repo || '').trim(),
        token: String(token || '').trim(),
        allowed_users: splitList(allowed),
        watch_mode: mode,
      },
    }),
    onSuccess: (result) => {
      setToken('')  // saved server-side; never keep it in component state
      if (result && result.ok === false && result.error) setNotice(String(result.error))
      else setNotice('')
      invalidateGithub(queryClient)
      invalidateChats(queryClient)
    },
  })

  const save = useMutation({
    mutationFn: () => rest('/github/settings', {
      method: 'POST',
      body: { allowed_users: splitList(allowed), watch_mode: mode },
    }),
    onSuccess: (result) => {
      if (result && result.ok === false && result.error) setNotice(String(result.error))
      else setNotice('')
      invalidateGithub(queryClient)
    },
  })

  const poll = useMutation({
    mutationFn: () => rest('/github/poll', { method: 'POST' }),
    onSuccess: (result) => {
      const errors = (result && result.errors) || []
      setNotice(errors.length ? String(errors[0]) : '')
      invalidateGithub(queryClient)
      invalidateChats(queryClient)
    },
  })

  const disconnect = useMutation({
    mutationFn: () => rest('/github/disconnect', { method: 'POST' }),
    onSuccess: () => {
      setToken('')
      setNotice('')
      invalidateGithub(queryClient)
      invalidateChats(queryClient)
    },
  })

  const work = useMutation({
    mutationFn: (number) => rest(`/github/prs/${number}/work`, { method: 'POST', body: {} }),
    onSuccess: (result) => {
      if (result && result.ok === false) {
        setNotice(String(result.error || 'could not start work on that PR'))
        return
      }
      setNotice('')
      invalidateGithub(queryClient)
      invalidateChats(queryClient)
      if (result && result.session && result.session.id && onSelectSession) {
        onSelectSession(result.session.id)
      }
    },
  })

  const failure = notice ||
    errorText(connect.error) || errorText(save.error) || errorText(poll.error) ||
    errorText(disconnect.error) || errorText(work.error)

  const openPrs = (prs && prs.prs) || (status && status.open_prs) || []
  const lastError = (status && status.last_error) || ''
  const statusLine = configured
    ? [
        status.login ? `@${status.login}` : 'connected',
        `${openPrs.length} open PR${openPrs.length === 1 ? '' : 's'}`,
        status.watching ? 'watching' : 'paused',
        `${status.poll_seconds || '—'}s poll`,
      ].join(' · ')
    : 'not connected'

  return jsxs('section', {
    'aria-label': 'GitHub',
    className: 'flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3',
    children: [
      jsx('div', {
        key: 'head', className: 'min-w-0 text-[0.6875rem] text-(--ui-text-tertiary)',
        children: statusLine,
      }),
      failure
        ? jsx('div', { key: 'failure', className: 'truncate text-[0.6875rem] text-(--ui-text-quaternary)', children: failure })
        : null,
      lastError && lastError !== failure
        ? jsx('div', {
            key: 'last-error', className: 'truncate text-[0.6875rem] text-(--ui-text-quaternary)',
            title: lastError, children: `last error: ${clip(lastError, 200)}`,
          })
        : null,
      jsxs('div', {
        key: 'columns', className: 'flex min-h-0 flex-col gap-4',
        children: [
          jsxs('div', {
            key: 'connection', className: 'flex min-w-0 flex-col gap-1.5',
            children: [
              jsx('div', {
                key: 'label', className: 'text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-quaternary)',
                children: 'Connection',
              }),
              jsx('input', {
                key: 'repo', type: 'text', value: repo, placeholder: 'owner/name',
                'aria-label': 'GitHub repository', autoComplete: 'off',
                onChange: (event) => setRepo(event.target.value),
                className: GITHUB_FIELD,
              }),
              jsx('input', {
                key: 'token', type: 'password', value: token,
                placeholder: tokenSet ? 'saved token' : 'GitHub token',
                'aria-label': 'GitHub token', autoComplete: 'off',
                onChange: (event) => setToken(event.target.value),
                className: GITHUB_FIELD,
              }),
              jsx('input', {
                key: 'allowed', type: 'text', value: allowed,
                placeholder: 'allowed users (comma separated)',
                'aria-label': 'Allowed GitHub users', autoComplete: 'off',
                onChange: (event) => setAllowed(event.target.value),
                className: GITHUB_FIELD,
              }),
              jsx('div', {
                key: 'allowed-help', className: 'text-[0.6875rem] leading-snug text-(--ui-text-quaternary)',
                children: 'Only these users can trigger work; the agent ignores its own comments — add a teammate or a second account.',
              }),
              jsxs('div', {
                key: 'mode', className: 'flex items-center gap-1 text-[0.6875rem] text-(--ui-text-quaternary)',
                children: [
                  jsx('span', { key: 'l', className: 'mr-1', children: 'Watch' }),
                  jsx(ModeButton, { key: 'observe', label: 'Observe', active: mode === 'observe', onClick: () => setMode('observe') }),
                  jsx(ModeButton, { key: 'auto', label: 'Auto', active: mode === 'auto', onClick: () => setMode('auto') }),
                ],
              }),
              jsxs('div', {
                key: 'actions', className: 'flex flex-wrap items-center gap-1',
                children: [
                  jsx(GhostButton, {
                    key: 'connect', disabled: connect.isPending || !String(repo || '').trim(),
                    onClick: () => connect.mutate(),
                    children: connect.isPending ? 'Connecting…' : 'Connect',
                  }),
                  jsx(GhostButton, {
                    key: 'save', disabled: save.isPending, onClick: () => save.mutate(),
                    children: save.isPending ? 'Saving…' : 'Save settings',
                  }),
                  jsx(GhostButton, {
                    key: 'poll', disabled: poll.isPending, onClick: () => poll.mutate(),
                    children: poll.isPending ? 'Polling…' : 'Poll now',
                  }),
                  jsx(GhostButton, {
                    key: 'disconnect', disabled: disconnect.isPending || !configured,
                    onClick: () => disconnect.mutate(),
                    children: disconnect.isPending ? 'Disconnecting…' : 'Disconnect',
                  }),
                ],
              }),
            ],
          }),
          jsxs('div', {
            key: 'prs', className: 'flex min-h-0 min-w-0 flex-1 flex-col gap-1',
            children: [
              jsxs('div', {
                key: 'label', className: 'flex items-baseline justify-between gap-2',
                children: [
                  jsx('span', {
                    key: 'l', className: 'text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-quaternary)',
                    children: 'Open pull requests',
                  }),
                  jsx('span', {
                    key: 'n', className: 'text-[0.6875rem] tabular-nums text-(--ui-text-quaternary)',
                    children: openPrs.length ? String(openPrs.length) : '',
                  }),
                ],
              }),
              openPrs.length
                ? jsx('div', {
                    key: 'list', className: 'flex min-h-0 flex-col gap-0.5 overflow-y-auto pr-1',
                    children: openPrs.map((pr) => jsx(GithubPrRow, {
                      key: String(pr.number),
                      pr,
                      onWork: (number) => work.mutate(number),
                      pending: work.isPending && work.variables === pr.number,
                    })),
                  })
                : jsx('div', {
                    key: 'empty', className: 'px-1 py-2 text-xs text-(--ui-text-tertiary)',
                    children: prsLoading
                      ? 'Loading pull requests…'
                      : configured
                        ? 'No open pull requests.'
                        : 'Connect a repository to watch its PRs.',
                  }),
            ],
          }),
        ],
      }),
    ],
  })
}

// -- onboarding -------------------------------------------------------------

const ONBOARDING_STEPS = [
  {
    key: 'settings',
    short: 'Model preset',
    title: 'Pick a model / preset',
    hint: 'Set a default in Settings, or use the model picker in the composer.',
  },
  {
    key: 'github',
    short: 'Watch a repo',
    title: 'Connect a GitHub repo to watch PRs',
    hint: 'An empty allow-list means nobody can trigger work, and the agent ignores its own comments.',
  },
  {
    key: 'chat',
    short: 'Start a chat',
    title: 'Start a chat below',
    hint: 'Describe the work, pick a repo if you want one, then press Enter.',
  },
]

/** One slim row above the transcript: three numbered steps, each clickable,
 *  and a dismiss that is remembered. Never a second card competing with the
 *  chat. */
function OnboardingBanner({ onSettings, onGithub, onStart, onDismiss }) {
  const actions = { settings: onSettings, github: onGithub, chat: onStart }
  return jsxs('section', {
    'aria-label': 'Getting started',
    className: cn('flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 rounded border',
      'border-(--ui-stroke-secondary) px-2.5 py-1.5 text-[0.6875rem]'),
    children: [
      jsx('span', {
        key: 'l', className: 'shrink-0 font-medium text-(--ui-text-secondary)',
        children: 'Get started',
      }),
      ...ONBOARDING_STEPS.map((step, index) => jsx('button', {
        key: step.key,
        type: 'button',
        title: step.hint,
        'aria-label': `${step.title}: ${step.hint}`,
        onClick: actions[step.key],
        className: cn('flex min-w-0 items-center gap-1 rounded px-1 py-0.5',
          'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'),
        children: [
          jsx('span', {
            key: 'n',
            className: cn('flex h-4 w-4 shrink-0 items-center justify-center rounded-full',
              'border border-(--ui-stroke-secondary) text-[0.625rem] tabular-nums text-(--ui-text-tertiary)'),
            children: String(index + 1),
          }),
          jsx('span', { key: 's', className: 'min-w-0 truncate', children: step.short }),
        ],
      })),
      jsx(GhostButton, {
        key: 'dismiss', className: 'ml-auto shrink-0 px-1 py-0',
        title: 'Dismiss getting started', 'aria-label': 'Dismiss getting started',
        onClick: onDismiss, children: '✕',
      }),
    ],
  })
}

// -- overlay drawers --------------------------------------------------------

/** A right-side panel over the chat. It is absolutely positioned, so opening
 *  GitHub or Settings cannot reflow the transcript, and it scrolls on its own.
 *  Escape and the ✕ close it; the page restores focus to the header trigger. */
function OverlayDrawer({ panel, title, subtitle, onClose, children }) {
  const panelRef = useRef(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    const el = panelRef.current
    if (el && typeof el.focus === 'function') el.focus()
    const onKeyDown = (event) => {
      if (!event || event.key !== 'Escape') return
      if (typeof event.preventDefault === 'function') event.preventDefault()
      closeRef.current()
    }
    if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return undefined
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  return jsxs('section', {
    'aria-label': title,
    'data-drawer': panel,
    ref: panelRef,
    tabIndex: -1,
    className: cn('absolute inset-y-0 right-0 z-20 flex w-full max-w-full flex-col border-l',
      'border-(--ui-stroke-secondary) bg-(--ui-bg-primary) shadow-lg focus:outline-none',
      'md:w-[380px] md:max-w-[92vw]'),
    children: [
      jsxs('div', {
        key: 'head',
        className: cn('flex h-9 shrink-0 items-center justify-between gap-2 border-b',
          'border-(--ui-stroke-secondary) px-3'),
        children: [
          jsxs('div', {
            key: 't', className: 'flex min-w-0 items-baseline gap-2',
            children: [
              jsx('span', {
                key: 'l', className: 'shrink-0 text-xs font-medium text-(--ui-text-secondary)',
                children: title,
              }),
              subtitle
                ? jsx('span', {
                    key: 's', title: subtitle,
                    className: 'min-w-0 truncate text-[0.6875rem] text-(--ui-text-quaternary)',
                    children: subtitle,
                  })
                : null,
            ],
          }),
          jsx('button', {
            key: 'x', type: 'button', 'aria-label': `Close ${title}`, title: 'Close (Esc)',
            onClick: onClose,
            className: cn('inline-flex h-6 w-6 shrink-0 items-center justify-center rounded',
              'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)'),
            children: '✕',
          }),
        ],
      }),
      jsx('div', { key: 'body', className: 'flex min-h-0 flex-1 flex-col overflow-hidden', children }),
    ],
  })
}

// -- page -------------------------------------------------------------------

function SuperagentPage() {
  const queryClient = useQueryClient()
  // The rail's "Show archived" toggle: a remembered view, not a per-session
  // flag. `?archived=all` keeps archived rows on the same board so they can be
  // read, unarchived or deleted.
  const [showArchived, setShowArchived] = useState(() => readStored('showArchived', '') === '1')
  const archivedMode = showArchived ? 'all' : 'hide'
  const chatsQuery = useChats(archivedMode)
  const modelsQuery = useModels()
  const [selectedId, setSelectedId] = useState(null)
  // The draft is per target; `draft:new` starts the page, a chat restores its
  // own bucket as soon as it is selected. Nothing here ever reads the server's
  // `prompt` field — that would overwrite what the person is typing.
  const [draft, setDraft] = useState(() => readStored(DRAFT_NEW_KEY, ''))
  const draftKeyRef = useRef(DRAFT_NEW_KEY)
  const draftRef = useRef(draft)
  draftRef.current = draft
  // The user's text, shown as a bubble until the server's own row lands.
  const [optimistic, setOptimistic] = useState(null)
  const [recentRepos, setRecentRepos] = useState(() => readRecentRepos())
  const [model, setModel] = useState(() => ({
    model: readStored('lastModel', ''), provider: readStored('lastProvider', ''),
  }))
  const [repo, setRepo] = useState(() => readStored('lastRepo', ''))
  // Exactly one drawer at a time: opening either closes the other.
  const [drawer, setDrawer] = useState(null)
  const githubOpen = drawer === 'github'
  const settingsOpen = drawer === 'settings'
  const usageOpen = drawer === 'usage'
  const [presetName, setPresetName] = useState('')
  const [onboarded, setOnboarded] = useState(() => readStored('onboarded', '') === '1')
  const detail = useChat(selectedId)
  const settingsQuery = useSettings(settingsOpen)
  const usageQuery = useUsage(usageOpen)
  const inputRef = useRef(null)
  // Focus returns here when a drawer closes (Escape, ✕, or a pick inside it).
  const githubRef = useRef(null)
  const settingsRef = useRef(null)

  // One bucket per target. Switching flushes the outgoing text *before* the
  // key moves, so a click 50ms after a keystroke cannot drop the draft.
  useEffect(() => {
    const key = draftKeyFor(selectedId)
    if (key === draftKeyRef.current) return
    writeStored(draftKeyRef.current, draftRef.current)
    draftKeyRef.current = key
    setDraft(readStored(key, ''))
    setOptimistic(null)
  }, [selectedId])

  // Debounced, so typing never writes storage on every keystroke.
  useEffect(() => {
    const key = draftKeyRef.current
    const timer = setTimeout(() => writeStored(key, draft), DRAFT_DEBOUNCE)
    return () => clearTimeout(timer)
  }, [draft])

  // Leaving the page keeps the last keystroke, even inside the debounce window.
  useEffect(() => () => { writeStored(draftKeyRef.current, draftRef.current) }, [])

  const data = chatsQuery.data
  const sessions = useMemo(() => {
    return ((data && data.sessions) || []).slice().sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))
  }, [data])
  // Completion toasts: diff statuses poll by poll. The ref is seeded by the
  // first effect run, so mounting the page never replays old completions.
  const previousStatus = useRef(null)
  useEffect(() => {
    previousStatus.current = announceCompletions(previousStatus.current, sessions)
  }, [sessions])
  const counts = (data && data.counts) || {}
  const running = (data && data.running) || 0
  const queued = counts.queued || 0
  const selectedSummary = useMemo(
    () => sessions.find((session) => session.id === selectedId) || null,
    [sessions, selectedId],
  )
  const selectedSession = (detail.data && detail.data.session) || selectedSummary

  // A PR badge anywhere on the board keeps the status fresh; the panel keeps
  // it fresh while open. Otherwise the status is fetched once (header dot).
  const hasPrBadge = useMemo(
    () => sessions.some((session) => !!session.pr_number),
    [sessions],
  )
  const githubQuery = useGithubStatus(githubOpen || hasPrBadge)
  const githubPrsQuery = useGithubPrs(githubOpen)

  const createChat = useMutation({
    mutationFn: (body) => rest('/chats', { method: 'POST', body }),
    onSuccess: (result) => {
      invalidateChats(queryClient)
      if (result && result.session && result.session.id) setSelectedId(result.session.id)
    },
  })

  const sendChat = useMutation({
    mutationFn: (vars) => rest(`/chats/${encodeURIComponent(vars.id)}/messages`, {
      method: 'POST', body: { content: vars.content },
    }),
    onSuccess: (result) => {
      invalidateChats(queryClient, selectedId)
      // A session with nothing to resume is replaced by a fresh chat.
      if (result && result.session && result.session.id) setSelectedId(result.session.id)
    },
  })

  const stopChat = useMutation({
    mutationFn: (id) => rest(`/chats/${encodeURIComponent(id)}/stop`, { method: 'POST' }),
    onSuccess: () => invalidateChats(queryClient, selectedId),
  })

  // Retry reuses the prompt already in the transcript — no new message is
  // appended, so the button is only offered for a stopped turn.
  const retryChat = useMutation({
    mutationFn: (id) => rest(`/chats/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
    onSuccess: () => invalidateChats(queryClient, selectedId),
  })

  // The export is a plain read: save the generated Markdown locally, no write
  // to the chat and no change to its state.
  const exportChat = useMutation({
    mutationFn: (id) => fetchChatExport(id),
    onSuccess: (data) => {
      if (data && data.markdown) {
        downloadMarkdown(data.filename || 'superagent-chat.md', data.markdown)
      }
    },
  })

  // A chat's model is fixed at creation; this swaps it for future turns.
  const setChatModel = useMutation({
    mutationFn: (vars) => rest(`/chats/${encodeURIComponent(vars.id)}/model`, {
      method: 'POST', body: { model: vars.model, provider: vars.provider },
    }),
    onSuccess: () => invalidateChats(queryClient, selectedId),
  })

  /** Paint one session's change before the next poll (a rename, an archive
   *  stamp). A shell without setQueryData simply waits for the refetch. */
  const patchSession = (id, patch) => {
    if (!queryClient || typeof queryClient.setQueryData !== 'function') return
    const apply = (row) => (row.id === id ? { ...row, ...patch } : row)
    queryClient.setQueryData([ID, 'chats', archivedMode], (current) => (
      current && Array.isArray(current.sessions)
        ? { ...current, sessions: current.sessions.map(apply) }
        : current
    ))
    queryClient.setQueryData([ID, 'chat', id], (current) => (
      current && current.session
        ? { ...current, session: { ...current.session, ...patch } }
        : current
    ))
  }

  /** Remove one row from the cached board (delete, or archive while hiding). */
  const dropSession = (id) => {
    if (!queryClient || typeof queryClient.setQueryData !== 'function') return
    queryClient.setQueryData([ID, 'chats', archivedMode], (current) => (
      current && Array.isArray(current.sessions)
        ? { ...current, sessions: current.sessions.filter((row) => row.id !== id) }
        : current
    ))
  }

  const renameChat = useMutation({
    mutationFn: (vars) => rest(`/chats/${encodeURIComponent(vars.id)}`, {
      method: 'PATCH', body: { title: vars.title },
    }),
    // A failed rename must put the old title back, which the refetch does.
    onSuccess: () => invalidateChats(queryClient, selectedId),
    onError: () => invalidateChats(queryClient, selectedId),
  })

  const archiveChat = useMutation({
    mutationFn: (vars) => rest(`/chats/${encodeURIComponent(vars.id)}/archive`, {
      method: 'POST', body: { archived: vars.archived },
    }),
    onSuccess: () => invalidateChats(queryClient, selectedId),
    onError: () => invalidateChats(queryClient, selectedId),
  })

  const deleteChat = useMutation({
    mutationFn: (id) => rest(`/chats/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    onSuccess: () => invalidateChats(queryClient, selectedId),
    onError: () => invalidateChats(queryClient, selectedId),
  })

  const renameSession = (id, title) => {
    patchSession(id, { title })
    renameChat.mutate({ id, title })
  }

  const archiveSession = (id, archived) => {
    // A live turn cannot be filed away (the server answers 409). Painting the
    // change first would flash the row out and back, so a busy chat waits for
    // the answer and the error notice instead.
    const target = sessions.find((row) => row.id === id)
    if (!isBusy(target)) {
      if (archived && archivedMode !== 'all') dropSession(id)
      else patchSession(id, { archived_at: archived ? Date.now() / 1000 : null })
    }
    archiveChat.mutate({ id, archived })
  }

  const deleteSession = (id) => {
    // Same rule as archive: never hide a row the server may refuse to delete.
    const target = sessions.find((row) => row.id === id)
    if (!isBusy(target)) {
      dropSession(id)
      if (selectedId === id) setSelectedId(null)
    }
    deleteChat.mutate(id)
  }

  const toggleArchived = () => {
    const next = !showArchived
    setShowArchived(next)
    writeStored('showArchived', next ? '1' : '0')
  }

  const pending = createChat.isPending || sendChat.isPending
  const stopPending = stopChat.isPending
  const busyTurn = isBusy(selectedSession)
  const notice = (createChat.data && createChat.data.ok === false && createChat.data.error) ||
    (sendChat.data && sendChat.data.ok === false && sendChat.data.error) || ''
  const failure = errorText(createChat.error) || errorText(sendChat.error) ||
    errorText(stopChat.error) || errorText(retryChat.error) ||
    errorText(exportChat.error) || errorText(setChatModel.error) ||
    errorText(renameChat.error) || errorText(archiveChat.error) ||
    errorText(deleteChat.error) || notice

  const changeSessionModel = (next) => {
    if (!selectedSession || !next) return
    const pickedModel = String(next.model || '').trim()
    const pickedProvider = String(next.provider || '').trim()
    if (!pickedModel && !pickedProvider) return
    setChatModel.mutate({ id: selectedSession.id, model: pickedModel, provider: pickedProvider })
  }

  /** How many rows in the open transcript already carry this exact text, so an
   *  optimistic bubble is replaced — never duplicated — when the server's own
   *  user message lands. */
  const userMessageCount = (text) => {
    const session = selectedSession
    if (!session) return 0
    let count = String(session.prompt || '').trim() === text ? 1 : 0
    for (const message of session.messages || []) {
      if (message && message.role === 'user' && String(message.content || '').trim() === text) count += 1
    }
    return count
  }

  const submit = () => {
    const text = String(draft || '').trim()
    if (!text || pending || busyTurn) return
    const key = draftKeyRef.current
    const sentRepo = String(repo || '').trim()
    // The bubble goes up now; the draft stays in the (disabled) box so a
    // failed send loses nothing.
    setOptimistic({ text, seen: userMessageCount(text) })
    const clear = (nextRepos) => {
      setDraft('')
      writeStored(key, '')
      setOptimistic(null)
      if (nextRepos) setRecentRepos(nextRepos)
    }
    const failed = () => setOptimistic(null)
    if (selectedId) {
      sendChat.mutate({ id: selectedId, content: text }, {
        onSuccess: () => clear(null),
        onError: failed,
      })
      return
    }
    writeStored('lastModel', model.model)
    writeStored('lastProvider', model.provider)
    writeStored('lastRepo', repo)
    createChat.mutate(
      {
        prompt: text, model: model.model, provider: model.provider, repo,
        preset: presetName || undefined,
      },
      {
        onSuccess: () => clear(sentRepo ? rememberRepo(sentRepo) : null),
        onError: failed,
      },
    )
  }

  // Selection restores that chat's own bucket; the effect above does the work.
  const select = (id) => setSelectedId(id)

  const pickPreset = (preset) => {
    if (!preset) return
    setPresetName(String(preset.name || ''))
    setModel({
      model: String(preset.model || ''),
      provider: String(preset.provider || ''),
    })
  }

  const changeModel = (next) => {
    // An ad-hoc pick is no longer the named preset; the chip clears.
    setPresetName('')
    setModel(next)
  }

  const dismissOnboarding = () => {
    setOnboarded(true)
    writeStored('onboarded', '1')
  }

  const startChat = () => {
    if (typeof window !== 'undefined') {
      window.setTimeout(() => { if (inputRef.current) inputRef.current.focus() }, 0)
    }
  }

  const newChat = () => {
    // The draft is not cleared here: the new-chat bucket (or an in-progress
    // empty box) is what the page should show.
    setSelectedId(null)
    setPresetName('')
    setModel({ model: readStored('lastModel', ''), provider: readStored('lastProvider', '') })
    setRepo(readStored('lastRepo', ''))
    startChat()
  }

  const presetList = (modelsQuery.data && modelsQuery.data.presets) || []

  /** Toggle one drawer; the other closes because there is a single slot. */
  const toggleDrawer = (name) => setDrawer((current) => (current === name ? null : name))

  /** Close, then hand focus back to the button that opened it. */
  const closeDrawer = () => {
    const which = drawer
    setDrawer(null)
    const target = which === 'settings'
      ? settingsRef.current
      : (which === 'github' ? githubRef.current : null)
    if (target && typeof target.focus === 'function') target.focus()
  }

  const stop = () => {
    if (!selectedSession || stopPending) return
    stopChat.mutate(selectedSession.id)
  }

  // Only the segments that are non-zero: '2 running · 1 review', never '0 running'.
  const summaryParts = []
  if (running) summaryParts.push(`${running} running`)
  if (queued) summaryParts.push(`${queued} queued`)
  if (counts.review) summaryParts.push(`${counts.review} review`)
  if (counts.failed) summaryParts.push(`${counts.failed} failed`)
  const summaryLabel = summaryParts.join(' · ')

  // The first-run banner rides above the transcript, not above the whole page.
  // A board whose only chats are archived is not a first run: the rail's
  // "Show archived" toggle is the answer there, not an onboarding card.
  const archivedTotal = (data && data.archived_count) || 0
  const onboarding = !chatsQuery.isLoading && !onboarded && sessions.length === 0 && !archivedTotal
    ? jsx(OnboardingBanner, {
        onSettings: () => setDrawer('settings'),
        onGithub: () => setDrawer('github'),
        onStart: startChat,
        onDismiss: dismissOnboarding,
      })
    : null

  return jsxs('div', {
    className: 'relative flex h-full min-h-0 flex-col gap-3 overflow-hidden p-4 text-sm',
    children: [
      jsxs('div', {
        key: 'header', className: 'flex h-11 shrink-0 items-center justify-between gap-3',
        children: [
          jsxs('div', {
            key: 'title', className: 'flex min-w-0 items-baseline gap-2',
            children: [
              jsx('span', { key: 'name', className: 'shrink-0 text-sm font-medium', children: 'Superagent' }),
              summaryLabel
                ? jsx('span', {
                    key: 'counts', className: 'min-w-0 truncate text-xs text-(--ui-text-quaternary)',
                    children: summaryLabel,
                  })
                : null,
            ],
          }),
          jsxs('div', {
            key: 'actions', className: 'flex shrink-0 items-center gap-1.5',
            children: [
              jsx('button', {
                key: 'new', type: 'button', onClick: newChat,
                className: cn('shrink-0 rounded bg-primary px-2.5 py-1 text-xs font-medium',
                  'text-primary-foreground hover:bg-primary/90'),
                children: 'New chat',
              }),
              jsx(GithubHeaderButton, {
                key: 'github', open: githubOpen, status: githubQuery.data,
                onClick: () => toggleDrawer('github'), buttonRef: githubRef,
              }),
              jsx(HeaderIconButton, {
                key: 'usage', label: 'Usage',
                tip: 'Usage: chats, tokens and models for the last 14 days',
                active: usageOpen,
                onClick: () => toggleDrawer('usage'),
                children: '▤',
              }),
              jsx(HeaderIconButton, {
                key: 'settings', label: 'Settings',
                tip: 'Settings: default model, presets and worker limits',
                active: settingsOpen,
                onClick: () => toggleDrawer('settings'),
                buttonRef: settingsRef,
                children: '⚙',
              }),
            ],
          }),
        ],
      }),
      chatsQuery.isError
        ? jsx('div', {
            key: 'error', className: 'shrink-0',
            children: jsx(ErrorCard, {
              title: 'Superagent backend unavailable. Enable the plugin first.',
              error: chatsQuery.error, onRetry: () => chatsQuery.refetch(),
            }),
          })
        : null,
      jsxs('div', {
        key: 'body', className: 'flex min-h-0 flex-1 flex-col gap-3 md:flex-row',
        children: [
          jsx(SessionsRail, {
            key: 'rail', sessions, selectedId, onSelect: select,
            onRename: renameSession, onArchive: archiveSession, onDelete: deleteSession,
            showArchived, onToggleArchived: toggleArchived,
            archivedCount: archivedTotal,
          }),
          jsx(ChatCenter, {
            key: 'center', summary: selectedSummary, detail,
            sessions, onSelect: select, banner: onboarding, optimistic,
            draft, onDraft: setDraft, onSend: submit, onStop: stop,
            pending, stopPending, error: failure,
            // The PR comment needs a saved token; the status query already
            // polls while a PR badge is on the board, so this is free.
            canComment: !!(githubQuery.data && githubQuery.data.token_set),
            model, onModelChange: changeModel, repo, onRepoChange: setRepo,
            models: modelsQuery.data, inputRef, recentRepos,
            onSessionModel: changeSessionModel, modelPending: setChatModel.isPending,
            presets: presetList, presetName, onPreset: pickPreset,
            onPresetClear: () => setPresetName(''),
            retry: selectedSession
              && (selectedSession.status === 'failed' || selectedSession.status === 'cancelled')
              ? { pending: retryChat.isPending, onRetry: () => retryChat.mutate(selectedSession.id) }
              : null,
            exportMd: selectedSession
              ? { pending: exportChat.isPending, onExport: () => exportChat.mutate(selectedSession.id) }
              : null,
            onUnarchive: selectedSession
              ? () => archiveSession(selectedSession.id, false)
              : null,
          }),
        ],
      }),
      githubOpen
        ? jsx(OverlayDrawer, {
            key: 'github-drawer',
            panel: 'github',
            title: 'GitHub',
            subtitle: (githubQuery.data && githubQuery.data.repo) || 'no repository',
            onClose: closeDrawer,
            children: jsx(GithubPanel, {
              status: githubQuery.data,
              prs: githubPrsQuery.data, prsLoading: githubPrsQuery.isLoading,
              onSelectSession: (id) => { select(id); closeDrawer() },
            }),
          })
        : null,
      settingsOpen
        ? jsx(OverlayDrawer, {
            key: 'settings-drawer',
            panel: 'settings',
            title: 'Settings',
            subtitle: 'defaults, presets, worker limits',
            onClose: closeDrawer,
            children: jsx(SettingsPanel, {
              settings: settingsQuery.data, models: modelsQuery.data,
              loading: settingsQuery.isLoading, error: settingsQuery.error,
              onRetry: () => settingsQuery.refetch(),
            }),
          })
        : null,
      usageOpen
        ? jsx(OverlayDrawer, {
            key: 'usage-drawer',
            panel: 'usage',
            title: 'Usage',
            subtitle: 'last 14 days',
            onClose: closeDrawer,
            children: jsx(UsagePanel, {
              usage: usageQuery.data, loading: usageQuery.isLoading,
              error: usageQuery.error, onRetry: () => usageQuery.refetch(),
            }),
          })
        : null,
    ],
  })
}

// -- ticket pane + chip (iteration 1 surface, kept) -------------------------

function TicketRow({ ticket }) {
  return jsxs('div', {
    className: 'flex flex-col gap-0.5 rounded px-2 py-1.5 text-xs',
    children: [
      jsxs('div', {
        key: 'head', className: 'flex items-baseline gap-1.5',
        children: [
          jsx('span', { key: 'icon', className: 'text-(--ui-text-quaternary)', children: ICONS[ticket.status] || '?' }),
          jsx('span', { key: 'id', className: 'font-mono text-(--ui-text-tertiary)', children: ticket.id }),
          jsx('span', { key: 'status', className: 'text-(--ui-text-tertiary)', children: ticket.status }),
          jsx('span', { key: 'req', className: 'truncate text-(--ui-text-secondary)', children: ticket.request }),
        ],
      }),
      ticket.reason
        ? jsx('div', { key: 'reason', className: 'truncate pl-4 text-(--ui-text-quaternary)', children: ticket.reason })
        : null,
    ],
  })
}

function ChatMiniRow({ session }) {
  return jsxs('div', {
    className: 'flex min-w-0 items-baseline gap-1.5 px-2 text-xs',
    children: [
      jsx(StatusGlyph, { key: 'g', status: session.status, pulse: true }),
      jsx('span', { key: 't', className: 'min-w-0 flex-1 truncate text-(--ui-text-secondary)', children: sessionTitle(session) }),
      jsx('span', { key: 'a', className: 'shrink-0 text-(--ui-text-quaternary)', children: timingLabel(session) }),
    ],
  })
}

function SuperagentPane() {
  const { data, isLoading, isError, error, refetch } = useTickets()
  const chats = useChats()

  if (isLoading && !data) {
    return jsx('div', { className: 'p-3 text-xs text-(--ui-text-tertiary)', children: 'Loading tickets…' })
  }

  if (isError) {
    return jsxs('div', {
      className: 'flex flex-col gap-2 p-3 text-xs',
      children: [
        jsx('div', {
          key: 'msg', className: 'text-(--ui-text-tertiary)',
          children: 'Superagent backend unavailable. Enable the plugin first.',
        }),
        jsx('div', { key: 'err', className: 'truncate text-(--ui-text-quaternary)', children: errorText(error) }),
        jsx('button', {
          key: 'retry', type: 'button',
          className: cn('self-start rounded px-2 py-1', 'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'),
          onClick: () => refetch(),
          children: 'Retry',
        }),
      ],
    })
  }

  const attention = (data && data.attention) || []
  const tickets = (data && data.tickets) || []
  const chatSessions = (chats.data && chats.data.sessions) || []
  const chatRunning = (chats.data && chats.data.running) || 0
  const live = chatSessions.filter((session) => isBusy(session) || session.status === 'review').slice(0, 4)

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 overflow-y-auto p-3 text-sm',
    children: [
      jsx('div', {
        key: 'head', className: 'flex items-center justify-between text-xs text-(--ui-text-tertiary)',
        children: [
          jsx('span', {
            key: 'counts',
            children: `chats ${chatRunning} running · tickets ${(data && data.running) || 0} running`,
          }),
          jsx('button', {
            key: 'refresh', type: 'button',
            className: 'rounded px-1.5 py-0.5 hover:bg-(--chrome-action-hover) hover:text-foreground',
            onClick: () => { refetch(); chats.refetch() },
            children: 'Refresh',
          }),
        ],
      }),
      live.length
        ? jsxs('div', {
            key: 'chats', className: 'flex flex-col gap-1',
            children: [
              jsx('div', { key: 'label', className: 'text-xs font-medium text-(--ui-text-tertiary)', children: 'Latest chats' }),
              ...live.map((session) => jsx(ChatMiniRow, { key: session.id, session })),
            ],
          })
        : null,
      attention.length
        ? jsxs('div', {
            key: 'needs', className: 'flex flex-col gap-1',
            children: [
              jsx('div', { key: 'label', className: 'text-xs font-medium', children: 'Needs you' }),
              ...attention.map((ticket) => jsx(TicketRow, { key: ticket.id, ticket })),
            ],
          })
        : jsx('div', { key: 'clear', className: 'text-xs text-(--ui-text-tertiary)', children: 'Nothing needs you.' }),
      tickets.length
        ? jsxs('div', {
            key: 'recent', className: 'flex flex-col gap-1',
            children: [
              jsx('div', { key: 'label', className: 'text-xs font-medium text-(--ui-text-tertiary)', children: 'Recent tickets' }),
              ...tickets.slice(0, 12).map((ticket) => jsx(TicketRow, { key: ticket.id, ticket })),
            ],
          })
        : null,
    ],
  })
}

function SuperagentChip() {
  const tickets = useTickets()
  const chats = useChats()
  const chatData = chats.data
  const counts = (chatData && chatData.counts) || {}
  const running = (chatData && chatData.running) || 0
  const waiting = (counts.review || 0) + (counts.failed || 0)
  // Chats are the primary surface: the chip speaks for them as soon as the
  // list returns. Ticket attention is only the fallback for a backend that has
  // not answered at all (disabled plugin, first paint, older data source).
  const ticketAttention = (tickets.data && tickets.data.attention && tickets.data.attention.length) || 0
  const attention = chatData ? waiting : ticketAttention
  const count = running + attention
  const parts = []
  if (running) parts.push(`▶ ${running}`)
  if (attention) parts.push(`◆ ${attention}`)
  const label = parts.length ? parts.join(' ') : 'superagent'
  const unit = chatData ? 'chat' : 'ticket'
  const plural = (n) => `${n} ${unit}${n === 1 ? '' : 's'}`
  const tip = running && attention
    ? `${plural(running)} running · ${plural(attention)} need you`
    : running
      ? `${plural(running)} running`
      : attention > 0
        ? (chatData ? `${plural(attention)} need review` : `${plural(attention)} need a human`)
        : 'Superagent: nothing needs you'
  return jsx(Tip, {
    label: tip,
    children: jsx('span', {
      className: cn('inline-flex h-full items-center px-1.5 text-[0.6875rem]',
        count > 0 ? 'text-foreground' : 'text-(--ui-text-tertiary)'),
      children: label,
    }),
  })
}

export default {
  id: ID, // must match the folder name
  name: 'Superagent',
  register(ctx) {
    rest = ctx.rest
    storage = ctx.storage && typeof ctx.storage.get === 'function' ? ctx.storage : null
    openExternal = ctx.os && typeof ctx.os.openExternal === 'function' ? ctx.os.openExternal : null
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Superagent',
      data: { placement: 'right', width: '300px' },
      render: () => jsx(SuperagentPane, {}),
    })
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 130,
      render: () => jsx(SuperagentChip, {}),
    })
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/superagent' },
        render: () => jsx(SuperagentPage, {}),
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: '/superagent', label: 'Superagent', codicon: 'project' },
      },
    ])
  },
}
