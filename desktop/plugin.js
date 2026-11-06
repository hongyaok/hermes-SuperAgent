/**
 * Superagent desktop surface — a chat-first workspace over headless chats.
 *
 * Data path: ctx.rest('/chats'), '/chats/{id}', '/chats/{id}/diff',
 * '/chats/{id}/comment-preview', '/models', '/settings', POST /chats,
 * POST /chats/{id}/messages, POST /chats/{id}/model, POST /chats/{id}/stop,
 * POST /chats/{id}/comment, POST /settings → dashboard/plugin_api.py. The list
 * query polls at 2s while a worker runs and 8s when the board is quiet; the
 * open transcript polls at 1.5s while its own turn is live. The diff panel is
 * lazy (only while open) and refetches at 4s only while the turn is running.
 * Every mutation invalidates both so the rail, the transcript, and the live
 * grid agree on the next paint. The per-card elapsed timer ticks on local
 * state only — it must never add a request to those budgets.
 *
 * The ticket pane and the status-bar chip stay (they answer "does a human need
 * to look at anything?"); the route page is chat-only.
 *
 * Plain ESM, loaded uncompiled — UI is jsx()/jsxs() calls, not JSX syntax.
 * Only @hermes/plugin-sdk, react and react/jsx-runtime resolve here. The SDK is
 * imported as a namespace: a shell that predates a component (the model
 * catalog menu, StatusDot) leaves that binding undefined and the page degrades
 * instead of failing to link.
 */

import * as SDK from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useMemo, useRef, useState } from 'react'

const {
  Tip, StatusDot, DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
  ModelCatalogMenu, ModelMenuCloseContext, cn, host,
  useQuery, useMutation, useQueryClient, ROUTES_AREA, SIDEBAR_NAV_AREA,
} = SDK

const ID = 'superagent'

// Polling budgets. A live worker is worth 2s; a quiet board is not.
const LIST_POLL_RUNNING = 2000
const LIST_POLL_QUIET = 8000
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

