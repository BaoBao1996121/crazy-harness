---
name: repo-maintainer
description: Diagnose and repair a repository task when changes must be minimal, policy-bounded, and proven by durable test and diff evidence.
license: Apache-2.0
compatibility: Crazy Harness repo-maintainer TaskPack with repo and test tools.
metadata:
  owner: crazy-harness
  version: "1"
allowed-tools: repo.list repo.read repo.search repo.write repo.replace test.run repo.diff shell.run
---

# Repository Maintainer

Treat the assignment contract as the goal and the CompletionGate as the definition of done.

1. Inspect the implementation and relevant tests before editing.
2. Form the smallest diagnosis that explains the observed failure.
3. Change only allowlisted implementation files; never weaken tests or policy files.
4. Run the real test tool after the change.
5. Record a non-empty repository diff as evidence.
6. Submit only when the required test and diff observations are durable facts.

The `allowed-tools` frontmatter is a discovery hint, not authority. ToolPolicy and the assignment permissions remain the only execution authority.
