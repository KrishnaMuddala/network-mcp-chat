"""
Unified Chat Server
Connects Ollama + Cisco MCP + FORWARD MCP via a single web chat UI
Runs on http://localhost:9000
"""

import os
import sys
import json
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, Response, send_from_directory
from openai import AsyncOpenAI
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

OLLAMA_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
CISCO_MCP_URL     = os.getenv("CISCO_MCP_URL", "http://localhost:8001/mcp")
FORWARD_MCP_URL = os.getenv("FORWARD_MCP_URL", "http://localhost:8000/mcp")
TARGET_WEBSITE    = os.getenv("TARGET_WEBSITE", "http://localhost:5000")

logging.basicConfig(level=logging.WARNING)
for name in ["httpx", "httpcore", "openai", "mcp", "uvicorn", "fastmcp", "werkzeug"]:
    logging.getLogger(name).setLevel(logging.CRITICAL)

app = Flask(__name__, static_folder=".")

# ── Tool cache ────────────────────────────────────────────────────────────────
_tools_cache = []
_tools_loaded = False

async def load_all_tools():
    """Load tools from both MCP servers."""
    global _tools_cache, _tools_loaded
    tools = []

    # FORWARD tools
    try:
        async with streamablehttp_client(FORWARD_MCP_URL) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
                for t in result.tools:
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": f"forward__{t.name}",
                            "description": f"[FORWARD Security Tool] {t.description or ''}",
                            "parameters": t.inputSchema or {"type": "object", "properties": {}}
                        }
                    })
        print(f"[Server] Loaded {len(tools)} FORWARD tools")
    except Exception as e:
        print(f"[Server] FORWARD MCP not available: {e}")

    # Cisco tools
    cisco_start = len(tools)
    try:
        async with streamablehttp_client(CISCO_MCP_URL) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
                for t in result.tools:
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": f"cisco__{t.name}",
                            "description": f"[Cisco Switch Tool] {t.description or ''}",
                            "parameters": t.inputSchema or {"type": "object", "properties": {}}
                        }
                    })
        print(f"[Server] Loaded {len(tools) - cisco_start} Cisco tools")
    except Exception as e:
        print(f"[Server] Cisco MCP not available: {e}")

    _tools_cache = tools
    _tools_loaded = True
    return tools


async def call_tool(tool_name: str, tool_args: dict) -> str:
    """Route tool call to the correct MCP server."""
    if tool_name.startswith("forward__"):
        actual = tool_name.replace("forward__", "")
        try:
            async with streamablehttp_client(FORWARD_MCP_URL) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    result = await session.call_tool(actual, tool_args)
                    parts = [c.text for c in result.content if hasattr(c, "text")]
                    return "\n".join(parts)
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif tool_name.startswith("cisco__"):
        actual = tool_name.replace("cisco__", "")
        try:
            async with streamablehttp_client(CISCO_MCP_URL) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    result = await session.call_tool(actual, tool_args)
                    parts = [c.text for c in result.content if hasattr(c, "text")]
                    return "\n".join(parts)
        except Exception as e:
            return json.dumps({"error": str(e)})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


async def stream_chat(messages: list, tools: list):
    """Run agentic chat loop and yield SSE events."""
    client = AsyncOpenAI(base_url=OLLAMA_BASE_URL, api_key="EMPTY")

    while True:
        response = await client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=messages,
            tools=tools if tools else None,
            tool_choice="auto" if tools else None,
            temperature=0.7,
            stream=False,
        )

        choice = response.choices[0]
        message = choice.message

        if message.content:
            yield f"data: {json.dumps({'type': 'text', 'content': message.content})}\n\n"

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
                except Exception:
                    tool_args = {}

                # Emit tool call event
                display_name = tool_name.replace("forward__", "").replace("cisco__", "")
                yield f"data: {json.dumps({'type': 'tool_call', 'name': display_name, 'args': tool_args})}\n\n"

                # Execute tool
                result = await call_tool(tool_name, tool_args)

                # Parse and emit result
                try:
                    parsed = json.loads(result)
                    display = parsed.get("output", result) if parsed.get("status") == "success" else result
                except Exception:
                    display = result

                yield f"data: {json.dumps({'type': 'tool_result', 'name': display_name, 'result': display})}\n\n"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result
                })

            continue  # get final LLM summary

        # No tool calls — done
        messages.append({"role": "assistant", "content": message.content or ""})
        break

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "chat_ui.html")


@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    user_message = data.get("message", "")
    history = data.get("history", [])

    system_prompt = f"""You are a unified network security assistant with two capabilities:

# 1. **FORWARD Security Scanner** — scan websites for vulnerabilities using tools like nmap, nikto, nuclei, gobuster, sqlmap etc.
#    - The local website to scan is: {TARGET_WEBSITE}
#    - When asked to scan, test, or check vulnerabilities on a website, use FORWARD tools
#    - Always start with server_health to check available tools
#    - Use analyze_target_intelligence or nmap_scan as starting points

1.You are a network intelligence agent of **FORWARD Networks** with access to Forward Networks tools.

IMPORTANT RULES:
- To show data as a graph, call the data tool directly with output_format='graph'
  Example: get_device_basic_info(network_id='123', output_format='graph')
- Do NOT call generate_graph with a query string — it needs real JSON data
- Always call list_networks first to get a valid network_id before any other tool
- Never invent or guess network IDs — always look them up first
- For device inventory queries, use get_device_basic_info with the real network_id
- For hardware EOL queries, use get_hardware_support with the real network_id

2. **Cisco Switch** — query the Cisco C2960CX switch using read-only show commands
   - When asked about interfaces, VLANs, MAC tables, ARP, spanning tree etc., use Cisco tools
   - Use cisco_list_commands first if unsure what commands are available

Always explain what you are doing and present results clearly.
When scanning the local website, use {TARGET_WEBSITE} as the target automatically — don't ask the user for it."""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    tools = _tools_cache if _tools_loaded else []

    def generate():
        loop = asyncio.new_event_loop()
        try:
            async def run():
                async for event in stream_chat(messages, tools):
                    yield event
            async def collect():
                events = []
                async for e in run():
                    events.append(e)
                return events
            events = loop.run_until_complete(collect())
            for e in events:
                yield e
        finally:
            loop.close()

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/tools")
def list_tools():
    return {"tools": [t["function"]["name"] for t in _tools_cache], "count": len(_tools_cache)}


@app.route("/reload-tools")
def reload_tools():
    loop = asyncio.new_event_loop()
    tools = loop.run_until_complete(load_all_tools())
    loop.close()
    return {"loaded": len(tools)}


# ── Startup ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[Server] Loading tools from MCP servers...")
    loop = asyncio.new_event_loop()
    loop.run_until_complete(load_all_tools())
    loop.close()
    print(f"[Server] {len(_tools_cache)} tools loaded")
    print(f"[Server] Chat UI → http://localhost:9000")
    print(f"[Server] Target  → {TARGET_WEBSITE}")
    app.run(host="0.0.0.0", port=9000, debug=False)
