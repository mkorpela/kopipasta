# kopipasta

[![Version](https://img.shields.io/pypi/v/kopipasta.svg)](https://pypi.python.org/pypi/kopipasta)
[![Downloads](http://pepy.tech/badge/kopipasta)](http://pepy.tech/project/kopipasta)

A CLI tool for taking **full, transparent control** of your LLM context. No black boxes.

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
3.  **Define:** Your default editor (`$EDITOR`) opens for you to write your instructions to the LLM.
4.  **Paste:** The final, comprehensive prompt is now on your clipboard, ready to be pasted into ChatGPT, Gemini, Claude, or your LLM of choice.

## Installation

```bash
# Using pipx (recommended for CLI tools)
pipx install kopipasta

# Or using standard pip
pip install kopipasta
```

## Usage

```bash
kopipasta [options] [files_or_directories_or_urls...]
```

**Arguments:**

*   `[files_or_directories_or_urls...]`: One or more paths to files, directories, or web URLs to use as the starting point for your context.

**Options:**

*   `-t TASK`, `--task TASK`: Provide the task description directly on the command line, skipping the editor.

## Key Features

*   **Total Context Control:** Interactively select files, directories, snippets, or even individual functions. You see everything that goes into the prompt.
*   **Transparent & Explicit:** No hidden RAG. You know exactly what's in the prompt because you built it. This makes debugging LLM failures possible.
*   **Web-Aware:** Pulls in content directly from URLs—perfect for API documentation.
*   **Safety First:**
    *   Automatically respects your `.gitignore` rules.
    *   Detects if you're about to include secrets from a `.env` file and asks what to do.
*   **Context-Aware:** Keeps a running total of the prompt size (in characters and estimated tokens) so you don't overload the LLM's context window.
*   **Developer-Friendly:**
    *   Uses your familiar `$EDITOR` for writing task descriptions.
    *   Copies the final prompt directly to your clipboard.
    *   Provides syntax highlighting during chunk selection.

## A Real-World Example

I had a bug where my `setup.py` didn't include all the dependencies from `requirements.txt`.

1.  I ran `kopipasta -t "Update setup.py to read dependencies dynamically from requirements.txt" setup.py requirements.txt`.
2.  The tool confirmed the inclusion of both files and copied the complete prompt to my clipboard.
3.  I pasted the prompt into my LLM chat window.
4.  I copied the LLM's suggested code back into my local `setup.py`.
5.  I tested the changes and committed.

No manual file reading, no clumsy copy-pasting, just a clean, context-rich prompt that I had full control over.

## Configuration

Set your preferred command-line editor via the `EDITOR` environment variable.
```bash
export EDITOR=nvim  # or vim, nano, code --wait, etc.
```