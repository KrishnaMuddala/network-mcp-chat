import os
from dotenv import load_dotenv
import httpx
from datetime import date
from pydantic import Field
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base

mcp = FastMCP("ForwardMCP", log_level="ERROR")

load_dotenv()

# ── Forward Networks config ───────────────────────────────────────────────

FORWARD_BASE_URL = os.getenv("FORWARD_API_BASE_URL", "").rstrip("/")
FORWARD_API_KEY = os.getenv("FORWARD_API_KEY", "")
FORWARD_API_SECRET = os.getenv("FORWARD_API_SECRET", "")
FORWARD_INSECURE = os.getenv("FORWARD_INSECURE_SKIP_VERIFY", "false").lower() == "true"


def _check_config():
    if not FORWARD_BASE_URL or not FORWARD_API_KEY:
        raise ValueError(
            "FORWARD_API_BASE_URL and FORWARD_API_KEY must be set in environment"
        )


def _forward_client() -> httpx.Client:
    """Returns an authenticated httpx client for Forward Networks API."""
    return httpx.Client(
        auth=(FORWARD_API_KEY, FORWARD_API_SECRET),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        verify=not FORWARD_INSECURE,
        timeout=60,
    )


def _get_latest_snapshot(client: httpx.Client, network_id: str) -> str:
    """Gets the latest processed snapshot ID for a network."""
    try:
        r = client.get(f"{FORWARD_BASE_URL}/api/networks/{network_id}/snapshots")
        r.raise_for_status()
        snapshots = r.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Failed to get snapshots for network {network_id}: "
            f"HTTP {e.response.status_code} — {e.response.text[:200]}"
        )
    except Exception as e:
        raise RuntimeError(
            f"Failed to get snapshots for network {network_id}: {type(e).__name__}: {e}"
        )

    if not snapshots:
        raise ValueError(f"No snapshots found for network {network_id}")

    # Handle both list and dict responses
    if isinstance(snapshots, list):
        return snapshots[0]["id"]
    elif isinstance(snapshots, dict) and "snapshots" in snapshots:
        return snapshots["snapshots"][0]["id"]
    elif isinstance(snapshots, dict) and "items" in snapshots:
        return snapshots["items"][0]["id"]
    else:
        raise ValueError(f"Unexpected snapshot response format: {str(snapshots)[:200]}")

def _forward_request(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    try:
        r = client.request(method, url, **kwargs)
        r.raise_for_status()
        # Handle empty responses
        if not r.content:
            return {}
        return r.json()
    except httpx.ConnectError as e:
        raise RuntimeError(f"Cannot connect to {FORWARD_BASE_URL}: {e}")
    except httpx.TimeoutException as e:
        raise RuntimeError(f"Request timed out: {url}: {e}")
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"HTTP {e.response.status_code} from {url}\n"
            f"Request body: {str(kwargs.get('json', ''))[:200]}\n"
            f"Response: {e.response.text[:300]}"
        )
    except Exception as e:
        raise RuntimeError(f"Unexpected error calling {url}: {type(e).__name__}: {e}")
# ── Output formatters ─────────────────────────────────────────────────────
def _clean(s: str) -> str:
    return str(s).replace("-", "_").replace(".", "_").replace(" ", "_").replace("/", "_")

def _to_markdown_table(data) -> str:
    """Converts Forward Networks API response to a markdown table."""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "items" in data:
        items = data["items"]
    elif isinstance(data, dict) and "paths" in data:
        # Flatten paths to hops for table view
        items = []
        for i, path in enumerate(data["paths"]):
            for j, hop in enumerate(path.get("hops", [])):
                hop["pathIndex"] = i
                hop["hopIndex"] = j
                items.append(hop)
    else:
        return str(data)

    if not items:
        return "No results found."

    headers = list(items[0].keys())
    header_row = "| " + " | ".join(headers) + " |"
    separator  = "| " + " | ".join(["---"] * len(headers)) + " |"
    rows = [
        "| " + " | ".join(str(item.get(h, "")) for h in headers) + " |"
        for item in items
    ]
    return "\n".join([header_row, separator] + rows)


