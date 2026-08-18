/** Service Definition for Crazy capabilities mounted into DeepSeek Harness. */

import { Context, Service } from '@deepseek-ai/cordis'

declare module '@deepseek-ai/cordis' {
  interface Context {
    crazyControlPlane: CrazyControlPlane
  }
}

/** Capabilities negotiated with the Crazy sidecar. */
export interface CrazyCapabilities {
  readonly protocol_version: 'crazy-dsh-v1'
  readonly control_plane_version: string
  readonly transport: 'http-json'
  readonly capabilities: readonly string[]
}

/** Stable cross-process Engineering Loop projection. */
export interface EngineeringLoopView {
  readonly loop_id: string
  readonly title: string
  readonly objective: string
  readonly status:
    | 'running'
    | 'pausing'
    | 'paused'
    | 'resuming'
    | 'awaiting_approval'
    | 'completed'
    | 'blocked'
    | 'cancelled'
  readonly active_score: string | null
  readonly iteration_count: number
  readonly child_run_ids: string[]
  readonly terminal_reason: string | null
}

/** Swappable Crazy sidecar client; providers own transport and validation. */
export abstract class CrazyControlPlane extends Service {
  constructor(ctx: Context) {
    super(ctx, 'crazyControlPlane')
  }

  /** Negotiate the sidecar protocol before capability calls. */
  abstract capabilities(signal: AbortSignal): Promise<CrazyCapabilities>

  /** Read one sanitized Engineering Loop projection. */
  abstract getEngineeringLoop(
    loopId: string,
    signal: AbortSignal,
  ): Promise<EngineeringLoopView>
}

export default CrazyControlPlane
