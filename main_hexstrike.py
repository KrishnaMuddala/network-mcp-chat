#!/usr/bin/env python3
"""
HexStrike AI MCP Client
Connects to hexstrike_mcp.py via streamable HTTP transport.

Startup order:
  Terminal 1: python hexstrike_server.py     (Flask API :8888)
  Terminal 2: python hexstrike_mcp.py        (MCP HTTP server :8000)
  Terminal 3: node server.js                 (Web UI :3000)
  Browser:    http://localhost:3000
"""

import sys
import os
import asyncio
import json
import logging
from pathlib import Path
from dotenv import load_dotenv
from openai import AsyncOpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# ── Encoding fix for Windows ──────────────────────────────────────────────────
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

# ── Load .env ─────────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

OLLAMA_BASE_URL     = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL        = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
HEXSTRIKE_MCP_URL   = os.getenv("HEXSTRIKE_MCP_URL", "http://localhost:8000/mcp")
HEXSTRIKE_API_URL   = os.getenv("HEXSTRIKE_API_URL", "http://localhost:8888")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

# Suppress noisy loggers
for name in ["httpx", "httpcore", "openai", "mcp", "uvicorn", "fastmcp"]:
    logging.getLogger(name).setLevel(logging.CRITICAL)


async def get_mcp_tools(session: ClientSession) -> list:
    """Fetch tools from HexStrike MCP server and convert to OpenAI format."""
    result = await session.list_tools()
    tools = []
    for tool in result.tools:
        tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema if tool.inputSchema else {
                    "type": "object",
                    "properties": {}
                }
            }
        })
    return tools


async def call_mcp_tool(session: ClientSession, tool_name: str, tool_args: dict) -> str:
    """Call a HexStrike MCP tool and return result as string."""
    try:
        result = await session.call_tool(tool_name, tool_args)
        if result.content:
            parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
            return "\n".join(parts)
        return "Tool executed with no output."
    except Exception as e:
        return f"Tool error: {str(e)}"


async def chat_loop(session: ClientSession, tools: list):
    """Main chat loop connecting Ollama + HexStrike MCP tools."""
    client = AsyncOpenAI(
        base_url=OLLAMA_BASE_URL,
        api_key="EMPTY"
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are HexStrike AI, an advanced cybersecurity assistant. "
                "You have access to 150+ security tools including nmap, nuclei, gobuster, sqlmap, "
                "metasploit, nikto, and many more via the HexStrike MCP server. "
                "When the user asks to scan, test, or analyze a target, use the appropriate tools. "
                "Always explain what you are doing and summarize the results clearly."
            )
        }
    ]

    session_history = InMemoryHistory()
    prompt_session = PromptSession(history=session_history)

    tool_names = [t["function"]["name"] for t in tools]
    print(f"\n[HexStrike AI] Connected — {len(tools)} tools available")
    print(f"[HexStrike AI] Model: {OLLAMA_MODEL}")
    print(f"[HexStrike AI] Type 'exit' or 'quit' to stop\n")

    while True:
        try:
            user_input = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: prompt_session.prompt("You: ")
            )
        except (EOFError, KeyboardInterrupt):
            print("\n[HexStrike AI] Goodbye!")
            break

        user_input = user_input.strip()
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("[HexStrike AI] Goodbye!")
            break

        messages.append({"role": "user", "content": user_input})

        # ── Agentic loop: keep calling tools until done ───────────────────
        while True:
            try:
                response = await client.chat.completions.create(
                    model=OLLAMA_MODEL,
                    messages=messages,
                    tools=tools if tools else None,
                    tool_choice="auto" if tools else None,
                    temperature=0.7,
                )
            except Exception as e:
                print(f"\n[Error] LLM call failed: {e}")
                break

            choice = response.choices[0]
            message = choice.message

            # ── Text response ─────────────────────────────────────────────
            if message.content:
                print(f"\nHexStrike AI: {message.content}\n")

            # ── Tool calls ────────────────────────────────────────────────
            if message.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in message.tool_calls
                    ]
                })

                for tc in message.tool_calls:
                    tool_name = tc.function.name
                    try:
                        tool_args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}

                    print(f"\n[Tool] {tool_name}({json.dumps(tool_args, indent=2)})")

                    tool_result = await call_mcp_tool(session, tool_name, tool_args)

                    # Pretty print result
                    try:
                        parsed = json.loads(tool_result)
                        if parsed.get("status") == "success" and "output" in parsed:
                            print(f"[Result]\n{parsed['output']}\n")
                        else:
                            print(f"[Result] {json.dumps(parsed, indent=2)}\n")
                    except Exception:
                        print(f"[Result] {tool_result}\n")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result
                    })

                # Continue loop to get final LLM summary
                continue

            # ── No more tool calls — done ─────────────────────────────────
            messages.append({
                "role": "assistant",
                "content": message.content or ""
            })
            break


async def main():
    """Connect to HexStrike MCP server and start chat loop."""
    print(f"[HexStrike AI] Connecting to MCP server at {HEXSTRIKE_MCP_URL}...")

    try:
        async with streamablehttp_client(HEXSTRIKE_MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print(f"[HexStrike AI] MCP server connected!")

                tools = await get_mcp_tools(session)
                await chat_loop(session, tools)

    except Exception as e:
        print(f"\n[Error] Could not connect to HexStrike MCP server at {HEXSTRIKE_MCP_URL}")
        print(f"[Error] {e}")
        print("\nMake sure both servers are running:")
        print("  Terminal 1: python hexstrike_server.py   (Flask API :8888)")
        print("  Terminal 2: python hexstrike_mcp.py      (MCP server :8000)")
        sys.exit(1)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())