def _to_mermaid_graph(data, graph_type: str = "path") -> str:
    """Converts Forward Networks API response to a Mermaid diagram string."""

    # ── Path tracing → flowchart ──────────────────────────────────────────
    if graph_type == "path":
        paths = data.get("paths", []) if isinstance(data, dict) else []
        if not paths:
            return "No path data found."

        lines = ["flowchart LR"]
        seen_links = set()
        for path in paths:
            hops = path.get("hops", [])
            for i in range(len(hops) - 1):
                src = hops[i].get("deviceName", f"hop{i}").replace("-", "_").replace(".", "_")
                dst = hops[i + 1].get("deviceName", f"hop{i+1}").replace("-", "_").replace(".", "_")
                iface = hops[i].get("egressInterface", "")
                link_key = f"{src}->{dst}"
                if link_key not in seen_links:
                    seen_links.add(link_key)
                    if iface:
                        lines.append(f'    {src} -->|"{iface}"| {dst}')
                    else:
                        lines.append(f"    {src} --> {dst}")
        return "\n".join(lines)

    # ── Device inventory → grouped by platform ────────────────────────────
    if graph_type == "inventory":
        items = data.get("items", []) if isinstance(data, dict) else data
        if not items:
            return "No inventory data found."

        lines = ["flowchart TD"]
        platforms: dict[str, list[str]] = {}
        for item in items:
            platform = str(item.get("platform", "Unknown")).replace("-", "_").replace(" ", "_")
            device = str(item.get("name", "unknown")).replace("-", "_").replace(".", "_")
            platforms.setdefault(platform, []).append(device)

        for platform, devices in platforms.items():
            lines.append(f"    subgraph {platform}")
            for d in devices:
                lines.append(f"        {d}[{d}]")
            lines.append("    end")
        return "\n".join(lines)

    # ── Hardware EOL → timeline ───────────────────────────────────────────
    if graph_type == "eol":
     items = data.get("items", []) if isinstance(data, dict) else data
    if not items:
        return "No EOL data found."

    # Group by EOL year — flowchart renders far better than timeline
    lines = ["flowchart TD"]
    by_year: dict = {}
    for item in items:
        eol  = item.get("endOfLife", "")
        year = eol[:4] if eol else "Unknown"
        device = _clean(str(item.get("deviceName", "unknown")))
        model  = str(item.get("model", ""))
        label  = f"{device}\\n{model}" if model else device
        by_year.setdefault(year, []).append((device, label))

    for year, devices in sorted(by_year.items()):
        year_id = f"Y{year}"
        lines.append(f"    {year_id}[📅 {year}]")
        lines.append(f"    style {year_id} fill:#8B0000,color:#fff")
        for device_id, label in devices:
            lines.append(f"    {year_id} --> {device_id}[{label}]")
            lines.append(f"    style {device_id} fill:#2d2d2d,color:#ff9999")

    return "\n".join(lines)

    # ── Networks list → simple node graph ────────────────────────────────
    if graph_type == "networks":
        items = data if isinstance(data, list) else data.get("items", [])
        if not items:
            return "No network data found."

        lines = ["flowchart TD", "    FWD[Forward Networks]"]
        for item in items:
            name = str(item.get("name", item.get("id", "unknown"))).replace("-", "_").replace(" ", "_")
            nid = str(item.get("id", "")).replace("-", "_")
            lines.append(f"    FWD --> {nid}[{name}]")
        return "\n".join(lines)

    return _to_markdown_table(data)



# ── Forward Networks tools ────────────────────────────────────────────────

@mcp.tool(
    name="debug_forward",
    description="Debug Forward Networks connection. Lists networks to verify credentials and base URL are correct.",
)
def debug_forward() -> str:
    _check_config()
    with _forward_client() as client:
        r = client.get(f"{FORWARD_BASE_URL}/api/networks")
        return (
            f"Status : {r.status_code}\n"
            f"URL    : {r.url}\n"
            f"Response (first 1000 chars):\n{r.text[:1000]}"
        )


