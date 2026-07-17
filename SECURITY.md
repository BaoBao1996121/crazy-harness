# Security Policy

Crazy is an early-stage project for local disposable environments and controlled experiments. Do not grant it production credentials or high-risk infrastructure access without an independent security review and a real sandbox.

## Reporting a vulnerability

Do not post tokens, private traces, production data, or directly exploitable details in a public issue. Prefer **Security -> Report a vulnerability** in the GitHub repository. If private reporting is unavailable, contact the maintainer through their GitHub profile and share only the minimum information needed to establish a private channel.

Include the affected commit, prerequisites, minimal reproduction, likely impact, and any proposed mitigation.

## Current trust boundaries

- Model responses, Skills, MCP metadata, web content, and external Agent messages are untrusted input.
- Tool effects must pass command validation, ToolPolicy, hooks, budgets, and the OperationLedger.
- `GuardedLocalRuntime` is a controlled host process, not a security sandbox. Production isolation requires a container or remote `SandboxRuntime`.
- The local EventLog and Artifact store do not provide multi-tenant encryption, distributed exactly-once effects, or production RBAC.
- Real API keys belong in environment variables or a secret store, never Events, prompts, screenshots, or Git.

Only the latest `main` branch is supported during the alpha stage. If a credential is committed, revoke it first and then clean the history; deleting the newest file alone is not remediation.
