import { Context } from '@deepseek-ai/cordis'
import type { ToolRunContext } from '@deepseek-ai/dsh-tools'
import {
  CrazyControlPlane,
  type CrazyCapabilities,
  type EngineeringLoopView,
} from '@crazy-harness/dsh-control-plane'
import { describe, expect, it, vi } from 'vitest'
import { createEngineeringLoopReadTool } from '../src/index.ts'

class FakeControlPlane extends CrazyControlPlane {
  readonly getEngineeringLoop = vi.fn(
    async (_loopId: string, _signal: AbortSignal): Promise<EngineeringLoopView> => ({
      loop_id: 'loop_123',
      title: 'Repository quality climb',
      objective: 'Reach the frozen quality gate',
      status: 'completed',
      active_score: '1.0',
      iteration_count: 2,
      child_run_ids: ['run_a', 'run_b'],
      terminal_reason: 'target reached',
    }),
  )

  capabilities(_signal: AbortSignal): Promise<CrazyCapabilities> {
    throw new Error('the provider owns protocol negotiation')
  }
}

describe('crazy_engineering_loop_get', () => {
  it('returns the sanitized loop view through the DSH tool contract', async () => {
    const controlPlane = new FakeControlPlane(new Context())
    const tool = createEngineeringLoopReadTool(controlPlane)
    const signal = AbortSignal.timeout(1_000)

    const value = await tool.execute(
      { loop_id: 'loop_123' },
      { signal } as ToolRunContext,
    )

    expect(value).toEqual({
      loop_id: 'loop_123',
      title: 'Repository quality climb',
      objective: 'Reach the frozen quality gate',
      status: 'completed',
      active_score: '1.0',
      iteration_count: 2,
      child_run_ids: ['run_a', 'run_b'],
      terminal_reason: 'target reached',
    })
    expect(controlPlane.getEngineeringLoop).toHaveBeenCalledWith('loop_123', signal)
    expect(tool.output.render({}, value)).toEqual([{
      type: 'text',
      text: 'Engineering Loop loop_123 is completed after 2 iteration(s); active score: 1.0.',
    }])
  })
})