@mcp.tool(
    name="list_networks",
    description="List all networks available in the Forward Networks platform.",
)
def list_networks(
    output_format: str = Field(
        default="table",
        description="Output format: 'table' or 'graph'",
    ),
) -> str:
    _check_config()
    with _forward_client() as client:
        response = client.get(f"{FORWARD_BASE_URL}/api/networks")
        response.raise_for_status()
        data = response.json()

    if output_format == "graph":
        return _to_mermaid_graph(data, graph_type="networks")
    return _to_markdown_table(data)


@mcp.tool(
    name="get_device_basic_info",
    description=(
        "Get basic device inventory: names, platforms, OS types, management IPs. "
        "Set output_format='graph' to return a Mermaid diagram directly. "
        "DO NOT call generate_graph after this — use output_format param instead."
    ),
)
def get_device_basic_info(
    network_id: str = Field(description="ID of the network"),
    snapshot_id: str = Field(default=None, description="Specific snapshot ID (optional — uses latest if omitted)"),
    limit: int = Field(default=100, description="Maximum number of devices to return"),
    offset: int = Field(default=0, description="Number of rows to skip for pagination"),
    output_format: str = Field(default="table", description="Output format: 'table' or 'graph'"),
) -> str:
    _check_config()
    nqe_query = """
    foreach device in network.devices
    select {
        name: device.name,
        platform: device.platform.vendor,
        os: device.platform.os,
        managementIp: device.platform.managementIps
    }
    """
    with _forward_client() as client:
        if not snapshot_id:
            snapshot_id = _get_latest_snapshot(client, network_id)

        body = {
            "query": nqe_query,
            "queryOptions": {
            "offset": offset,
             "limit": limit,
           "itemFormat": "JSON"
           }
        }
        response = client.post(
            f"{FORWARD_BASE_URL}/api/nqe",
            params={"networkId": network_id},
            json=body,
        )
        response.raise_for_status()
        data = response.json()

    # if output_format == "graph":
    #     return _to_mermaid_graph(data, graph_type="inventory")
    return _to_mermaid_graph(data, "eol") if output_format == "graph" else _to_markdown_table(data)


@mcp.tool(
    name="search_paths",
    description=(
        "Trace a packet path through the network from a source to a destination. "
        "'dst_ip' is always required. Use 'src_ip' or 'from_device' (device name) as the source. "
        "Device names are NOT supported in dst_ip — use actual IP addresses."
    ),
)
def search_paths(
    network_id: str = Field(description="Network ID to search in"),
    dst_ip: str = Field(description="Destination IP address or subnet (required)"),
    src_ip: str = Field(default=None, description="Source IP address or subnet"),
    from_device: str = Field(default=None, description="Source device name"),
    src_port: str = Field(default=None, description="Source port"),
    dst_port: str = Field(default=None, description="Destination port"),
    ip_proto: int = Field(default=None, description="IP protocol number (6=TCP, 17=UDP)"),
    intent: str = Field(
        default="PREFER_DELIVERED",
        description="Search intent: PREFER_DELIVERED, PREFER_VIOLATIONS, or VIOLATIONS_ONLY",
    ),
    max_results: int = Field(default=10, description="Maximum number of results to return"),
    snapshot_id: str = Field(default=None, description="Snapshot ID (optional — uses latest if omitted)"),
    output_format: str = Field(default="table", description="Output format: 'table' or 'graph'"),
) -> str:
    _check_config()
    with _forward_client() as client:
        if not snapshot_id:
            snapshot_id = _get_latest_snapshot(client, network_id)

        params = {"networkId": network_id, "snapshotId": snapshot_id}
        body: dict = {
            "dstIp": dst_ip,
            "intent": intent,
            "maxResults": max_results,
        }
        if src_ip:
            body["srcIp"] = src_ip
        if from_device:
            body["from"] = from_device
        if src_port:
            body["srcPort"] = src_port
        if dst_port:
            body["dstPort"] = dst_port
        if ip_proto is not None:
            body["ipProto"] = ip_proto

        response = client.post(
            f"{FORWARD_BASE_URL}/api/paths",
            params=params,
            json=body,
        )
        response.raise_for_status()
        data = response.json()

    if output_format == "graph":
        return _to_mermaid_graph(data, graph_type="path")
    return _to_markdown_table(data)


