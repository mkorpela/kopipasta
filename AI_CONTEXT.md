# Project Constitution: Quad-Memory Architecture

## 1. Core Philosophy
This project implements the **"Quad-Memory" Architecture** to solve context drift and repetitive prompting in LLM-assisted development.

### The Four Memory Layers
1.  **Global Kernel (`~/.config/kopipasta/ai_profile.md`)**: User-specific preferences (e.g., "I use VS Code", "Prefer TypeScript"). Injected at the top of every prompt.
2.  **Project Constitution (`./AI_CONTEXT.md`)**: This file. Persistent project constraints, architecture patterns, and "Laws of Physics". **Pinned** to every prompt.
3.  **Working Memory (`./AI_SESSION.md`)**: Ephemeral scratchpad for the current active task. Tracks progress, next steps, and state. **Pinned** and **Auto-loaded**.
4.  **The Gardener**: The maintenance loop (User + CLI) that updates Session state (`u`) and harvests knowledge into Context (`f`).

## 2. Technical Constraints & Patterns

### Session Management
*   **Ephemeral State**: `AI_SESSION.md` is strictly for *current* task state. It must be added to `.gitignore`.
*   **Lifecycle**:
    *   `n` (Init): Creates session file, snapshots git commit.
    *   `u` (Update): Compresses session state for handover to next LLM window.
    *   `f` (Finish): Harvests learnings to `AI_CONTEXT.md`, deletes session file, and offers to squash commits.

### Git Integration
*   **Auto-Checkpoints**: The tool performs `git commit --no-verify` automatically during patching to prevent data loss.
*   **Squash on Finish**: The "Harvest" command (`f`) uses `git reset --soft <start_commit>` to squash session iteration commits into a single staged change, keeping history clean.

### Patching Standards
*   **Unified Diff**: The patcher supports standard unified diffs (with or without `diff --git` headers).
*   **Explicit Headers**: Legacy support for `# FILE: path` headers exists but raw diffs are preferred for speed.
*   **Markdown Robustness**: The patcher handles nested code blocks (e.g., inside docstrings). When generating code that includes markdown fences, use 4+ backticks for the outer container.
*   **Safety**: Large file changes without diff headers will trigger full overwrites (destructive).

## 3. Development Workflow
1.  **Start**: Run `kopipasta` and press `n` to begin a task.
2.  **Iterate**: Use the "Gardener" loop. Keep `AI_SESSION.md` updated via patches.
3.  **Commit**: The tool handles auto-checkpoints.
4.  **Harvest**: Press `f` to merge architectural changes here and clear the scratchpad.