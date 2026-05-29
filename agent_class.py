# agent_class.py
import asyncio
import os
import json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openai import OpenAI

MCP_URL   = os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp")
LLM_URL   = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")

SYSTEM_PROMPT = """You are a network intelligence agent with access to Forward Networks tools.
You can query network inventory, trace paths, check hardware EOL status, run NQE queries,
search configs, and manage network locations.

When asked about the network:
1. Always call list_networks first if you don't have a network_id yet
2. Use get_device_basic_info to discover devices
3. Use search_paths to trace traffic flows
4. Use get_hardware_support to find EOL devices
5. Present results as markdown tables when possible
"""


class ForwardAgent:
    def __init__(self):
        self.openai   = OpenAI(base_url=LLM_URL, api_key="ollama")
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.tools    = []

    async def connect(self):
        """Load tools from MCP server once at startup."""
        async with streamablehttp_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                self.tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.inputSchema,
                        },
                    }
                    for t in result.tools
                ]
        print(f"✅ Loaded {len(self.tools)} tools from MCP server")

    async def chat(self, user_message: str, on_tool_call=None, on_text=None) -> str:
        """Send a message and run the agent loop. Returns final response."""
        self.messages.append({"role": "user", "content": user_message})

        async with streamablehttp_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                while True:
                    response = self.openai.chat.completions.create(
                        model=LLM_MODEL,
                        messages=self.messages,
                        tools=self.tools,
                        tool_choice="auto",
                        max_tokens=4096,
                    )

                    choice = response.choices[0]
                    self.messages.append({
                        "role": "assistant",
                        "content": choice.message.content,
                        "tool_calls": choice.message.tool_calls,
                    })

                    # Final response
                    if choice.finish_reason != "tool_calls":
                        final = choice.message.content or ""
                        if on_text:
                            on_text(final)
                        return final

                    # Execute tool calls
                    for tc in choice.message.tool_calls:
                        tool_name = tc.function.name
                        tool_args = json.loads(tc.function.arguments)

                        if on_tool_call:
                            on_tool_call(tool_name, tool_args)

                        result = await session.call_tool(tool_name, tool_args)
                        content = "\n".join(
                            c.text for c in result.content if hasattr(c, "text")
                        )

                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": content,
                        })

    def clear_history(self):
        """Reset conversation but keep system prompt."""
        self.messages = [self.messages[0]]


async def main():
    agent = ForwardAgent()
    await agent.connect()

    print("\nForward Networks Agent ready. Type 'exit' to quit, 'clear' to reset.\n")

    while True:
        user_input = input("> ").strip()

        if user_input.lower() in ("exit", "quit"):
            break
        if user_input.lower() == "clear":
            agent.clear_history()
            print("Conversation cleared.\n")
            continue
        if not user_input:
            continue

        await agent.chat(
            user_input,
            on_tool_call=lambda name, args: print(f"\n🔧 {name}({json.dumps(args)[:100]})"),
            on_text=lambda text: print(f"\n{text}\n"),
        )


if __name__ == "__main__":
    asyncio.run(main())