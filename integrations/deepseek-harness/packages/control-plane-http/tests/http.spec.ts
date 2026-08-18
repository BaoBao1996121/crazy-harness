import { Context } from '@deepseek-ai/cordis'
import { describe, expect, it, vi } from 'vitest'
import { CrazyControlPlaneHttp } from '../src/index.ts'

describe('CrazyControlPlaneHttp', () => {
  it('accepts additive v1 capabilities while requiring engineering loop read', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({
      protocol_version: 'crazy-dsh-v1',
      control_plane_version: '0.11.0-dev',
      transport: 'http-json',
      capabilities: ['engineering_loop.read', 'scientific_job.read'],
    }), { status: 200, headers: { 'content-type': 'application/json' } }))
    const client = new CrazyControlPlaneHttp(
      new Context(),
      { baseUrl: 'http://127.0.0.1:8765' },
      fetchImpl,
    )

    await expect(client.capabilities(AbortSignal.timeout(1_000))).resolves.toMatchObject({
      capabilities: ['engineering_loop.read', 'scientific_job.read'],
    })
  })

  it('fails closed when the required capability is absent or a response drifts', async () => {
    const response = (body: unknown) => new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
    const missingCapability = new CrazyControlPlaneHttp(
      new Context(),
      { baseUrl: 'http://127.0.0.1:8765' },
      vi.fn<typeof fetch>(async () => response({
        protocol_version: 'crazy-dsh-v1',
        control_plane_version: '0.11.0-dev',
        transport: 'http-json',
        capabilities: ['scientific_job.read'],
      })),
    )
    await expect(missingCapability.capabilities(
      AbortSignal.timeout(1_000),
    )).rejects.toMatchObject({
      code: 'required_capability_missing',
      retryable: false,
    })

    const schemaDrift = new CrazyControlPlaneHttp(
      new Context(),
      { baseUrl: 'http://127.0.0.1:8765' },
      vi.fn<typeof fetch>(async () => response({
        protocol_version: 'crazy-dsh-v1',
        control_plane_version: '0.11.0-dev',
        transport: 'http-json',
        capabilities: ['engineering_loop.read'],
        unexpected: true,
      })),
    )
    await expect(schemaDrift.capabilities(
      AbortSignal.timeout(1_000),
    )).rejects.toMatchObject({
      code: 'invalid_response',
      retryable: false,
    })
  })

  it('does not let one caller abort another concurrent handshake', async () => {
    const firstController = new AbortController()
    const secondController = new AbortController()
    let requestCount = 0
    const response = () => new Response(JSON.stringify({
      protocol_version: 'crazy-dsh-v1',
      control_plane_version: '0.10.0-dev',
      transport: 'http-json',
      capabilities: ['engineering_loop.read'],
    }), { status: 200, headers: { 'content-type': 'application/json' } })
    const fetchImpl = vi.fn<typeof fetch>(async (_input, init) => {
      requestCount += 1
      if (requestCount > 1) return response()
      return await new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          reject(init.signal?.reason)
        }, { once: true })
      })
    })
    const client = new CrazyControlPlaneHttp(
      new Context(),
      { baseUrl: 'http://127.0.0.1:8765' },
      fetchImpl,
    )

    const first = client.capabilities(firstController.signal)
    const second = client.capabilities(secondController.signal)
    firstController.abort(new DOMException('first caller stopped', 'AbortError'))

    await expect(first).rejects.toMatchObject({ name: 'AbortError' })
    await expect(second).resolves.toMatchObject({
      protocol_version: 'crazy-dsh-v1',
    })
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })

  it('forbids redirects so a loopback endpoint cannot forward requests remotely', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async (_input, init) => {
      expect(init?.redirect).toBe('error')
      return new Response(JSON.stringify({
        protocol_version: 'crazy-dsh-v1',
        control_plane_version: '0.10.0-dev',
        transport: 'http-json',
        capabilities: ['engineering_loop.read'],
      }), { status: 200, headers: { 'content-type': 'application/json' } })
    })
    const client = new CrazyControlPlaneHttp(
      new Context(),
      { baseUrl: 'http://127.0.0.1:8765' },
      fetchImpl,
    )

    await client.capabilities(AbortSignal.timeout(1_000))
  })

  it('preserves the structured Crazy wire error contract', async () => {
    const fetchImpl = vi.fn<typeof fetch>(async input => {
      if (String(input).endsWith('/capabilities')) {
        return new Response(JSON.stringify({
          protocol_version: 'crazy-dsh-v1',
          control_plane_version: '0.10.0-dev',
          transport: 'http-json',
          capabilities: ['engineering_loop.read'],
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      return new Response(JSON.stringify({
        detail: {
          code: 'engineering_loop_not_found',
          message: 'engineering loop not found',
          retryable: false,
        },
      }), { status: 404, headers: { 'content-type': 'application/json' } })
    })
    const client = new CrazyControlPlaneHttp(
      new Context(),
      { baseUrl: 'http://127.0.0.1:8765' },
      fetchImpl,
    )

    await expect(client.getEngineeringLoop(
      'loop_missing',
      AbortSignal.timeout(1_000),
    )).rejects.toMatchObject({
      name: 'CrazyControlPlaneHttpError',
      status: 404,
      code: 'engineering_loop_not_found',
      retryable: false,
      message: 'engineering loop not found',
    })
  })

  it('stops reading an oversized error body at the byte limit', async () => {
    let pulls = 0
    let cancelled = false
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1
        if (pulls > 64) {
          controller.close()
          return
        }
        controller.enqueue(new Uint8Array(1_024).fill(0x78))
      },
      cancel() {
        cancelled = true
      },
    })
    const client = new CrazyControlPlaneHttp(
      new Context(),
      { baseUrl: 'http://127.0.0.1:8765' },
      vi.fn<typeof fetch>(async () => new Response(body, {
        status: 502,
        headers: { 'content-type': 'application/json' },
      })),
    )

    await expect(client.capabilities(AbortSignal.timeout(1_000)))
      .rejects.toMatchObject({
        code: 'http_502',
        retryable: true,
      })
    expect(cancelled).toBe(true)
    expect(pulls).toBeLessThanOrEqual(18)
  })

  it('rejects a declared oversized response without consuming its body', async () => {
    let pulls = 0
    let cancelled = false
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1
        controller.enqueue(new Uint8Array([0x7b]))
      },
      cancel() {
        cancelled = true
      },
    })
    const client = new CrazyControlPlaneHttp(
      new Context(),
      { baseUrl: 'http://127.0.0.1:8765' },
      vi.fn<typeof fetch>(async () => new Response(body, {
        status: 200,
        headers: {
          'content-length': '20000',
          'content-type': 'application/json',
        },
      })),
    )

    await expect(client.capabilities(AbortSignal.timeout(1_000)))
      .rejects.toMatchObject({ code: 'invalid_response' })
    expect(cancelled).toBe(true)
    expect(pulls).toBeLessThanOrEqual(1)
  })

  it('handshakes once before reading a sanitized Engineering Loop view', async () => {
    const loopView = {
      loop_id: 'loop_123',
      title: 'Repository quality climb',
      objective: 'Reach the frozen quality gate',
      status: 'running',
      active_score: null,
      iteration_count: 0,
      child_run_ids: [],
      terminal_reason: null,
    }
    const fetchImpl = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      expect(init?.signal).toBeInstanceOf(AbortSignal)
      if (url.endsWith('/capabilities')) {
        return new Response(JSON.stringify({
          protocol_version: 'crazy-dsh-v1',
          control_plane_version: '0.10.0-dev',
          transport: 'http-json',
          capabilities: ['engineering_loop.read'],
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      expect(url).toBe(
        'http://127.0.0.1:8765/api/integrations/dsh/v1/engineering-loops/loop_123',
      )
      return new Response(JSON.stringify(loopView), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    })
    const client = new CrazyControlPlaneHttp(
      new Context(),
      { baseUrl: 'http://127.0.0.1:8765' },
      fetchImpl,
    )

    await expect(client.getEngineeringLoop('loop_123', AbortSignal.timeout(1_000)))
      .resolves.toEqual(loopView)
    await expect(client.getEngineeringLoop('loop_123', AbortSignal.timeout(1_000)))
      .resolves.toEqual(loopView)

    expect(fetchImpl).toHaveBeenCalledTimes(3)
  })
})
