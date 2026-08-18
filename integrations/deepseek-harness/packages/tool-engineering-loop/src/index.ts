/** Read-only DSH tool consumer for Crazy Engineering Loops. */

import type { Context } from '@deepseek-ai/cordis'
import {
  defineTool,
  type ToolDefinition,
} from '@deepseek-ai/dsh-tools'
import type {
  CrazyControlPlane,
  EngineeringLoopView,
} from '@crazy-harness/dsh-control-plane'

const LOOP_STATUSES = [
  'running',
  'pausing',
  'paused',
  'resuming',
  'awaiting_approval',
  'completed',
  'blocked',
  'cancelled',
] as const

/** Build the public model tool against any Crazy control-plane provider. */
export function createEngineeringLoopReadTool(
  controlPlane: CrazyControlPlane,
): ToolDefinition {
  return defineTool({
    name: 'crazy_engineering_loop_get',
    description:
      'Read one Crazy Engineering Loop status by id. This tool is read-only and cannot advance, drain, pause, resume, or cancel a loop.',
    parameters: {
      loop_id: {
        type: 'string',
        required: true,
        description: 'Opaque Crazy Engineering Loop id in loop_<alphanumeric> form.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          loop_id: { type: 'string', required: true },
          title: { type: 'string', required: true },
          objective: { type: 'string', required: true },
          status: { type: 'string', required: true, enum: [...LOOP_STATUSES] },
          active_score: {
            oneOf: [{ type: 'string' }, { type: 'null' }],
            required: true,
          },
          iteration_count: { type: 'integer', required: true },
          child_run_ids: {
            type: 'array',
            required: true,
            items: { type: 'string' },
          },
          terminal_reason: {
            oneOf: [{ type: 'string' }, { type: 'null' }],
            required: true,
          },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: `Engineering Loop ${value.loop_id} is ${value.status} after ${value.iteration_count} iteration(s); active score: ${value.active_score ?? 'not available'}.`,
      }],
    },
    async execute(args, exec): Promise<EngineeringLoopView> {
      return controlPlane.getEngineeringLoop(args.loop_id, exec.signal)
    },
    isConcurrencySafe: () => true,
  })
}

export const name = 'crazy-tool-engineering-loop'
export const inject = ['tools', 'crazyControlPlane']

/** Register the read-only Engineering Loop tool in the DSH tool registry. */
export function apply(ctx: Context): void {
  ctx.tools.register(createEngineeringLoopReadTool(ctx.crazyControlPlane))
}