/** Token count for a finished card: '812 tok' / '4.2k tok' / '1.3M tok'. */
function fmtTokens(tokens) {
  const n = Number(tokens)
  if (!isFinite(n) || n <= 0) return ''
  if (n < 1000) return `${Math.round(n)} tok`
  if (n < 1000000) return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k tok`
  return `${(n / 1000000).toFixed(1)}M tok`
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

function errorText(error) {
  if (!error) return ''
  return String((error && error.message) || error)
}

// -- data hooks -------------------------------------------------------------

function fetchTickets() {
  return rest ? rest('/tickets') : Promise.reject(new Error('backend not ready'))
}

function fetchChats() {
  return rest ? rest('/chats?limit=50') : Promise.reject(new Error('backend not ready'))
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

function fetchModels() {
  return rest ? rest('/models') : Promise.reject(new Error('backend not ready'))
}

function fetchSettings() {
  return rest ? rest('/settings') : Promise.reject(new Error('backend not ready'))
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

function useChats() {
  return useQuery({
    queryKey: [ID, 'chats'], queryFn: fetchChats, retry: false,
    refetchInterval: (query) => {
      const data = query.state.data
      return data && (data.running || 0) > 0 ? LIST_POLL_RUNNING : LIST_POLL_QUIET
    },
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

function Pill({ children }) {
  return jsx('span', {
    className: cn('inline-flex items-center rounded border border-(--ui-stroke-secondary)',
      'px-1.5 py-0.5 text-[0.6875rem] text-(--ui-text-tertiary)'),
    children,
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

function GithubButton({ open, status, onClick }) {
  const configured = !!(status && status.configured)
  const repo = configured ? String(status.repo || '') : ''
  const short = repo ? repo.split('/').filter(Boolean).pop() : ''
  return jsxs('button', {
    type: 'button',
    onClick,
    'aria-expanded': !!open,
    'aria-label': 'GitHub connection',
    className: cn('flex items-center gap-1 rounded px-2 py-1 text-xs',
      'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)',
      open && 'bg-(--chrome-action-hover)'),
    children: [
      jsx(GithubDot, { key: 'dot', configured }),
      jsx('span', { key: 'label', children: 'GitHub' }),
      short
        ? jsx('span', {
            key: 'repo', className: 'max-w-[7rem] truncate text-(--ui-text-quaternary)',
            title: repo, children: short,
          })
        : null,
      jsx('span', { key: 'chevron', className: 'text-(--ui-text-quaternary)', children: '▾' }),
    ],
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

function SessionRow({ session, selected, onSelect }) {
  const age = session.status === 'running' ? timingLabel(session) : fmtAge(session.updated_at || session.created_at)
  return jsxs('button', {
    type: 'button',
    onClick: () => onSelect(session.id),
    className: cn('flex w-full flex-col gap-0.5 rounded px-2 py-1.5 text-left',
      selected ? 'bg-(--chrome-action-hover)' : 'hover:bg-(--chrome-action-hover)'),
    children: [
      jsxs('div', {
        key: 'top', className: 'flex min-w-0 items-baseline gap-1.5',
        children: [
          jsx(StatusGlyph, { key: 'g', status: session.status, pulse: true }),
          jsx('span', { key: 't', className: 'min-w-0 flex-1 truncate text-(--ui-text-secondary)', children: sessionTitle(session) }),
        ],
      }),
      jsxs('div', {
        key: 'meta', className: 'flex min-w-0 items-baseline gap-1.5 pl-4 text-[0.6875rem] text-(--ui-text-quaternary)',
        children: [
          jsx('span', { key: 'm', className: 'min-w-0 flex-1 truncate', children: sessionModelLabel(session) }),
          jsx(PrBadge, { key: 'pr', session }),
          jsx('span', { key: 'a', className: 'shrink-0 tabular-nums', children: age }),
        ],
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

function SessionsRail({ sessions, selectedId, onSelect }) {
  const [filter, setFilter] = useState('all')
  const shown = useMemo(
    () => (filter === 'all' ? sessions : sessions.filter((session) => railFilterMatch(session, filter))),
    [sessions, filter],
  )
  return jsxs('div', {
    className: 'flex min-h-0 w-full flex-col gap-2 md:w-[17rem] md:shrink-0',
    children: [
      jsxs('div', {
        key: 'head', className: 'flex items-center justify-between gap-2',
        children: [
          jsx('div', {
            key: 'label', className: 'text-xs font-medium text-(--ui-text-tertiary)',
            children: sessions.length ? `Chats (${sessions.length})` : 'Chats',
          }),
          jsx(RailFilter, { key: 'filter', value: filter, onChange: setFilter }),
        ],
      }),
      jsx('div', {
        key: 'list', className: 'flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto pr-1',
        children: shown.length
          ? shown.map((session) =>
              jsx(SessionRow, { key: session.id, session, selected: session.id === selectedId, onSelect }))
          : jsx('div', { className: 'px-2 py-1 text-xs text-(--ui-text-tertiary)', children: RAIL_EMPTY[filter] }),
      }),
    ],
  })
}

// -- transcript -------------------------------------------------------------

/** Prompt bubble first (the opening turn is not a chat_messages row), then
 *  messages + tool events merged on created_at. */
function buildTranscript(session) {
  if (!session) return []
  const items = [{
    key: 'prompt', kind: 'message', role: 'user',
    content: session.prompt || '', created_at: session.created_at || 0,
  }]
  for (const message of session.messages || []) {
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

function Bubble({ role, content }) {
  const mine = role === 'user'
  return jsx('div', {
    className: cn('max-w-[85%] whitespace-pre-wrap break-words rounded px-2.5 py-1.5',
      'text-xs leading-relaxed text-(--ui-text-secondary)',
      mine ? 'self-end bg-(--chrome-action-hover)' : 'self-start border border-(--ui-stroke-secondary)'),
    children: content,
  })
}

function EventRow({ item }) {
  return jsxs('div', {
    className: 'flex min-w-0 items-baseline gap-1.5 pl-1 text-[0.6875rem] text-(--ui-text-quaternary)',
    children: [
      jsx('span', { key: 'b', children: '▸' }),
      jsx('span', { key: 'k', className: 'shrink-0 font-mono', children: item.eventKind }),
      jsx('span', { key: 'c', className: 'shrink-0', children: ':' }),
      item.detail
        ? jsx('span', { key: 'd', className: 'min-w-0 flex-1 truncate', children: clip(item.detail, 200) })
        : null,
    ],
  })
}

function Transcript({ session }) {
  const items = useMemo(() => buildTranscript(session), [session])
  const boxRef = useRef(null)
  const stickRef = useRef(true)
  const sessionId = session && session.id
  const tail = items.length ? items[items.length - 1].key : 'empty'

  // Switching chats starts at the newest line; polling never moves the box
  // unless the reader was already at the bottom.
  useEffect(() => { stickRef.current = true }, [sessionId])
  useEffect(() => {
    const el = boxRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [sessionId, items.length, tail])

  return jsx('div', {
    ref: boxRef,
    onScroll: () => {
      const el = boxRef.current
      if (!el) return
      stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 96
    },
    className: cn('flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto rounded',
      'border border-(--ui-stroke-secondary) p-3'),
    children: items.length
      ? items.map((item) => item.kind === 'event'
          ? jsx(EventRow, { key: item.key, item })
          : jsx(Bubble, { key: item.key, role: item.role, content: item.content }))
      : jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'No transcript yet.' }),
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

/** Full-width settings surface: defaults, named presets and worker limits.
 *
 * Everything is local state until Save; the effect below reseeds from the
 * server whenever the saved values change (including after a save), so a
 * Cancel/close never leaves stale text behind on the next open.
 */
function SettingsPanel({ settings, models, loading, error, onRetry, onClose }) {
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
    className: cn('flex shrink-0 flex-col gap-2 overflow-y-auto rounded border',
      'border-(--ui-stroke-secondary) p-3 max-h-[26rem]'),
    children: [
      jsxs('div', {
        key: 'head', className: 'flex flex-wrap items-baseline justify-between gap-2',
        children: [
          jsxs('div', {
            key: 'title', className: 'flex min-w-0 items-baseline gap-2',
            children: [
              jsx('span', { key: 'l', className: 'text-xs font-medium text-(--ui-text-secondary)', children: 'Settings' }),
              jsx('span', {
                key: 'h', className: 'min-w-0 truncate text-[0.6875rem] text-(--ui-text-quaternary)',
                children: 'defaults apply to new chats and worker launches',
              }),
            ],
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
              jsx(GhostButton, { key: 'cancel', onClick: onClose, children: 'Cancel' }),
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
            key: 'columns', className: 'flex min-h-0 flex-col gap-3 md:flex-row',
            children: [
              jsxs('div', {
                key: 'defaults', className: 'flex min-w-0 flex-col gap-2 md:w-[22rem] md:shrink-0',
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
                        children: 'No presets yet — the built-in picker list is used.',
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

/** Named preset chips above the composer. Picking one fills the model and
 *  leaves the ModelPicker available for an ad-hoc choice. */
function PresetChips({ presets, active, disabled, onPick }) {
  const list = Array.isArray(presets) ? presets : []
  if (!list.length) return null
  return jsxs('div', {
    role: 'group', 'aria-label': 'Model presets',
    className: 'flex flex-wrap items-center gap-1',
    children: [
      jsx('span', { key: 'l', className: 'shrink-0 pr-0.5', children: 'Preset' }),
      ...list.map((preset) => {
        const name = String(preset.name || '')
        const selected = name === active
        return jsx('button', {
          key: name,
          type: 'button',
          'aria-pressed': selected,
          disabled,
          title: presetLabelText(preset),
          onClick: () => onPick(preset),
          className: cn('rounded border px-1.5 py-0.5 text-[0.6875rem]',
            selected
              ? 'border-(--ui-stroke-secondary) bg-(--chrome-action-hover) text-(--ui-text-secondary)'
              : 'border-transparent text-(--ui-text-quaternary) hover:border-(--ui-stroke-secondary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)',
            disabled && 'opacity-50'),
          children: name,
        })
      }),
    ],
  })
}

/** `provider: model` for a preset (mirrors the backend's label). */
function presetLabelText(preset) {
  const name = String((preset && preset.model) || '')
  const provider = String((preset && preset.provider) || '')
  if (provider && name) return `${provider}: ${name}`
  return name || provider
}

function Composer(props) {
  const { mode, session, draft, onDraft, onSend, onStop, pending, stopPending, error,
    model, onModelChange, repo, onRepoChange, models, inputRef,
    onSessionModel, modelPending, presets, presetName, onPreset } = props
  const busy = isBusy(session)
  const canSend = !!String(draft || '').trim() && !pending && !busy
  const placeholder = mode === 'session'
    ? (busy ? 'Waiting for this turn to finish…' : 'Reply to this chat…')
    : 'Describe the work…'
  const meta = mode === 'session'
    ? [
        jsx(SessionModelControl, {
          key: 'model', session, models, pending: modelPending, onChange: onSessionModel,
        }),
      ]
    : [
        jsx(ModelPicker, { key: 'picker', value: model, models, disabled: pending, onChange: onModelChange }),
        jsx('input', {
          key: 'repo', type: 'text', value: repo, placeholder: 'repo path (optional)',
          onChange: (event) => onRepoChange(event.target.value),
          className: cn('min-w-0 flex-1 rounded border border-(--ui-stroke-secondary) bg-transparent',
            'px-1.5 py-1 text-[0.6875rem] text-(--ui-text-secondary)',
            'placeholder:text-(--ui-text-quaternary) focus:outline-none'),
        }),
        jsx('span', { key: 'hint', className: 'shrink-0', children: 'Enter sends · Shift+Enter newline' }),
      ]

  return jsxs('div', {
    className: 'flex flex-col gap-1.5',
    children: [
      error ? jsx('div', { key: 'err', className: 'truncate text-[0.6875rem] text-(--ui-text-quaternary)', children: error }) : null,
      mode === 'new'
        ? jsx(PresetChips, {
            key: 'presets', presets, active: presetName, disabled: pending, onPick: onPreset,
          })
        : null,
      jsxs('div', {
        key: 'row', className: 'flex items-end gap-2',
        children: [
          jsx('textarea', {
            key: 'input', ref: inputRef, rows: 2, value: draft,
            disabled: pending || busy, placeholder,
            onChange: (event) => onDraft(event.target.value),
            onKeyDown: (event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                onSend()
              }
            },
            className: cn('min-h-16 min-w-0 flex-1 resize-none rounded border border-(--ui-stroke-secondary)',
              'bg-transparent px-2 py-1.5 text-xs text-(--ui-text-secondary)',
              'placeholder:text-(--ui-text-quaternary) focus:outline-none disabled:opacity-60'),
          }),
          jsxs('div', {
            key: 'actions', className: 'flex shrink-0 flex-col gap-1',
            children: [
              jsx('button', {
                key: 'send', type: 'button', disabled: !canSend, onClick: onSend,
                className: cn('rounded px-3 py-1.5 text-xs text-(--ui-text-secondary)',
                  'hover:bg-(--chrome-action-hover)', !canSend && 'opacity-50'),
                children: pending ? (mode === 'session' ? 'Sending…' : 'Starting…') : 'Send',
              }),
              busy ? jsx('button', {
                key: 'stop', type: 'button', disabled: stopPending, onClick: onStop,
                className: 'rounded px-3 py-1.5 text-xs text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover)',
                children: stopPending ? 'Stopping…' : 'Stop',
              }) : null,
            ],
          }),
        ],
      }),
      jsxs('div', {
        key: 'meta', className: 'flex flex-wrap items-center gap-2 text-[0.6875rem] text-(--ui-text-quaternary)',
        children: meta,
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

function ChatHeader({ session, onStop, stopPending, tile, diff, comment }) {
  const canOpen = !!session.hermes_session_id && !!(host && typeof host.openSession === 'function')
  const canTile = !tile && !!(host && typeof host.openWorkspace === 'function')
  const meta = [
    jsx(Pill, { key: 'status', children: session.status }),
    jsx('span', { key: 'model', className: 'text-(--ui-text-tertiary)', children: sessionModelLabel(session) }),
  ]
  if (session.repo) {
    meta.push(jsx('span', {
      key: 'repo', className: 'min-w-0 max-w-[18rem] truncate font-mono text-(--ui-text-quaternary)',
      children: session.repo,
    }))
  }
  if (session.pr_number) {
    meta.push(jsx(PrBadge, { key: 'pr', session }))
  } else if (session.pr_url) {
    meta.push(jsx('button', {
      key: 'pr', type: 'button', onClick: () => openUrl(session.pr_url),
      className: 'shrink-0 text-(--ui-text-secondary) underline',
      children: 'Pull request',
    }))
  }
  if (session.error) {
    meta.push(jsx('span', {
      key: 'err', className: 'min-w-0 max-w-[22rem] truncate text-(--ui-text-quaternary)',
      children: clip(session.error, 160),
    }))
  }

  return jsxs('div', {
    className: 'flex flex-wrap items-center justify-between gap-2',
    children: [
      jsx('div', { key: 'meta', className: 'flex min-w-0 flex-wrap items-center gap-2 text-[0.6875rem]', children: meta }),
      jsxs('div', {
        key: 'actions', className: 'flex shrink-0 items-center gap-1',
        children: [
          diff ? jsx(GhostButton, {
            key: 'diff',
            title: 'Show what this session changed in its repository',
            'aria-expanded': !!diff.open,
            onClick: diff.onToggle,
            children: diff.open ? 'Diff ▴' : (diff.count ? `Diff (${diff.count})` : 'Diff'),
          }) : null,
          comment ? jsx(GhostButton, {
            key: 'comment',
            title: comment.disabled
              ? 'Wait for this turn to finish, then comment on the pull request'
              : `Write a comment on PR #${session.pr_number}`,
            'aria-expanded': !!comment.open,
            disabled: comment.disabled,
            onClick: comment.onToggle,
            children: comment.open ? 'Comment ▴' : 'Comment on PR',
          }) : null,
          canTile ? jsx(GhostButton, {
            key: 'tile', title: 'Open in a workspace tile',
            onClick: () => openChatTile(session),
            children: '⧉',
          }) : null,
          canOpen ? jsx(GhostButton, {
            key: 'open',
            onClick: () => { host.openSession(session.hermes_session_id).catch(() => {}) },
            children: 'Open in Hermes',
          }) : null,
          isBusy(session) && typeof onStop === 'function' ? jsx(GhostButton, {
            key: 'stop', onClick: onStop, disabled: stopPending,
            children: stopPending ? 'Stopping…' : 'Stop',
          }) : null,
        ],
      }),
    ],
  })
}

