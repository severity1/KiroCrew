import { useMemo, useState } from 'react'
import { ArrowRightLeft, ChevronRight, GitBranch } from 'lucide-react'

import { i18nT } from '../i18n/t'
import { switchSlot } from '../store/chatSlice'
import { useAppDispatch, useAppSelector } from '../store'
import type { ChatSlot } from '../types'

/**
 * Session Lineage tree — who was handed off / forked from whom.
 *
 * A session records its parent in `forked_from`. The backend stores the bare,
 * normalized parent slot key, but we defensively strip a leading `dashboard:`
 * prefix anyway (a no-op on current data, protecting against any legacy or
 * externally-written value that carried the prefix) — the same normalization
 * ChatPane's "↳ fork of <parent>" tag does. `handoff === true` marks the edge
 * as a HANDOFF (a `kind='handover'` tangent, drawn with ArrowRightLeft to match
 * FollowUpCard's handover chrome); otherwise it is a plain fork (GitBranch).
 *
 * Only sessions that PARTICIPATE in lineage are rendered — a session with a
 * resolvable parent, or one that is somebody's parent. A flat list of unrelated
 * sessions would be noise, so when nothing is related this renders nothing, the
 * same way SessionBreakdownTree renders nothing without a spawn tree.
 *
 * The forest is built defensively: a `forked_from` pointing at a key not in
 * `slots` (a dangling parent) is treated as no parent (the child becomes a
 * root), and a `forked_from` chain that loops (a cycle) is broken so traversal
 * cannot infinite-loop.
 */

const PARENT_PREFIX = /^dashboard:/

interface LineageNode {
  slot: ChatSlot
  children: LineageNode[]
  /** How this node is attached to its parent. Roots carry `null`. */
  edge: 'fork' | 'handoff' | null
}

/** Bare parent slot key for a slot, or null when it declares no parent. */
function parentKeyOf(slot: ChatSlot): string | null {
  if (!slot.forked_from) return null
  return slot.forked_from.replace(PARENT_PREFIX, '')
}

/**
 * Build the parent→child forest.
 *
 * Returns the root nodes plus the set of slot keys that participate in any
 * lineage relationship (so the caller can decide whether to render at all).
 * Dangling parents collapse to roots; cycles are broken at the first repeated
 * key on a walk from each node to its root.
 */
export function buildLineageForest(slots: ChatSlot[]): {
  roots: LineageNode[]
  participants: Set<string>
} {
  const byKey = new Map<string, ChatSlot>()
  for (const s of slots) byKey.set(s.key, s)

  // Resolve each slot's EFFECTIVE parent: only a parent that actually exists in
  // `slots` counts. A dangling pointer resolves to null (→ root).
  const parentOf = new Map<string, string | null>()
  for (const s of slots) {
    const pk = parentKeyOf(s)
    parentOf.set(s.key, pk && byKey.has(pk) ? pk : null)
  }

  // Break cycles: a slot whose forked_from chain loops back onto itself must
  // not keep an edge, or the forest would contain a cycle and traversal would
  // never terminate. Walk each slot's ancestry; if we revisit a key before
  // reaching a root, sever this slot's own parent link (treat it as a root).
  for (const s of slots) {
    const seen = new Set<string>([s.key])
    let cur = parentOf.get(s.key) ?? null
    while (cur) {
      if (seen.has(cur)) {
        parentOf.set(s.key, null)
        break
      }
      seen.add(cur)
      cur = parentOf.get(cur) ?? null
    }
  }

  const nodes = new Map<string, LineageNode>()
  for (const s of slots) {
    const edge: LineageNode['edge'] =
      parentOf.get(s.key) == null ? null : s.handoff === true ? 'handoff' : 'fork'
    nodes.set(s.key, { slot: s, children: [], edge })
  }

  const participants = new Set<string>()
  const roots: LineageNode[] = []
  for (const s of slots) {
    const pk = parentOf.get(s.key) ?? null
    const node = nodes.get(s.key)!
    if (pk) {
      nodes.get(pk)!.children.push(node)
      participants.add(s.key)
      participants.add(pk)
    } else {
      roots.push(node)
    }
  }

  // A root with no children took part in no relationship — drop it from the
  // rendered forest so unrelated sessions are not listed.
  const liveRoots = roots.filter(r => r.children.length > 0)
  return { roots: liveRoots, participants }
}

