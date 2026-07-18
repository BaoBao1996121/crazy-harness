# Contributing to Crazy Harness

Crazy welcomes small, reproducible contributions to its Agent Loop, durable runtime, Context, Capability, A2A, Memory, and Eval layers.

## Regression stages

Use the smallest stage that can disprove the change, then let later stages broaden confidence. This keeps vertical exploration fast without removing release gates.

| Stage | Purpose | Typical command |
|---|---|---|
| Changed | Exact RED/GREEN tests for the edited behavior | `python -m pytest -q path/to/test.py::test_name` |
| Smoke | Fast end-to-end and safety signal | `python -m pytest -q -m smoke` |
| Core | Deterministic harness regression at a mechanism milestone | `python -m pytest -q -m "not llm and not nightly"` |
| Release | Lint, Core, frontend tests/build, and CI platform matrix | Commands below |
| Nightly | Stress, chaos, repeated sampling, live adapters | `python -m pytest -q -m nightly` plus opt-in live tests |

Authorization, command validation, fencing, completion gates, irreversible side effects, and terminal-state invariants are hard boundaries. Changes to them require immediate focused tests even during rapid exploration.

## Release checks

```powershell
python -m pip install -e ".[dev,browser,mcp]"
python -m playwright install chromium
python -m pytest -q -m "not llm and not nightly"
python -m ruff check --no-cache crazy_harness tests work labs\16h_sprint

cd frontend
npm ci
npm test
npm run build
```

Live DeepSeek tests require both `DEEPSEEK_API_KEY` and `CRAZY_RUN_LLM_TESTS=1`. The scheduled workflow also requires repository variable `CRAZY_NIGHTLY_LLM=1`, so storing a key alone never creates recurring model cost.

## Contribution rules

1. Keep the main Agent Loop under Crazy's control; integrate third-party systems through ports and adapters.
2. Treat model output as a candidate. Side effects require validation, policy, hooks, budgets, and the operation ledger.
3. EventLog, Mailbox, Ledger, and Artifact records are recovery facts; in-memory objects are not.
4. New mechanisms need an off baseline, replayable evidence, failure-path tests, and honest limits.
5. Never commit credentials, runtime databases, private research or chats, local paths, or unreviewed third-party source.

Pull requests should explain behavior, failure modes, and validation. Use Red-Green-Refactor for features and fixes. Synchronize public types, tests, and docs when interfaces change. Label empirical thresholds as initial until evaluation supports them.
