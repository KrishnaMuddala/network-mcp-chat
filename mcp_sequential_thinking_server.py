"""
MCP Server: Sequential Thinking
Runs on :8003 — drives structured, step-by-step reasoning for complex problems.
"""

import json
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

mcp = FastMCP("SequentialThinkingMCP", log_level="ERROR", host="0.0.0.0", port=8003)

_state = {
    "active": False,
    "thoughts": [],
    "current_number": 0,
    "total": 0,
}


def _summary() -> dict:
    return {
        "active": _state["active"],
        "current_number": _state["current_number"],
        "total_thoughts": _state["total"],
        "thoughts": _state["thoughts"],
    }


@mcp.tool(
    name="sequential_thinking",
    description=(
        "Use structured, step-by-step reasoning to break down and solve complex, "
        "multi-step problems (troubleshooting, root cause analysis, planning). "
        "Set should_continue=True to keep adding steps; the model maintains a "
        "chain of thoughts. Set should_continue=False to finish."
    ),
)
def sequential_thinking(
    thought: str,
    thought_number: int = 0,
    total_thoughts: int = 0,
    next_thought_needed: bool = True,
    branch: str = "",
    needs_revision: bool = False,
    revision_reason: str = "",
    branches_taken: list[str] | None = None,
    branch_result: str = "",
) -> list[TextContent]:
    now = datetime.utcnow().isoformat() + "Z"
    entry = {
        "number": thought_number,
        "thought": thought,
        "branch": branch,
        "needs_revision": needs_revision,
        "revision_reason": revision_reason,
        "timestamp": now,
    }
    _state["thoughts"].append(entry)
    _state["current_number"] = thought_number or len(_state["thoughts"])
    if total_thoughts:
        _state["total"] = total_thoughts
    _state["active"] = bool(next_thought_needed)

    payload = {
        "status": "continue" if next_thought_needed else "completed",
        "current_thought": entry,
        "thought_chain": _state["thoughts"],
        "should_continue": next_thought_needed,
        "suggestion": (
            "Continue the reasoning chain with the next thought."
            if next_thought_needed
            else "Reasoning complete — synthesize the final answer."
        ),
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