@mcp.tool(
    name="run_nqe_query_by_id",
    description=(
        "Run a saved NQE (Network Query Engine) query by its ID. "
        "Use list_nqe_queries first to discover available query IDs."
    ),
)
def run_nqe_query_by_id(
    network_id: str = Field(description="Network ID to run the query against"),
    query_id: str = Field(description="Query ID from the NQE library (e.g. FQ_abc123...)"),
    snapshot_id: str = Field(default=None, description="Specific snapshot ID (optional — uses latest if omitted)"),
    limit: int = Field(default=100, description="Maximum number of rows to return"),
    offset: int = Field(default=0, description="Number of rows to skip"),
    parameters: dict = Field(default=None, description="Optional parameters for parameterized queries"),
    output_format: str = Field(default="table", description="Output format: 'table' or 'graph'"),
) -> str:
    _check_config()
    with _forward_client() as client:
        if not snapshot_id:
            snapshot_id = _get_latest_snapshot(client, network_id)

        body: dict = {
           "queryId": query_id,
            "queryOptions": {
            "offset": offset,
             "limit": limit,
           "itemFormat": "JSON"
           }
        }
        
        if parameters:
            body["parameters"] = parameters

        response = client.post(
            f"{FORWARD_BASE_URL}/api/nqe",
            params={"networkId": network_id},
            json=body,
        )
        response.raise_for_status()
        data = response.json()

    if output_format == "graph":
        return _to_mermaid_graph(data, graph_type="inventory")
    return _to_markdown_table(data)


@mcp.tool(
    name="get_hardware_support",
    description=(
        "Check hardware support status including end-of-life (EOL) dates and "
        "compliance status for devices in a network. Critical for security audits "
        "and hardware refresh planning."
    ),
)
def get_hardware_support(
    network_id: str = Field(description="ID of the network"),
    snapshot_id: str = Field(default=None, description="Specific snapshot ID (optional — uses latest if omitted)"),
    eol_only: bool = Field(default=False, description="If True, return only devices that have already passed EOL"),
    limit: int = Field(default=100, description="Maximum number of rows to return"),
    offset: int = Field(default=0, description="Number of rows to skip"),
    output_format: str = Field(default="table", description="Output format: 'table' or 'graph'"),
) -> str:
    _check_config()
    nqe_query = """
   ZERO_DURATION = days(0);

isDurationNegative(duration) = isPresent(duration) && duration < ZERO_DURATION;

safeSubtract(a, b) =
  if isPresent(a) && isPresent(b)
  then a - b
  else null: Duration;

withWarning(someDate, todayDate) =
  if isDurationNegative(safeSubtract(someDate, todayDate))
  then withInfoStatus(someDate, InfoStatus.WARNING)
  else someDate;

@primaryKey(Device, "Part Name")
foreach device in network.devices
let platform = device.platform
foreach component in platform.components
let support = component.support
where isPresent(support)
let todayTime = if isPresent(device.snapshotInfo.collectionTime)
                then device.snapshotInfo.collectionTime
                else device.snapshotInfo.backfillTime
let todayDate = if isPresent(todayTime)
                then date(todayTime)
                else null : Date
let daysUntilEndOfAnySupport = safeSubtract(support.lastSupportDate, todayDate)
select {
  Device: device.name,
  "Part Name": component.name,
  "Days until End of any support": daysUntilEndOfAnySupport,
  "End of any OS maintenance": withWarning(support.lastMaintenanceDate, todayDate),
  "End of any OS vulnerability": withWarning(support.lastVulnerabilityDate, todayDate),
  "End of any support": withWarning(support.lastSupportDate, todayDate),
  Vendor: platform.vendor,
  Model: platform.model,
  "Part ID": component.partId,
  "Part Serial Number": component.serialNumber,
  "Part Type": component.partType,
  "Part Description": component.description,
  "Part Version ID": component.versionId,
  "Management IP(s)": platform.managementIps,
  Type: platform.deviceType,
  URL: support.announcementUrl
}
order by "Days until End of any support" asc, Device asc natural, "Part Name" asc

    """
    with _forward_client() as client:
        if not snapshot_id:
            snapshot_id = _get_latest_snapshot(client, network_id)

        data = _forward_request(
            client, "POST",
            f"{FORWARD_BASE_URL}/api/nqe",
            params={"networkId": network_id},
            
            json = {
            "query": nqe_query,
            "queryOptions": {
            "offset": offset,
             "limit": limit,
           "itemFormat": "JSON"
           }
        }
            
        )

    if eol_only and "items" in data:
        today = date.today().isoformat()
        data["items"] = [i for i in data["items"] if i.get("endOfLife") and i["endOfLife"] <= today]
        

    if output_format == "graph":
        return _to_mermaid_graph(data, graph_type="eol")
    return _to_markdown_table(data)

