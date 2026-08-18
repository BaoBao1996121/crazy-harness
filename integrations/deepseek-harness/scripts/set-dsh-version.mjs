import { readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const workspaceRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
const exactVersion = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/
const dshPackage = /^@deepseek-ai\/dsh(?:$|-)/
const dependencySections = [
  'dependencies',
  'devDependencies',
  'optionalDependencies',
  'peerDependencies',
]
const manifestPaths = [
  join(workspaceRoot, 'package.json'),
  ...readdirSync(join(workspaceRoot, 'packages'), { withFileTypes: true })
    .filter(entry => entry.isDirectory())
    .map(entry => join(workspaceRoot, 'packages', entry.name, 'package.json')),
].sort()
const manifests = manifestPaths.map((path) => ({
  path,
  value: JSON.parse(readFileSync(path, 'utf8')),
}))

function directDshDependencies(manifest) {
  return dependencySections.flatMap((section) => {
    return Object.entries(manifest[section] ?? {})
      .filter(([name]) => dshPackage.test(name))
      .map(([name, version]) => ({ name, section, version }))
  })
}

const requestedVersion = process.argv[2]
if (requestedVersion === '--check') {
  const workspaceManifest = manifests.find(({ path }) => {
    return path === join(workspaceRoot, 'package.json')
  })?.value
  const baseline = workspaceManifest?.devDependencies?.['@deepseek-ai/dsh']
  if (typeof baseline !== 'string' || !exactVersion.test(baseline)) {
    throw new Error('package.json must exact-pin @deepseek-ai/dsh')
  }
  let count = 0
  for (const { path, value } of manifests) {
    for (const dependency of directDshDependencies(value)) {
      count += 1
      if (dependency.version !== baseline) {
        throw new Error(
          `${path} ${dependency.section}.${dependency.name} must equal ${baseline}`,
        )
      }
    }
  }
  if (count === 0) throw new Error('no direct DSH dependencies found')
  console.log(`Verified ${count} direct DSH pins at ${baseline}.`)
} else {
  if (requestedVersion === undefined || !exactVersion.test(requestedVersion)) {
    throw new Error('usage: pnpm dsh:pin <exact-version>')
  }
  let count = 0
  for (const manifest of manifests) {
    const dependencies = directDshDependencies(manifest.value)
    for (const dependency of dependencies) {
      manifest.value[dependency.section][dependency.name] = requestedVersion
      count += 1
    }
    if (dependencies.length > 0) {
      writeFileSync(manifest.path, `${JSON.stringify(manifest.value, null, 2)}\n`)
    }
  }
  if (count === 0) throw new Error('no direct DSH dependencies found')
  console.log(`Pinned ${count} direct DSH dependencies to ${requestedVersion}.`)
}
