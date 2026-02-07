# Project Roadmap

This document tracks planned features and architectural improvements for `kopipasta`. Items are
grouped by priority based on how much daily workflow friction they eliminate.

## 🚀 High Priority

### 1. Explicit File Deletion Support
**Problem:** The Patcher can create and edit files, but cannot delete them. Architectural refactors (e.g., migrating build systems) often require removing legacy files (`setup.py`, `requirements.txt`), forcing the user to manually intervene.
**Solution:**
- Define a syntax for deletion, e.g., a specific marker content like `<<<DELETE>>>` or a header flag `<!-- ACTION: DELETE -->`.
- Update `kopipasta/patcher.py` to recognize this signal.
- Prompt user for confirmation before deletion (`🗑️ Delete setup.py? y/N`).

### 2. Extend Context Mode (`--extend` / `e` hotkey)
**Problem:** Mid-conversation you often need to add a file or two to the LLM's context. Today this means regenerating the entire prompt (tree, task instructions, patching rules) just to get the raw content of one file. This is wasteful and breaks conversational flow.
**Solution:**
- **CLI mode**: `kopipasta --extend src/utils.py src/config.py` — outputs just the file contents (with `# FILE:` headers) to clipboard. No tree selector, no task instructions.
- **Interactive mode**: New hotkey `e` in the tree selector. Opens a minimal prompt that copies only the selected files' contents, formatted for pasting as a follow-up message.
- **Template**: A separate lightweight Jinja2 template (`extend_template.j2`) containing only the file content blocks, e.g.:
  ```
  Here are the additional files you requested:
  
  ### {{ file.path }}
  ```{{ file.language }}
  {{ file.content }}
  ```
  ```
- **Benefit**: Turns a multi-minute regeneration cycle into a 5-second hotkey press.

### 3. Intelligent Selection Import (The "Director" Pattern)
**Problem:** After the LLM analyzes a broad context and requests specific files for the deep-dive, the user must manually find and select them in the tree. This is slow and error-prone.
**Solution:**
- **Action:** New hotkey (e.g., `i` for Import) in Tree View.
- **Input:** User pastes the raw LLM response (text).
- **Logic:** `kopipasta` regex-scans the text for strings matching valid paths in the project tree.
- **Result:** Automatically toggles selection for those files.
- **Benefit:** Allows the LLM to "drive" the context window for the next turn.

### 4. Fix Hotkey — Pre-commit / Lint Integration (`x`)
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

### 5. Lightweight "Consult" Prompt (`c`)
**Problem:** Step 1 of the typical workflow is sending the full project context just so the LLM can tell you *which files are relevant*. This wastes tokens — the LLM only needs the tree structure and task description, not file contents.
**Solution:**
- **New hotkey `c`** that generates a prompt containing only: the project tree + `AI_CONTEXT.md` + `AI_SESSION.md` + task instructions. No file contents.
- The prompt instructs the LLM to respond with a list of file paths needed for the task.
- Pairs naturally with the "Import Selection" feature (#3): press `c`, paste into LLM, get file list back, press `i` to import selection, press `q` to generate the focused prompt.
- **Benefit**: The consult→import→generate cycle becomes the standard "step 1" with minimal token cost.

## 💡 Future Ideas

### Dynamic Environment Context
**Problem:** The LLM often lacks visibility into the specific local environment (Python version, OS details, installed tool versions), leading to "it works on my machine" suggestions (e.g., suggesting Python 3.8 compatible code when the local env is 3.12, or vice versa).
**Solution:**
- Add a configuration section to `AI_CONTEXT.md` or `ai_profile.md` defining "probe commands" (e.g., `python --version`, `uv --version`).
- Execute these commands during prompt generation and inject the output into a `## System Context` section.

### Command Execution / Verification
**Problem:** The LLM suggests verification steps (e.g., "Run `pytest`"), but the user must manually copy-paste them.
**Solution:**
- Define a syntax for suggested commands, e.g.:
  ```markdown
  <!-- COMMAND: verify -->
  ```bash
  uv run pytest
  ```
  ```
- Update `kopipasta` UI to parse these blocks.
- Add a hotkey (e.g., `v`) in the interactive menu to execute the suggested command immediately after patching.

### Tree-Sitter Integration
**Problem:** Regex-based patching is fragile for complex refactors.
**Solution:** Use Tree-Sitter for semantic awareness when applying diffs to code files, allowing for smarter context matching and syntax validation.

### Undo Last Patch
**Problem:** When a patch goes wrong, the user must manually `git checkout` the affected files.
**Solution:** Track which files were modified by the last patch application. Add a hotkey (e.g., `z`) that runs `git checkout -- <files>` to revert them instantly.

### Named Selection Profiles
**Problem:** The `r` (reuse) hotkey only remembers the last selection. When iterating between broad context and focused context, users want to recall specific named selections.
**Solution:** Allow saving selections as named profiles (e.g., `kopipasta --save-selection core-modules`) and loading them (`kopipasta --load-selection core-modules` or a hotkey in the tree selector).