function EdgeIcon({ edge }: { edge: LineageNode['edge'] }) {
  if (edge === 'handoff') {
    return (
      <ArrowRightLeft
        className="lucide-inline text-accent shrink-0"
        size={12}
        aria-label={i18nT('components.sessionLineage.handoff_edge')}
      />
    )
  }
  return (
    <GitBranch
      className="lucide-inline text-muted shrink-0"
      size={12}
      aria-label={i18nT('components.sessionLineage.fork_edge')}
    />
  )
}

function LineageRow({ node, depth }: { node: LineageNode; depth: number }) {
  const dispatch = useAppDispatch()
  const [open, setOpen] = useState(true)
  const hasChildren = node.children.length > 0
  const title = node.slot.title || node.slot.key
  // Root nodes carry no edge icon (they are nobody's child within this forest);
  // child nodes show the fork/handoff glyph.
  const isRoot = node.edge === null

  return (
    <div className="relative">
      <div
        className="flex items-center gap-1.5 pr-3 py-1.5 cursor-pointer hover:bg-bg-hover rounded-md"
        style={{ paddingLeft: 8 + depth * 16 }}
        role="button"
        tabIndex={0}
        onClick={() => dispatch(switchSlot(node.slot.key))}
        onKeyDown={(e: React.KeyboardEvent) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            dispatch(switchSlot(node.slot.key))
          }
        }}
      >
        <button
          type="button"
          className={`shrink-0 transition-transform bg-transparent border-none p-0 cursor-pointer ${
            open ? 'rotate-90 text-accent' : 'text-muted'
          } ${hasChildren ? '' : 'invisible'}`}
          aria-expanded={hasChildren ? open : undefined}
          aria-label={i18nT('components.sessionLineage.toggle_children')}
          onClick={(e) => {
            e.stopPropagation()
            if (hasChildren) setOpen(v => !v)
          }}
          tabIndex={hasChildren ? 0 : -1}
        >
          <ChevronRight className="lucide-inline" size={12} />
        </button>
        {isRoot ? <span className="inline-block w-3 shrink-0" /> : <EdgeIcon edge={node.edge} />}
        <span className="text-[12.5px] text-text truncate min-w-0">{title}</span>
      </div>
      {open && hasChildren ? (
        <div>
          {node.children.map(child => (
            <LineageRow key={child.slot.key} node={child} depth={depth + 1} />
          ))}
        </div>
      ) : null}
    </div>
  )
}

/**
 * Collapsible Session Lineage section. Reads the slot list from the store,
 * builds the forest, and renders nothing when no session participates in a
 * lineage relationship.
 */
export default function SessionLineage() {
  const [open, setOpen] = useState(true)
  const slots = useAppSelector(s => s.dashboard.slots)
  const { roots } = useMemo(() => buildLineageForest(slots), [slots])

  if (roots.length === 0) return null

  return (
    <div className="border-t border-border pt-2 mt-1">
      <button
        type="button"
        className="w-full flex items-center gap-1.5 px-2 py-1 bg-transparent border-none cursor-pointer text-muted hover:text-text"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
      >
        <ChevronRight
          className={`lucide-inline transition-transform ${open ? 'rotate-90' : ''}`}
          size={12}
          aria-hidden="true"
        />
        <span className="text-[11px] font-semibold uppercase tracking-wide">
          {i18nT('components.sessionLineage.title')}
        </span>
      </button>
      {open ? (
        <div className="py-0.5" role="tree" aria-label={i18nT('components.sessionLineage.title')}>
          {roots.map(root => (
            <LineageRow key={root.slot.key} node={root} depth={0} />
          ))}
        </div>
      ) : null}
    </div>
  )
}