@mcp.tool(name="debug_snapshot", description="Debug snapshot fetching for a network")
def debug_snapshot(network_id: str = Field(description="Network ID to test")) -> str:
    _check_config()
    with _forward_client() as client:
        # Try different snapshot URL patterns
        results = []

        urls = [
            f"{FORWARD_BASE_URL}/api/networks/{network_id}/snapshots",
            f"{FORWARD_BASE_URL}/api/snapshots?networkId={network_id}",
            f"{FORWARD_BASE_URL}/api/networks/{network_id}/versions",
        ]

        for url in urls:
            try:
                r = client.get(url)
                results.append(f"✅ {url}\n   Status: {r.status_code}\n   Body: {r.text[:200]}")
            except Exception as e:
                results.append(f"❌ {url}\n   Error: {e}")

        return "\n\n".join(results)
# ── Entry point ───────────────────────────────────────────────────────────


@mcp.tool(
    name="generate_graph",
    description=(
        "Generate a Mermaid graph from REAL data returned by other tools. "
        "IMPORTANT: You must first call get_device_basic_info, get_hardware_support, "
        "search_paths, or another data tool to get actual results, then pass that "
        "data here. Do NOT pass query strings or descriptions as data."
    ),
)
def generate_graph(
    data: dict = Field(description="JSON data to visualize"),
    graph_type: str = Field(
        default="auto",
        description=(
            "Graph type: 'auto' (detect), 'flowchart', 'timeline', "
            "'pie', 'xychart', 'sankey', 'mindmap', 'er'"
        ),
    ),
    title: str = Field(default=None, description="Optional title for the graph"),
    direction: str = Field(default="LR", description="Flowchart direction: LR, TD, RL, BT"),
) -> str:
    import json

    def _clean(s: str) -> str:
        return str(s).replace("-", "_").replace(".", "_").replace(" ", "_").replace("/", "_")

    def _detect_type(d) -> str:
        if isinstance(d, dict):
            if "paths" in d:           return "flowchart"
            if "items" in d:
                items = d["items"]
                if items and "endOfLife" in items[0]:  return "timeline"
                if items and "platform" in items[0]:   return "flowchart"
            if "nodes" in d and "edges" in d:          return "flowchart"
        if isinstance(d, list):
            if d and "endOfLife" in d[0]:              return "timeline"
            if d and "value" in d[0] and "label" in d[0]: return "pie"
        return "flowchart"

    # ── Resolve graph type ────────────────────────────────────────────
    gtype = graph_type if graph_type != "auto" else _detect_type(data)

    # ── Flowchart ─────────────────────────────────────────────────────
    if gtype == "flowchart":
        lines = [f"flowchart {direction}"]
        if title:
            lines = [f"---\ntitle: {title}\n---"] + lines

        # paths with hops
        if isinstance(data, dict) and "paths" in data:
            seen = set()
            for path in data["paths"]:
                hops = path.get("hops", [])
                for i in range(len(hops) - 1):
                    src   = _clean(hops[i].get("deviceName", f"hop{i}"))
                    dst   = _clean(hops[i+1].get("deviceName", f"hop{i+1}"))
                    iface = hops[i].get("egressInterface", "")
                    key   = f"{src}->{dst}"
                    if key not in seen:
                        seen.add(key)
                        lines.append(f'    {src} -->|"{iface}"| {dst}' if iface else f"    {src} --> {dst}")

        # nodes + edges
        elif isinstance(data, dict) and "nodes" in data and "edges" in data:
            for node in data["nodes"]:
                nid   = _clean(node.get("id", node.get("name", "node")))
                label = node.get("label", node.get("name", nid))
                shape = node.get("shape", "rect")
                if shape == "circle":
                    lines.append(f"    {nid}(({label}))")
                elif shape == "diamond":
                    lines.append(f"    {nid}{{{label}}}")
                else:
                    lines.append(f"    {nid}[{label}]")
            for edge in data["edges"]:
                src   = _clean(edge.get("from", edge.get("source", "")))
                dst   = _clean(edge.get("to",   edge.get("target", "")))
                label = edge.get("label", "")
                lines.append(f'    {src} -->|"{label}"| {dst}' if label else f"    {src} --> {dst}")

        # items grouped by platform/category
        elif isinstance(data, dict) and "items" in data:
            items = data["items"]
            groups: dict = {}
            group_key = next((k for k in ["platform", "category", "type", "vendor", "os"] if k in (items[0] if items else {})), None)
            name_key  = next((k for k in ["name", "deviceName", "id", "title"] if k in (items[0] if items else {})), None)

            if group_key and name_key:
                for item in items:
                    g = _clean(str(item.get(group_key, "Unknown")))
                    n = _clean(str(item.get(name_key, "unknown")))
                    groups.setdefault(g, []).append(n)
                for group, members in groups.items():
                    lines.append(f"    subgraph {group}")
                    for m in members:
                        lines.append(f"        {m}[{m}]")
                    lines.append("    end")
            else:
                # flat list — chain them
                flat = [_clean(str(item.get("name", item.get("id", f"item{i}")))) for i, item in enumerate(items)]
                for i in range(len(flat) - 1):
                    lines.append(f"    {flat[i]} --> {flat[i+1]}")

        # generic dict — key → value nodes
        elif isinstance(data, dict):
            root = _clean(title or "root")
            lines.append(f"    {root}[{title or 'root'}]")
            for k, v in data.items():
                kid = _clean(k)
                lines.append(f"    {root} --> {kid}[{k}: {v}]")

        # generic list
        elif isinstance(data, list):
            root = _clean(title or "root")
            lines.append(f"    {root}[{title or 'items'}]")
            for i, item in enumerate(data):
                kid = f"item{i}"
                label = str(item.get("name", item.get("id", item))) if isinstance(item, dict) else str(item)
                lines.append(f"    {root} --> {kid}[{label[:40]}]")

        return "\n".join(lines)

    # ── Timeline ──────────────────────────────────────────────────────
    elif gtype == "timeline":
        lines = ["timeline", f"    title {title or 'Timeline'}"]
        items = data.get("items", data) if isinstance(data, dict) else data
        by_year: dict = {}
        for item in items:
            date_val = item.get("endOfLife") or item.get("date") or item.get("year") or "Unknown"
            year = str(date_val)[:4]
            label = item.get("deviceName") or item.get("name") or item.get("label") or str(item)
            extra = item.get("model") or item.get("detail") or ""
            by_year.setdefault(year, []).append(f"{label} ({extra})" if extra else label)
        for year in sorted(by_year.keys()):
            lines.append(f"    section {year}")
            for entry in by_year[year]:
                lines.append(f"        {entry} : event")
        return "\n".join(lines)

    # ── Pie chart ─────────────────────────────────────────────────────
    elif gtype == "pie":
        lines = [f'pie title {title or "Distribution"}']
        items = data.get("items", data) if isinstance(data, dict) else data
        for item in items:
            label = item.get("label") or item.get("name") or item.get("key") or "unknown"
            value = item.get("value") or item.get("count") or item.get("total") or 0
            lines.append(f'    "{label}" : {value}')
        return "\n".join(lines)

    # ── XY Chart (bar) ────────────────────────────────────────────────
    elif gtype == "xychart":
        lines = [f"xychart-beta", f'    title "{title or "Chart"}"']
        items = data.get("items", data) if isinstance(data, dict) else data
        labels = [str(item.get("label") or item.get("name") or item.get("x") or i) for i, item in enumerate(items)]
        values = [item.get("value") or item.get("count") or item.get("y") or 0 for item in items]
        lines.append(f'    x-axis [{", ".join(f"{chr(34)}{l}{chr(34)}" for l in labels)}]')
        lines.append(f'    bar [{", ".join(str(v) for v in values)}]')
        return "\n".join(lines)

    # ── Mindmap ───────────────────────────────────────────────────────
    elif gtype == "mindmap":
        lines = ["mindmap", f"  root(({title or 'root'}))"]
        items = data.get("items", data) if isinstance(data, dict) else data
        if isinstance(data, dict) and "items" not in data:
            for k, v in data.items():
                lines.append(f"    {k}")
                if isinstance(v, list):
                    for item in v[:5]:
                        lines.append(f"      {str(item)[:30]}")
                else:
                    lines.append(f"      {str(v)[:30]}")
        else:
            group_key = next((k for k in ["platform", "category", "type", "vendor"] if k in (items[0] if items else {})), None)
            name_key  = next((k for k in ["name", "deviceName", "id"] if k in (items[0] if items else {})), None)
            if group_key and name_key:
                groups: dict = {}
                for item in items:
                    g = str(item.get(group_key, "Unknown"))
                    n = str(item.get(name_key, "unknown"))
                    groups.setdefault(g, []).append(n)
                for group, members in groups.items():
                    lines.append(f"    {group}")
                    for m in members[:5]:
                        lines.append(f"      {m}")
            else:
                for item in items[:10]:
                    label = str(item.get("name", item.get("id", str(item))))
                    lines.append(f"    {label[:30]}")
        return "\n".join(lines)

    # ── Sankey ────────────────────────────────────────────────────────
    elif gtype == "sankey":
        lines = ["sankey-beta"]
        items = data.get("links", data.get("items", data)) if isinstance(data, dict) else data
        for item in items:
            src   = item.get("source") or item.get("from") or item.get("src") or "unknown"
            dst   = item.get("target") or item.get("to")   or item.get("dst") or "unknown"
            value = item.get("value")  or item.get("count") or item.get("bandwidth") or 1
            lines.append(f'    {src},{dst},{value}')
        return "\n".join(lines)

    # ── ER diagram ────────────────────────────────────────────────────
    elif gtype == "er":
        lines = ["erDiagram"]
        if isinstance(data, dict) and "entities" in data:
            for entity in data["entities"]:
                name   = entity.get("name", "Entity")
                fields = entity.get("fields", [])
                lines.append(f"    {name} {{")
                for field in fields:
                    ftype = field.get("type", "string")
                    fname = field.get("name", "field")
                    lines.append(f"        {ftype} {fname}")
                lines.append("    }")
            for rel in data.get("relations", []):
                a      = rel.get("from", "A")
                b      = rel.get("to",   "B")
                rel_type = rel.get("type", "||--o{")
                label  = rel.get("label", "relates")
                lines.append(f'    {a} {rel_type} {b} : "{label}"')
        return "\n".join(lines)

    return f"Unknown graph type: {gtype}"

if __name__ == "__main__":
    mcp.run(transport="streamable-http")