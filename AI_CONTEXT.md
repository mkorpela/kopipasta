# Project Constitution: Quad-Memory Architecture

## 1. Architectural Invariants
This project strictly adheres to the **"Quad-Memory" Architecture** to manage context.

### The Four Layers
1.  **Global Kernel**: User identity & preferences (injected via `~/.config/kopipasta/ai_profile.md`).
2.  **Project Constitution**: This file (`./AI_CONTEXT.md`). Persistent architectural decisions, immutable constraints, and domain definitions.
3.  **Working Memory**: Ephemeral state (`./AI_SESSION.md`). Tracks the *current* task's progress. Must be git-ignored.
4.  **The Gardener**: The lifecycle loop (`Start` -> `Update` -> `Finish`) that moves state from Memory to Context.

## 2. Technical Contracts

### Session Metadata
*   `AI_SESSION.md` **must** store session metadata in a hidden HTML comment on the first line:
    `<!-- KOPIPASTA_METADATA {"start_commit": "hash", "timestamp": "iso8601"} -->`
*   This metadata is used for squashing commits upon task completion.

### Git Operations
*   **Session Exclusion**: `AI_SESSION.md` is strictly ephemeral. Git operations (add/commit) must ensure it is never committed.
*   **Pathspec Safety**: When programmatic git commands exclude files (e.g., `git add . :!AI_SESSION.md`), code must first verify the file is NOT already ignored by `.gitignore`. Git throws errors if you try to exclude a path that is already ignored.

### Filesystem Safety
*   **Heuristic Overwrite Protection**: The patcher must guard against "snippet hallucinations" (where an LLM outputs a snippet instead of the full file).
*   **Trigger Conditions**: A safety check (user confirmation `y/N`) is mandatory for "Full File" overwrites if:
    1.  The target file is non-trivial (> 200 chars) and the new content shrinks it by > 50%.
    2.  The content contains diff markers (e.g., `@@ ... @@`), indicating a parsing failure of a diff block.

### Development Standards
*   **Language**: Python 3.10+.
*   **Typing**: Strict type hints (`mypy` compliant) are mandatory for all function signatures.
*   **Paradigm**: Functional over OOP. Use `TypedDict` or `dataclass` for state, and pure functions for logic. Avoid complex class hierarchies unless interacting with an OOP-heavy library.
*   **UI**: Use `rich` library for all terminal output.
*   **Dependencies**: Managed via `uv` (`pyproject.toml`).

## 3. Anti-Patterns (Do Not Do)
*   Do not hardcode directory trees in documentation; `kopipasta` generates them dynamically in the prompt.
*   Do not duplicate prompt instructions (e.g., "How to patch") in this file; they belong in `prompt_template.j2`.