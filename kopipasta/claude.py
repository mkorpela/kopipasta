"""
Auto-configure Claude Desktop to use the kopipasta MCP server.
"""
import datetime
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

from rich.console import Console


def _get_claude_config_path() -> Path:
    """Determine the location of claude_desktop_config.json based on OS."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA")
        if not base:
            raise OSError("APPDATA environment variable not found.")
        return Path(base) / "Claude" / "claude_desktop_config.json"
    elif system == "Darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def configure_claude_desktop(
    project_root: str,
    local: bool = False,
    console: Console | None = None,
) -> bool:
    """
    Injects the kopipasta-ralph MCP server entry into claude_desktop_config.json.

    Args:
        project_root: Absolute path to the project root (used as cwd for the server).
        local: If True, uses sys.executable + -m for dev mode.
               If False, uses uvx --from kopipasta kopipasta-mcp.
        console: Optional Rich console for output.

    Returns:
        True if configuration was written successfully, False otherwise.
    """
    if console is None:
        console = Console()

    server_name = "kopipasta-ralph"

    # 1. Locate config
    try:
        config_path = _get_claude_config_path()
    except OSError as e:
        console.print(f"[red]❌ Error locating Claude config: {e}[/red]")
        return False

    console.print(f"[dim]📍 Config path: {config_path}[/dim]")

    # 2. Load or create config
    data: Dict[str, Any] = {"mcpServers": {}}
    if config_path.exists():
        # Backup before modifying
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = config_path.with_name(f"{config_path.name}.{timestamp}.bak")
        try:
            shutil.copy2(config_path, backup_path)
            console.print(f"[dim]📦 Backup created: {backup_path.name}[/dim]")
        except OSError as e:
            console.print(f"[yellow]⚠ Could not create backup: {e}[/yellow]")

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
        except json.JSONDecodeError:
            console.print(
                "[yellow]⚠ Existing config was invalid JSON. Starting fresh.[/yellow]"
            )

    # 3. Check if already configured
    mcp_servers = data.setdefault("mcpServers", {})
    if server_name in mcp_servers:
        console.print(f"[dim]♻ Updating existing {server_name} config...[/dim]")

    # 4. Inject server entry
    # Capture current PATH to ensure tools like 'uv', 'npm', 'cargo' are found
    # Claude Desktop often runs with a stripped environment.
    current_path = os.environ.get("PATH", "")
    # Escape single quotes for shell safety
    safe_path = current_path.replace("'", "'\\''")

    if local:
        python_exe = sys.executable
        console.print(
            f"[dim]🔧 Local dev mode: {python_exe} -m kopipasta.mcp_server[/dim]"
        )
        mcp_servers[server_name] = {
            "command": "/bin/sh",
            "args": [
                "-c",
                f"KOPIPASTA_PROJECT_ROOT='{project_root}' PATH='{safe_path}' exec '{python_exe}' -m kopipasta.mcp_server",
            ],
        }
    else:
        mcp_servers[server_name] = {
            "command": "/bin/sh",
            "args": [
                "-c",
                f"KOPIPASTA_PROJECT_ROOT='{project_root}' PATH='{safe_path}' exec uvx --from kopipasta kopipasta-mcp",
            ],
        }

    # 5. Write config
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        console.print(f"[green]✅ {server_name} configured in Claude Desktop.[/green]")
        console.print("[dim]   Restart Claude Desktop to load the new toolset.[/dim]")
        return True
    except OSError as e:
        console.print(f"[red]❌ Failed to write config: {e}[/red]")
        return False
