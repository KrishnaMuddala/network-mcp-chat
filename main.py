import asyncio
import sys
import os
from dotenv import load_dotenv
from contextlib import AsyncExitStack

from mcp_client import MCPClient
from core.openai_localllm import Openai_LocalLLM

from core.cli_chat import CliChat
from core.cli import CliApp

load_dotenv()

# Local LLM config  (Ollama by default)
local_model = os.getenv("LOCAL_LLM_MODEL", "llama3.2")
local_base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")

print(f"Using local model: {local_model}  ({local_base_url})")


async def main():
    openai_service = Openai_LocalLLM(model=local_model)

    server_scripts = sys.argv[1:]
    clients = {}

    command, args = (
        ("uv", ["run", "mcp_server.py"])
        if os.getenv("USE_UV", "0") == "1"
        else ("python", ["mcp_server.py"])
    )

    async with AsyncExitStack() as stack:
        doc_client = await stack.enter_async_context(
            MCPClient(command=command, args=args)
        )
        clients["doc_client"] = doc_client

        for i, server_script in enumerate(server_scripts):
            client_id = f"client_{i}_{server_script}"
            client = await stack.enter_async_context(
                MCPClient(command="uv", args=["run", server_script])
            )
            clients[client_id] = client

        chat = CliChat(
            doc_client=doc_client,
            clients=clients,
            openai_service=openai_service,
        )

        cli = CliApp(chat)
        await cli.initialize()
        await cli.run()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
