# Project Roadmap

This document tracks planned features and architectural improvements for `kopipasta`.

### Fix Hotkey — Pre-commit / Lint Integration (`x`)
**Problem:** After applying patches, the user tries to commit. Pre-commit hooks (ruff, prettier, mypy, etc.) fail. The user must manually copy error output, switch back to the LLM, paste it, get fixes, then apply patches again. This loop happens on almost every commit.
**Solution:**
- **New hotkey `x`** ("fi**x**") in the tree selector that:
  1. Runs a configurable command (e.g., `uv run pre-commit run --all-files` or `ruff check .`).
  2. Captures stdout/stderr.
  3. If the command fails, generates a focused prompt containing: the error output + the affected files (auto-detected from the error output or from `git diff --name-only`).
  4. Copies the prompt to clipboard, ready to paste into the LLM.
- **Configuration** in `AI_CONTEXT.md` or `pyproject.toml [tool.kopipasta]`:
  ```markdown
  ## Fix Command
  `uv run pre-commit run --all-files`
  ```
- **Auto-fix layer** (optional): Before generating a prompt, attempt to run auto-fixable linters first (e.g., `ruff check --fix`, `prettier --write`). Only generate a prompt for errors that remain after auto-fix.
- **Benefit**: Collapses the "commit → fail → copy errors → paste → fix → patch → commit" loop into `x` → paste → `p` → commit.
