import { Context } from '@deepseek-ai/cordis'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import { CrazyControlPlaneHttp } from '@crazy-harness/dsh-control-plane-http'
import {
  apply as applyEngineeringLoopTool,
  inject as engineeringLoopToolInject,
} from '@crazy-harness/dsh-tool-engineering-loop'
import { describe, expect, it } from 'vitest'

describe('Crazy DSH runtime composition', () => {
  it('boots the provider and registers the Engineering Loop tool', async () => {
    const ctx = new Context()
    try {
      await ctx.plugin(SystemPrompt).await()
      await ctx.plugin(ToolRuntime).await()
      await ctx.plugin(CrazyControlPlaneHttp, {
        baseUrl: 'http://127.0.0.1:9',
      }).await()
      const toolFiber = ctx.plugin({
        inject: [...engineeringLoopToolInject],
        apply: applyEngineeringLoopTool,
      })
      await toolFiber.await()

      expect(ctx.crazyControlPlane).toBeInstanceOf(CrazyControlPlaneHttp)
      expect(ctx.tools.get('crazy_engineering_loop_get')?.name)
        .toBe('crazy_engineering_loop_get')
      expect(ctx.tools.schemas().map(tool => tool.name))
        .toContain('crazy_engineering_loop_get')

      await toolFiber.dispose()
      expect(ctx.tools.get('crazy_engineering_loop_get')).toBeUndefined()
    } finally {
      await ctx.fiber.dispose()
    }
  })
})
