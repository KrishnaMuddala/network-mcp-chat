# autonomous_agent.py
import asyncio
import os
import json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openai import OpenAI

MCP_URL   = os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp")
LLM_URL   = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
MAX_STEPS = 10  # prevent infinite loops

SYSTEM_PROMPT = """You are an autonomous network intelligence agent.

You have access to Forward Networks tools. For complex tasks:
1. PLAN — break the task into steps
2. EXECUTE — call tools step by step
3. SYNTHESIZE — combine results into a clear answer

Always:
- Call list_networks first to get network_id if you don't have one
- Use output_format='graph' for topology/path questions
- Use output_format='table' for inventory/compliance questions
- If a tool fails, try an alternative approach
- Be concise in your final answer

Current date: {date}
"""

import datetime

async def run_autonomous_agent(task: str, verbose: bool = True):
    """Run an autonomous agent that plans and executes multi-step tasks."""

    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Load tools
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

            openai_client = OpenAI(base_url=LLM_URL, api_key="ollama")

            messages = [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(date=datetime.date.today()),
                },
                {"role": "user", "content": task},
            ]

            steps = 0

            if verbose:
                print(f"\n🤖 Task: {task}")
                print(f"🔧 Tools available: {len(tools)}\n")

            while steps < MAX_STEPS:
                steps += 1

                response = openai_client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=4096,
                )

                choice = response.choices[0]

                # Thinking / text output
                if choice.message.content and verbose:
                    print(f"💭 {choice.message.content[:300]}")

                messages.append({
                    "role": "assistant",
                    "content": choice.message.content,
                    "tool_calls": choice.message.tool_calls,
                })

                # Done
                if choice.finish_reason != "tool_calls":
                    if verbose:
                        print(f"\n✅ Final answer (after {steps} steps):")
                        print(choice.message.content)
                    return choice.message.content

                # Execute tools
                for tc in choice.message.tool_calls:
                    tool_name = tc.function.name
                    tool_args = json.loads(tc.function.arguments)

                    if verbose:
                        print(f"\n  → Step {steps}: {tool_name}")
                        print(f"    {json.dumps(tool_args)[:150]}")

                    try:
                        result = await session.call_tool(tool_name, tool_args)
                        content = "\n".join(
                            c.text for c in result.content if hasattr(c, "text")
                        )
                        if verbose:
                            print(f"    ✓ {content[:200]}")
                    except Exception as e:
                        content = f"Error calling {tool_name}: {e}"
                        if verbose:
                            print(f"    ✗ {content}")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content,
                    })

            return "Max steps reached without completing the task."


async def main():
    # Example complex tasks the agent handles autonomously
    tasks = [
        "List all networks and show me the device inventory for the first one",
        "Find all EOL hardware in the network and show as a timeline graph",
        "Trace the path from 192.168.1.10 to 10.0.0.1 on port 443 and show as a graph",
        "Search for 'BGP' in device configs and summarize what you find",
    ]

    print("Forward Networks Autonomous Agent")
    print("="*50)
    print("Example tasks:")
    for i, t in enumerate(tasks, 1):
        print(f"  {i}. {t}")
    print("\nOr type your own task:")

    user_input = input("\n> ").strip()

    if user_input.isdigit() and 1 <= int(user_input) <= len(tasks):
        task = tasks[int(user_input) - 1]
    else:
        task = user_input

    await run_autonomous_agent(task)


if __name__ == "__main__":
    asyncio.run(main())