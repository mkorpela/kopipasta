#!/usr/bin/env python3
import os
import argparse
import pyperclip
import fnmatch

def read_gitignore():
    default_ignore_patterns = [
        '.git', 'node_modules', 'venv', '.venv', 'dist', '.idea', '__pycache__',
        '*.pyc', '.ruff_cache', '.mypy_cache', '.pytest_cache', '.vscode', '.vite',
        '.terraform', 'output', 'poetry.lock', 'package-lock.json', '.env',
        '*.log', '*.bak', '*.swp', '*.swo', '*.tmp', 'tmp', 'temp', 'logs',
        'build', 'target', '.DS_Store', 'Thumbs.db', '*.class', '*.jar',
        '*.war', '*.ear', '*.sqlite', '*.db', '.github', '.gitignore'
    ]
    gitignore_patterns = default_ignore_patterns.copy()

    if os.path.exists('.gitignore'):
        with open('.gitignore', 'r') as file:
            for line in file:
                line = line.strip()
                if line and not line.startswith('#'):
                    gitignore_patterns.append(line)
    return gitignore_patterns

def is_ignored(path, ignore_patterns):
    path = os.path.normpath(path)
    for pattern in ignore_patterns:
        if fnmatch.fnmatch(os.path.basename(path), pattern) or fnmatch.fnmatch(path, pattern):
            return True
    return False

def get_project_structure(ignore_patterns):
    tree = []
    for root, dirs, files in os.walk('.'):
        # Remove ignored directories
        dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d), ignore_patterns)]
        # Remove ignored files
        files = [f for f in files if not is_ignored(os.path.join(root, f), ignore_patterns)]
        level = root.replace('.', '').count(os.sep)
        indent = ' ' * 4 * level + '|-- '
        tree.append(f"{indent}{os.path.basename(root)}/")
        subindent = ' ' * 4 * (level + 1) + '|-- '
        for f in files:
            tree.append(f"{subindent}{f}")
    return '\n'.join(tree)

def read_file_contents(file_path):
    try:
        with open(file_path, 'r') as file:
            return file.read()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return ""

def get_relative_path(file_path):
    return os.path.relpath(file_path)

def get_language_for_file(file_path):
    extension = os.path.splitext(file_path)[1].lower()
    language_map = {
        '.py': 'python',
        '.js': 'javascript',
        '.jsx': 'jsx',
        '.ts': 'typescript',
        '.tsx': 'tsx',
        '.html': 'html',
        '.css': 'css',
        '.json': 'json',
        '.md': 'markdown',
        '.sql': 'sql',
        '.sh': 'bash',
        '.yml': 'yaml',
        '.yaml': 'yaml',
        '.go': 'go',
        '.toml': 'toml'
    }
    return language_map.get(extension, '')

def select_files_in_directory(directory, ignore_patterns):
    files = [f for f in os.listdir(directory)
             if os.path.isfile(os.path.join(directory, f)) and not is_ignored(os.path.join(directory, f), ignore_patterns)]

    if not files:
        return []

    print(f"\nDirectory: {directory}")
    print("Files:")
    for file in files:
        print(f"- {file}")

    while True:
        choice = input("\n(y)es add all / (n)o ignore all / (s)elect individually / (q)uit? ").lower()
        if choice == 'y':
            print(f"Added all files from {directory}")
            return files
        elif choice == 'n':
            print(f"Ignored all files from {directory}")
            return []
        elif choice == 's':
            selected_files = []
            for file in files:
                while True:
                    file_choice = input(f"{file} (y/n/q)? ").lower()
                    if file_choice == 'y':
                        selected_files.append(file)
                        break
                    elif file_choice == 'n':
                        break
                    elif file_choice == 'q':
                        print(f"Quitting selection for {directory}")
                        return selected_files
                    else:
                        print("Invalid choice. Please enter 'y', 'n', or 'q'.")
            print(f"Added {len(selected_files)} files from {directory}")
            return selected_files
        elif choice == 'q':
            print(f"Quitting selection for {directory}")
            return []
        else:
            print("Invalid choice. Please try again.")

def process_directory(directory, ignore_patterns):
    files_to_include = []
    processed_dirs = set()

    for root, dirs, files in os.walk(directory):
        # Remove ignored directories
        dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d), ignore_patterns)]
        # Remove ignored files
        files = [f for f in files if not is_ignored(os.path.join(root, f), ignore_patterns)]

        if root in processed_dirs:
            continue

        selected_files = select_files_in_directory(root, ignore_patterns)
        full_paths = [os.path.join(root, f) for f in selected_files]
        files_to_include.extend(full_paths)
        processed_dirs.add(root)

    return files_to_include, processed_dirs

def generate_prompt(files_to_include, ignore_patterns):
    prompt = "# Project Overview\n\n"
    prompt += "## Summary of Included Files\n\n"
    for file in files_to_include:
        relative_path = get_relative_path(file)
        prompt += f"- {relative_path}\n"
    prompt += "\n"

    prompt += "## Project Structure\n\n"
    prompt += "```\n"
    prompt += get_project_structure(ignore_patterns)
    prompt += "\n```\n\n"

    prompt += "## File Contents\n\n"
    for file in files_to_include:
        relative_path = get_relative_path(file)
        language = get_language_for_file(file)
        prompt += f"### {relative_path}\n\n"
        prompt += f"```{language}\n"
        prompt += read_file_contents(file)
        prompt += "\n```\n\n"

    prompt += "## Task Instructions\n\n"
    task_instructions = input("Enter the task instructions: ")
    prompt += f"{task_instructions}\n\n"

    prompt += "## Task Analysis and Planning\n\n"
    prompt += (
        "Before starting, explain the task back to me in your own words. "
        "Ask for any clarifications if needed. Once you're clear, ask to proceed.\n\n"
        "Then, outline a plan for the task. Finally, use your plan to complete the task."
    )

    return prompt

def main():
    parser = argparse.ArgumentParser(description="Generate a prompt with project structure and file contents.")
    parser.add_argument('inputs', nargs='+', help='Files or directories to include in the prompt')
    args = parser.parse_args()

    ignore_patterns = read_gitignore()

    files_to_include = []
    processed_dirs = set()

    for input_path in args.inputs:
        if os.path.isfile(input_path):
            if not is_ignored(input_path, ignore_patterns):
                files_to_include.append(input_path)
                print(f"Added file: {input_path}")
            else:
                print(f"Ignored file: {input_path}")
        elif os.path.isdir(input_path):
            dir_files, dir_processed = process_directory(input_path, ignore_patterns)
            files_to_include.extend(dir_files)
            processed_dirs.update(dir_processed)
        else:
            print(f"Warning: {input_path} is not a valid file or directory. Skipping.")

    if not files_to_include:
        print("No files were selected. Exiting.")
        return

    print("\nFile selection complete.")
    print(f"Summary: Added {len(files_to_include)} files from {len(processed_dirs)} directories.")

    prompt = generate_prompt(files_to_include, ignore_patterns)
    print("\nGenerated prompt:")
    print(prompt)

    # Copy the prompt to clipboard
    try:
        pyperclip.copy(prompt)
        print("\nPrompt has been copied to clipboard.")
    except pyperclip.PyperclipException as e:
        print(f"Failed to copy to clipboard: {e}")

if __name__ == "__main__":
    main()

