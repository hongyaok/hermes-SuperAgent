/**
 * Superagent desktop panel — shows only what needs a human.
 *
 * Data path: ctx.rest('/tickets') → dashboard/plugin_api.py (read-only).
 * Polls every 15s through the app's React Query client (shared cache, so the
 * pane and the status chip make one request, not two).
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax. Only
 * these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import { Tip, cn, useQuery } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

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

function fetchTickets() {
  if (!rest) return Promise.reject(new Error('backend not ready'))
  return rest('/tickets')
}

function useTickets() {
  return useQuery({
    queryKey: [ID, 'tickets'],
    queryFn: fetchTickets,
    refetchInterval: POLL_MS,
    retry: false,
  })
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
  },
}
