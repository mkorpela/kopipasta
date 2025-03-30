# kopipasta

[![Version](https://img.shields.io/pypi/v/kopipasta.svg)](https://pypi.python.org/pypi/kopipasta)
[![Downloads](http://pepy.tech/badge/kopipasta)](http://pepy.tech/project/kopipasta)

Streamline your interaction with LLMs for coding tasks. `kopipasta` helps you provide comprehensive context (project structure, file contents, web content) and facilitates an interactive, patch-based workflow. Go beyond TAB TAB TAB and take control of your LLM context.

<img src="kopipasta.jpg" alt="kopipasta" width="300">

- An LLM told me that kopi means Coffee in some languages.. and a Diffusion model then made this delicious soup.

## Installation

You can install kopipasta using pipx (recommended) or pip:

```bash
# Using pipx (recommended)
pipx install kopipasta

# Or using pip
pip install kopipasta
```

## Usage

```bash
kopipasta [options] [files_or_directories_or_urls...]
```

**Arguments:**

*   `[files_or_directories_or_urls...]`: Paths to files, directories, or web URLs to include as context.

**Options:**

*   `-t TASK`, `--task TASK`: Provide the task description directly via the command line. If omitted (and not using `-I`), an editor will open for you to write the task.
*   `-I`, `--interactive`: Start an interactive chat session with Google's Gemini model after preparing the context. Requires `GOOGLE_API_KEY` environment variable.

**Examples:**

1.  **Generate prompt and copy to clipboard (classic mode):**
    ```bash
    # Interactively select files from src/, include config.json, fetch web content,
    # then open editor for task input. Copy final prompt to clipboard.
    kopipasta src/ config.json https://example.com/api-docs

    # Provide task directly, include specific files, copy final prompt.
    kopipasta -t "Refactor setup.py to read deps from requirements.txt" setup.py requirements.txt
    ```

2.  **Start an interactive chat session:**
    ```bash
    # Interactively select files, provide task directly, then start chat.
    kopipasta -I -t "Implement the apply_simple_patch function" kopipasta/main.py

    # Interactively select files, open editor for initial task, then start chat.
    kopipasta -I kopipasta/ tests/
    ```

## Workflow

`kopipasta` is designed to support the following workflow when working with LLMs (like Gemini, ChatGPT, Claude, etc.) for coding tasks:

1.  **Gather Context:** Run `kopipasta` with the relevant files, directories, and URLs. Interactively select exactly what content (full files, snippets, or specific code chunks/patches) should be included.
2.  **Define Task:** Provide your coding task instructions, either via the `-t` flag or through your default editor.
3.  **Interact (if using `-I`):**
    *   `kopipasta` prepares the context and your task as an initial prompt.
    *   An interactive chat session starts (currently using Google Gemini via `google-genai`).
    *   Discuss the task, clarify requirements, and ask the LLM to generate code.
    *   The initial prompt includes instructions guiding the LLM to provide incremental changes and clear explanations.
4.  **Request Patches (`-I` mode):**
    *   During the chat, use the `/patch` command to ask the LLM to provide the proposed changes in a structured format.
    *   `kopipasta` will prompt you to review the proposed patches (file, reasoning, code change).
5.  **Apply Patches (`-I` mode):**
    *   If you approve, `kopipasta` will attempt to automatically apply the patches to your local files. It validates that the original code exists and is unique before applying.
6.  **Test & Iterate:** Test the changes locally. If further changes are needed, continue the chat, request new patches, or make manual edits.
7.  **Commit:** Once satisfied, commit the changes.

For non-interactive mode, `kopipasta` generates the complete prompt (context + task) and copies it to your clipboard (Step 1 & 2). You can then paste this into your preferred LLM interface and proceed manually from Step 3 onwards.

## Features

*   **Comprehensive Context Generation:** Creates structured prompts including:
    *   Project directory tree overview.
    *   Selected file contents.
    *   Content fetched from web URLs.
    *   Your specific task instructions.
*   **Interactive File Selection:**
    *   Guides you through selecting files and directories.
    *   Option to include full file content, a snippet (first lines/bytes), or **select specific code chunks/patches** for large or complex files.
    *   Syntax highlighting during chunk selection for supported languages.
    *   Ignores files based on common `.gitignore` patterns and detects binary files.
    *   Displays estimated character/token counts during selection.
*   **Web Content Fetching:** Includes content directly from URLs. Handles JSON/CSV content types.
*   **Editor Integration:** Opens your preferred editor (`$EDITOR`) to input task instructions (if not using `-t`).
*   **Environment Variable Handling:** Detects potential secrets from a `.env` file in included content and prompts you to mask, skip, or keep them.
*   **Clipboard Integration:** Automatically copies the generated prompt to the clipboard (non-interactive mode).
*   **Interactive Chat Mode (`-I`, `--interactive`):**
    *   Starts a chat session directly after context generation.
    *   Uses the `google-genai` library to interact with Google's Gemini models.
    *   Requires the `GOOGLE_API_KEY` environment variable to be set.
    *   Includes built-in instructions for the LLM to encourage clear, iterative responses.
*   **Patch Management (`-I` mode):**
    *   `/patch` command to request structured code changes from the LLM.
    *   Prompts user to review proposed patches (reasoning, file, original/new code snippets).
    *   **Automatic patch application** to local files upon confirmation.

## Configuration

*   **Editor:** Set the `EDITOR` environment variable to your preferred command-line editor (e.g., `vim`, `nvim`, `nano`, `emacs`, `code --wait`).
*   **API Key (for `-I` mode):** Set the `GOOGLE_API_KEY` environment variable with your Google AI Studio API key to use the interactive chat feature.

## Real life example (Non-Interactive)

Context: I had a bug where `setup.py` didn't include all dependencies listed in `requirements.txt`.

1.  `kopipasta -t "Update setup.py to read dependencies dynamically from requirements.txt" setup.py requirements.txt`
2.  Paste the generated prompt (copied to clipboard) into my preferred LLM chat interface.
3.  Review the LLM's proposed code.
4.  Copy the code and update `setup.py` manually.
5.  Test the changes.

## Real life example (Interactive)

Context: I want to refactor a function in `main.py`.

1.  `export GOOGLE_API_KEY="YOUR_API_KEY_HERE"` (ensure key is set)
2.  `kopipasta -I -t "Refactor the handle_content function in main.py to be more modular" module/main.py`
3.  The tool gathers context, shows the file size, and confirms inclusion.
4.  An interactive chat session starts with the context and task sent to Gemini.
5.  Chat with the LLM:
    *   *User:* "Proceed"
    *   *LLM:* "Okay, I understand. My plan is to..."
    *   *User:* "Looks good."
    *   *LLM:* "Here's the first part of the refactoring..." (shows code)
6.  Use the `/patch` command:
    *   *User:* `/patch`
    *   `kopipasta` asks the LLM for structured patches.
    *   `kopipasta` displays proposed patches: "Apply 1 patch to module/main.py? (y/N):"
7.  Apply the patch:
    *   *User:* `y`
    *   `kopipasta` applies the change to `module/main.py`.
8.  Test locally. If it works, commit. If not, continue chatting, request more patches, or debug.

