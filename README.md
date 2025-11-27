# kopipasta

[![Version](https://img.shields.io/pypi/v/kopipasta.svg)](https://pypi.python.org/pypi/kopipasta)
[![Downloads](http://pepy.tech/badge/kopipasta)](http://pepy.tech/project/kopipasta)

**kopipasta bridges the gap between your local file system and LLM context windows.**

A CLI tool for taking **full, transparent control** of your prompt. No black boxes.

<img src="kopipasta.jpg" alt="kopipasta" width="300">

- An LLM told me that "kopi" means Coffee in some languages... and a Diffusion model then made this delicious soup.

## The Philosophy: You Control the Context

Many AI coding assistants use Retrieval-Augmented Generation (RAG) to automatically find what *they think* is relevant context. This is a black box. When the LLM gives a bad answer, you can't debug it because you don't know what context it was actually given.

**`kopipasta` is the opposite.** I built it for myself on the principle of **explicit context control**. You are in the driver's seat. You decide *exactly* what files, functions, and snippets go into the prompt. This transparency is the key to getting reliable, debuggable results from an LLM.

It's a "smart copy" command for your project, not a magic wand.

## How It Works

The workflow is dead simple:

1.  **Gather:** Run `kopipasta` and point it at the files, directories, and URLs that matter for your task.
2.  **Select:** The tool interactively helps you choose what to include. For large files, you can send just a snippet or even hand-pick individual functions.
3.  **Define:** Write your instructions to the LLM in an interactive prompt directly in your terminal.
4.  **Paste:** The final, comprehensive prompt is now on your clipboard, ready to be pasted into ChatGPT, Gemini, Claude, or your LLM of choice.
5.  **Apply:** Inside the file selector, press `p`, paste the LLM's markdown response, and the tool will automatically patch your local files.

## Installation

```bash
# Using pipx (recommended for CLI tools)
pipx install kopipasta

# Or using standard pip
pip install kopipasta
```

## Usage

`kopipasta` has two main modes: creating prompts and applying patches.

### Creating a Prompt

```bash
kopipasta [options] [files_or_directories_or_urls...]
```

**Arguments:**

*   `[files_or_directories_or_urls...]`: One or more paths to files, directories, or web URLs to use as the starting point for your context.

**Options:**

*   `-t TASK`, `--task TASK`: Provide the task description directly on the command line, skipping the editor.

### Applying Patches

`kopipasta` can apply changes suggested by an LLM directly to your codebase, assuming you are in a Git repository.

1.  While running `kopipasta` in the interactive file selector, press the `p` key.
2.  Paste the entire markdown response from your LLM into the terminal prompt and submit.
3.  The tool will find code blocks with file paths (e.g., `// FILE: src/main.py`) and immediately write those changes to your local files.
4.  After applying, use standard Git commands like `git diff` to review the changes before staging and committing them.

## Key Features

*   **Total Context Control:** Interactively select files, directories, snippets, or even individual functions. You see everything that goes into the prompt.
*   **Interactive Code Patcher:** Press `p` in the file selector to paste and apply LLM-suggested changes directly to your local files. Relies on your version control (like Git) for safety, enabling a fast workflow.
*   **Transparent & Explicit:** No hidden RAG. You know exactly what's in the prompt because you built it. This makes debugging LLM failures possible.
*   **Web-Aware:** Pulls in content directly from URLs—perfect for API documentation.
*   **Safety First:**
    *   Automatically respects your `.gitignore` rules.
    *   Detects if you're about to include secrets from a `.env` file and asks what to do.
*   **Context-Aware:** Keeps a running total of the prompt size (in characters and estimated tokens) so you don't overload the LLM's context window.
*   **Developer-Friendly:**
    *   Provides a rich, interactive prompt for writing task descriptions in terminal.
    *   Copies the final prompt directly to your clipboard.
    *   Provides syntax highlighting during chunk selection.

## A Real-World Example

I had a bug where my `setup.py` didn't include all the dependencies from `requirements.txt`.

1.  I ran `kopipasta -t "Update setup.py to read dependencies dynamically from requirements.txt" setup.py requirements.txt`.
2.  The tool confirmed the inclusion of both files and copied the complete prompt to my clipboard.
3.  I pasted the prompt into my LLM chat window.
4.  I copied the LLM's response (which included a modified `setup.py` in a markdown code block).
5.  Inside `kopipasta`, I pressed `p`, pasted the response, and my local `setup.py` was updated.
6.  I ran `git diff` to review the changes, then tested and committed.

No manual file reading, no clumsy copy-pasting, just a clean, context-rich prompt that I had full control over, and a seamless way to apply the results.