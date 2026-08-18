import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  healProfilesModuleFallback,
  loadProfile,
} from '@deepseek-ai/dsh-app-boot'
import { DEFAULT_SCHEMA, dump, load, Type } from 'js-yaml'
import { describe, expect, it } from 'vitest'
import type {} from '@crazy-harness/dsh-control-plane'
import type {} from '@deepseek-ai/dsh-tools'
import { runNodeCommand } from './process-runner.js'

interface PatchInsert {
  readonly insert: readonly {
    readonly id: string
    readonly name: string
  }[]
}

interface ComposedPluginRow {
  readonly id: string
  readonly name: string
}

interface PackageManifest {
  readonly name: string
  readonly version?: string
  readonly dependencies?: Readonly<Record<string, string>>
  readonly devDependencies?: Readonly<Record<string, string>>
  readonly optionalDependencies?: Readonly<Record<string, string>>
  readonly peerDependencies?: Readonly<Record<string, string>>
}

interface PackageArchive {
  readonly name: string
  readonly version: string
  readonly path: string
}

const cordisConfigSchema = DEFAULT_SCHEMA.extend([
  new Type('tag:yaml.org,2002:js', {
    kind: 'scalar',
    construct: value => value,
  }),
])
const DSH_CLI_TIMEOUT_MS = 10_000
const DSH_BOOT_TIMEOUT_MS = 20_000
const BOOT_RESULT_PREFIX = 'CRAZY_DSH_BOOT_RESULT='
const EXACT_VERSION = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/
const DSH_PACKAGE = /^@deepseek-ai\/dsh(?:$|-)/
const dependencySections = [
  'dependencies',
  'devDependencies',
  'optionalDependencies',
  'peerDependencies',
] as const

async function packPackage(packageDir: string, archivePath: string): Promise<PackageArchive> {
  const manifest = JSON.parse(
    readFileSync(join(packageDir, 'package.json'), 'utf8'),
  ) as PackageManifest
  if (manifest.version === undefined) {
    throw new Error(`${packageDir} must declare a version before packing`)
  }
  const pnpmCli = process.env.npm_execpath
  if (
    pnpmCli === undefined
    || !existsSync(pnpmCli)
    || !/^pnpm\.(?:[cm]?js)$/i.test(basename(pnpmCli))
  ) {
    throw new Error(
      'pnpm JavaScript CLI unavailable; run this contract through `pnpm test`',
    )
  }
  const result = await runNodeCommand([
    pnpmCli,
    'pack',
    '--out',
    archivePath,
  ], {
    cwd: packageDir,
    timeoutMs: DSH_CLI_TIMEOUT_MS,
  })

  expect(result.status, `${result.stdout}\n${result.stderr}`).toBe(0)
  return {
    name: manifest.name,
    version: manifest.version,
    path: archivePath,
  }
}

function fileSpec(path: string): string {
  return `file:${path.replaceAll('\\', '/')}`
}

