# Project Constitution: Quad-Memory Architecture

## 1. Architectural Invariants
This project strictly adheres to the **"Quad-Memory" Architecture** to manage context.

### The Four Layers
1.  **Global Kernel**: User identity & preferences (injected via `~/.config/kopipasta/ai_profile.md`).
2.  **Project Constitution**: This file (`./AI_CONTEXT.md`). Persistent architectural decisions, immutable constraints, and domain definitions.
3.  **Working Memory**: Ephemeral state (`./AI_SESSION.md`). Tracks the *current* task's progress. Must be git-ignored.
4.  **The Gardener**: The lifecycle loop (`Start` -> `Update` -> `Finish`) that moves state from Memory to Context.

## 2. Technical Contracts

### Technical Contracts

### Context State Model
Selection is managed via a three-state engine to distinguish between background context and active focus:
*   **Unselected**: File is not included in the prompt.
*   **Base (Cyan)**: "Synced" context. Files the LLM has already seen in previous turns.
*   **Delta (Green)**: "Active Focus." Newly selected files, modified files (after a patch), or files identified via Intelligent Import.
*   **Transitions**:
    - `Space` cycle: `Unselected` -> `Delta` -> `Unselected`. If `Base`, toggles to `Delta`.
    - **Promotion**: Files transition `Delta` -> `Base` during "Extend Context" (`e`), "Patch" (`p`), or "Quit" (`q`) to mark them as synced for the next turn.

### Ralph Loop (`r` hotkey) — MCP Agent Integration
The Ralph Loop enables an external AI agent (e.g. Claude Desktop) to iteratively patch and test code via MCP until a verification command passes.
*   **Decoupled Architecture**: The `kopipasta` TUI and the MCP Server (`kopipasta/mcp_server.py`) run as separate processes. They communicate implicitly via the filesystem (`.ralph.json`, project files).
*   **Access Control**:
    - **Editable** (Delta/Green files): Agent can read and patch these.
    - **Read-Only** (Project-Wide): Agent can read any non-ignored file in the project to gather context, but cannot modify them unless they are in the Editable set.
*   **MCP Tools**: `read_context` (returns file listing + task), `read_files` (read any file), `apply_patch` (with permission enforcement), `run_verification` (executes the pass/fail command).
*   **Configuration File**: `.ralph.json` is written to the project root. Must be git-ignored. Contains `project_root`, `verification_command`, `task_description`, `editable_files`, `readable_files`.
*   **Auto-Configure Claude Desktop**: `_action_ralph` calls `configure_claude_desktop()` (`kopipasta/claude.py`) to inject the MCP server entry into `claude_desktop_config.json`. Backs up existing config before modifying.
*   **Claude Desktop `cwd`/`env` Limitation**: Claude Desktop ignores `cwd` and `env` fields in `claude_desktop_config.json`. The workaround is to use `/bin/sh -c "KOPIPASTA_PROJECT_ROOT='...' exec python -m kopipasta.mcp_server"` so the env var is baked into the shell command string. The MCP server resolves config via `_get_project_root_override()` which checks CLI args, then `KOPIPASTA_PROJECT_ROOT` env var, then `Path.cwd()` as fallback.
*   **Config Overwrite**: `configure_claude_desktop()` always overwrites an existing `kopipasta-ralph` entry (no short-circuit on match) to ensure the command/args stay current.
*   **No auto-commit**: The Ralph workflow does not auto-commit. The agent's changes are reviewed by the user.

### Intelligent Import (Universal Intake)
The `p` (Process) command acts as a universal intake for LLM output:
*   **Fallback**: If no code blocks (patches) are detected, the tool must regex-scan the text for valid project paths.
*   **Normalization**: All path matching must be cross-platform. Normalize both the source text and local project paths to forward slashes (`/`) during scanning to ensure compatibility between LLM output and local OS (Windows/POSIX) path separators.
*   **Visibility**: When a path is imported, the UI must ensure the path is visible in the tree (auto-expand parents).

### Path Matching Delimiters
*   The `find_paths_in_text` regex boundary character class must include: whitespace, quotes, backticks, brackets, **and** `:;,` — to handle linter/compiler output where paths are followed by `:line:col:`.

### Session Metadata
*   `AI_SESSION.md` **must** store session metadata in a hidden HTML comment on the first line:
    `<!-- KOPIPASTA_METADATA {"start_commit": "hash", "timestamp": "iso8601"} -->`
### Git Operations
*   **Session Exclusion**: `AI_SESSION.md` is strictly ephemeral. Git operations (add/commit) must ensure it is never committed.
*   **Pathspec Safety**: When programmatic git commands exclude files (e.g., `git add . :!AI_SESSION.md`), code must first verify the file is NOT already ignored by `.gitignore`. Git throws errors if you try to exclude a path that is already ignored.

### Filesystem Safety
*   **Heuristic Overwrite Protection**: The patcher must guard against "snippet hallucinations" (where an LLM outputs a snippet instead of the full file).
*   **Trigger Conditions**: A safety check (user confirmation `y/N`) is mandatory for "Full File" overwrites if:
    1.  The target file is non-trivial (> 200 chars) and the new content shrinks it by > 50%.
    2.  The content contains diff markers (e.g., `@@ ... @@`), indicating a parsing failure of a diff block.

*   **Header Precedence**: Explicit `# FILE:` headers (inside block content) override non-explicit markdown headers (`### path/file.ext`) found via lookback. The parser distinguishes these via a `(path, is_explicit)` tuple from `_find_header_context`.
*   **Empty Patch Prevention**: When a markdown header provides `initial_path` but an explicit `# FILE:` inside the block overrides it, the parser must NOT finalize an empty patch for the markdown header's path. Instead, it skips finalization when `initial_is_explicit=False` and `current_lines` is empty.
*   **Lookback Blank Line Handling**: The blank-line skip in `_find_header_context` must `continue` after decrementing `k`, or it double-decrements and skips the actual header line.

### Development Standards
*   **Language**: Python 3.10+.
*   **Typing**: Strict type hints (`mypy` compliant) are mandatory for all function signatures.
*   **Dependencies**: Managed via `uv` (`pyproject.toml`).

### Observability & Telemetry
*   **Structured Logging**: All significant user actions and system events must be logged using `structlog` to a local JSONL file.
*   **Storage Location**: Logs are stored in the XDG State Home (e.g., `~/.local/state/kopipasta/events.jsonl`).
*   **Patch Forensics**: To enable deterministic debugging of patch failures, the patcher must log the **full original content** of the target file (if it exists) and the **raw patch content** before attempting application.
*   **Privacy**: Logs are strictly local. No data is exfiltrated.

## 3. Anti-Patterns (Do Not Do)
*   Do not hardcode directory trees in documentation; `kopipasta` generates them dynamically in the prompt.
*   Do not duplicate prompt instructions (e.g., "How to patch") in this file; they belong in `prompt_template.j2`.