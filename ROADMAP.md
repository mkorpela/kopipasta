# Project Roadmap

This document tracks planned features and architectural improvements for `kopipasta`.

## 🚀 High Priority

### 1. Explicit File Deletion Support
**Problem:** The Patcher can create and edit files, but cannot delete them. Architectural refactors (e.g., migrating build systems) often require removing legacy files (`setup.py`, `requirements.txt`), forcing the user to manually intervene.
**Solution:**
- Define a syntax for deletion, e.g., a specific marker content like `<<<DELETE>>>` or a header flag `<!-- ACTION: DELETE -->`.
- Update `kopipasta/patcher.py` to recognize this signal.
- Prompt user for confirmation before deletion (`🗑️ Delete setup.py? y/N`).

### 2. Command Execution / Verification
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

### 3. Intelligent Selection Import (The "Director" Pattern)
**Problem:** After the LLM analyzes a broad context and requests specific files for the deep-dive, the user must manually find and select them in the tree. This is slow and error-prone.
**Solution:**
- **Action:** New hotkey (e.g., `i` for Import) in Tree View.
- **Input:** User pastes the raw LLM response (text).
- **Logic:** `kopipasta` regex-scans the text for strings matching valid paths in the project tree.
- **Result:** Automatically toggles selection for those files.
- **Benefit:** Allows the LLM to "drive" the context window for the next turn.


## 💡 Future Ideas

### Dynamic Environment Context
**Problem:** The LLM often lacks visibility into the specific local environment (Python version, OS details, installed tool versions), leading to "it works on my machine" suggestions (e.g., suggesting Python 3.8 compatible code when the local env is 3.12, or vice versa).
**Solution:**
- Add a configuration section to `AI_CONTEXT.md` or `ai_profile.md` defining "probe commands" (e.g., `python --version`, `uv --version`).
- Execute these commands during prompt generation and inject the output into a `## System Context` section.

### Tree-Sitter Integration
**Problem:** Regex-based patching is fragile for complex refactors.
**Solution:** Use Tree-Sitter for semantic awareness when applying diffs to code files, allowing for smarter context matching and syntax validation.