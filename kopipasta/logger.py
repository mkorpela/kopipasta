import os
import sys
from pathlib import Path

import structlog


def _get_log_file_path() -> Path:
    """Determines the log file path following XDG standards."""
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        base_dir = Path(state_home)
    else:
        # Mac/Linux default: ~/.local/state
        # Windows default: %LOCALAPPDATA%
        if sys.platform == "win32":
            base_dir = Path(os.environ["LOCALAPPDATA"])
        else:
            base_dir = Path.home() / ".local" / "state"

    log_dir = base_dir / "kopipasta"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "events.jsonl"


def configure_logging():
    """Configures structlog to write JSONL to disk."""
    log_file = _get_log_file_path()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.WriteLoggerFactory(
            file=open(log_file, "a", encoding="utf-8", buffering=1)  # Line buffered
        ),
    )


def get_logger() -> structlog.stdlib.BoundLogger:
    """Returns a structured logger instance."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger()
    return logger
