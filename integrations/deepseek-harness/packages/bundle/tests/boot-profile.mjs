import { join } from 'node:path'
import {
  boot,
  healProfilesModuleFallback,
  loadProfile,
} from '@deepseek-ai/dsh-app-boot'
import { provideCmdline } from '@deepseek-ai/dsh-cmdline'

const RESULT_PREFIX = 'CRAZY_DSH_BOOT_RESULT='
const [dshHome, installAnchor] = process.argv.slice(2)

if (dshHome === undefined || installAnchor === undefined) {
  throw new Error('boot-profile requires DSH_HOME and an install anchor')
}

process.env.DSH_HOME = dshHome
healProfilesModuleFallback(installAnchor, dshHome)
const profile = loadProfile(
  'crazy-dsh-contract',
  'crazy-contract',
  installAnchor,
  dshHome,
)
const rootConfig = join(profile.dir, 'cordis.yml')
let context

try {
  context = await boot(
    'crazy-dsh-contract',
    rootConfig,
    [
      ...profile.layers.flatMap(layer => layer.patches),
      ...profile.patches,
      { id: 'session-telemetry-otel', disabled: true },
    ],
    bootContext => provideCmdline(bootContext, { args: [], exit: () => {} }),
  )
  const result = {
    controlPlane: context.get('crazyControlPlane') !== undefined,
    engineeringLoopTool: context.get('tools')?.get('crazy_engineering_loop_get') !== undefined,
  }
  if (!result.controlPlane || !result.engineeringLoopTool) {
    throw new Error(`Crazy DSH plugins did not activate: ${JSON.stringify(result)}`)
  }
  await context.fiber.dispose()
  context = undefined
  process.stdout.write(`${RESULT_PREFIX}${JSON.stringify(result)}\n`)
} finally {
  await context?.fiber.dispose()
}
