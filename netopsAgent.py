"""
netops_agent.py
Minimal Planner -> Executor -> Verifier -> Responder agent loop
using local Ollama (qwen2.5:7b) + your existing MCP servers
(mcp_ciscoserver.py, hexstrike_mcp.py, mcp_fwdnetworkserver.py)
over streamable HTTP transport.

Run:
    python netops_agent.py
"""

import asyncio
import json
import requests
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b"

MCP_SERVERS = {
    "cisco":            "http://localhost:8001/mcp",
    # "hexstrike":        "http://localhost:8000/mcp",
    "forward_networks": "http://localhost:8000/mcp",
}

# Forward Networks network id - set this to your actual network
FWD_NETWORK_ID = "183751"


# ---------------------------------------------------------------------
# Ollama helper
# ---------------------------------------------------------------------
def llm(messages, force_json=False):
    payload = {"model": MODEL, "messages": messages, "stream": False}
    if force_json:
        payload["format"] = "json"
    r = requests.post(OLLAMA_URL, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"]


# ---------------------------------------------------------------------
# MCP session wrapper - opens a session per server, on demand
# ---------------------------------------------------------------------
class MCPRouter:
    def __init__(self, servers: dict):
        self.servers = servers
        self._sessions = {}
        self._tool_index = {}   # tool_name -> server_key

    async def connect(self):
        for key, url in self.servers.items():
            ctx = streamablehttp_client(url)
            read, write, _ = await ctx.__aenter__()
            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()
            tools = await session.list_tools()
            for t in tools.tools:
                self._tool_index[t.name] = key
            self._sessions[key] = (session, ctx)
            print(f"[connected] {key}: {len(tools.tools)} tools")

    def all_tools_schema(self):
        """Flatten tool schemas across servers for the planner prompt."""
        schema = []
        for key, (session, _) in self._sessions.items():
            # tools were already listed at connect time; re-fetch lightweight
            pass
        return list(self._tool_index.keys())

    async def call_tool(self, name, args):
        key = self._tool_index.get(name)
        if not key:
            return {"error": f"Unknown tool: {name}"}
        session, _ = self._sessions[key]
        result = await session.call_tool(name, args)
        # mcp result content -> plain text/json
        out = []
        for block in result.content:
            if hasattr(block, "text"):
                out.append(block.text)
        return "\n".join(out)

    async def close(self):
        for key, (session, ctx) in self._sessions.items():
            await session.__aexit__(None, None, None)
            await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------
# Agent loop: Planner -> Executor -> Verifier -> Responder
# ---------------------------------------------------------------------
def plan(query, tool_names, history_note=""):
    prompt = f"""You are a network operations planning agent.
Available tools (call exactly one per step): {json.dumps(tool_names)}

Known prerequisites:
- Before any Forward Networks path search (search_paths / search_paths_bulk),
  you MUST first ensure the network is selected. If no network has been set
  this session, first call "set_default_network" with
  {{"network_id": "{FWD_NETWORK_ID}"}}.

User query: {query}
{history_note}

Respond with ONLY valid JSON: {{"tool": "<tool_name>", "args": {{...}}}}
"""
    raw = llm([{"role": "user", "content": prompt}], force_json=True)
    return json.loads(raw)


def verify(query, tool_used, result):
    prompt = f"""User query: {query}
Tool called: {tool_used}
Tool result:
{result}

Does this result accurately and completely answer the user's query?
Respond with ONLY valid JSON: {{"ok": true|false, "reason": "<short reason if false>"}}
"""
    raw = llm([{"role": "user", "content": prompt}], force_json=True)
    return json.loads(raw)


def respond(query, result):
    prompt = f"""User query: {query}
Tool result (ground truth - use ONLY this, do not invent extra detail):
{result}

Write a clear, concise answer for the user based strictly on the data above.
"""
    return llm([{"role": "user", "content": prompt}])


async def run_query(router: MCPRouter, query: str, max_retries=2):
    tool_names = router.all_tools_schema()
    history_note = ""
    network_initialized = False

    for attempt in range(max_retries + 1):
        # Hardcoded prerequisite step (not left to the LLM)
        if not network_initialized and "set_default_network" in tool_names:
            await router.call_tool(
                "set_default_network", {"network_id": FWD_NETWORK_ID}
            )
            network_initialized = True

        p = plan(query, tool_names, history_note)
        print(f"[plan] {p}")

        result = await router.call_tool(p["tool"], p.get("args", {}))
        print(f"[result] {result[:300]}...")

        v = verify(query, p["tool"], result)
        print(f"[verify] {v}")

        if v.get("ok"):
            return respond(query, result)

        history_note = (
            f"\nPrevious attempt used tool '{p['tool']}' with args "
            f"{p.get('args')} but failed: {v.get('reason')}. "
            f"Try different tool/args (e.g. use 'search_paths_bulk' instead "
            f"of 'search_paths' if applicable)."
        )

    return "Unable to get an accurate result after retries. " \
           "Check MCP server logs / network_id configuration."


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------
async def main():
    router = MCPRouter(MCP_SERVERS)
    await router.connect()

    try:
        while True:
            query = input("\nQuery (or 'exit'): ").strip()
            if query.lower() in ("exit", "quit"):
                break
            answer = await run_query(router, query)
            print(f"\n=== ANSWER ===\n{answer}\n")
    finally:
        await router.close()


if __name__ == "__main__":
    asyncio.run(main())