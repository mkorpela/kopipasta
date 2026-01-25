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

### Development Standards
*   **Language**: Python 3.8+.
*   **Typing**: Strict type hints (`mypy` compliant) are mandatory for all function signatures.
*   **Paradigm**: Functional over OOP. Use `TypedDict` or `dataclass` for state, and pure functions for logic. Avoid complex class hierarchies unless interacting with an OOP-heavy library.
*   **UI**: Use `rich` library for all terminal output.
*   **Dependencies**: Managed via `requirements.txt` (currently) or `poetry` / `uv`.

## 3. Anti-Patterns (Do Not Do)
*   Do not hardcode directory trees in documentation; `kopipasta` generates them dynamically in the prompt.
*   Do not duplicate prompt instructions (e.g., "How to patch") in this file; they belong in `prompt_template.j2`.