/** Loopback HTTP provider for the Crazy control-plane capability. */

import { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import {
  CrazyControlPlane,
  type CrazyCapabilities,
  type EngineeringLoopView,
} from '@crazy-harness/dsh-control-plane'
import { z as zod } from 'zod'

const capabilitiesSchema = zod.object({
  protocol_version: zod.literal('crazy-dsh-v1'),
  control_plane_version: zod.string().min(1),
  transport: zod.literal('http-json'),
  capabilities: zod.array(zod.string().min(1)),
}).strict()

const engineeringLoopSchema = zod.object({
  loop_id: zod.string().min(1),
  title: zod.string().min(1),
  objective: zod.string().min(1),
  status: zod.enum([
    'running',
    'pausing',
    'paused',
    'resuming',
    'awaiting_approval',
    'completed',
    'blocked',
    'cancelled',
  ]),
  active_score: zod.string().nullable(),
  iteration_count: zod.number().int().nonnegative(),
  child_run_ids: zod.array(zod.string().min(1)),
  terminal_reason: zod.string().nullable(),
}).strict()

const wireErrorSchema = zod.object({
  detail: zod.object({
    code: zod.string().min(1),
    message: zod.string().min(1),
    retryable: zod.boolean(),
  }).strict(),
}).strict()

const MAX_RESPONSE_BODY_BYTES = 16_384

interface BoundedBody {
  readonly text: string
  readonly truncated: boolean
}

async function readBoundedBody(
  response: Response,
): Promise<BoundedBody> {
  const declared = Number(response.headers.get('content-length') ?? Number.NaN)
  if (Number.isFinite(declared) && declared > MAX_RESPONSE_BODY_BYTES) {
    await response.body?.cancel().catch(() => undefined)
    return { text: '', truncated: true }
  }
  if (response.body === null) return { text: '', truncated: false }

  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let byteLength = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      byteLength += value.byteLength
      if (byteLength > MAX_RESPONSE_BODY_BYTES) {
        return { text: '', truncated: true }
      }
      chunks.push(value)
    }
  } finally {
    await reader.cancel().catch(() => undefined)
  }

  const bytes = new Uint8Array(byteLength)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  return { text: new TextDecoder().decode(bytes), truncated: false }
}

/** HTTP provider configuration. Remote endpoints are intentionally unsupported. */
export interface Config {
  readonly baseUrl?: string
}

type ResolvedConfig = Required<Config>

/** Error returned by the versioned Crazy HTTP seam. */
export class CrazyControlPlaneHttpError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly code = 'crazy_control_plane_error',
    readonly retryable = false,
  ) {
    super(message)
    this.name = 'CrazyControlPlaneHttpError'
  }
}

/** Validate and normalize the sidecar URL without trusting proxy headers. */
function resolveBaseUrl(value: string): string {
  const url = new URL(value)
  if (url.protocol !== 'http:') {
    throw new Error('crazy control plane must use loopback HTTP in v1')
  }
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
    throw new Error('crazy control plane v1 only supports a loopback host')
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error('crazy control plane baseUrl must not contain credentials, query, or fragment')
  }
  return url.toString().replace(/\/$/, '')
}

/** Strict, version-negotiating HTTP provider for the Crazy sidecar. */
export class CrazyControlPlaneHttp extends CrazyControlPlane {
  static Config: z<Config> = z.object({
    baseUrl: z.string().default('http://127.0.0.1:8765'),
  })

  private readonly baseUrl: string
  private negotiatedCapabilities: CrazyCapabilities | undefined

  constructor(
    ctx: Context,
    config: Config,
    private readonly fetchImpl: typeof fetch = fetch,
  ) {
    super(ctx)
    const resolved = config as ResolvedConfig
    this.baseUrl = resolveBaseUrl(resolved.baseUrl ?? 'http://127.0.0.1:8765')
  }

  async capabilities(signal: AbortSignal): Promise<CrazyCapabilities> {
    if (this.negotiatedCapabilities !== undefined) {
      return this.negotiatedCapabilities
    }
    const capabilities = await this.readJson(
      '/api/integrations/dsh/v1/capabilities',
      capabilitiesSchema,
      signal,
    )
    if (!capabilities.capabilities.includes('engineering_loop.read')) {
      throw new CrazyControlPlaneHttpError(
        'crazy control plane does not provide engineering_loop.read',
        undefined,
        'required_capability_missing',
      )
    }
    this.negotiatedCapabilities = capabilities
    return capabilities
  }

  async getEngineeringLoop(
    loopId: string,
    signal: AbortSignal,
  ): Promise<EngineeringLoopView> {
    await this.capabilities(signal)
    if (!/^loop_[A-Za-z0-9]+$/.test(loopId)) {
      throw new TypeError('engineering loop id must use the loop_<alphanumeric> form')
    }
    return this.readJson(
      `/api/integrations/dsh/v1/engineering-loops/${encodeURIComponent(loopId)}`,
      engineeringLoopSchema,
      signal,
    )
  }

  private async readJson<T>(
    path: string,
    schema: zod.ZodType<T>,
    signal: AbortSignal,
  ): Promise<T> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: 'GET',
      headers: { accept: 'application/json' },
      redirect: 'error',
      signal,
    })
    const contentType = response.headers.get('content-type') ?? ''
    const isJson = contentType.toLowerCase().includes('application/json')
    const { text: body, truncated } = await readBoundedBody(response)
    if (!response.ok) {
      if (isJson && !truncated) {
        try {
          const parsed = wireErrorSchema.safeParse(JSON.parse(body))
          if (parsed.success) {
            throw new CrazyControlPlaneHttpError(
              parsed.data.detail.message,
              response.status,
              parsed.data.detail.code,
              parsed.data.detail.retryable,
            )
          }
        } catch (error) {
          if (error instanceof CrazyControlPlaneHttpError) throw error
        }
      }
      throw new CrazyControlPlaneHttpError(
        `crazy control plane request failed with HTTP ${response.status}`,
        response.status,
        `http_${response.status}`,
        response.status >= 500,
      )
    }
    if (truncated) {
      throw new CrazyControlPlaneHttpError(
        'crazy control plane response exceeds the maximum body size',
        response.status,
        'invalid_response',
      )
    }
    if (!isJson) {
      throw new CrazyControlPlaneHttpError(
        'crazy control plane returned a non-JSON response',
        response.status,
        'invalid_response',
      )
    }
    let value: unknown
    try {
      value = JSON.parse(body)
    } catch {
      throw new CrazyControlPlaneHttpError(
        'crazy control plane returned invalid JSON',
        response.status,
        'invalid_response',
      )
    }
    const parsed = schema.safeParse(value)
    if (!parsed.success) {
      throw new CrazyControlPlaneHttpError(
        'crazy control plane response does not match the negotiated schema',
        response.status,
        'invalid_response',
      )
    }
    return parsed.data
  }
}

export default CrazyControlPlaneHttp
