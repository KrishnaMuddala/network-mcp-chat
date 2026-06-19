"""
MCP Tool: Cisco C2960CX SSH Read-Only Commands
Uses netmiko for reliable Cisco IOS connectivity.
"""

import json
import os
import warnings
from anyio import Path
from dotenv import load_dotenv
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

load_dotenv()
# Suppress legacy cipher warnings from Cisco IOS 15.2
warnings.filterwarnings("ignore")
CISCO_DEVICE_HOST = os.getenv("CISCO_DEVICE_HOST", "")
CISCO_DEVICE_USER = os.getenv("CISCO_DEVICE_USER", "")
CISCO_DEVICE_PASS = os.getenv("CISCO_DEVICE_PASS", "")
CISCO_DEVICE_PORT = int(os.getenv("CISCO_DEVICE_PORT", 22))
print(f"config values: {CISCO_DEVICE_HOST}: {CISCO_DEVICE_USER}")
# ─────────────────────────────────────────────
# Safety: only allow read-only Cisco commands
# ─────────────────────────────────────────────
ALLOWED_COMMANDS = {
    "show version",
    "show running-config",
    "show startup-config",
    "show interfaces",
    "show interfaces status",
    "show interfaces trunk",
    "show ip interface brief",
    "show vlan",
    "show vlan brief",
    "show mac address-table",
    "show arp",
    "show cdp neighbors",
    "show cdp neighbors detail",
    "show spanning-tree",
    "show spanning-tree summary",
    "show etherchannel summary",
    "show lldp neighbors",
    "show lldp neighbors detail",
    "show logging",
    "show clock",
    "show inventory",
    "show environment",
    "show power inline",
    "show ip route",
    "show access-lists",
    "show port-security",
    "show port-security summary",
    "show storm-control",
    "show processes cpu",
    "show memory statistics",
    "show users",
    "show snmp",
    "show ntp status",
    "show ntp associations",
}

def is_allowed(command: str) -> bool:
    cmd = command.strip().lower()
    for allowed in ALLOWED_COMMANDS:
        if cmd == allowed or cmd.startswith(allowed + " "):
            return True
    return False


def ssh_run_command(host: str, command: str) -> dict:
    """Connect to Cisco C2960CX via netmiko and run a read-only command."""

    if not is_allowed(command):
        return {
            "status": "blocked",
            "reason": f"Command '{command}' is not in the read-only allowlist.",
            "allowed_commands": sorted(ALLOWED_COMMANDS)
        }
    print(f"Running command:{command} on {host}: {CISCO_DEVICE_USER}")
    try:
        device = {
            "device_type": "cisco_ios",
            "host": host,
            "username":  CISCO_DEVICE_USER,
            "password": CISCO_DEVICE_PASS,
            "port": CISCO_DEVICE_PORT,
            "timeout": 15,
            "session_timeout": 60,
            "fast_cli": False,
        }

        with ConnectHandler(**device) as conn:
            output = conn.send_command(command, read_timeout=30)

        return {
            "status": "success",
            "host": host,
            "command": command,
            "output": output.strip()
        }

    except NetmikoAuthenticationException:
        return {"status": "error", "reason": "Authentication failed. Check username/password."}
    except NetmikoTimeoutException:
        return {"status": "error", "reason": "Connection timed out. Check host/port."}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# ─────────────────────────────────────────────
# MCP Server Definition
# ─────────────────────────────────────────────
server = FastMCP(name="cisco-c2960cx", host="0.0.0.0", log_level="ERROR", port=8001)


@server.tool(
    name="cisco_show",
    description=(
        "Run a read-only 'show' command on a Cisco C2960CX switch via SSH. "
        "Only safe, non-destructive commands are allowed (show version, show interfaces, "
        "show vlan, show mac address-table, show running-config, etc.)."
    )
)
def cisco_show(host: str,  command: str):
    result = ssh_run_command(host, command)
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


@server.tool(
    name="cisco_list_commands",
    description="List all allowed read-only commands for the Cisco C2960CX MCP tool."
)
def cisco_list_commands():
    return [TextContent(
        type="text",
        text=json.dumps({"allowed_commands": sorted(ALLOWED_COMMANDS)}, indent=2)
    )]


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    server.run(transport="streamable-http")