describe('@crazy-harness/dsh-bundle', () => {
  it('keeps every direct DSH dependency on one exact baseline', () => {
    const packageRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
    const workspaceRoot = join(packageRoot, '..', '..')
    const packageDirs = readdirSync(join(workspaceRoot, 'packages'), {
      withFileTypes: true,
    })
      .filter(entry => entry.isDirectory())
      .map(entry => join(workspaceRoot, 'packages', entry.name))
    const manifests = [workspaceRoot, ...packageDirs].map((dir) => {
      return JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8')) as PackageManifest
    })
    const baseline = manifests[0]?.devDependencies?.['@deepseek-ai/dsh']

    expect(baseline).toMatch(EXACT_VERSION)
    const directDshDependencies = manifests.flatMap((manifest) => {
      return dependencySections.flatMap((section) => {
        return Object.entries(manifest[section] ?? {})
          .filter(([name]) => DSH_PACKAGE.test(name))
          .map(([name, version]) => ({
            manifest: manifest.name,
            name,
            version,
          }))
      })
    })
    expect(directDshDependencies.length).toBeGreaterThan(0)
    for (const dependency of directDshDependencies) {
      expect(dependency.version, `${dependency.manifest} -> ${dependency.name}`)
        .toBe(baseline)
    }
  })

  it('only inserts Crazy plugins and never replaces the DSH agent loop', () => {
    const packageRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
    const manifest = JSON.parse(
      readFileSync(join(packageRoot, 'package.json'), 'utf8'),
    ) as { dsh: { bundle: { patch: string } } }
    const patchPath = join(packageRoot, manifest.dsh.bundle.patch)
    const patches = load(readFileSync(patchPath, 'utf8')) as PatchInsert[]

    expect(patches).toEqual([{
      insert: [
        {
          id: 'crazy-control-plane-http',
          name: '@crazy-harness/dsh-control-plane-http',
        },
        {
          id: 'crazy-tool-engineering-loop',
          name: '@crazy-harness/dsh-tool-engineering-loop',
        },
      ],
    }])
    expect(JSON.stringify(patches)).not.toContain('agent-loop')
  })

  it('installs packed components and boots without replacing the official agent loop', async () => {
    const packageRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
    const workspaceRoot = join(packageRoot, '..', '..')
    const dshBin = join(
      workspaceRoot,
      'node_modules',
      '@deepseek-ai',
      'dsh',
      'lib',
      'bin.js',
    )
    const dshHome = mkdtempSync(join(tmpdir(), 'crazy-dsh-contract-'))
    const artifactsDir = join(dshHome, 'artifacts')
    mkdirSync(artifactsDir)

    try {
      const packageSources = [
        ['control-plane', join(workspaceRoot, 'packages', 'control-plane')],
        ['control-plane-http', join(workspaceRoot, 'packages', 'control-plane-http')],
        ['tool-engineering-loop', join(workspaceRoot, 'packages', 'tool-engineering-loop')],
        ['bundle', packageRoot],
      ] as const
      const packageArchives: PackageArchive[] = []
      for (const [archiveName, packageDir] of packageSources) {
        const archivePath = join(artifactsDir, `${archiveName}.tgz`)
        packageArchives.push(await packPackage(packageDir, archivePath))
      }
      const initialize = await runNodeCommand([
        dshBin,
        'plugin',
        '--profile',
        'crazy-contract',
        'install',
        '--offline',
      ], {
        cwd: workspaceRoot,
        env: { ...process.env, DSH_HOME: dshHome },
        timeoutMs: DSH_CLI_TIMEOUT_MS,
      })

      expect(initialize.status, `${initialize.stdout}\n${initialize.stderr}`).toBe(0)
      const profileDir = join(dshHome, 'profiles', 'crazy-contract')
      const workspacePolicyPath = join(profileDir, 'pnpm-workspace.yaml')
      const workspacePolicy = load(
        readFileSync(workspacePolicyPath, 'utf8'),
      ) as Record<string, unknown>
      const componentOverrides = Object.fromEntries(
        packageArchives.slice(0, -1).map((archive) => [
          `${archive.name}@${archive.version}`,
          fileSpec(archive.path),
        ]),
      )
      writeFileSync(
        workspacePolicyPath,
        dump({ ...workspacePolicy, overrides: componentOverrides }),
      )
      const bundleArchive = packageArchives.at(-1)
      if (bundleArchive === undefined) {
        throw new Error('bundle archive was not packed')
      }
      expect(bundleArchive.name).toBe('@crazy-harness/dsh-bundle')
      const install = await runNodeCommand([
        dshBin,
        'plugin',
        '--profile',
        'crazy-contract',
        'add',
        '--offline',
        bundleArchive.path,
      ], {
        cwd: workspaceRoot,
        env: { ...process.env, DSH_HOME: dshHome },
        timeoutMs: DSH_CLI_TIMEOUT_MS,
      })

      expect(install.status, `${install.stdout}\n${install.stderr}`).toBe(0)
      const result = await runNodeCommand([
        dshBin,
        '--profile',
        'crazy-contract',
        '--dump-config',
      ], {
        cwd: workspaceRoot,
        env: { ...process.env, DSH_HOME: dshHome },
        timeoutMs: DSH_CLI_TIMEOUT_MS,
      })

      expect(result.status, result.stderr).toBe(0)
      const rows = load(result.stdout, { schema: cordisConfigSchema }) as ComposedPluginRow[]
      expect(rows.filter(row => row.name === '@deepseek-ai/dsh-agent-loop')).toHaveLength(1)
      expect(rows.slice(-2)).toEqual([
        {
          id: 'crazy-control-plane-http',
          name: '@crazy-harness/dsh-control-plane-http',
        },
        {
          id: 'crazy-tool-engineering-loop',
          name: '@crazy-harness/dsh-tool-engineering-loop',
        },
      ])

      const installAnchor = join(dirname(dshBin), '..', 'package.json')
      healProfilesModuleFallback(installAnchor, dshHome)
      const profile = loadProfile(
        'crazy-dsh-contract',
        'crazy-contract',
        installAnchor,
        dshHome,
      )
      expect(profile.layers.map(layer => layer.packageName)).toEqual([
        '@deepseek-ai/dsh-base',
        '@crazy-harness/dsh-bundle',
      ])
      const profileManifest = JSON.parse(
        readFileSync(join(profile.dir, 'package.json'), 'utf8'),
      ) as PackageManifest
      expect(profileManifest.dependencies).toEqual({
        '@crazy-harness/dsh-bundle': fileSpec(bundleArchive.path),
      })
      const rootConfig = join(profile.dir, 'cordis.yml')
      writeFileSync(rootConfig, '[]\n')
      const bootScript = fileURLToPath(new URL('./boot-profile.mjs', import.meta.url))
      const bootResult = await runNodeCommand([
        bootScript,
        dshHome,
        installAnchor,
      ], {
        cwd: workspaceRoot,
        env: { ...process.env, DSH_HOME: dshHome },
        timeoutMs: DSH_BOOT_TIMEOUT_MS,
      })
      expect(bootResult.status, `${bootResult.stdout}\n${bootResult.stderr}`).toBe(0)
      const bootReportLine = bootResult.stdout
        .split(/\r?\n/u)
        .find(line => line.startsWith(BOOT_RESULT_PREFIX))
      expect(bootReportLine).toBeDefined()
      const bootReport = JSON.parse(
        bootReportLine?.slice(BOOT_RESULT_PREFIX.length) ?? '{}',
      ) as Record<string, unknown>
      expect(bootReport).toEqual({
        controlPlane: true,
        engineeringLoopTool: true,
      })
    } finally {
      rmSync(dshHome, { recursive: true, force: true })
    }
  }, 60_000)
})
