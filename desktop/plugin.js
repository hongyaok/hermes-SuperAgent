/**
 * Superagent desktop panel plus full page — shows only what needs a human.
 *
 * Data path: ctx.rest('/tickets'), ctx.rest('/sessions'), POST /ask, POST /run
 * → dashboard/plugin_api.py. Polls every 15s through the app's React Query
 * client (shared cache, so the pane, the page, and the status chip share
 * requests). Mutations invalidate the ticket/session queries on success.
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax. Only
 * these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import { Tip, cn, useQuery, useMutation, useQueryClient, ROUTES_AREA, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

const ID = 'superagent'
const POLL_MS = 15000

// Set in register(); the data hooks below need it.
let rest = null

const ICONS = {
  observed: '·',
  queued: '…',
  running: '▶',
  review: '◆',
  blocked: '!',
  failed: '×',
  done: '✓',
  ignored: '–',
}

const RUNNABLE = ['observed', 'blocked', 'failed', 'queued']

function fetchTickets() {
  if (!rest) return Promise.reject(new Error('backend not ready'))
  return rest('/tickets')
}

function fetchSessions() {
  if (!rest) return Promise.reject(new Error('backend not ready'))
  return rest('/sessions')
}

function useTickets() {
  return useQuery({
    queryKey: [ID, 'tickets'],
    queryFn: fetchTickets,
    refetchInterval: POLL_MS,
    retry: false,
  })
}

function useSessions() {
  return useQuery({
    queryKey: [ID, 'sessions'],
    queryFn: fetchSessions,
    refetchInterval: POLL_MS,
    retry: false,
  })
}

function invalidateSuperagent(client) {
  client.invalidateQueries({ queryKey: [ID, 'tickets'] })
  client.invalidateQueries({ queryKey: [ID, 'sessions'] })
}

function TicketRow({ ticket }) {
  const icon = ICONS[ticket.status] || '?'
  return jsx('div', {
    className: 'flex flex-col gap-0.5 rounded px-2 py-1.5 text-xs',
    children: [
      jsx('div', {
        key: 'head',
        className: 'flex items-baseline gap-1.5',
        children: [
          jsx('span', {
            key: 'icon',
            className: 'text-(--ui-text-quaternary)',
            children: icon,
          }),
          jsx('span', {
            key: 'id',
            className: 'font-mono text-(--ui-text-tertiary)',
            children: ticket.id,
          }),
          jsx('span', {
            key: 'status',
            className: 'text-(--ui-text-tertiary)',
            children: ticket.status,
          }),
          jsx('span', {
            key: 'req',
            className: 'truncate text-(--ui-text-secondary)',
            children: ticket.request,
          }),
        ],
      }),
      ticket.reason
        ? jsx('div', {
            key: 'reason',
            className: 'truncate pl-4 text-(--ui-text-quaternary)',
            children: ticket.reason,
          })
        : null,
    ],
  })
}

function SuperagentPane() {
  const { data, isLoading, isError, error, refetch } = useTickets()

  if (isLoading && !data) {
    return jsx('div', {
      className: 'p-3 text-xs text-(--ui-text-tertiary)',
      children: 'Loading tickets…',
    })
  }

  if (isError) {
    return jsxs('div', {
      className: 'flex flex-col gap-2 p-3 text-xs',
      children: [
        jsx('div', {
          key: 'msg',
          className: 'text-(--ui-text-tertiary)',
          children: 'Superagent backend unavailable. Enable the plugin first.',
        }),
        jsx('div', {
          key: 'err',
          className: 'truncate text-(--ui-text-quaternary)',
          children: String((error && error.message) || error),
        }),
        jsx('button', {
          key: 'retry',
          type: 'button',
          className: cn(
            'self-start rounded px-2 py-1',
            'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)',
          ),
          onClick: () => refetch(),
          children: 'Retry',
        }),
      ],
    })
  }

  if (!data || data.db_exists === false) {
    return jsx('div', {
      className: 'p-3 text-xs text-(--ui-text-tertiary)',
      children: 'No tickets yet. Ask Superagent for work from any chat.',
    })
  }

  const attention = data.attention || []
  const tickets = data.tickets || []
  const running = data.running || 0
  const queued = data.queued || 0

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 overflow-y-auto p-3 text-sm',
    children: [
      jsx('div', {
        key: 'head',
        className: 'flex items-center justify-between text-xs text-(--ui-text-tertiary)',
        children: [
          jsx('span', {
            key: 'counts',
            children: `running ${running} · queued ${queued}`,
          }),
          jsx('button', {
            key: 'refresh',
            type: 'button',
            className: 'rounded px-1.5 py-0.5 hover:bg-(--chrome-action-hover) hover:text-foreground',
            onClick: () => refetch(),
            children: 'Refresh',
          }),
        ],
      }),
      attention.length
        ? jsxs('div', {
            key: 'needs',
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', {
                key: 'label',
                className: 'text-xs font-medium',
                children: 'Needs you',
              }),
              ...attention.map((t) => jsx(TicketRow, { key: t.id, ticket: t })),
            ],
          })
        : jsx('div', {
            key: 'clear',
            className: 'text-xs text-(--ui-text-tertiary)',
            children: 'Nothing needs you.',
          }),
      tickets.length
        ? jsxs('div', {
            key: 'recent',
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', {
                key: 'label',
                className: 'text-xs font-medium text-(--ui-text-tertiary)',
                children: 'Recent',
              }),
              ...tickets
                .slice(0, 12)
                .map((t) => jsx(TicketRow, { key: t.id, ticket: t })),
            ],
          })
        : null,
    ],
  })
}

function SessionCard({ session, onRun, runPending }) {
  const icon = ICONS[session.status] || '?'
  const canRun = RUNNABLE.indexOf(session.status) !== -1
  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded border border-(--ui-stroke-secondary) p-2 text-xs',
    children: [
      jsxs('div', {
        key: 'head',
        className: 'flex items-center gap-1.5',
        children: [
          jsx('span', {
            key: 'icon',
            className: 'text-(--ui-text-quaternary)',
            children: icon,
          }),
          jsx('span', {
            key: 'id',
            className: 'font-mono text-(--ui-text-tertiary)',
            children: session.id,
          }),
          jsx('span', {
            key: 'status',
            className: 'text-(--ui-text-secondary)',
            children: session.status,
          }),
          session.worker_id
            ? jsx('span', {
                key: 'worker',
                className: 'truncate text-(--ui-text-quaternary)',
                children: session.worker_id,
              })
            : null,
        ],
      }),
      jsx('div', {
        key: 'req',
        className: 'truncate text-(--ui-text-secondary)',
        children: session.request,
      }),
      jsxs('div', {
        key: 'foot',
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('span', {
            key: 'meta',
            className: 'truncate text-(--ui-text-quaternary)',
            children: session.last_event
              ? `${session.last_event.kind}: ${(session.last_event.detail || '').slice(0, 80)}`
              : (session.reason || session.result || ''),
          }),
          canRun
            ? jsx('button', {
                key: 'run',
                type: 'button',
                disabled: runPending,
                className: cn(
                  'shrink-0 rounded px-2 py-1',
                  'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)',
                ),
                onClick: onRun,
                children: runPending ? 'Queuing…' : 'Run',
              })
            : jsx('span', {
                key: 'state',
                className: 'shrink-0 text-(--ui-text-quaternary)',
                children: session.status,
              }),
        ],
      }),
    ],
  })
}

function SuperagentPage() {
  const ticketsQuery = useTickets()
  const sessionsQuery = useSessions()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState('')

  const askMutation = useMutation({
    mutationFn: (text) => rest('/ask', { method: 'POST', body: { request: text, requester: 'desktop' } }),
    onSuccess: () => invalidateSuperagent(queryClient),
  })
  const runMutation = useMutation({
    mutationFn: (id) => rest('/run', { method: 'POST', body: { ticket_id: id } }),
    onSuccess: () => invalidateSuperagent(queryClient),
  })

  const ticketsData = ticketsQuery.data
  const sessionsData = sessionsQuery.data
  const tickets = (ticketsData && ticketsData.tickets) || []
  const sessions = (sessionsData && sessionsData.sessions) || []
  const running = (ticketsData && ticketsData.running) || 0
  const queued = (ticketsData && ticketsData.queued) || 0
  const askPending = askMutation.isPending || askMutation.isLoading
  const runPendingId = runMutation.isPending || runMutation.isLoading ? runMutation.variables : null

  const refreshAll = () => {
    ticketsQuery.refetch()
    sessionsQuery.refetch()
  }

  const submitDraft = () => {
    const text = (draft || '').trim()
    if (!text || askPending) return
    askMutation.mutate(text, { onSuccess: () => setDraft('') })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-y-auto p-4 text-sm',
    children: [
      jsxs('div', {
        key: 'header',
        className: 'flex items-center justify-between gap-2',
        children: [
          jsxs('div', {
            key: 'title',
            className: 'flex items-baseline gap-2',
            children: [
              jsx('span', {
                key: 'name',
                className: 'text-sm font-medium',
                children: 'Superagent',
              }),
              jsx('span', {
                key: 'counts',
                className: 'text-xs text-(--ui-text-tertiary)',
                children: `running ${running} · queued ${queued} · sessions ${sessions.length}`,
              }),
            ],
          }),
          jsx('button', {
            key: 'refresh',
            type: 'button',
            className: cn(
              'rounded px-2 py-1 text-xs',
              'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)',
            ),
            onClick: refreshAll,
            children: 'Refresh',
          }),
        ],
      }),
      askMutation.isError
        ? jsx('div', {
            key: 'ask-err',
            className: 'truncate text-xs text-(--ui-text-quaternary)',
            children: `Ask failed: ${String((askMutation.error && askMutation.error.message) || askMutation.error)}`,
          })
        : null,
      runMutation.isError
        ? jsx('div', {
            key: 'run-err',
            className: 'truncate text-xs text-(--ui-text-quaternary)',
            children: `Run failed: ${String((runMutation.error && runMutation.error.message) || runMutation.error)}`,
          })
        : null,
      jsxs('div', {
        key: 'cols',
        className: 'flex min-h-0 flex-1 flex-col gap-3 md:flex-row',
        children: [
          jsxs('div', {
            key: 'chat',
            className: 'flex min-w-0 flex-1 flex-col gap-2',
            children: [
              jsx('div', {
                key: 'label',
                className: 'text-xs font-medium text-(--ui-text-tertiary)',
                children: 'Chat — ask for work',
              }),
              jsxs('div', {
                key: 'history',
                className: 'flex min-h-24 flex-col gap-1 overflow-y-auto rounded border border-(--ui-stroke-secondary) p-2',
                children: tickets.length
                  ? tickets.slice(0, 20).map((t) =>
                      jsx(TicketRow, { key: t.id, ticket: t }),
                    )
                  : [
                      jsx('div', {
                        key: 'empty',
                        className: 'p-2 text-xs text-(--ui-text-tertiary)',
                        children: 'No tickets yet — type work below to queue it.',
                      }),
                    ],
              }),
              jsxs('form', {
                key: 'form',
                className: 'flex gap-2',
                onSubmit: (e) => {
                  e.preventDefault()
                  submitDraft()
                },
                children: [
                  jsx('input', {
                    key: 'input',
                    type: 'text',
                    value: draft,
                    placeholder: 'Ask Superagent for work…',
                    onChange: (e) => setDraft(e.target.value),
                    className: 'min-w-0 flex-1 rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1.5 text-sm text-(--ui-text-secondary)',
                  }),
                  jsx('button', {
                    key: 'send',
                    type: 'submit',
                    disabled: askPending || !(draft || '').trim(),
                    className: cn(
                      'shrink-0 rounded px-3 py-1.5 text-xs',
                      'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)',
                    ),
                    onClick: submitDraft,
                    children: askPending ? 'Sending…' : 'Send',
                  }),
                ],
              }),
            ],
          }),
          jsxs('div', {
            key: 'sessions',
            className: 'flex min-w-0 flex-1 flex-col gap-2',
            children: [
              jsx('div', {
                key: 'label',
                className: 'text-xs font-medium text-(--ui-text-tertiary)',
                children: `Sessions${sessions.length ? ` (${sessions.length})` : ''}`,
              }),
              sessionsQuery.isLoading && !sessionsData
                ? jsx('div', {
                    key: 'loading',
                    className: 'p-2 text-xs text-(--ui-text-tertiary)',
                    children: 'Loading sessions…',
                  })
                : null,
              sessionsQuery.isError
                ? jsx('div', {
                    key: 'err',
                    className: 'p-2 text-xs text-(--ui-text-tertiary)',
                    children: 'Sessions unavailable — is the plugin backend enabled?',
                  })
                : null,
              sessionsData && sessionsData.db_exists === false
                ? jsx('div', {
                    key: 'empty',
                    className: 'p-2 text-xs text-(--ui-text-tertiary)',
                    children: 'No sessions yet.',
                  })
                : null,
              jsxs('div', {
                key: 'grid',
                className: 'grid grid-cols-1 gap-2 overflow-y-auto',
                children: sessions.map((s) =>
                  jsx(SessionCard, {
                    key: s.id,
                    session: s,
                    runPending: runPendingId === s.id,
                    onRun: () => {
                      if (runPendingId) return
                      runMutation.mutate(s.id)
                    },
                  }),
                ),
              }),
            ],
          }),
        ],
      }),
    ],
  })
}

function SuperagentChip() {
  const { data } = useTickets()
  const count = (data && data.attention && data.attention.length) || 0
  const label = count > 0 ? `◆ ${count}` : 'superagent'
  return jsx(Tip, {
    label: count > 0 ? `${count} ticket(s) need a human` : 'Superagent: nothing needs you',
    children: jsx('span', {
      className: cn(
        'inline-flex h-full items-center px-1.5 text-[0.6875rem]',
        count > 0
          ? 'text-foreground'
          : 'text-(--ui-text-tertiary)',
      ),
      children: label,
    }),
  })
}

export default {
  id: ID, // must match the folder name
  name: 'Superagent',
  register(ctx) {
    rest = ctx.rest
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
