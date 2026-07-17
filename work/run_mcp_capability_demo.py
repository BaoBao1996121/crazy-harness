from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.agents import AgentLoop, AssignmentContract
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.capabilities import (
    CAPABILITY_SEARCH_TOOL_NAME,
    CapabilityCatalog,
    CapabilityCompiler,
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySearchService,
    MCPToolGrant,
    MCPToolMount,
    SDKSessionMCPClient,
)
from crazy_harness.core.events import Event
from crazy_harness.core.models import FakeModelProvider
from crazy_harness.core.runtime import CooperativeScheduler, DurableMailbox
from crazy_harness.core.tools import ToolRegistry
from crazy_harness.core.tools.pipeline import OperationLedger, ToolPipeline
from crazy_harness.core.tools.policy import PolicyContext


def _action(**payload: object) -> str:
    return json.dumps(payload)


def _server(remote_calls: list[str]) -> FastMCP:
    server = FastMCP("docs")

    @server.tool()
    def lookup(query: str) -> CallToolResult:
        remote_calls.append(query)
        return CallToolResult(
            content=[TextContent(type="text", text=f"found:{query}")],
            structuredContent={"answer": 42, "query": query},
            _meta={"secret": "client-only-secret"},
        )

    return server


def run_demo(data_dir: Path) -> dict[str, object]:
    data_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"run_{uuid4().hex[:12]}"
    task_id = f"task_{uuid4().hex[:12]}"
    agent_id = f"mcp-researcher-{run_id[-6:]}"
    remote_calls: list[str] = []
    server = _server(remote_calls)
    client = SDKSessionMCPClient(
        server_name="docs",
        session_factory=lambda: create_connected_server_and_client_session(server),
    )
    tools = ToolRegistry()
    catalog = CapabilityCatalog()
    MCPToolMount(
        client,
        grants={
            "lookup": MCPToolGrant(
                side_effect_level="none",
                approval_required=False,
                is_read_only=True,
                is_concurrency_safe=True,
            )
        },
    ).refresh(tools, catalog)
    remote_name = "mcp.docs.lookup"
    search = CapabilitySearchService(
        catalog,
        allowed_names={remote_name},
        max_results=2,
    )
    search.install(tools)
    search_spec = tools.spec(CAPABILITY_SEARCH_TOOL_NAME)
    catalog.register(
        CapabilityDefinition(
            name=search_spec.name,
            kind=CapabilityKind.FUNCTION,
            description=search_spec.description,
            input_schema=search_spec.input_schema,
        )
    )

    store = SQLiteEventStore(data_dir / "control_plane.db")
    created = store.append(
        Event(
            run_id=run_id,
            task_id=task_id,
            type="run.created",
            source="demo.mcp",
            payload={
                "title": "MCP 渐进发现实验 / MCP delayed discovery",
                "brief": "Discover and call one official MCP tool through the canonical loop.",
                "model_mode": "scripted",
                "execution_mode": "single",
                "task_pack": "mcp-capability-demo",
                "behavior_version": "v0.2.0-dev",
            },
        )
    )
    assignment = store.append(
        Event(
            run_id=run_id,
            task_id=task_id,
            type="assignment.created",
            source="demo.mcp",
            payload={
                "assignment_id": task_id,
                "agent_id": agent_id,
                "goal": "inspect the assigned record through an authorized MCP server",
                "exit_criteria": ["remote evidence is recorded"],
            },
            causation_id=created.id,
        )
    )
    model = FakeModelProvider(
        [
            _action(
                type="call_tool",
                reason="discover the remote dossier lookup",
                tool_name=CAPABILITY_SEARCH_TOOL_NAME,
                tool_args={"query": "remote dossier lookup"},
            ),
            _action(
                type="call_tool",
                reason="read the remote dossier",
                tool_name=remote_name,
                tool_args={"query": "leases"},
            ),
            _action(type="stop", reason="remote evidence collected"),
        ]
    )
    all_names = frozenset(spec.name for spec in tools.specs())
    loop = AgentLoop(
        agent_id=agent_id,
        task_id=task_id,
        model=model,
        event_log=store,
        artifact_store=ArtifactStore(data_dir / "artifacts"),
        tool_registry=tools,
        assignment_contract=AssignmentContract(
            goal="inspect the assigned record",
            exit_criteria=("collect remote evidence",),
            output_schema={"type": "object"},
        ),
        tool_pipeline=ToolPipeline(
            tools,
            ledger=OperationLedger(data_dir / "operations" / f"{run_id}.jsonl"),
        ),
        policy_context=PolicyContext(
            agent_id=agent_id,
            assignment_id=task_id,
            mode="scripted",
            allowed_tools=all_names,
        ),
        capability_compiler=CapabilityCompiler(
            catalog,
            inline_limit=1,
            search_limit=1,
        ),
        capability_always_include=(CAPABILITY_SEARCH_TOOL_NAME,),
    )
    mailbox = DurableMailbox(agent_id, store)
    scheduler = CooperativeScheduler(store)
    terminal_event_type: str | None = None

    def step(delivery):
        nonlocal terminal_event_type
        loop.run_once()
        terminal = next(
            (
                event
                for event in reversed(store.read_all(task_id=task_id))
                if event.type in {"agent.stopped", "agent.submitted", "agent.failed"}
            ),
            None,
        )
        terminal_event_type = terminal.type if terminal is not None else None
        if terminal_event_type is None:
            mailbox.send(
                Event(
                    run_id=run_id,
                    task_id=task_id,
                    type="agent.turn.requested",
                    source="runtime.scheduler",
                    causation_id=delivery.event.id if delivery else assignment.id,
                )
            )
        return None

    scheduler.register(agent_id, mailbox, step)
    mailbox.send(
        Event(
            run_id=run_id,
            task_id=task_id,
            type="agent.turn.requested",
            source="demo.mcp",
            causation_id=assignment.id,
        )
    )
    scheduler_steps = 0
    while scheduler_steps < 8 and scheduler.wake(agent_id):
        if not scheduler.run_once():
            break
        scheduler_steps += 1
    if terminal_event_type not in {"agent.stopped", "agent.submitted"}:
        raise RuntimeError(f"MCP demo did not reach a successful terminal event: {terminal_event_type}")
    succeeded = store.append(
        Event(
            run_id=run_id,
            task_id=task_id,
            type="run.succeeded",
            source="demo.mcp",
            payload={"summary": "official MCP tool discovered and called"},
            causation_id=store.last().id,
        )
    )
    events = store.read_all(run_id=run_id)
    manifests = [
        event.payload["manifest"]
        for event in events
        if event.type == "capability.manifest.compiled"
    ]
    return {
        "run_id": run_id,
        "task_id": task_id,
        "status": "succeeded",
        "scheduler_steps": scheduler_steps,
        "model_calls": model.call_count,
        "event_count": len(events),
        "manifest_count": len(manifests),
        "remote_calls": remote_calls,
        "mcp_provider": manifests[-1].get("providers", {}).get(remote_name),
        "last_event_id": succeeded.id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run official MCP discovery through the durable Crazy AgentLoop."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("runs/mcp_capability_demo"),
    )
    result = run_demo(parser.parse_args().data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
