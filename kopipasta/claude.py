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
from typing import Any, Dict, Optional

from rich.console import Console

from kopipasta.config import set_active_project


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
    console: Optional[Console] = None,
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

    # 0. Always update the active project pointer (hot-swap)
    set_active_project(Path(project_root))

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

    if local:
        python_exe = sys.executable
        console.print(
            f"[dim]🔧 Local dev mode: {python_exe} -m kopipasta.mcp_server[/dim]"
        )
        mcp_servers[server_name] = {
            "command": python_exe,
            "args": [
                "-m",
                "kopipasta.mcp_server",
                # No project-root arg; server uses pointer file
            ],
            "cwd": str(project_root),
        }
    else:
        # Production mode via uvx
        # We assume 'uvx' is in the PATH.
        mcp_servers[server_name] = {
            "command": "uvx",
            "args": [
                "--from",
                "kopipasta",
                "kopipasta-mcp",
                # No project-root arg; server uses pointer file
            ],
            "cwd": str(project_root),
        }

    # 5. Write config
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Optimization: Don't rewrite if identical (prevents unnecessary file modification time updates)
        # Note: We reconstruct 'data' above by loading it, but if it existed, we parsed it.
        # However, to be strictly safe and avoid rewriting identical JSON:
        current_content = ""
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                current_content = f.read().strip()
        
        new_content = json.dumps(data, indent=2)
        
        if current_content and json.loads(current_content) == data:
            console.print(f"[green]✅ {server_name} config is up to date.[/green]")
            console.print(f"[dim]   Active project set to: {project_root}[/dim]")
            return True
            
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        console.print(f"[green]✅ {server_name} configured in Claude Desktop.[/green]")
        console.print("[dim]   Restart Claude Desktop to load the new toolset.[/dim]")
        return True
    except OSError as e:
        console.print(f"[red]❌ Failed to write config: {e}[/red]")
        return False
