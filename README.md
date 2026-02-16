# kopipasta

[![Version](https://img.shields.io/pypi/v/kopipasta.svg)](https://pypi.python.org/pypi/kopipasta)
[![Downloads](http://pepy.tech/badge/kopipasta)](http://pepy.tech/project/kopipasta)

`cat project | LLM | patch` and repeat.

> "Finally, a tool that realizes my clipboard is the most important part of my AI workflow."
> — **Nobody**

`kopipasta` is a CLI tool for taking **full, transparent control** of your prompt context. No black boxes, no hidden RAG, no "magic" that breaks your build.

```text
➜  ~ kopipasta

  📁 Project Files
  |-- 📂 src/
  |   |-- ● 📄 main.py (4.2 KB)
  |   |-- ○ 📄 utils.py (1.5 KB)
  |-- ● 📄 AI_SESSION.md (0.8 KB)

  [j/k]: Nav  [Space]: Toggle  [p]: Patch  [e]: Extend  [q]: Copy & Quit
  Context: 2 files | ~5,000 chars | ~1,400 tokens
```

## You Control the Context

I built **`kopipasta`** on the principle of **explicit context control**. You are in the driver's seat. You decide *exactly* what files, functions, and snippets go into the prompt.

## How It Works

The workflow is a fast, iterative cycle:

1.  **Context:** Run `kopipasta` to select files and define your task.
2.  **Generate:** Paste the prompt into your LLM (ChatGPT, Claude, Gemini etc.).
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

**Note on External Agents:** You can also use the **Ralph Loop (`r`)** to expose this selection state to an MCP-capable agent (like Claude Desktop). The agent gets **Write Access** to **Delta** files and **Read Access** to the **entire project**.

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

### Fix Workflow (`x`)

When a commit fails or a linter reports errors, press `x` to auto-diagnose:

1.  `kopipasta` runs a configurable command (e.g., your pre-commit hook or linter).
2.  If it fails, error output is captured and scanned for project file paths.
3.  Affected files are added to **Delta** (Green) automatically.
4.  A diagnostic prompt (errors + `git diff` + affected files) is copied to your clipboard.
5.  Paste into your LLM, copy the response, press `p` to apply fixes.

**Configuration:** Add an HTML comment anywhere in `AI_CONTEXT.md`:
```html
<!-- KOPIPASTA_FIX_CMD: uv run pre-commit run --all-files -->
```

**Fallback:** If no command is configured, `kopipasta` checks for `.git/hooks/pre-commit`, then falls back to `git diff --check HEAD`.

### Ralph Loop (`r`) : MCP Agent Integration

> "If you can define success as making a freaking slow shell script pass, then why not give the task to computer?"
> — **mkorpela**

Turn `kopipasta` into a secure, verifiable sandbox for AI agents (like Claude Desktop).

1.  **Select Scope:**
    * **Green (Delta):** The *only* files the agent is allowed to **EDIT**.
    * **Read Access:** The agent can see your whole project (to understand context).
2. **Set the Goal:**
    Press `r`. Enter a verification command (e.g., `pytest`, `npm test`, `./check.sh`). This becomes the agent's definition of "done."
3. **Auto-Connect:**
    `kopipasta` generates the configuration and **automatically registers** the local MCP server in Claude Desktop.
4. **Delegate:**
    The agent wakes up, reads the context, makes edits, and runs your verification command to self-correct.

## Interactive Controls

| Key | Action | Description |
| :--- | :--- | :--- |
| `Space` | **Toggle** | Cycle selection: `Unselected` $\leftrightarrow$ `Delta` (Green). |
| `p` | **Process** | Universal Intake. Applies patches OR imports file paths from text. |
| `e` | **Extend** | Copy only **Delta** (Green) files to clipboard -> Promote to Base. |
| `x` | **Fix** | Run fix command, detect affected files, copy diagnostic prompt. |
| `c` | **Clear** | Open Clear/Reset menu (Selection, Task, or All). |
| `s` | **Snippet** | Toggle Snippet Mode (include only first 50 lines). |
| `d` | **Deps** | Analyze imports and add related local files. |
| `g` | **Grep** | Search text patterns inside a directory. |
| `a` | **Add All** | Add all files in the current directory. |
| `r` | **Ralph** | Configure MCP Server for Agentic workflows (Claude Desktop). |
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
