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
        '*.war', '*.ear', '*.sqlite', '*.db', '.github', '.gitignore',
        '*.jpg', '*.jpeg', '*.png', '*.gif', '*.bmp', '*.tiff',
        '*.ico', '*.svg', '*.webp', '*.mp3', '*.mp4', '*.avi',
        '*.mov', '*.wmv', '*.flv', '*.pdf', '*.doc', '*.docx',
        '*.xls', '*.xlsx', '*.ppt', '*.pptx', '*.zip', '*.rar',
        '*.tar', '*.gz', '*.7z', '*.exe', '*.dll', '*.so', '*.dylib'
    ]
    gitignore_patterns = default_ignore_patterns.copy()

    if os.path.exists('.gitignore'):
        print(".gitignore detected.")
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

def is_binary(file_path):
    try:
        with open(file_path, 'rb') as file:
            return b'\0' in file.read(1024)
    except IOError:
        return False

def get_human_readable_size(size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0

def get_project_structure(ignore_patterns):
    tree = []
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d), ignore_patterns)]
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

def select_files_in_directory(directory, ignore_patterns, current_char_count=0):
    files = [f for f in os.listdir(directory)
             if os.path.isfile(os.path.join(directory, f)) and not is_ignored(os.path.join(directory, f), ignore_patterns) and not is_binary(os.path.join(directory, f))]

    if not files:
        return [], current_char_count

    print(f"\nDirectory: {directory}")
    print("Files:")
    for file in files:
        file_path = os.path.join(directory, file)
        file_size = os.path.getsize(file_path)
        file_size_readable = get_human_readable_size(file_size)
        file_char_estimate = file_size  # Assuming 1 byte ≈ 1 character for text files
        file_token_estimate = file_char_estimate // 4
        print(f"- {file} ({file_size_readable}, ~{file_char_estimate} chars, ~{file_token_estimate} tokens)")

    while True:
        print_char_count(current_char_count)
        choice = input("(y)es add all / (n)o ignore all / (s)elect individually / (q)uit? ").lower()
        if choice == 'y':
            for file in files:
                current_char_count += os.path.getsize(os.path.join(directory, file))
            print(f"Added all files from {directory}")
            return files, current_char_count
        elif choice == 'n':
            print(f"Ignored all files from {directory}")
            return [], current_char_count
        elif choice == 's':
            selected_files = []
            for file in files:
                file_path = os.path.join(directory, file)
                file_size = os.path.getsize(file_path)
                file_size_readable = get_human_readable_size(file_size)
                file_char_estimate = file_size
                file_token_estimate = file_char_estimate // 4
                while True:
                    if current_char_count > 0:
                        print_char_count(current_char_count)
                    file_choice = input(f"{file} ({file_size_readable}, ~{file_char_estimate} chars, ~{file_token_estimate} tokens) (y/n/q)? ").lower()
                    if file_choice == 'y':
                        selected_files.append(file)
                        current_char_count += file_char_estimate
                        break
                    elif file_choice == 'n':
                        break
                    elif file_choice == 'q':
                        print(f"Quitting selection for {directory}")
                        return selected_files, current_char_count
                    else:
                        print("Invalid choice. Please enter 'y', 'n', or 'q'.")
            print(f"Added {len(selected_files)} files from {directory}")
            return selected_files, current_char_count
        elif choice == 'q':
            print(f"Quitting selection for {directory}")
            return [], current_char_count
        else:
            print("Invalid choice. Please try again.")

def process_directory(directory, ignore_patterns, current_char_count=0):
    files_to_include = []
    processed_dirs = set()

    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d), ignore_patterns)]
        files = [f for f in files if not is_ignored(os.path.join(root, f), ignore_patterns) and not is_binary(os.path.join(root, f))]

        if root in processed_dirs:
            continue

        selected_files, current_char_count = select_files_in_directory(root, ignore_patterns, current_char_count)
        full_paths = [os.path.join(root, f) for f in selected_files]
        files_to_include.extend(full_paths)
        processed_dirs.add(root)

    return files_to_include, processed_dirs, current_char_count

def generate_prompt(files_to_include, ignore_patterns):
    prompt = "# Project Overview\n\n"
    prompt += "## Project Structure\n\n"
    prompt += "```\n"
    prompt += get_project_structure(ignore_patterns)
    prompt += "\n```\n\n"
    prompt += "## File Contents\n\n"
    for file in files_to_include:
        relative_path = get_relative_path(file)
        language = get_language_for_file(file)
        file_content = f"### {relative_path}\n\n```{language}\n{read_file_contents(file)}\n```\n\n"
        prompt += file_content
    prompt += "## Task Instructions\n\n"
    task_instructions = input("Enter the task instructions: ")
    prompt += f"{task_instructions}\n\n"
    prompt += "## Task Analysis and Planning\n\n"
    analysis_text = (
        "Before starting, explain the task back to me in your own words. "
        "Ask for any clarifications if needed. Once you're clear, ask to proceed.\n\n"
        "Then, outline a plan for the task. Finally, use your plan to complete the task."
    )
    prompt += analysis_text
    return prompt

def print_char_count(count):
    token_estimate = count // 4
    print(f"\rCurrent prompt size: {count} characters (~ {token_estimate} tokens)", flush=True)

def main():
    parser = argparse.ArgumentParser(description="Generate a prompt with project structure and file contents.")
    parser.add_argument('inputs', nargs='+', help='Files or directories to include in the prompt')
    args = parser.parse_args()

    ignore_patterns = read_gitignore()

    files_to_include = []
    processed_dirs = set()
    current_char_count = 0

    for input_path in args.inputs:
        if os.path.isfile(input_path):
            if not is_ignored(input_path, ignore_patterns) and not is_binary(input_path):
                files_to_include.append(input_path)
                print(f"Added file: {input_path}")
            else:
                print(f"Ignored file: {input_path}")
        elif os.path.isdir(input_path):
            dir_files, dir_processed, current_char_count = process_directory(input_path, ignore_patterns, current_char_count)
            files_to_include.extend(dir_files)
            processed_dirs.update(dir_processed)
        else:
            print(f"Warning: {input_path} is not a valid file or directory. Skipping.")

    if not files_to_include:
        print("No files were selected. Exiting.")
        return

    print("\nFile selection complete.")
    print_char_count(current_char_count)
    print(f"Summary: Added {len(files_to_include)} files from {len(processed_dirs)} directories.")

    prompt = generate_prompt(files_to_include, ignore_patterns)
    print("\n\nGenerated prompt:")
    print(prompt)

    # Copy the prompt to clipboard
    try:
        pyperclip.copy(prompt)
        separator = "\n" + "=" * 40 + "\n☕🍝       Kopipasta Complete!       🍝☕\n" + "=" * 40 + "\n"
        print(separator)
        final_char_count = len(prompt)
        final_token_estimate = final_char_count // 4
        print(f"Prompt has been copied to clipboard. Final size: {final_char_count} characters (~ {final_token_estimate} tokens)")
    except pyperclip.PyperclipException as e:
        print(f"Failed to copy to clipboard: {e}")

if __name__ == "__main__":
    main()