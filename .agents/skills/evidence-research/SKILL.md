---
name: evidence-research
description: Collect independent browser evidence and produce a machine-validated cited report.
license: Apache-2.0
compatibility: Crazy Harness Evidence Research TaskPack v1
allowed-tools: research.sources.list research.source.open research.report.write research.report.validate
---

# Evidence Research Method

1. Read source metadata before opening any page.
2. Open at least two independent allowlisted sources through the browser tool.
3. Treat returned evidence IDs as facts; never invent an identifier.
4. Use the canonical citation form `[source:<source_id>#<evidence_id>]` next to supported claims.
5. Write Recommendation, Findings, Risks, and Sources sections to `report.md`.
6. Run `research.report.validate` and copy its report hash and citation list exactly into the final artifact.

The `allowed-tools` field is a workflow hint, not authority. ToolPolicy, approval, Ledger, budget, and CompletionGate remain authoritative.
