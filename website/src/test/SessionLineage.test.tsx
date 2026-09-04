/**
 * SessionLineage — the parent→child (fork/handoff) tree over the live sessions.
 *
 * `switchSlot` is the real chat-slice async thunk (it fetches over the network),
 * so it is mocked to a plain synchronous action-creator: the tree only cares
 * THAT it dispatched with the right slot key, not what the thunk does. Every
 * other chatSlice export is preserved. i18nT resolves to the English catalog
 * string in tests, so assertions read against English text / aria-labels.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'

import { renderWithProviders, createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

const { switchSlotMock } = vi.hoisted(() => ({
  switchSlotMock: vi.fn((key: string) => ({ type: 'chat/switchSlot/mocked', payload: key })),
}))

vi.mock('../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return { ...actual, switchSlot: (...args: unknown[]) => switchSlotMock(...args) }
})

const slot = (over: Partial<ChatSlot> = {}): ChatSlot =>
  ({ key: 'k', messages: 0, running: false, ...over } as ChatSlot)

/** Seed the store's slot list and render SessionLineage against it. */
async function renderTree(slots: ChatSlot[]) {
  const store = createTestStore()
  store.dispatch(sseSlots(slots as never))
  // Import AFTER the mock is registered so the component picks up the stub.
  const { default: SessionLineage } = await import('../components/SessionLineage')
  return renderWithProviders(<SessionLineage />, { store })
}

beforeEach(() => {
  switchSlotMock.mockClear()
})

describe('SessionLineage', () => {
  it('renders a parent with a fork child and a handoff child, with distinct icons and labels', async () => {
    await renderTree([
      slot({ key: 'parent', title: 'Parent session' }),
      slot({ key: 'fork', title: 'Fork child', forked_from: 'dashboard:parent' }),
      slot({ key: 'handoff', title: 'Handoff child', forked_from: 'dashboard:parent', handoff: true }),
    ])
    // All three titles present.
    expect(screen.getByText('Parent session')).toBeInTheDocument()
    expect(screen.getByText('Fork child')).toBeInTheDocument()
    expect(screen.getByText('Handoff child')).toBeInTheDocument()
    // Distinct edge glyphs: exactly one fork icon and one handoff icon.
    expect(screen.getByLabelText(/forked from parent session/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/handed off from parent session/i)).toBeInTheDocument()
  })

  it('dispatches switchSlot with the clicked node key', async () => {
    await renderTree([
      slot({ key: 'parent', title: 'Parent session' }),
      slot({ key: 'fork', title: 'Fork child', forked_from: 'dashboard:parent' }),
    ])
    fireEvent.click(screen.getByText('Fork child'))
    expect(switchSlotMock).toHaveBeenCalledWith('fork')
    // Clicking the parent opens it too.
    fireEvent.click(screen.getByText('Parent session'))
    expect(switchSlotMock).toHaveBeenCalledWith('parent')
  })

  it('treats a dangling parent (forked_from -> missing slot) as a root without crashing', async () => {
    // `ghost` is not in the slot list, so `orphan` has no resolvable parent and
    // is a root; it becomes visible only because it has a child of its own.
    await renderTree([
      slot({ key: 'orphan', title: 'Orphan session', forked_from: 'dashboard:ghost' }),
      slot({ key: 'child', title: 'Real child', forked_from: 'dashboard:orphan' }),
    ])
    expect(screen.getByText('Orphan session')).toBeInTheDocument()
    expect(screen.getByText('Real child')).toBeInTheDocument()
    // The dangling-parent root shows no edge glyph (it is nobody's child here);
    // its real child carries exactly one fork glyph, and no handoff glyph exists.
    expect(screen.getAllByLabelText(/forked from parent session/i)).toHaveLength(1)
    expect(screen.queryByLabelText(/handed off from parent session/i)).not.toBeInTheDocument()
  })

  it('does not infinite-loop on a cycle in the forked_from chain', async () => {
    // a -> b -> a forms a cycle. buildLineageForest must break it; rendering
    // simply completing is the assertion (a real infinite loop hangs the test).
    await renderTree([
      slot({ key: 'a', title: 'Node A', forked_from: 'dashboard:b' }),
      slot({ key: 'b', title: 'Node B', forked_from: 'dashboard:a' }),
    ])
    // Both nodes still render (each is the other's child once the cycle is cut
    // at one edge; the severed one becomes a root).
    expect(screen.getByText('Node A')).toBeInTheDocument()
    expect(screen.getByText('Node B')).toBeInTheDocument()
  })

  it('renders nothing when no session participates in any lineage', async () => {
    const { container } = await renderTree([
      slot({ key: 'solo1', title: 'Solo one' }),
      slot({ key: 'solo2', title: 'Solo two' }),
    ])
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByText(/session lineage/i)).not.toBeInTheDocument()
  })

  it('collapses child rows when the parent toggle is clicked (does not switch slot)', async () => {
    await renderTree([
      slot({ key: 'parent', title: 'Parent session' }),
      slot({ key: 'fork', title: 'Fork child', forked_from: 'dashboard:parent' }),
    ])
    const toggle = screen.getAllByLabelText(/toggle child sessions/i)[0]
    fireEvent.click(toggle)
    // The toggle stops propagation, so no navigation fires and the child hides.
    expect(switchSlotMock).not.toHaveBeenCalled()
    expect(screen.queryByText('Fork child')).not.toBeInTheDocument()
    expect(screen.getByText('Parent session')).toBeInTheDocument()
  })
})

