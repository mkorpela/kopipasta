
# kopipasta

![kopipasta](kopipasta.jpg)

A CLI tool to generate prompts with project structure and file contents.

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

## License

This project is licensed under the MIT License.
