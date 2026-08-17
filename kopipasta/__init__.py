"""Kopipasta - Context generator for LLM prompts."""

import importlib.metadata
import json
from pathlib import Path
from typing import Tuple


def _resolve_distribution_info() -> Tuple[str, bool]:
    """Read version and editable status from package metadata."""
    version = "unknown"
    is_editable = False
    try:
        dist = importlib.metadata.distribution("kopipasta")
        version = dist.version
    except Exception:
        dist = None

    if dist is not None:
        try:
            direct_url_text = dist.read_text("direct_url.json")
            if direct_url_text:
                data = json.loads(direct_url_text)
                if data.get("dir_info", {}).get("editable", False):
                    is_editable = True
        except Exception:
            pass

    return version, is_editable


def format_version() -> str:
    """Format one-line human-readable version and installation details."""
    pkg_dir = Path(__file__).resolve().parent
    version, is_editable = _resolve_distribution_info()
    suffix = ", editable" if is_editable else ""
    return f"kopipasta {version} ({pkg_dir}{suffix})"


__version__, _ = _resolve_distribution_info()
__all__ = ["__version__", "format_version"]
