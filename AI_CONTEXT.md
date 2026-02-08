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

### Fix Workflow (`x` hotkey)
The fix workflow runs a command, captures errors, detects affected files, and generates a diagnostic prompt.
*   **Command Resolution** (3-tier fallback):
    1.  HTML comment in `AI_CONTEXT.md`: `<!-- KOPIPASTA_FIX_CMD: your command here -->`
    2.  `.git/hooks/pre-commit` (platform-aware: POSIX checks `+x` bit; Windows invokes via `sh`/`bash`).
    3.  `git diff --check HEAD` (universal fallback).
*   **Path Detection**: `find_paths_in_text` is reused from Intelligent Import. Its delimiter regex includes `:;,` to support linter output formats (e.g., `path/file.py:10:5: E302`).
*   **Prompt Assembly**: Error output + `git diff HEAD` + content of affected Delta files. Uses `FIX_TEMPLATE` in `prompt.py`.
*   **State Transitions**: Detected files are added to **Delta**. Base files found in errors are promoted to Delta. The user then pastes the fix prompt into the LLM, copies the response, and uses `p` to apply patches.
*   **Configuration Pattern**: The `<!-- KOPIPASTA_FIX_CMD: ... -->` HTML comment is machine-parseable, invisible in rendered markdown, and consistent with the `KOPIPASTA_METADATA` pattern used in `AI_SESSION.md`.
*   **No auto-commit**: Unlike the `p` handler, `x` does not auto-commit. The user reviews and commits manually after applying the LLM's fix.

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

## 3. Anti-Patterns (Do Not Do)
*   Do not hardcode directory trees in documentation; `kopipasta` generates them dynamically in the prompt.
*   Do not duplicate prompt instructions (e.g., "How to patch") in this file; they belong in `prompt_template.j2`.