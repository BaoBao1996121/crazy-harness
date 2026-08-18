import { spawn } from 'node:child_process'
import { join } from 'node:path'

const PROCESS_OUTPUT_LIMIT = 1_048_576
const PROCESS_TREE_KILL_TIMEOUT_MS = 2_000

export interface NodeCommandOptions {
  readonly cwd: string
  readonly env?: NodeJS.ProcessEnv
  readonly timeoutMs: number
}

export interface NodeCommandResult {
  readonly status: number | null
  readonly signal: NodeJS.Signals | null
  readonly stdout: string
  readonly stderr: string
}

export class NodeCommandTimeoutError extends Error {
  readonly result: NodeCommandResult

  constructor(
    timeoutMs: number,
    result: NodeCommandResult,
    terminationError?: unknown,
  ) {
    const cleanupFailure = terminationError === undefined
      ? ''
      : `; process-tree cleanup failed: ${String(terminationError)}`
    super(
      `Node command timed out after ${timeoutMs}ms${cleanupFailure}`
      + `\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`,
    )
    this.name = 'NodeCommandTimeoutError'
    this.result = result
  }
}

function appendOutput(current: string, chunk: string): string {
  const combined = current + chunk
  if (combined.length <= PROCESS_OUTPUT_LIMIT) return combined
  return `[output truncated]\n${combined.slice(-PROCESS_OUTPUT_LIMIT)}`
}

async function runTaskkill(pid: number): Promise<void> {
  const systemRoot = process.env.SystemRoot ?? process.env.WINDIR ?? 'C:\\Windows'
  const taskkillPath = join(systemRoot, 'System32', 'taskkill.exe')

  await new Promise<void>((resolve, reject) => {
    const killer = spawn(taskkillPath, ['/pid', String(pid), '/t', '/f'], {
      stdio: 'ignore',
      windowsHide: true,
    })
    const deadline = setTimeout(() => {
      killer.kill('SIGKILL')
      reject(new Error(`taskkill did not finish for process tree ${pid}`))
    }, PROCESS_TREE_KILL_TIMEOUT_MS)
    const finish = (error?: Error): void => {
      clearTimeout(deadline)
      if (error === undefined) resolve()
      else reject(error)
    }

    killer.once('error', error => finish(error))
    killer.once('exit', (code) => {
      if (code === 0 || code === 128) finish()
      else finish(new Error(`taskkill exited with status ${String(code)} for process tree ${pid}`))
    })
  })
}

async function terminateProcessTree(child: ReturnType<typeof spawn>): Promise<void> {
  const pid = child.pid
  if (pid === undefined) return

  let terminationError: unknown
  try {
    if (process.platform === 'win32') {
      await runTaskkill(pid)
    } else {
      try {
        process.kill(-pid, 'SIGKILL')
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error
      }
    }
  } catch (error) {
    terminationError = error
  }

  if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL')
  if (terminationError !== undefined) throw terminationError
}

export function runNodeCommand(
  args: readonly string[],
  options: NodeCommandOptions,
): Promise<NodeCommandResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, {
      cwd: options.cwd,
      detached: true,
      env: options.env ?? process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')

    let stdout = ''
    let stderr = ''
    let settled = false
    let timedOut = false
    let terminationComplete = false
    let terminationError: unknown
    let closeResult: NodeCommandResult | undefined
    let killGrace: ReturnType<typeof setTimeout> | undefined

    const cleanup = (): void => {
      clearTimeout(deadline)
      if (killGrace !== undefined) clearTimeout(killGrace)
    }
    const finish = (
      result: NodeCommandResult | undefined,
      error: unknown,
    ): void => {
      if (settled) return
      settled = true
      cleanup()
      if (error === undefined && result !== undefined) resolve(result)
      else reject(error)
    }
    const timeoutError = (): NodeCommandTimeoutError => {
      return new NodeCommandTimeoutError(
        options.timeoutMs,
        closeResult ?? {
          status: null,
          signal: null,
          stdout,
          stderr,
        },
        terminationError,
      )
    }
    const finishTimedOut = (): void => {
      if (!terminationComplete || closeResult === undefined) return
      finish(undefined, timeoutError())
    }

    child.stdout.on('data', chunk => { stdout = appendOutput(stdout, chunk) })
    child.stderr.on('data', chunk => { stderr = appendOutput(stderr, chunk) })
    child.once('error', error => finish(undefined, error))
    child.once('close', (status, signal) => {
      closeResult = { status, signal, stdout, stderr }
      if (timedOut) finishTimedOut()
      else finish(closeResult, undefined)
    })

    const deadline = setTimeout(() => {
      timedOut = true
      void terminateProcessTree(child)
        .catch((error: unknown) => { terminationError = error })
        .finally(() => {
          terminationComplete = true
          finishTimedOut()
          if (settled) return
          killGrace = setTimeout(() => finish(undefined, timeoutError()), PROCESS_TREE_KILL_TIMEOUT_MS)
        })
    }, options.timeoutMs)
  })
}