describe('buildLineageForest', () => {
  it('excludes unrelated sessions and only returns roots with children', async () => {
    const { buildLineageForest } = await import('../components/SessionLineage')
    const { roots, participants } = buildLineageForest([
      slot({ key: 'p' }),
      slot({ key: 'c', forked_from: 'dashboard:p' }),
      slot({ key: 'lonely' }),
    ])
    expect(roots.map(r => r.slot.key)).toEqual(['p'])
    expect(roots[0].children.map(c => c.slot.key)).toEqual(['c'])
    expect(participants.has('lonely')).toBe(false)
  })

  it('marks handoff vs fork edges on children', async () => {
    const { buildLineageForest } = await import('../components/SessionLineage')
    const { roots } = buildLineageForest([
      slot({ key: 'p' }),
      slot({ key: 'f', forked_from: 'dashboard:p' }),
      slot({ key: 'h', forked_from: 'dashboard:p', handoff: true }),
    ])
    const edges = Object.fromEntries(roots[0].children.map(c => [c.slot.key, c.edge]))
    expect(edges).toEqual({ f: 'fork', h: 'handoff' })
  })

  it('breaks a 3-cycle without dropping any node or looping', async () => {
    // a -> b -> c -> a. The 2-cycle test exercises a different severing path;
    // a longer cycle walks more ancestors before the repeat is seen. Every node
    // must survive (one link severed to a root), and the call must terminate.
    const { buildLineageForest } = await import('../components/SessionLineage')
    const { roots, participants } = buildLineageForest([
      slot({ key: 'a', forked_from: 'dashboard:c' }),
      slot({ key: 'b', forked_from: 'dashboard:a' }),
      slot({ key: 'c', forked_from: 'dashboard:b' }),
    ])
    // All three still participate — none vanished when the cycle was cut.
    expect(participants.has('a')).toBe(true)
    expect(participants.has('b')).toBe(true)
    expect(participants.has('c')).toBe(true)
    // Exactly one node became a root (its parent link severed to break the loop).
    expect(roots.length).toBe(1)
    // The forest reaches all three nodes from that single root (no orphan).
    const reached = new Set<string>()
    const walk = (n: { slot: { key: string }; children: unknown[] }) => {
      reached.add(n.slot.key)
      for (const ch of n.children as typeof roots) walk(ch)
    }
    roots.forEach(walk)
    expect(reached).toEqual(new Set(['a', 'b', 'c']))
  })
})
