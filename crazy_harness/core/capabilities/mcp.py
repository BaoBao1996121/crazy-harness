from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.core.capabilities.catalog import (
    CapabilityCatalog,
    CapabilityDefinition,
    CapabilityKind,
)
from crazy_harness.core.tools import ToolCall, ToolRegistry, ToolResult, ToolSpec

_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
_SUCCESS_STATUSES = frozenset({"ok", "success", "succeeded"})


class MCPToolDescriptor(BaseModel):
    """Transport-neutral MCP tool metadata returned by a complete list snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "additionalProperties": False,
        }
    )
    output_schema: dict[str, Any] | None = None


class MCPCallResult(BaseModel):
    """Only server data that may enter a model-visible Tool Observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: tuple[dict[str, Any], ...] = ()
    structured_content: dict[str, Any] | None = None
    is_error: bool = False


class MCPToolGrant(BaseModel):
    """Local authority and side-effect classification; never inferred from server hints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    side_effect_level: str = "external"
    approval_required: bool = True
    output_offload_policy: str = "auto"
    is_read_only: bool = False
    is_destructive: bool = False
    is_concurrency_safe: bool = False


class MCPMountSnapshot(BaseModel):
    """One validated replacement snapshot of an MCP server's mounted tools."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server_name: str
    mounted_names: tuple[str, ...]
    excluded_remote_names: tuple[str, ...]
    missing_grant_names: tuple[str, ...]
    removed_names: tuple[str, ...]


@runtime_checkable
class MCPClientPort(Protocol):
    """Synchronous Core port; protocol adapters own transport, pagination, and sessions."""

    server_name: str

    def snapshot_tools(self) -> tuple[MCPToolDescriptor, ...]: ...

    def invoke_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult: ...


class InProcessMCPAdapter:
    """Dependency-free MCP-shaped adapter retained for deterministic Core tests."""

    def __init__(self, *, server_name: str, registry: ToolRegistry) -> None:
        self.server_name = server_name
        self.registry = registry

    def list_tools(self) -> list[ToolSpec]:
        return self.registry.specs()

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        return self.registry.call(ToolCall(name=name, args=arguments))

    def snapshot_tools(self) -> tuple[MCPToolDescriptor, ...]:
        return tuple(
            MCPToolDescriptor(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema,
            )
            for spec in self.registry.specs()
        )

    def invoke_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult:
        result = self.call(name, arguments)
        content: list[dict[str, Any]] = []
        if result.output:
            content.append({"type": "text", "text": result.output})
        if result.error:
            content.append({"type": "text", "text": result.error})
        return MCPCallResult(
            content=tuple(content),
            is_error=result.status.casefold() not in _SUCCESS_STATUSES,
        )


