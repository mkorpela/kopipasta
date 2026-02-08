# kopipasta

[![Version](https://img.shields.io/pypi/v/kopipasta.svg)](https://pypi.python.org/pypi/kopipasta)
[![Downloads](http://pepy.tech/badge/kopipasta)](http://pepy.tech/project/kopipasta)

**kopipasta bridges the gap between your local file system and LLM context windows.**

A CLI tool for taking **full, transparent control** of your prompt. No black boxes.

```text
➜  ~ kopipasta

  📁 Project Files
  |-- 📂 src/
  |   |-- ● 📄 main.py (4.2 KB)
  |   |-- ○ 📄 utils.py (1.5 KB)
  |-- ○ 📄 README.md (2.1 KB)

  Current: src/main.py | Selected: 1 full | ~4,200 chars
```

## You Control the Context

Many AI coding assistants automatically find what *they think* is relevant context. This is a black box. When the LLM gives a bad answer, you can't debug it because you don't know what context it was actually given.

**`kopipasta` is the opposite.** I built it on the principle of **explicit context control**. You are in the driver's seat. You decide *exactly* what files, functions, and snippets go into the prompt.

It's a "smart copy" command for your project, not a magic wand.

## How It Works

The workflow is a fast, iterative cycle:

1.  **Context:** Run `kopipasta` to select files and define your task.
2.  **Generate:** Paste the prompt into your LLM (ChatGPT, Claude, etc.).
3.  **Patch:** Press `p` in `kopipasta` and paste the LLM's response to apply changes or import new files.
4.  **Iterate:** Review with `git diff`, then repeat for the next step.

## The Three-State Selection Model

`kopipasta` tracks file state to distinguish between background context and active focus:

1.  **Unselected (White/Dim)**: File is not included in the prompt.
2.  **Base (Cyan)**: **"Synced Context."** Files that have already been sent to the LLM in a previous turn.
3.  **Delta (Green)**: **"Active Focus."** Newly selected files, files found via path scanning, or applied patches.

**Transitions:**
*   **Space**: Toggles a file between **Unselected** and **Delta** (Green).
*   **Extend (`e`)**: Copies only **Delta** files to the clipboard, then promotes them to **Base**.
*   **Patch (`p`)**: Promotes patched files to **Delta** (marking them as the current focus).

## Session Management

`kopipasta` uses a **Quad-Memory Architecture** to prevent context drift in long tasks.

### 🧠 Sessions (`AI_SESSION.md`)
Instead of re-explaining the task in every prompt, use a Session to maintain state.

1.  **Start (`n`)**: Creates `AI_SESSION.md` (a scratchpad) and snapshots your git commit.
2.  **Work**: The session file is pinned to your prompt. The LLM updates it (via patches) to track progress.
3.  **Update (`u`)**: Generates a handover prompt to compress the session state for the next LLM window.
4.  **Finish (`f`)**: Harvests architectural learnings into `AI_CONTEXT.md` (Constitution), deletes the session file, and offers to **squash** your work into a clean commit.

### 📜 Context (`AI_CONTEXT.md`)
The "Laws of Physics" for your project. This file is **always pinned** to the prompt if it exists. Use it for architecture decisions and tech stack constraints.

### 👤 Profile (`~/.config/kopipasta/ai_profile.md`)
Your global preferences (e.g., "I use VS Code", "Always use TypeScript"). Injected into every prompt automatically.

## Usage

### Universal Intake (`p`)

Press `p` to paste *any* text from your LLM. `kopipasta` intelligently decides how to handle it:

1.  **Code Patches**: If markdown code blocks are found (Full files or Unified Diffs), it applies them to your local files.
2.  **Intelligent Import**: If no code is found, it scans the text for valid project paths.
    *   *Example:* Paste a traceback or a list of "Files to analyze" suggested by the LLM.
    *   The tool will find the paths and ask if you want to **[A]ppend** them to your selection (as Delta/Green) or **[R]eplace** your selection entirely.

### Extend Context (`e`)

Mid-conversation, you often need to show the LLM one or two new files without regenerating the entire context.

1.  Select the new file(s) in the tree (they will turn **Green/Delta**).
2.  Press `e`.
3.  `kopipasta` copies a **minimal prompt** containing *only* the Green files.
4.  The files are automatically promoted to **Cyan/Base** (synced) for future turns.

### Creating a Full Prompt

By default `kopipasta` opens the tree selector on the current directory.

**Selection Basics:**
*   Every code block applied via a patch **must** start with a file path comment for the tool to locate the target.
    *   `# FILE: src/main.py`
*   **To EDIT**: Use **Unified Diff** format (`@@ ... @@`).
*   **To CREATE/OVERWRITE**: Provide the **FULL** file content.

**CLI Arguments:**
```bash
kopipasta [options] [files_or_directories_or_urls...]
```

*   `[files...]`: Paths or URLs to use as the starting point.
*   `-t TASK`, `--task TASK`: Provide the task description via CLI, skipping the editor.

## Interactive Controls

| Key | Action | Description |
| :--- | :--- | :--- |
| `Space` | **Toggle** | Cycle selection: `Unselected` $\leftrightarrow$ `Delta` (Green). |
| `p` | **Process** | Universal Intake. Applies patches OR imports file paths from text. |
| `e` | **Extend** | Copy only **Delta** (Green) files to clipboard -> Promote to Base. |
| `c` | **Clear Base**| Unselect **Base** (Cyan) files. Keep **Delta** (Green). |
| `s` | **Snippet** | Toggle Snippet Mode (include only first 50 lines). |
| `d` | **Deps** | Analyze imports and add related local files. |
| `g` | **Grep** | Search text patterns inside a directory. |
| `a` | **Add All** | Add all files in the current directory. |
| `r` | **Reuse** | Reuse file selection from the previous run. |
| `n` | **Start** | Initialize `AI_SESSION.md`. |
| `u` | **Update** | Generate "Handover" prompt to update session state. |
| `f` | **Finish** | Generate "Harvest" prompt, delete session, and squash. |
| `Enter` | **Expand** | Expand or collapse directory. |
| `q` | **Quit** | Finalize selection, copy full context (Base + Delta), and exit. |

## Installation

```bash
# Using uv (recommended)
uv tool install kopipasta

# For development
uv sync
```

## Safety First
*   Automatically respects `.gitignore`.
*   Detects secrets in `.env` files and asks to mask/redact them.
*   Heuristic protection against "snippet hallucinations" (prevents accidental file wipes).
