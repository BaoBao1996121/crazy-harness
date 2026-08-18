# Crazy DeepSeek Harness Integration

This workspace packages Crazy capabilities as out-of-tree Cordis plugins for the
official DeepSeek Harness (DSH). DSH owns the agent loop, session, tool UX, and
plugin lifecycle. Crazy remains a sidecar that owns durable domain facts.

## Pinned Baseline

- DSH npm package: `@deepseek-ai/dsh@0.1.0-rc.7`
- DSH upstream tag: `dsh-v0.1.0-rc.7`
- DSH upstream commit: `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`
- Cordis: `@deepseek-ai/cordis@4.0.1`
- Wire protocol: `crazy-dsh-v1`

All DSH packages are exact pins. Do not replace them with `latest`, `next`, a
caret range, a vendored checkout, or a Git submodule. DSH is a Developer Preview;
an upgrade is accepted only after the lockfile and contract tests pass together.

## Packages

| Package | Responsibility |
|---|---|
| `dsh-control-plane` | Transport-independent `ctx.crazyControlPlane` service definition |
| `dsh-control-plane-http` | Loopback-only, version-negotiating HTTP provider |
| `dsh-tool-engineering-loop` | Read-only `crazy_engineering_loop_get` DSH tool |
| `dsh-bundle` | Patch-only composition package; inserts the provider and tool |

The Bundle does not import or replace `@deepseek-ai/dsh-agent-loop`. It contains
only a manifest and `cordis.patch.yml`; the component packages own runtime code.

## Local Verification

Use Node `^22.19.0` or `>=24.0.0`. DSH rc.7 uses Node APIs that are absent from
22.14; the workspace `engines` field is a compatibility boundary, not a
recommendation. The verified local runtime for this tracer bullet is Node
24.19.0.

Run the integration commands from the integration workspace:

```powershell
Set-Location integrations\deepseek-harness
pnpm install --frozen-lockfile
pnpm test
```

`pnpm test` first verifies that every direct DSH dependency matches the exact
workspace baseline, builds the local component packages, then runs the unit,
profile-composition, and lifecycle contracts. Use `pnpm build` alone when only
the TypeScript artifacts are needed.

The Bundle contract packs all four Crazy packages, maps the three unpublished
component versions to those temporary archives, and installs only the Bundle
archive into an isolated DSH profile. It dumps and boots the official base
profile, then asserts that the official `agent-loop` remains exactly once while
the Crazy service and tool activate through the real Loader lifecycle. A
smaller Boot test keeps fast feedback for the ToolRuntime injection boundary.
Pack, DSH CLI, and full boot commands run without a shell in isolated process
groups. Their deadlines terminate the complete child tree on both Windows and
POSIX, so a lifecycle regression cannot outlive the contract or consume the
whole CI job timeout.

The packed profile keeps the install offline and passes the workspace's effective
pnpm store through `PNPM_CONFIG_STORE_DIR` while using a cold `PNPM_HOME`. This is
intentional: a temporary profile on another Windows drive must not select an empty
store or fall back to an undeclared machine cache.

To rerun only the exact-pin and packed-profile contracts without reading or
modifying a personal DSH profile:

```powershell
pnpm dsh:check-pins
pnpm test packages/bundle/tests/bundle.spec.ts
```

Do not treat `dsh plugin add packages/bundle` as a release simulation. A linked
workspace directory can expose the patch while leaving its sibling runtime
packages unresolvable from an external profile. The packed-profile contract is
the local installation proof until the private packages are published.

The Python sidecar command runs from the repository root in a separate terminal:

```powershell
python -m crazy_harness.control_plane --port 8765 --data-dir runs\control_plane_dsh
```

The Bundle is not published to a registry yet, and the only model-facing
operation requires an existing Crazy `loop_id`; there is no DSH list/create tool
in v1. The commands above are therefore verification and contributor workflows,
not a complete end-user installation guide.

The v1 provider rejects non-loopback URLs, credentials in URLs, non-JSON
responses, schema drift, oversized JSON bodies, HTTP errors, and redirects. DSH
never reads Crazy's SQLite database or raw filesystem paths.

Installation executes only the reviewed allowlist inherited from the rc.7
consumer closure: `node-pty`, `koffi`, and `dsh-subprocess-local`. Optional
`@google/genai` and `protobufjs` scripts are explicitly denied. Frozen lockfile
does not make lifecycle scripts harmless; `strictDepBuilds` makes any unlisted
script fail the install until it is reviewed and classified.

## Upgrade Protocol

1. Read the new DSH release notes and extension docs.
2. Run `pnpm dsh:pin <exact-version>` and review every changed direct DSH pin.
3. Regenerate `pnpm-lock.yaml`; review dependency and build-script changes.
4. Run install, tests, and build.
5. Confirm the real composed config still contains one official `agent-loop` and
   only the expected Crazy rows.
6. Add or update wire-contract tests before widening Crazy capabilities.
7. Record the accepted DSH tag/commit and any migration decision in the design
   document and development journal.

The main integration job and scheduled `dsh-next-probe` are configured for
Ubuntu/Node 22.19 and Windows/Node 24. The probe applies the npm `next` version
only to an ephemeral checkout and runs the same contracts for early warning. It
does not restore or save the production pin's pnpm cache, and never updates the
committed production pin automatically. The rc.7 matrix is verified by GitHub
Actions run `32102216325` (both DSH jobs, both backend jobs, and frontend passed).
This is production-pin evidence only; `next` has not yet been proven against a
different upstream release.
