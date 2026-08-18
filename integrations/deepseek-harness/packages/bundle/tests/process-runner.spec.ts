import { describe, expect, it } from 'vitest'
import {
  NodeCommandTimeoutError,
  runNodeCommand,
} from './process-runner.js'

function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ESRCH') return false
    throw error
  }
}

async function waitForProcessExit(pid: number): Promise<void> {
  const deadline = Date.now() + 1_000
  while (Date.now() < deadline) {
    if (!isProcessAlive(pid)) return
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  throw new Error(`grandchild process ${pid} survived the process-tree deadline`)
}

describe('DSH contract process runner', () => {
  it('terminates a timed-out subprocess tree', async () => {
    const childScript = [
      "import { spawn } from 'node:child_process'",
      "const grandchild = spawn(process.execPath, ['--input-type=module', '--eval', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' })",
      "process.stdout.write(`GRANDCHILD_PID=${grandchild.pid}\\n`)",
      'setInterval(() => {}, 1000)',
    ].join('\n')

    let timeoutError: NodeCommandTimeoutError | undefined
    try {
      await runNodeCommand([
        '--input-type=module',
        '--eval',
        childScript,
      ], {
        cwd: process.cwd(),
        timeoutMs: 1_000,
      })
    } catch (error) {
      expect(error).toBeInstanceOf(NodeCommandTimeoutError)
      timeoutError = error as NodeCommandTimeoutError
    }

    expect(timeoutError?.message).toContain('timed out after 1000ms')
    const pidMatch = /GRANDCHILD_PID=(\d+)/u.exec(timeoutError?.result.stdout ?? '')
    expect(pidMatch?.[1]).toBeDefined()
    const grandchildPid = Number(pidMatch?.[1])
    try {
      await waitForProcessExit(grandchildPid)
    } finally {
      if (isProcessAlive(grandchildPid)) process.kill(grandchildPid, 'SIGKILL')
    }
  }, 8_000)
})
