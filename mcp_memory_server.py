"""
MCP Server: Network Memory (entity-based knowledge store)
Runs on :8002 — provides cross-conversation memory for the network assistant.
"""

import json
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

mcp = FastMCP("MemoryMCP", log_level="ERROR", host="0.0.0.0", port=8002)

# ── In-memory store ───────────────────────────────────────────────────────
# entity: { id, name, entity_type, observations: [ {content, timestamp} ] }
_entities = {}


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _find_entities(query: str) -> list[dict]:
    q = query.strip().lower()
    results = []
    for ent in _entities.values():
        haystack = (ent["name"] + " " + ent.get("entity_type", "")).lower()
        for obs in ent["observations"]:
            haystack += " " + obs["content"].lower()
        if not q or q in haystack:
            results.append(ent)
    return results


@mcp.tool(
    name="search_memory",
    description=(
        "Search the network knowledge base for entities relevant to a query. "
        "Returns matching entities with their names, types and stored observations."
    ),
)
def search_memory(query: str) -> list[TextContent]:
    results = _find_entities(query or "")
    payload = {
        "query": query,
        "count": len(results),
        "entities": [
            {
                "id": e["id"],
                "name": e["name"],
                "entity_type": e.get("entity_type", ""),
                "observations": e["observations"],
            }
            for e in results
        ],
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


@mcp.tool(
    name="create_entity",
    description=(
        "Create a new entity (e.g. a network device, network, VLAN, host, issue) "
        "in the knowledge base. Returns the entity id."
    ),
)
def create_entity(
    name: str,
    entity_type: str = "device",
    observations: list[str] | None = None,
) -> list[TextContent]:
    entity_id = f"entity-{len(_entities) + 1}"
    _entities[entity_id] = {
        "id": entity_id,
        "name": name,
        "entity_type": entity_type,
        "observations": [
            {"content": obs, "timestamp": _now()}
            for obs in (observations or [])
        ],
    }
    return [TextContent(
        type="text",
        text=json.dumps({
            "id": entity_id,
            "name": name,
            "entity_type": entity_type,
            "observation_count": len(_entities[entity_id]["observations"]),
        }, indent=2),
    )]


@mcp.tool(
    name="add_observation",
    description=(
        "Add observations (facts) to an existing entity, creating it by name "
        "if it does not yet exist. Also accepts entity_id to target a specific entity."
    ),
)
def add_observation(
    content: str,
    entity_name: str = "",
    entity_id: str = "",
    entity_type: str = "device",
) -> list[TextContent]:
    if entity_id and entity_id in _entities:
        ent = _entities[entity_id]
    elif entity_name:
        entity_id = f"entity-{len(_entities) + 1}"
        ent = {
            "id": entity_id,
            "name": entity_name,
            "entity_type": entity_type,
            "observations": [],
        }
        _entities[entity_id] = ent
    else:
        return [TextContent(
            type="text",
            text=json.dumps({"error": "Provide entity_name or a valid entity_id."}, indent=2),
        )]

    ent["observations"].append({"content": content, "timestamp": _now()})
    return [TextContent(
        type="text",
        text=json.dumps({
            "entity_id": ent["id"],
            "entity_name": ent["name"],
            "observation_count": len(ent["observations"]),
            "added": content,
        }, indent=2),
    )]


@mcp.tool(
    name="list_all_entities",
    description="List all entities currently stored in memory.",
)
def list_all_entities() -> list[TextContent]:
    payload = {
        "count": len(_entities),
        "entities": [
            {"id": e["id"], "name": e["name"], "entity_type": e.get("entity_type", "")}
            for e in _entities.values()
        ],
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
