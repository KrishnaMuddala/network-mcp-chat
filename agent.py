# agent.py
import asyncio
import os
import json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()

MCP_URL   = os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp")
LLM_URL   = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")

openai = OpenAI(base_url=LLM_URL, api_key="ollama")
print(f"   ✅ using model: {LLM_MODEL}...")

async def run_agent(user_message: str):
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. Get all tools from MCP server
            tools_result = await session.list_tools()
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.inputSchema,
                    },
                }
                for t in tools_result.tools
            ]

            messages = [{"role": "user", "content": user_message}]

            # 2. Agent loop
            while True:
                response = openai.chat.completions.create(
                    model=LLM_MODEL,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=4096,
                )

                choice = response.choices[0]
                messages.append({
                    "role": "assistant",
                    "content": choice.message.content,
                    "tool_calls": choice.message.tool_calls,
                })

                # 3. No tool calls → done
                if choice.finish_reason != "tool_calls":
                    print("\nAgent:", choice.message.content)
                    break

                # 4. Execute each tool call
                for tc in choice.message.tool_calls:
                    tool_name = tc.function.name
                    tool_args = json.loads(tc.function.arguments)

                    print(f"\n🔧 Calling: {tool_name}")
                    print(f"   Args: {json.dumps(tool_args, indent=2)}")

                    result = await session.call_tool(tool_name, tool_args)
                    content = "\n".join(
                        c.text for c in result.content if hasattr(c, "text")
                    )

                    print(f"   ✅ Result: {content[:200]}...")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content,
                    })


async def main():
    print("Forward Networks Agent — type 'exit' to quit\n")
    while True:
        user_input = input("> ").strip()
        if user_input.lower() in ("exit", "quit"):
            break
        if not user_input:
            continue
        await run_agent(user_input)


if __name__ == "__main__":
    asyncio.run(main())