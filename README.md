# kopipasta

A CLI tool to generate prompts with project structure, file contents, and web content, while handling environment variables securely and offering snippets for large files.

<img src="kopipasta.jpg" alt="kopipasta" width="300">

## Installation

You can install kopipasta using pipx (or pip):

```
pipx install kopipasta
```

## Usage

To use kopipasta, run the following command in your terminal:

```
kopipasta [files_or_directories_or_urls]
```

Replace `[files_or_directories_or_urls]` with the paths to the files or directories you want to include in the prompt, as well as any web URLs you want to fetch content from.

Example:
```
kopipasta src/ config.json https://example.com/api-docs
```

This will generate a prompt including:
- The project structure
- Contents of the specified files and directories (with snippet options for large files)
- Content fetched from the provided URLs (with snippet options for large content)
- Handling of environment variables found in a `.env` file (if present)

Files and directories typically excluded in version control (based on common .gitignore patterns) are ignored.

The generated prompt will be displayed in the console and automatically copied to your clipboard.

## Features

- Generates a structured prompt with project overview, file contents, web content, and task instructions
- Offers snippet options for large files (>100 KB) and web content (>10,000 characters)
- Fetches and includes content from web URLs
- Detects and securely handles environment variables from a `.env` file
- Ignores files and directories based on common .gitignore patterns
- Allows interactive selection of files to include
- Automatically copies the generated prompt to the clipboard

## Environment Variable Handling

If a `.env` file is present in the current directory, kopipasta will:
1. Read and store the environment variables
2. Detect these variables in file contents and web content
3. Prompt you to choose how to handle each detected variable:
   - (m)ask: Replace the value with asterisks
   - (s)kip: Replace the value with "[REDACTED]"
   - (k)eep: Leave the value as-is

This ensures sensitive information is handled securely in the generated prompt.

## Snippet Functionality

For large files (>100 KB) and web content (>10,000 characters), kopipasta offers a snippet option:

- For files: The first 50 lines or 4 KB (4,096 bytes), whichever comes first
- For web content: The first 1,000 characters

This helps manage the overall prompt size while still providing useful information about the content structure.

## Example output

```bash
❯ kopipasta . https://example.com/api-docs

Directory: .
Files:
- __init__.py
- main.py (120 KB, ~120000 chars, ~30000 tokens)
- large_data.csv (5 MB, ~5000000 chars, ~1250000 tokens)
- .env

(y)es add all / (n)o ignore all / (s)elect individually / (q)uit? y
main.py is large. Use (f)ull content or (s)nippet? s
large_data.csv is large. Use (f)ull content or (s)nippet? s
Added all files from .
Added web content from: https://example.com/api-docs

File and web content selection complete.
Current prompt size: 10500 characters (~ 2625 tokens)
Summary: Added 3 files from 1 directory and 1 web source.

Detected environment variables:
- API_KEY=12345abcde

How would you like to handle API_KEY? (m)ask / (k)eep: m

Enter the task instructions: Implement new API endpoint

Generated prompt:
# Project Overview

## Project Structure

```
|-- ./
    |-- __init__.py
    |-- main.py
    |-- large_data.csv
    |-- .env
```

## File Contents

### __init__.py

```python
# Initialize package
```

### main.py (snippet)

```python
import os
import pandas as pd

API_KEY = os.getenv('API_KEY')

def process_data(file_path):
    df = pd.read_csv(file_path)
    # Rest of the function...

# More code...
```

### large_data.csv (snippet)

```
id,name,value
1,John,100
2,Jane,200
3,Bob,150
4,Alice,300
# ... (first 50 lines or 4 KB)
```

## Web Content

### https://example.com/api-docs (snippet)

```
API Documentation
Endpoint: /api/v1/data
Method: GET
Authorization: Bearer **********
...
```

## Task Instructions

Implement new API endpoint

## Task Analysis and Planning

Before starting, explain the task back to me in your own words. Ask for any clarifications if needed. Once you're clear, ask to proceed.

Then, outline a plan for the task. Finally, use your plan to complete the task.

Prompt has been copied to clipboard. Final size: 1500 characters (~ 375 tokens)
```

## License

This project is licensed under the MIT License.