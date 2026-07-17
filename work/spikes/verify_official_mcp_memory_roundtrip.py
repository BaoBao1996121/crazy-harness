import anyio
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

server = FastMCP("crazy-spike")

@server.tool()
def echo(text: str) -> str:
    return f"mcp:{text}"

async def verify() -> None:
    async with create_connected_server_and_client_session(server) as session:
        page = await session.list_tools()
        assert [tool.name for tool in page.tools] == ["echo"]
        result = await session.call_tool("echo", {"text": "proof"})
        assert result.isError is False and result.content[0].text == "mcp:proof"

anyio.run(verify)
print("official MCP in-memory roundtrip: ok")
