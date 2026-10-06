"""Check wire compatibility with the official MCP client without device actions."""

import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_official_client_round_trip():
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ios_control_mcp"],
        env={"IOS_CONTROL_TRANSPORT": "usb", "IOS_CONTROL_TIMEOUT": "60"},
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            waited = await session.call_tool("iphone_wait", {"ms": 0})
            rejected = await session.call_tool("iphone_tap", {"x": 100, "y": 0.5})
            assert initialized.model_dump(by_alias=True)["protocolVersion"] == "2024-11-05"
            assert len(tools.tools) == 9
            assert waited.model_dump(by_alias=True)["isError"] is False
            assert rejected.model_dump(by_alias=True)["isError"] is True
