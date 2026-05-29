import sys
import asyncio
from typing import Optional, Any
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import json
from pydantic import AnyUrl


class MCPClient:
    def __init__(
        self,
        command: str,
        args: list[str],
        env: Optional[dict] = None,
    ):
        self._command = command
        self._args = args
        self._env = env
        self._session: Optional[ClientSession] = None
        self._exit_stack: AsyncExitStack = AsyncExitStack()

    async def connect(self):
        server_params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
        )
        stdio_transport = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        _stdio, _write = stdio_transport
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(_stdio, _write)
        )
        await self._session.initialize()

    def session(self) -> ClientSession:
        if self._session is None:
            raise ConnectionError(
                "Client session not initialized. Call connect() first."
            )
        return self._session

    async def list_tools(self) -> list[types.Tool]:
        result = await self.session().list_tools()
        return result.tools

    async def call_tool(
        self, tool_name: str, tool_input: dict
    ) -> types.CallToolResult | None:
        return await self.session().call_tool(tool_name, tool_input)

    async def list_prompts(self) -> list[types.Prompt]:
        result = await self.session().list_prompts()
        return result.prompts

    async def get_prompt(self, prompt_name, args: dict[str, str]):
        result = await self.session().get_prompt(prompt_name, args)
        return result.messages

    async def read_resource(self, uri: str) -> Any:
        result = await self.session().read_resource(AnyUrl(uri))
        resource = result.contents[0]
        if isinstance(resource, types.TextResourceContents):
            if resource.mimeType == "application/json":
                return json.loads(resource.text)
            return resource.text

    async def cleanup(self):
        await self._exit_stack.aclose()
        self._session = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()


# ─────────────────────────────────────────────
# Test: Cisco C2960CX MCP Server
# ─────────────────────────────────────────────
async def main():
    print("Starting...")
    # ── Update these ──────────────────────────
    SWITCH_HOST     = "xx.xx.xx.xx"   # your switch IP
    SWITCH_USER     = "username"         # your SSH username
    SWITCH_PASS     = "password"      # your SSH password
    SWITCH_PORT     = 22              # SSH port (default 22)
    # ─────────────────────────────────────────

    async with MCPClient(
        command="uv",
        args=["run", "cisco_c2960cx_mcp.py"],   # path to your MCP server file
    ) as client:
        print("Connected to MCP server!")
        # 1. List all available tools
        print("\n=== Available Tools ===")
        tools = await client.list_tools()
        print(f"Found {len(tools)} tools:")
        for tool in tools:
            print(f"  • {tool.name}: {tool.description}")

        # 2. List all allowed read-only commands
        print("\n=== Allowed Commands ===")
        result = await client.call_tool("cisco_list_commands", {})
        for content in result.content:
            data = json.loads(content.text)
            for cmd in data["allowed_commands"]:
                print(f"  • {cmd}")

        # 3. Run: show version
        print("\n=== show version ===")
        result = await client.call_tool("cisco_show", {
            "host":     SWITCH_HOST,
            "username": SWITCH_USER,
            "password": SWITCH_PASS,
            "port":     SWITCH_PORT,
            "command":  "show version"
        })
        for content in result.content:
            data = json.loads(content.text)
            if data["status"] == "success":
                print(data["output"])
            else:
                print(f"Error: {data}")

        # 4. Run: show interfaces status
        print("\n=== show interfaces status ===")
        result = await client.call_tool("cisco_show", {
            "host":     SWITCH_HOST,
            "username": SWITCH_USER,
            "password": SWITCH_PASS,
            "port":     SWITCH_PORT,
            "command":  "show interfaces status"
        })
        for content in result.content:
            data = json.loads(content.text)
            if data["status"] == "success":
                print(data["output"])
            else:
                print(f"Error: {data}")

        # 5. Run: show vlan brief
        print("\n=== show vlan brief ===")
        result = await client.call_tool("cisco_show", {
            "host":     SWITCH_HOST,
            "username": SWITCH_USER,
            "password": SWITCH_PASS,
            "port":     SWITCH_PORT,
            "command":  "show vlan brief"
        })
        for content in result.content:
            data = json.loads(content.text)
            if data["status"] == "success":
                print(data["output"])
            else:
                print(f"Error: {data}")

        # 6. Test blocked command (should be rejected)
        print("\n=== Test: blocked command (configure terminal) ===")
        result = await client.call_tool("cisco_show", {
            "host":     SWITCH_HOST,
            "username": SWITCH_USER,
            "password": SWITCH_PASS,
            "port":     SWITCH_PORT,
            "command":  "configure terminal"   # ← should be blocked
        })
        for content in result.content:
            data = json.loads(content.text)
            print(f"  Status : {data['status']}")
            print(f"  Reason : {data.get('reason', '')}")

# For testing
async def main():
    async with MCPClient(
        # If using Python without UV, update command to 'python' and remove "run" from args.
        command="uv",
        args=["run", "mcp_cisco_c2960cx_server.py"],
    ) as _client:
        pass


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())