function ChatCenter(props) {
  const { summary, detail, onStop, stopPending, canComment, ...composerProps } = props
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
      className: 'flex min-h-0 min-w-0 flex-1 flex-col gap-2',
      children: [
        jsx('div', {
          key: 'empty',
          className: cn('flex min-h-0 flex-1 items-center justify-center rounded border',
            'border-(--ui-stroke-secondary) p-4 text-center text-xs text-(--ui-text-tertiary)'),
          children: 'Start a chat — pick a model and describe the work.',
        }),
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
    className: 'flex min-h-0 min-w-0 flex-1 flex-col gap-2',
    children: [
      jsx(ChatHeader, {
        key: 'head', session, onStop, stopPending,
        diff: diffAction, comment: commentAction,
      }),
      detail && detail.isError
        ? jsx(ErrorCard, {
            key: 'err', title: `Could not load ${session.id}.`, error: detail.error,
            onRetry: () => detail.refetch(),
          })
        : jsx(Transcript, { key: 'transcript', session }),
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

// -- live workers -----------------------------------------------------------

/** Three dim event lines, oldest first — the last thing this worker did. */
function WorkerFeed({ session }) {
  const events = Array.isArray(session.recent_events) ? session.recent_events.filter(Boolean) : []
  const lines = events.length
    ? events.slice(0, 3).reverse().map((event) => {
        const kind = String(event.kind || '').trim()
        const detail = String(event.detail || '').replace(/\s+/g, ' ').trim()
        return clip(detail ? `${kind}: ${detail}` : kind, 120)
      })
    : (() => {
        const fallback = String(session.last_event || '').replace(/\s+/g, ' ').trim()
        return fallback ? [clip(fallback, 120)] : []
      })()
  if (!lines.length) return null
  return jsx('div', {
    className: 'flex min-w-0 flex-col gap-0.5 text-[0.6875rem] leading-snug text-(--ui-text-quaternary)',
    children: lines.map((line, index) => jsx('div', {
      key: `${index}:${line.slice(0, 16)}`,
      className: 'min-w-0 truncate',
      title: line,
      children: line,
    })),
  })
}

function WorkerCard({ session, onSelect }) {
  const busy = isBusy(session)
  // Client-only ticker: it repaints this card every second and touches no
  // query, so polling budgets stay exactly as configured.
  const now = useNow(busy)
  const timing = busy
    ? (session.status === 'queued'
        ? `queued ${fmtSeconds(liveElapsedSeconds(session, now))}`
        : fmtSeconds(liveElapsedSeconds(session, now)))
    : timingLabel(session)
  const tokens = busy ? '' : fmtTokens(session.tokens)
  return jsxs('button', {
    type: 'button',
    onClick: () => onSelect(session.id),
    className: cn('flex w-full flex-col gap-1 rounded border border-(--ui-stroke-secondary)',
      'p-2 text-left text-xs hover:bg-(--chrome-action-hover)'),
    children: [
      jsxs('div', {
        key: 'top', className: 'flex min-w-0 items-center gap-1.5',
        children: [
          jsx(ChatDot, { key: 'dot', status: session.status }),
          jsx('span', { key: 't', className: 'min-w-0 flex-1 truncate text-(--ui-text-secondary)', children: sessionTitle(session) }),
          jsx(PrBadge, { key: 'pr', session }),
          jsx('span', { key: 's', className: 'shrink-0 text-[0.6875rem] text-(--ui-text-quaternary)', children: session.status }),
        ],
      }),
      jsxs('div', {
        key: 'meta', className: 'flex min-w-0 items-baseline justify-between gap-2 text-[0.6875rem] text-(--ui-text-quaternary)',
        children: [
          jsx('span', { key: 'm', className: 'min-w-0 truncate', children: sessionModelLabel(session) }),
          jsxs('span', {
            key: 't', className: 'flex shrink-0 items-baseline gap-1 tabular-nums',
            children: [
              tokens ? jsx('span', { key: 'k', children: tokens }) : null,
              tokens && timing ? jsx('span', { key: 'd', children: '·' }) : null,
              jsx('span', { key: 'v', children: timing }),
            ],
          }),
        ],
      }),
      jsx(WorkerFeed, { key: 'feed', session }),
    ],
  })
}

function LiveWorkers({ sessions, onSelect }) {
  const live = sessions.filter((session) => LIVE_STATUSES.indexOf(session.status) !== -1).slice(0, MAX_WORKERS)
  return jsxs('div', {
    className: 'flex min-h-0 w-full flex-col gap-2 md:w-[20rem] md:shrink-0',
    children: [
      jsx('div', {
        key: 'label', className: 'text-xs font-medium text-(--ui-text-tertiary)',
        children: live.length ? `Live workers (${live.length})` : 'Live workers',
      }),
      jsx('div', {
        key: 'grid', className: 'grid min-h-0 flex-1 grid-cols-1 content-start gap-2 overflow-y-auto pr-1',
        children: live.length
          ? live.map((session) => jsx(WorkerCard, { key: session.id, session, onSelect }))
          : jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Nothing running.' }),
      }),
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
    className: cn('flex shrink-0 flex-col gap-2 overflow-y-auto rounded border',
      'border-(--ui-stroke-secondary) p-3 max-h-[24rem]'),
    children: [
      jsxs('div', {
        key: 'head', className: 'flex flex-wrap items-baseline justify-between gap-2',
        children: [
          jsxs('div', {
            key: 'title', className: 'flex min-w-0 items-baseline gap-2',
            children: [
              jsx('span', { key: 'l', className: 'text-xs font-medium text-(--ui-text-secondary)', children: 'GitHub' }),
              jsx('span', {
                key: 'r', className: 'min-w-0 truncate text-[0.6875rem] text-(--ui-text-quaternary)',
                title: statusRepo, children: statusRepo || 'no repository',
              }),
            ],
          }),
          jsx('span', {
            key: 'status', className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: statusLine,
          }),
        ],
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
        key: 'columns', className: 'flex min-h-0 flex-col gap-3 md:flex-row',
        children: [
          jsxs('div', {
            key: 'connection', className: 'flex min-w-0 flex-col gap-1.5 md:w-[21rem] md:shrink-0',
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
    title: 'Pick a model / preset',
    hint: 'Set a default in Settings, or tap a preset chip before your first chat.',
    action: 'Open settings',
    key: 'settings',
  },
  {
    title: 'Connect a GitHub repo to watch PRs',
    hint: 'An empty allow-list means nobody can trigger work, and the agent ignores its own comments.',
    action: 'Open GitHub',
    key: 'github',
  },
  {
    title: 'Start a chat below',
    hint: 'Describe the work, pick a repo if you want one, then press Enter.',
    action: '',
    key: 'chat',
  },
]

/** Compact first-run card: three numbered steps, dismissible for good. */
function OnboardingCard({ onSettings, onGithub, onStart, onDismiss }) {
  const actions = { settings: onSettings, github: onGithub, chat: onStart }
  return jsxs('section', {
    'aria-label': 'Getting started',
    className: cn('flex shrink-0 flex-col gap-1.5 rounded border border-(--ui-stroke-secondary)',
      'bg-transparent p-2.5 text-xs'),
    children: [
      jsxs('div', {
        key: 'head', className: 'flex items-baseline justify-between gap-2',
        children: [
          jsx('span', { key: 't', className: 'font-medium text-(--ui-text-secondary)', children: 'Get started' }),
          jsx(GhostButton, {
            key: 'dismiss', className: 'shrink-0 px-1 py-0', title: 'Dismiss getting started',
            'aria-label': 'Dismiss getting started', onClick: onDismiss, children: '×',
          }),
        ],
      }),
      ...ONBOARDING_STEPS.map((step, index) => jsxs('div', {
        key: step.key, className: 'flex min-w-0 items-start gap-2',
        children: [
          jsx('span', {
            key: 'n',
            className: cn('mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full',
              'border border-(--ui-stroke-secondary) text-[0.625rem] tabular-nums text-(--ui-text-tertiary)'),
            children: String(index + 1),
          }),
          jsxs('div', {
            key: 'body', className: 'flex min-w-0 flex-1 flex-col gap-0.5',
            children: [
              jsx('span', { key: 't', className: 'text-(--ui-text-secondary)', children: step.title }),
              jsx('span', {
                key: 'h', className: 'text-[0.6875rem] leading-snug text-(--ui-text-quaternary)',
                children: step.hint,
              }),
            ],
          }),
          step.action
            ? jsx(GhostButton, {
                key: 'a', className: 'shrink-0 self-center', onClick: actions[step.key],
                children: step.action,
              })
            : null,
        ],
      })),
    ],
  })
}

// -- page -------------------------------------------------------------------

function SuperagentPage() {
  const queryClient = useQueryClient()
  const chatsQuery = useChats()
  const modelsQuery = useModels()
  const [selectedId, setSelectedId] = useState(null)
  const [draft, setDraft] = useState('')
  const [model, setModel] = useState(() => ({
    model: readStored('lastModel', ''), provider: readStored('lastProvider', ''),
  }))
  const [repo, setRepo] = useState(() => readStored('lastRepo', ''))
  const [githubOpen, setGithubOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [presetName, setPresetName] = useState('')
  const [onboarded, setOnboarded] = useState(() => readStored('onboarded', '') === '1')
  const detail = useChat(selectedId)
  const settingsQuery = useSettings(settingsOpen)
  const inputRef = useRef(null)

  const data = chatsQuery.data
  const sessions = useMemo(() => {
    return ((data && data.sessions) || []).slice().sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))
  }, [data])
  const counts = (data && data.counts) || {}
  const running = (data && data.running) || 0
  const queued = counts.queued || 0
  const waiting = (counts.review || 0) + (counts.failed || 0)
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

  // A chat's model is fixed at creation; this swaps it for future turns.
  const setChatModel = useMutation({
    mutationFn: (vars) => rest(`/chats/${encodeURIComponent(vars.id)}/model`, {
      method: 'POST', body: { model: vars.model, provider: vars.provider },
    }),
    onSuccess: () => invalidateChats(queryClient, selectedId),
  })

  const pending = createChat.isPending || sendChat.isPending
  const stopPending = stopChat.isPending
  const busyTurn = isBusy(selectedSession)
  const notice = (createChat.data && createChat.data.ok === false && createChat.data.error) ||
    (sendChat.data && sendChat.data.ok === false && sendChat.data.error) || ''
  const failure = errorText(createChat.error) || errorText(sendChat.error) ||
    errorText(stopChat.error) || errorText(setChatModel.error) || notice

  const changeSessionModel = (next) => {
    if (!selectedSession || !next) return
    const pickedModel = String(next.model || '').trim()
    const pickedProvider = String(next.provider || '').trim()
    if (!pickedModel && !pickedProvider) return
    setChatModel.mutate({ id: selectedSession.id, model: pickedModel, provider: pickedProvider })
  }

  const submit = () => {
    const text = String(draft || '').trim()
    if (!text || pending || busyTurn) return
    if (selectedId) {
      sendChat.mutate({ id: selectedId, content: text }, { onSuccess: () => setDraft('') })
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
      { onSuccess: () => setDraft('') },
    )
  }

  const select = (id) => {
    setSelectedId(id)
    setDraft('')
  }

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
    setSelectedId(null)
    setDraft('')
    setPresetName('')
    setModel({ model: readStored('lastModel', ''), provider: readStored('lastProvider', '') })
    setRepo(readStored('lastRepo', ''))
    startChat()
  }

  const presetList = (modelsQuery.data && modelsQuery.data.presets) || []

  const refresh = () => {
    chatsQuery.refetch()
    modelsQuery.refetch()
    if (selectedId) detail.refetch()
    if (githubOpen) {
      githubQuery.refetch()
      githubPrsQuery.refetch()
    }
  }

  const stop = () => {
    if (!selectedSession || stopPending) return
    stopChat.mutate(selectedSession.id)
  }

  const countLabel = queued
    ? `${running} running · ${waiting} waiting · ${queued} queued`
    : `${running} running · ${waiting} waiting`

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-3 overflow-hidden p-4 text-sm',
    children: [
      jsxs('div', {
        key: 'header', className: 'flex flex-wrap items-center justify-between gap-2',
        children: [
          jsxs('div', {
            key: 'title', className: 'flex flex-wrap items-baseline gap-2',
            children: [
              jsx('span', { key: 'name', className: 'text-sm font-medium', children: 'Superagent' }),
              jsx('span', { key: 'counts', className: 'text-xs text-(--ui-text-tertiary)', children: countLabel }),
            ],
          }),
          jsxs('div', {
            key: 'actions', className: 'flex items-center gap-1',
            children: [
              jsx(GhostButton, {
                key: 'settings',
                title: 'Settings: default model, presets and worker limits',
                'aria-label': 'Settings', 'aria-expanded': !!settingsOpen,
                onClick: () => setSettingsOpen((value) => !value),
                children: '⚙',
              }),
              jsx(GithubButton, {
                key: 'github', open: githubOpen, status: githubQuery.data,
                onClick: () => setGithubOpen((value) => !value),
              }),
              jsx(GhostButton, { key: 'new', onClick: newChat, children: 'New chat' }),
              jsx(GhostButton, { key: 'refresh', onClick: refresh, children: 'Refresh' }),
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
      githubOpen
        ? jsx(GithubPanel, {
            key: 'github', status: githubQuery.data,
            prs: githubPrsQuery.data, prsLoading: githubPrsQuery.isLoading,
            onSelectSession: select,
          })
        : null,
      settingsOpen
        ? jsx(SettingsPanel, {
            key: 'settings-panel', settings: settingsQuery.data, models: modelsQuery.data,
            loading: settingsQuery.isLoading, error: settingsQuery.error,
            onRetry: () => settingsQuery.refetch(),
            onClose: () => setSettingsOpen(false),
          })
        : null,
      !chatsQuery.isLoading && !onboarded && sessions.length === 0
        ? jsx(OnboardingCard, {
            key: 'onboarding',
            onSettings: () => setSettingsOpen(true),
            onGithub: () => setGithubOpen(true),
            onStart: startChat,
            onDismiss: dismissOnboarding,
          })
        : null,
      jsxs('div', {
        key: 'body', className: 'flex min-h-0 flex-1 flex-col gap-3 md:flex-row',
        children: [
          jsx(SessionsRail, { key: 'rail', sessions, selectedId, onSelect: select }),
          jsx(ChatCenter, {
            key: 'center', summary: selectedSummary, detail,
            draft, onDraft: setDraft, onSend: submit, onStop: stop,
            pending, stopPending, error: failure,
            // The PR comment needs a saved token; the status query already
            // polls while a PR badge is on the board, so this is free.
            canComment: !!(githubQuery.data && githubQuery.data.token_set),
            model, onModelChange: changeModel, repo, onRepoChange: setRepo,
            models: modelsQuery.data, inputRef,
            onSessionModel: changeSessionModel, modelPending: setChatModel.isPending,
            presets: presetList, presetName, onPreset: pickPreset,
          }),
          jsx(LiveWorkers, { key: 'workers', sessions, onSelect: select }),
        ],
      }),
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
  const attention = (tickets.data && tickets.data.attention && tickets.data.attention.length) || 0
  const running = (chats.data && chats.data.running) || 0
  const count = running || attention
  const label = running ? `▶ ${running}` : attention > 0 ? `◆ ${attention}` : 'superagent'
  const tip = running
    ? `${running} Superagent chat${running === 1 ? '' : 's'} running`
    : attention > 0
      ? `${attention} ticket(s) need a human`
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
