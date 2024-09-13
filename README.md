
# kopipasta

A CLI tool to generate prompts with project structure and file contents.

<img src="kopipasta.jpg" alt="kopipasta" width="300">

## Installation

You can install kopipasta using pipx (or pip):

```
pipx install kopipasta
```

## Usage

To use kopipasta, run the following command in your terminal:

```
kopipasta [files_or_directories]
```

Replace `[files_or_directories]` with the paths to the files or directories you want to include in the prompt.

Example:
```
kopipasta src/ config.json
```

This will generate a prompt including the project structure and contents of the specified files and directories, ignoring files and directories typically excluded in version control (based on common .gitignore patterns).

The generated prompt will be displayed in the console and automatically copied to your clipboard.

## Features

- Generates a structured prompt with project overview, file contents, and task instructions
- Ignores files and directories based on common .gitignore patterns
- Allows interactive selection of files to include
- Automatically copies the generated prompt to the clipboard

## Example output

```bash
    ❯ kopipasta .

    Directory: .
    Files:
    - __init__.py
    - main.py

    (y)es add all / (n)o ignore all / (s)elect individually / (q)uit? s
    __init__.py (y/n/q)? y
    main.py (y/n/q)? n
    Added 1 files from .

    File selection complete.
    Summary: Added 1 files from 1 directories.
    Enter the task instructions: Do my work

    Generated prompt:
    # Project Overview

    ## Summary of Included Files

    - __init__.py

    ## Project Structure

    ```
    |-- ./
        |-- __init__.py
        |-- main.py
    ```

    ## File Contents

    ### __init__.py

    ```python

    ```

    ## Task Instructions

    Do my work

    ## Task Analysis and Planning

    Before starting, explain the task back to me in your own words. Ask for any clarifications if needed. Once you're clear, ask to proceed.

    Then, outline a plan for the task. Finally, use your plan to complete the task.

    Prompt has been copied to clipboard.
```

## License

This project is licensed under the MIT License.
