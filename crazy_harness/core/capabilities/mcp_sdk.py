from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager
from typing import Any, TypeVar

import anyio

from crazy_harness.core.capabilities.mcp import (
    MCPCallResult,
    MCPToolDescriptor,
)

_T = TypeVar("_T")


class SDKSessionMCPClient:
    """Official SDK adapter using a fresh initialized ClientSession per operation."""

    def __init__(
        self,
        *,
        server_name: str,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        max_list_pages: int = 100,
    ) -> None:
        if max_list_pages < 1:
            raise ValueError("max_list_pages must be positive")
        self.server_name = server_name
        self.session_factory = session_factory
        self.max_list_pages = max_list_pages

    def snapshot_tools(self) -> tuple[MCPToolDescriptor, ...]:
        async def collect() -> tuple[MCPToolDescriptor, ...]:
            from mcp.types import PaginatedRequestParams

            descriptors: list[MCPToolDescriptor] = []
            seen_cursors: set[str] = set()
            cursor: str | None = None
            async with self.session_factory() as session:
                for _ in range(self.max_list_pages):
                    if cursor is None:
                        page = await session.list_tools()
                    else:
                        page = await session.list_tools(
                            params=PaginatedRequestParams(cursor=cursor)
                        )
                    descriptors.extend(_descriptor(tool) for tool in page.tools)
                    next_cursor = getattr(page, "nextCursor", None)
                    if not next_cursor:
                        return tuple(descriptors)
                    if next_cursor in seen_cursors:
                        raise RuntimeError(
                            f"MCP tools/list repeated cursor: {next_cursor}"
                        )
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
            raise RuntimeError(
                f"MCP tools/list exceeded {self.max_list_pages} pages"
            )

        return _run_sync(collect)

    def invoke_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult:
        async def invoke() -> MCPCallResult:
            async with self.session_factory() as session:
                result = await session.call_tool(name, arguments)
            content = tuple(_content_block(item) for item in result.content)
            structured = getattr(result, "structuredContent", None)
            return MCPCallResult(
                content=content,
                structured_content=structured,
                is_error=bool(getattr(result, "isError", False)),
            )

        return _run_sync(invoke)


def _descriptor(tool: Any) -> MCPToolDescriptor:
    return MCPToolDescriptor(
        name=tool.name,
        description=tool.description or "",
        input_schema=dict(tool.inputSchema),
        output_schema=(
            dict(tool.outputSchema)
            if getattr(tool, "outputSchema", None) is not None
            else None
        ),
    )


def _content_block(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        payload = item.model_dump(by_alias=True, exclude_none=True)
    elif isinstance(item, dict):
        payload = dict(item)
    else:
        payload = {"type": "text", "text": str(item)}
    return _strip_private_metadata(payload)


def _strip_private_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_private_metadata(item)
            for key, item in value.items()
            if key != "_meta"
        }
    if isinstance(value, list):
        return [_strip_private_metadata(item) for item in value]
    return value


def _run_sync(operation: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return anyio.run(operation)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(anyio.run, operation).result()