class MCPToolMount:
    """Mount an authority-scoped MCP snapshot into native Tool and Capability registries."""

    def __init__(
        self,
        client: MCPClientPort,
        *,
        grants: Mapping[str, MCPToolGrant],
    ) -> None:
        _validate_name(client.server_name, label="server name")
        self.client = client
        self.grants = dict(grants)
        self.provider = f"mcp:{client.server_name}"
        self._mounted_names: set[str] = set()

    def refresh(
        self,
        registry: ToolRegistry,
        catalog: CapabilityCatalog,
    ) -> MCPMountSnapshot:
        descriptors = tuple(sorted(self.client.snapshot_tools(), key=lambda item: item.name))
        remote_names = [descriptor.name for descriptor in descriptors]
        if len(remote_names) != len(set(remote_names)):
            raise ValueError(f"duplicate MCP tool names from {self.client.server_name}")

        planned: list[
            tuple[str, ToolSpec, Callable[[dict[str, Any]], ToolResult], CapabilityDefinition]
        ] = []
        excluded: list[str] = []
        for descriptor in descriptors:
            grant = self.grants.get(descriptor.name)
            if grant is None:
                excluded.append(descriptor.name)
                continue
            local_name = self._local_name(descriptor.name)
            if registry.has(local_name) and local_name not in self._mounted_names:
                raise ValueError(f"MCP tool name collision: {local_name}")
            if catalog.has(local_name) and local_name not in self._mounted_names:
                raise ValueError(f"MCP capability name collision: {local_name}")
            planned.append(self._plan_tool(local_name, descriptor, grant))

        next_names = {item[0] for item in planned}
        removed_names = tuple(sorted(self._mounted_names - next_names))
        for name in sorted(self._mounted_names):
            registry.unregister(name)
            catalog.unregister(name)
        for local_name, spec, handler, definition in planned:
            registry.register(spec, handler)
            catalog.register(definition)
        self._mounted_names = next_names

        return MCPMountSnapshot(
            server_name=self.client.server_name,
            mounted_names=tuple(sorted(next_names)),
            excluded_remote_names=tuple(sorted(excluded)),
            missing_grant_names=tuple(sorted(set(self.grants) - set(remote_names))),
            removed_names=removed_names,
        )

    def _plan_tool(
        self,
        local_name: str,
        descriptor: MCPToolDescriptor,
        grant: MCPToolGrant,
    ) -> tuple[
        str,
        ToolSpec,
        Callable[[dict[str, Any]], ToolResult],
        CapabilityDefinition,
    ]:
        schema = _validated_input_schema(descriptor)
        description = _clean_description(descriptor.description, descriptor.name)
        tags = ["mcp", f"server:{self.client.server_name}", grant.side_effect_level]
        if grant.is_read_only:
            tags.append("read_only")
        if grant.is_destructive:
            tags.append("destructive")
        spec = ToolSpec(
            name=local_name,
            description=description,
            input_schema=schema,
            side_effect_level=grant.side_effect_level,
            approval_required=grant.approval_required,
            output_offload_policy=grant.output_offload_policy,
            is_read_only=grant.is_read_only,
            is_destructive=grant.is_destructive,
            is_concurrency_safe=grant.is_concurrency_safe,
        )
        definition = CapabilityDefinition(
            name=local_name,
            kind=CapabilityKind.MCP,
            description=description,
            tags=tags,
            input_schema=schema,
            provider=self.provider,
        )
        return (
            local_name,
            spec,
            self._handler(local_name, descriptor.name),
            definition,
        )

    def _handler(
        self,
        local_name: str,
        remote_name: str,
    ) -> Callable[[dict[str, Any]], ToolResult]:
        def handle(arguments: dict[str, Any]) -> ToolResult:
            try:
                result = self.client.invoke_tool(remote_name, arguments)
            except Exception as exc:  # A transport failure must become a durable Observation.
                return ToolResult(
                    name=local_name,
                    status="error",
                    error=f"MCP call failed: {type(exc).__name__}: {exc}"[:500],
                )
            payload: dict[str, Any] = {"content": list(result.content)}
            if result.structured_content is not None:
                payload["structured_content"] = result.structured_content
            return ToolResult(
                name=local_name,
                status="error" if result.is_error else "ok",
                output=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                error="MCP tool reported an error" if result.is_error else None,
            )

        return handle

    def _local_name(self, remote_name: str) -> str:
        _validate_name(remote_name, label="tool name")
        local_name = f"mcp.{self.client.server_name}.{remote_name}"
        if len(local_name) > 128:
            raise ValueError(f"namespaced MCP tool exceeds 128 characters: {local_name}")
        return local_name


def _validate_name(value: str, *, label: str) -> None:
    if not value or not _NAME_PATTERN.fullmatch(value):
        raise ValueError(f"invalid MCP {label}: {value!r}")


def _validated_input_schema(descriptor: MCPToolDescriptor) -> dict[str, Any]:
    schema = descriptor.input_schema
    if schema.get("type", "object") != "object":
        raise ValueError(f"MCP tool input schema must describe an object: {descriptor.name}")
    return schema


def _clean_description(description: str, remote_name: str) -> str:
    normalized = " ".join(description.split())
    return (normalized or f"MCP tool {remote_name}")[:1_000]
