#!/usr/bin/env python3
import csv
import io
import json
import os
import argparse
import sys
import re
import subprocess
import tempfile
from typing import Dict, List, Optional, Set, Tuple
import pyperclip
import fnmatch
from pygments import highlight
from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.formatters import TerminalFormatter
import pygments.util

import requests

from pydantic import BaseModel, Field
import traceback
from google import genai
from google.genai.types import GenerateContentConfig

FileTuple = Tuple[str, bool, Optional[List[str]], str]

class SimplePatchItem(BaseModel):
    """A single change described by reasoning, file path, original text, and new text."""
    reasoning: str = Field(..., description="Explanation for why this specific change is proposed.")
    file_path: str = Field(..., description="Relative path to the file to be modified.")
    original_text: str = Field(..., description="The exact, unique block of text to be replaced.")
    new_text: str = Field(..., description="The text to replace the original_text with.")

class SimplePatchArgs(BaseModel):
    """A list of proposed code changes."""
    patches: List[SimplePatchItem] = Field(..., description="A list of patches to apply.")

def apply_simple_patch(patch_item: SimplePatchItem) -> bool:
    """
    Applies a single patch defined by replacing original_text with new_text in file_path.

    Validates that the file exists and the original_text is unique.
    """
    print(f"\nApplying patch to: {patch_item.file_path}")
    print(f"Reasoning: {patch_item.reasoning}")
    print("-" * 20)

    file_path = patch_item.file_path
    original_text = patch_item.original_text
    new_text = patch_item.new_text

    # --- Validation ---
    if not os.path.exists(file_path):
        print(f"❌ Error: File not found: {file_path}")
        print("-" * 20)
        return False

    try:
        # Read the file content, attempting to preserve line endings implicitly
        with open(file_path, 'r', encoding='utf-8', newline='') as f:
            content = f.read()

        # Check for unique occurrence of original_text
        occurrences = content.count(original_text)
        if occurrences == 0:
            print(f"❌ Error: Original text not found in {file_path}.")
            # Optional: print a snippet of the expected text for debugging
            # print(f"   Expected original text snippet: '{original_text[:100]}...'")
            print("-" * 20)
            return False
        elif occurrences > 1:
            print(f"❌ Error: Original text is not unique in {file_path} (found {occurrences} times).")
            print(f"   Patch cannot be applied automatically due to ambiguity.")
            # print(f"   Ambiguous original text snippet: '{original_text[:100]}...'")
            print("-" * 20)
            return False

        # --- Application ---
        # Replace the single unique occurrence
        new_content = content.replace(original_text, new_text, 1)

        # Heuristic to check if a newline might be needed at the end
        original_ends_with_newline = content.endswith(('\n', '\r'))
        new_ends_with_newline = new_content.endswith(('\n', '\r'))

        if original_ends_with_newline and not new_ends_with_newline and new_content:
             # Try to determine the original newline type
             if content.endswith('\r\n'):
                 new_content += '\r\n'
             else: # Assume '\n' otherwise
                 new_content += '\n'
        elif not original_ends_with_newline and new_ends_with_newline:
             # If original didn't end with newline, remove the one added by replacement
             # This is less common but possible if new_text ends with \n and original_text didn't
             new_content = new_content.rstrip('\r\n')


        # Write the modified content back
        with open(file_path, 'w', encoding='utf-8', newline='') as f:
            f.write(new_content)

        print(f"✅ Patch applied successfully to {file_path}.")
        print("-" * 20)
        return True

    except IOError as e:
        print(f"❌ Error reading or writing file {file_path}: {e}")
        print("-" * 20)
        return False
    except Exception as e:
        print(f"❌ An unexpected error occurred during patch application: {e}")
        traceback.print_exc()
        print("-" * 20)
        return False

def get_colored_code(file_path, code):
     try:
         lexer = get_lexer_for_filename(file_path)
     except pygments.util.ClassNotFound:
         lexer = TextLexer()
     return highlight(code, lexer, TerminalFormatter())

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
            chunk = file.read(1024)
            if b'\0' in chunk:  # null bytes indicate binary file
                return True
            if file_path.lower().endswith(('.json', '.csv')):
                return False
            return False
    except IOError:
        return False

def get_human_readable_size(size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0

def is_large_file(file_path, threshold=102400):  # 100 KB threshold
    return os.path.getsize(file_path) > threshold

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
        '.htm': 'html',
        '.css': 'css',
        '.json': 'json',
        '.md': 'markdown',
        '.sql': 'sql',
        '.sh': 'bash',
        '.yml': 'yaml',
        '.yaml': 'yaml',
        '.go': 'go',
        '.toml': 'toml',
        '.c': 'c',
        '.cpp': 'cpp',
        '.cc': 'cpp',
        '.h': 'cpp',
        '.hpp': 'cpp',
    }
    return language_map.get(extension, '')

def split_python_file(file_content):
    """
    Splits Python code into logical chunks using the AST module.
    Ensures each chunk is at least 10 lines.
    Returns a list of tuples: (chunk_code, start_line, end_line)
    """
    import ast
    tree = ast.parse(file_content)
    chunks = []
    prev_end = 0
    lines = file_content.splitlines(keepends=True)

    def get_code(start, end):
        return ''.join(lines[start:end])

    nodes = [node for node in ast.iter_child_nodes(tree) if hasattr(node, 'lineno')]

    i = 0
    while i < len(nodes):
        node = nodes[i]
        start_line = node.lineno - 1  # Convert to 0-indexed
        end_line = getattr(node, 'end_lineno', None)
        if end_line is None:
            end_line = start_line + 1

        # Merge chunks to meet minimum lines
        chunk_start = start_line
        chunk_end = end_line
        while (chunk_end - chunk_start) < 10 and i + 1 < len(nodes):
            i += 1
            next_node = nodes[i]
            next_start = next_node.lineno - 1
            next_end = getattr(next_node, 'end_lineno', None) or next_start + 1
            chunk_end = next_end

        # Add code before the node (e.g., imports or global code)
        if prev_end < chunk_start:
            code = get_code(prev_end, chunk_start)
            if code.strip():
                chunks.append((code, prev_end, chunk_start))

        # Add the merged chunk
        code = get_code(chunk_start, chunk_end)
        chunks.append((code, chunk_start, chunk_end))
        prev_end = chunk_end
        i += 1

    # Add any remaining code at the end
    if prev_end < len(lines):
        code = get_code(prev_end, len(lines))
        if code.strip():
            chunks.append((code, prev_end, len(lines)))

    return merge_small_chunks(chunks)

def merge_small_chunks(chunks, min_lines=10):
    """
    Merges chunks to ensure each has at least min_lines lines.
    """
    merged_chunks = []
    buffer_code = ''
    buffer_start = None
    buffer_end = None

    for code, start_line, end_line in chunks:
        num_lines = end_line - start_line
        if buffer_code == '':
            buffer_code = code
            buffer_start = start_line
            buffer_end = end_line
        else:
            buffer_code += code
            buffer_end = end_line

        if (buffer_end - buffer_start) >= min_lines:
            merged_chunks.append((buffer_code, buffer_start, buffer_end))
            buffer_code = ''
            buffer_start = None
            buffer_end = None

    if buffer_code:
        merged_chunks.append((buffer_code, buffer_start, buffer_end))

    return merged_chunks

def split_javascript_file(file_content):
    """
    Splits JavaScript code into logical chunks using regular expressions.
    Returns a list of tuples: (chunk_code, start_line, end_line)
    """
    lines = file_content.splitlines(keepends=True)
    chunks = []
    pattern = re.compile(
        r'^\s*(export\s+)?(async\s+)?(function\s+\w+|class\s+\w+|\w+\s*=\s*\(.*?\)\s*=>)',
        re.MULTILINE
    )
    matches = list(pattern.finditer(file_content))

    if not matches:
        return [(file_content, 0, len(lines))]

    prev_end_line = 0
    for match in matches:
        start_index = match.start()
        start_line = file_content.count('\n', 0, start_index)
        if prev_end_line < start_line:
            code = ''.join(lines[prev_end_line:start_line])
            chunks.append((code, prev_end_line, start_line))

        function_code_lines = []
        brace_count = 0
        in_block = False
        for i in range(start_line, len(lines)):
            line = lines[i]
            function_code_lines.append(line)
            brace_count += line.count('{') - line.count('}')
            if '{' in line:
                in_block = True
            if in_block and brace_count == 0:
                end_line = i + 1
                code = ''.join(function_code_lines)
                chunks.append((code, start_line, end_line))
                prev_end_line = end_line
                break
        else:
            end_line = len(lines)
            code = ''.join(function_code_lines)
            chunks.append((code, start_line, end_line))
            prev_end_line = end_line

    if prev_end_line < len(lines):
        code = ''.join(lines[prev_end_line:])
        chunks.append((code, prev_end_line, len(lines)))

    return merge_small_chunks(chunks)

def split_html_file(file_content):
    """
    Splits HTML code into logical chunks based on top-level elements using regular expressions.
    Returns a list of tuples: (chunk_code, start_line, end_line)
    """
    pattern = re.compile(r'<(?P<tag>\w+)(\s|>).*?</(?P=tag)>', re.DOTALL)
    lines = file_content.splitlines(keepends=True)
    chunks = []
    matches = list(pattern.finditer(file_content))

    if not matches:
        return [(file_content, 0, len(lines))]

    prev_end = 0
    for match in matches:
        start_index = match.start()
        end_index = match.end()
        start_line = file_content.count('\n', 0, start_index)
        end_line = file_content.count('\n', 0, end_index)

        if prev_end < start_line:
            code = ''.join(lines[prev_end:start_line])
            chunks.append((code, prev_end, start_line))

        code = ''.join(lines[start_line:end_line])
        chunks.append((code, start_line, end_line))
        prev_end = end_line

    if prev_end < len(lines):
        code = ''.join(lines[prev_end:])
        chunks.append((code, prev_end, len(lines)))

    return merge_small_chunks(chunks)

def split_c_file(file_content):
    """
    Splits C/C++ code into logical chunks using regular expressions.
    Returns a list of tuples: (chunk_code, start_line, end_line)
    """
    pattern = re.compile(r'^\s*(?:[\w\*\s]+)\s+(\w+)\s*\([^)]*\)\s*\{', re.MULTILINE)
    lines = file_content.splitlines(keepends=True)
    chunks = []
    matches = list(pattern.finditer(file_content))

    if not matches:
        return [(file_content, 0, len(lines))]

    prev_end_line = 0
    for match in matches:
        start_index = match.start()
        start_line = file_content.count('\n', 0, start_index)
        if prev_end_line < start_line:
            code = ''.join(lines[prev_end_line:start_line])
            chunks.append((code, prev_end_line, start_line))

        function_code_lines = []
        brace_count = 0
        in_function = False
        for i in range(start_line, len(lines)):
            line = lines[i]
            function_code_lines.append(line)
            brace_count += line.count('{') - line.count('}')
            if '{' in line:
                in_function = True
            if in_function and brace_count == 0:
                end_line = i + 1
                code = ''.join(function_code_lines)
                chunks.append((code, start_line, end_line))
                prev_end_line = end_line
                break
        else:
            end_line = len(lines)
            code = ''.join(function_code_lines)
            chunks.append((code, start_line, end_line))
            prev_end_line = end_line

    if prev_end_line < len(lines):
        code = ''.join(lines[prev_end_line:])
        chunks.append((code, prev_end_line, len(lines)))

    return merge_small_chunks(chunks)

def split_generic_file(file_content):
    """
    Splits generic text files into chunks based on double newlines.
    Returns a list of tuples: (chunk_code, start_line, end_line)
    """
    lines = file_content.splitlines(keepends=True)
    chunks = []
    start = 0
    for i, line in enumerate(lines):
        if line.strip() == '':
            if start < i:
                chunk_code = ''.join(lines[start:i])
                chunks.append((chunk_code, start, i))
            start = i + 1
    if start < len(lines):
        chunk_code = ''.join(lines[start:])
        chunks.append((chunk_code, start, len(lines)))
    return merge_small_chunks(chunks)

def select_file_patches(file_path):
    file_content = read_file_contents(file_path)
    language = get_language_for_file(file_path)
    chunks = []
    total_char_count = 0

    if language == 'python':
        code_chunks = split_python_file(file_content)
    elif language == 'javascript':
        code_chunks = split_javascript_file(file_content)
    elif language == 'html':
        code_chunks = split_html_file(file_content)
    elif language in ['c', 'cpp']:
        code_chunks = split_c_file(file_content)
    else:
        code_chunks = split_generic_file(file_content)
    placeholder = get_placeholder_comment(language)

    print(f"\nSelecting patches for {file_path}")
    for index, (chunk_code, start_line, end_line) in enumerate(code_chunks):
        print(f"\nChunk {index + 1} (Lines {start_line + 1}-{end_line}):")
        colored_chunk = get_colored_code(file_path, chunk_code)
        print(colored_chunk)
        while True:
            choice = input("(y)es include / (n)o skip / (q)uit rest of file? ").lower()
            if choice == 'y':
                chunks.append(chunk_code)
                total_char_count += len(chunk_code)
                break
            elif choice == 'n':
                if not chunks or chunks[-1] != placeholder:
                    chunks.append(placeholder)
                total_char_count += len(placeholder)
                break
            elif choice == 'q':
                print("Skipping the rest of the file.")
                if chunks and chunks[-1] != placeholder:
                    chunks.append(placeholder)
                return chunks, total_char_count
            else:
                print("Invalid choice. Please enter 'y', 'n', or 'q'.")

    return chunks, total_char_count

def get_placeholder_comment(language):
    comments = {
        'python': '# Skipped content\n',
        'javascript': '// Skipped content\n',
        'typescript': '// Skipped content\n',
        'java': '// Skipped content\n',
        'c': '// Skipped content\n',
        'cpp': '// Skipped content\n',
        'html': '<!-- Skipped content -->\n',
        'css': '/* Skipped content */\n',
        'default': '# Skipped content\n'
    }
    return comments.get(language, comments['default'])

def get_file_snippet(file_path, max_lines=50, max_bytes=4096):
    snippet = ""
    byte_count = 0
    with open(file_path, 'r') as file:
        for i, line in enumerate(file):
            if i >= max_lines or byte_count >= max_bytes:
                break
            snippet += line
            byte_count += len(line.encode('utf-8'))
    return snippet

def get_colored_file_snippet(file_path, max_lines=50, max_bytes=4096):
    snippet = get_file_snippet(file_path, max_lines, max_bytes)
    return get_colored_code(file_path, snippet)

def print_char_count(count):
    token_estimate = count // 4
    print(f"\rCurrent prompt size: {count} characters (~ {token_estimate} tokens)", flush=True)

def select_files_in_directory(directory: str, ignore_patterns: List[str], current_char_count: int = 0) -> Tuple[List[FileTuple], int]:
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
        selected_files: List[FileTuple] = []
        if choice == 'y':
            for file in files:
                file_path = os.path.join(directory, file)
                if is_large_file(file_path):
                    while True:
                        snippet_choice = input(f"{file} is large. Use (f)ull content or (s)nippet? ").lower()
                        if snippet_choice in ['f', 's']:
                            break
                        print("Invalid choice. Please enter 'f' or 's'.")
                    if snippet_choice == 's':
                        selected_files.append((file, True, None, get_language_for_file(file)))
                        current_char_count += len(get_file_snippet(file_path))
                    else:
                        selected_files.append((file, False, None, get_language_for_file(file)))
                        current_char_count += os.path.getsize(file_path)
                else:
                    selected_files.append((file, False, None, get_language_for_file(file)))
                    current_char_count += os.path.getsize(file_path)
            print(f"Added all files from {directory}")
            return selected_files, current_char_count
        elif choice == 'n':
            print(f"Ignored all files from {directory}")
            return [], current_char_count
        elif choice == 's':
            for file in files:
                file_path = os.path.join(directory, file)
                file_size = os.path.getsize(file_path)
                file_size_readable = get_human_readable_size(file_size)
                file_char_estimate = file_size
                file_token_estimate = file_char_estimate // 4
                while True:
                    if current_char_count > 0:
                        print_char_count(current_char_count)
                    file_choice = input(f"{file} ({file_size_readable}, ~{file_char_estimate} chars, ~{file_token_estimate} tokens) (y/n/p/q)? ").lower()
                    if file_choice == 'y':
                        if is_large_file(file_path):
                            while True:
                                snippet_choice = input(f"{file} is large. Use (f)ull content or (s)nippet? ").lower()
                                if snippet_choice in ['f', 's']:
                                    break
                                print("Invalid choice. Please enter 'f' or 's'.")
                            if snippet_choice == 's':
                                selected_files.append((file, True, None, get_language_for_file(file_path)))
                                current_char_count += len(get_file_snippet(file_path))
                            else:
                                selected_files.append((file, False, None, get_language_for_file(file_path)))
                                current_char_count += file_char_estimate
                        else:
                            selected_files.append((file, False, None, get_language_for_file(file_path)))
                            current_char_count += file_char_estimate
                        break
                    elif file_choice == 'n':
                        break
                    elif file_choice == 'p':
                        chunks, char_count = select_file_patches(file_path)
                        if chunks:
                            selected_files.append((file_path, False, chunks, get_language_for_file(file_path)))
                            current_char_count += char_count
                        break
                    elif file_choice == 'q':
                        print(f"Quitting selection for {directory}")
                        return selected_files, current_char_count
                    else:
                        print("Invalid choice. Please enter 'y', 'n', 'p', or 'q'.")
            print(f"Added {len(selected_files)} files from {directory}")
            return selected_files, current_char_count
        elif choice == 'q':
            print(f"Quitting selection for {directory}")
            return [], current_char_count
        else:
            print("Invalid choice. Please try again.")

def process_directory(directory: str, ignore_patterns: List[str], current_char_count: int = 0) -> Tuple[List[FileTuple], Set[str], int]:
    files_to_include: List[FileTuple] = []
    processed_dirs: Set[str] = set()

    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d), ignore_patterns)]
        files = [f for f in files if not is_ignored(os.path.join(root, f), ignore_patterns) and not is_binary(os.path.join(root, f))]

        if root in processed_dirs:
            continue

        print(f"\nExploring directory: {root}")
        choice = input("(y)es explore / (n)o skip / (q)uit? ").lower()
        if choice == 'y':
            selected_files, current_char_count = select_files_in_directory(root, ignore_patterns, current_char_count)
            for file_tuple in selected_files:
                full_path = os.path.join(root, file_tuple[0])
                files_to_include.append((full_path, file_tuple[1], file_tuple[2], file_tuple[3]))
            processed_dirs.add(root)
        elif choice == 'n':
            dirs[:] = []  # Skip all subdirectories
            continue
        elif choice == 'q':
            break
        else:
            print("Invalid choice. Skipping this directory.")
            continue

    return files_to_include, processed_dirs, current_char_count

def fetch_web_content(url: str) -> Tuple[Optional[FileTuple], Optional[str], Optional[str]]:
    try:
        response = requests.get(url)
        response.raise_for_status()
        content_type = response.headers.get('content-type', '').lower()
        full_content = response.text
        snippet = full_content[:10000] + "..." if len(full_content) > 10000 else full_content

        if 'json' in content_type:
            content_type = 'json'
        elif 'csv' in content_type:
            content_type = 'csv'
        else:
            content_type = 'text'

        return (url, False, None, content_type), full_content, snippet
    except requests.RequestException as e:
        print(f"Error fetching content from {url}: {e}")
        return None, None, None

def read_file_content(file_path):
    _, ext = os.path.splitext(file_path)
    if ext.lower() == '.json':
        with open(file_path, 'r') as f:
            return json.load(f), 'json'
    elif ext.lower() == '.csv':
        with open(file_path, 'r') as f:
            return f.read(), 'csv'
    else:
        with open(file_path, 'r') as f:
            return f.read(), 'text'

def get_content_snippet(content, content_type, max_lines=50, max_chars=4096):
    if content_type == 'json':
        return json.dumps(content, indent=2)[:max_chars]
    elif content_type == 'csv':
        csv_content = content if isinstance(content, str) else content.getvalue()
        csv_reader = csv.reader(io.StringIO(csv_content))
        rows = list(csv_reader)[:max_lines]
        output = io.StringIO()
        csv.writer(output).writerows(rows)
        return output.getvalue()[:max_chars]
    else:
        return '\n'.join(content.split('\n')[:max_lines])[:max_chars]

def handle_content(content, content_type, file_or_url):
    is_large = len(json.dumps(content)) > 102400 if content_type == 'json' else len(content) > 102400

    if is_large:
        while True:
            choice = input(f"{file_or_url} is large. View (f)ull content, (s)nippet, or (p)review? ").lower()
            if choice in ['f', 's', 'p']:
                break
            print("Invalid choice. Please enter 'f', 's', or 'p'.")

        if choice == 'f':
            return content, False
        elif choice == 's':
            return get_content_snippet(content, content_type), True
        else:  # preview
            preview = get_content_preview(content, content_type)
            print(f"\nPreview of {file_or_url}:\n{preview}\n")
            return handle_content(content, content_type, file_or_url)
    else:
        return content, False


def get_content_preview(content, content_type):
    if content_type == 'json':
        return json.dumps(content, indent=2)[:1000] + "\n..."
    elif content_type == 'csv':
        csv_content = content if isinstance(content, str) else content.getvalue()
        csv_reader = csv.reader(io.StringIO(csv_content))
        rows = list(csv_reader)[:10]
        output = io.StringIO()
        csv.writer(output).writerows(rows)
        return output.getvalue() + "\n..."
    else:
        return '\n'.join(content.split('\n')[:20]) + "\n..."

def read_env_file():
    env_vars = {}
    if os.path.exists('.env'):
        with open('.env', 'r') as env_file:
            for line in env_file:
                line = line.strip()
                if line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip()
    return env_vars

def detect_env_variables(content, env_vars):
    detected_vars = []
    for key, value in env_vars.items():
        if value in content:
            detected_vars.append((key, value))
    return detected_vars

def handle_env_variables(content, env_vars):
    detected_vars = detect_env_variables(content, env_vars)
    if not detected_vars:
        return content

    print("Detected environment variables:")
    for key, value in detected_vars:
        print(f"- {key}={value}")

    for key, value in detected_vars:
        while True:
            choice = input(f"How would you like to handle {key}? (m)ask / (s)kip / (k)eep: ").lower()
            if choice in ['m', 's', 'k']:
                break
            print("Invalid choice. Please enter 'm', 's', or 'k'.")

        if choice == 'm':
            content = content.replace(value, '*' * len(value))
        elif choice == 's':
            content = content.replace(value, "[REDACTED]")
        # If 'k', we don't modify the content

    return content

def generate_prompt_template(files_to_include: List[FileTuple], ignore_patterns: List[str], web_contents: Dict[str, Tuple[FileTuple, str]], env_vars: Dict[str, str]) -> Tuple[str, int]:
    prompt = "# Project Overview\n\n"
    prompt += "## Project Structure\n\n"
    prompt += "```\n"
    prompt += get_project_structure(ignore_patterns)
    prompt += "\n```\n\n"
    prompt += "## File Contents\n\n"
    for file, use_snippet, chunks, content_type in files_to_include:
        relative_path = get_relative_path(file)
        language = content_type if content_type else get_language_for_file(file)

        if chunks is not None:
            prompt += f"### {relative_path} (selected patches)\n\n```{language}\n"
            for chunk in chunks:
                prompt += f"{chunk}\n"
            prompt += "```\n\n"
        elif use_snippet:
            file_content = get_file_snippet(file)
            prompt += f"### {relative_path} (snippet)\n\n```{language}\n{file_content}\n```\n\n"
        else:
            file_content = read_file_contents(file)
            file_content = handle_env_variables(file_content, env_vars)
            prompt += f"### {relative_path}\n\n```{language}\n{file_content}\n```\n\n"

    if web_contents:
        prompt += "## Web Content\n\n"
        for url, (file_tuple, content) in web_contents.items():
            _, is_snippet, _, content_type = file_tuple
            content = handle_env_variables(content, env_vars)
            language = content_type if content_type in ['json', 'csv'] else ''
            prompt += f"### {url}{' (snippet)' if is_snippet else ''}\n\n```{language}\n{content}\n```\n\n"

    prompt += "## Task Instructions\n\n"
    cursor_position = len(prompt)
    prompt += "\n\n"
    prompt += "## Instructions for Achieving the Task\n\n"
    analysis_text = (
        "1. **Confirm and Understand the Task**:\n"
        "   - Rephrase the task in your own words to ensure understanding.\n"
        "   - Ask for any necessary clarifications.\n"
        "   - Once everything is clear, ask to proceed.\n\n"
        "2. **Outline a Plan**:\n"
        "   - Provide a brief plan on how to approach the task.\n"
        "   - Make minimal incremental changes to maintain a working codebase at each step.\n"
        "   - This is an iterative process aimed at achieving the task step by step.\n\n"
        "3. **Implement Changes Iteratively**:\n"
        "   - Apply changes in small, manageable increments.\n"
        "   - Ensure the codebase remains functional after each change.\n"
        "   - After each increment, verify stability before proceeding to the next step.\n\n"
        "4. **Present Code Changes Clearly**:\n"
        "   - Specify the file being modified at the beginning of each code block.\n"
        "   - Format changes for clarity:\n"
        "     - For small changes: Show only the changed lines with clear comments.\n"
        "     - For larger changes: Provide the full new implementation of changed parts, using placeholders like `'// ... (rest of the function)'` for unchanged code.\n"
        "   - Provide context by including important unchanged parts as needed.\n"
        "   - Use clear comments to explain the changes and reference old code if helpful.\n\n"
        "5. **Encourage User Testing and Collaboration**:\n"
        "   - Ask the user to test the code on their machine after each change.\n"
        "   - If debugging is needed, include debugging messages in the code.\n"
        "   - Request the user to share any error messages or outputs from debugging to assist further.\n"
    )
    prompt += analysis_text
    return prompt, cursor_position

def open_editor_for_input(template: str, cursor_position: int) -> str:
    editor = os.environ.get('EDITOR', 'vim')
    with tempfile.NamedTemporaryFile(mode='w+', suffix='.md', delete=False) as temp_file:
        temp_file.write(template)
        temp_file.flush()
        temp_file_path = temp_file.name

    try:
        cursor_line = template[:cursor_position].count('\n') + 1
        cursor_column = cursor_position - template.rfind('\n', 0, cursor_position)

        if 'vim' in editor or 'nvim' in editor:
            subprocess.call([editor, f'+call cursor({cursor_line}, {cursor_column})', '+startinsert', temp_file_path])
        elif 'emacs' in editor:
            subprocess.call([editor, f'+{cursor_line}:{cursor_column}', temp_file_path])
        elif 'nano' in editor:
            subprocess.call([editor, f'+{cursor_line},{cursor_column}', temp_file_path])
        else:
            subprocess.call([editor, temp_file_path])

        with open(temp_file_path, 'r') as file:
            content = file.read()
        return content
    finally:
        os.unlink(temp_file_path)

def start_chat_session(initial_prompt: str):
    """Starts an interactive chat session with the Gemini API using google-genai."""
    if not genai:
        # Error message already printed during import if it failed
        sys.exit(1)

    # The google-genai library automatically uses GOOGLE_API_KEY env var if set
    # We still check if it's set to provide a clearer error message upfront
    if not os.environ.get('GOOGLE_API_KEY'):
        print("Error: GOOGLE_API_KEY environment variable not set.")
        print("Please set the GOOGLE_API_KEY environment variable with your API key.")
        sys.exit(1)

    try:
        # Create the client - it will use the env var automatically
        client = genai.Client()
        print("Google GenAI Client created (using GOOGLE_API_KEY).")
        # You could add a check here like listing models to verify the key early
        # print("Available models:", [m.name for m in client.models.list()])
    except Exception as e:
        print(f"Error creating Google GenAI client: {e}")
        print("Please ensure your GOOGLE_API_KEY is valid and has permissions.")
        sys.exit(1)

    model_name = 'gemini-2.5-pro-exp-03-25'
    print(f"Using model: {model_name}")

    try:
        # Create a chat session using the client
        chat = client.chats.create(model=model_name)
        # Note: History is managed by the chat object itself

        print("\n--- Starting Interactive Chat with Gemini ---")
        print("Type /q to quit, /help or /? for help, /patch to request a diff patch.")

        # Send the initial prompt using send_message_stream
        print("\n🤖 Gemini:")
        full_response_text = ""
        # Use send_message_stream for streaming responses
        response_stream = chat.send_message_stream(initial_prompt)
        for chunk in response_stream:
            print(chunk.text, end="", flush=True)
            full_response_text += chunk.text
        print("\n" + "-"*20)

        while True:
            try:
                user_input = input("👤 You: ")
            except EOFError: # Handle Ctrl+D
                 print("\nExiting...")
                 break
            except KeyboardInterrupt: # Handle Ctrl+C
                 print("\nExiting...")
                 break

            if user_input.lower() == '/q':
                break
            elif user_input.strip() == '/patch':
                print("\n🤖 Gemini: Thinking... (requesting code changes)")
                # Prompt instructing the model to use the new JSON format
                patch_request_prompt = (
                    "Based on our conversation, generate the necessary code changes "
                    "to fulfill my request. Provide the changes as a JSON list, where each item "
                    "is an object with the following keys:\n"
                    "- 'reasoning': Explain why this specific change is needed.\n"
                    "- 'file_path': The relative path to the file to modify.\n"
                    "- 'original_text': The exact, unique block of text to replace.\n"
                    "- 'new_text': The text to replace original_text with.\n"
                    "Ensure 'original_text' is unique within the specified 'file_path'. "
                    "Format the response strictly as: { \"patches\": [ { patch_item_1 }, { patch_item_2 }, ... ] }"
                )

                try:
                    # Request the response using the new schema
                    response = chat.send_message(
                        patch_request_prompt,
                        config=GenerateContentConfig(
                            response_schema=SimplePatchArgs.model_json_schema(),
                            response_mime_type='application/json'
                        )
                    )

                    print("🤖 Gemini: Received potential patches.")
                    try:
                        # Validate and parse args using the Pydantic model
                        # response.parsed should contain an instance of SimplePatchArgs
                        patch_args: SimplePatchArgs = response.parsed

                        if not patch_args or not patch_args.patches:
                            print("🤖 Gemini: No patches were proposed in the response.")
                            print("-" * 20)
                            continue

                        print("\nProposed Patches:")
                        print("=" * 30)
                        for i, patch_item in enumerate(patch_args.patches):
                            print(f"Patch {i+1}/{len(patch_args.patches)}:")
                            print(f"  File: {patch_item.file_path}")
                            print(f"  Reasoning: {patch_item.reasoning}")
                            # Optionally show snippets of original/new text for review
                            print(f"  Original (snippet): '{patch_item.original_text[:80].strip()}...'")
                            print(f"  New (snippet):      '{patch_item.new_text[:80].strip()}...'")
                            print("-" * 20)

                        confirm = input(f"Apply these {len(patch_args.patches)} patches? (y/N): ").lower()
                        if confirm == 'y':
                            applied_count = 0
                            failed_count = 0
                            for patch_item in patch_args.patches:
                                # Call the new apply function for each patch
                                success = apply_simple_patch(patch_item)
                                if success:
                                    applied_count += 1
                                else:
                                    failed_count += 1

                            print("\nPatch Application Summary:")
                            if applied_count > 0:
                                print(f"✅ Successfully applied {applied_count} patches.")
                            if failed_count > 0:
                                print(f"❌ Failed to apply {failed_count} patches.")
                            if applied_count == 0 and failed_count == 0: # Should not happen if list wasn't empty
                                 print("⚪ No patches were applied.")
                            print("=" * 30)
                        else:
                            print("🤖 Gemini: Patches not applied by user.")
                            print("-" * 20)

                    except Exception as e: # Catch Pydantic validation errors or other issues
                        print(f"❌ Error processing patch response: {e}")
                        # Attempt to show the raw response text if parsing failed
                        raw_text = ""
                        try:
                            if response.parts:
                                raw_text = "".join(part.text for part in response.parts if hasattr(part, 'text'))
                            elif hasattr(response, 'text'):
                                raw_text = response.text
                        except Exception:
                            pass # Ignore errors getting raw text
                        if raw_text:
                           print(f"   Received response text:\n{raw_text}")
                        else:
                           print(f"   Received response content: {response}") # Fallback representation

                except Exception as e:
                    print(f"\n❌ An error occurred while requesting patches from Gemini: {e}")
                    print("   Please check your connection, API key, and model permissions/capabilities.")
                    print("-" * 20)

                continue # Go to next loop iteration after handling /patch
            elif user_input.strip() in ['/help', '/?']:
                print("🤖 Gemini: Available commands:")
                print("  /q          - Quit the chat session.")
                print("  /patch      - Request a diff patch (not fully implemented yet).")
                print("  /help or /? - Show this help message.")
                print("-" * 20)
                continue
            elif not user_input.strip(): # Ignore empty input
                continue

            print("\n🤖 Gemini:")
            full_response_text = ""
            try:
                # Use send_message_stream for subsequent messages
                response_stream = chat.send_message_stream(user_input)
                for chunk in response_stream:
                    print(chunk.text, end="", flush=True)
                    full_response_text += chunk.text
                print("\n" + "-"*20)
            except Exception as e:
                 print(f"\nAn unexpected error occurred: {e}")
                 print("Try again or type 'exit'.")

    except Exception as e:
        # Catch other potential errors
        print(f"\nAn error occurred setting up the chat session: {e}")

def main():
    parser = argparse.ArgumentParser(description="Generate a prompt with project structure, file contents, and web content.")
    parser.add_argument('inputs', nargs='+', help='Files, directories, or URLs to include in the prompt')
    parser.add_argument('-t', '--task', help='Task description for the AI prompt')
    parser.add_argument('-I', '--interactive', action='store_true', help='Start an interactive chat session after generating the prompt.')
    args = parser.parse_args()

    ignore_patterns = read_gitignore()
    env_vars = read_env_file()

    files_to_include: List[FileTuple] = []
    processed_dirs = set()
    web_contents: Dict[str, Tuple[FileTuple, str]] = {}
    current_char_count = 0

    for input_path in args.inputs:
        if input_path.startswith(('http://', 'https://')):
            result = fetch_web_content(input_path)
            if result:
                file_tuple, full_content, snippet = result
                is_large = len(full_content) > 10000
                if is_large:
                    print(f"\nContent from {input_path} is large. Here's a snippet:\n")
                    print(get_colored_code(input_path, snippet))
                    print("\n" + "-"*40 + "\n")

                    while True:
                        choice = input("Use (f)ull content or (s)nippet? ").lower()
                        if choice in ['f', 's']:
                            break
                        print("Invalid choice. Please enter 'f' or 's'.")

                    if choice == 'f':
                        content = full_content
                        is_snippet = False
                        print("Using full content.")
                    else:
                        content = snippet
                        is_snippet = True
                        print("Using snippet.")
                else:
                    content = full_content
                    is_snippet = False
                    print(f"Content from {input_path} is not large. Using full content.")

                file_tuple = (file_tuple[0], is_snippet, file_tuple[2], file_tuple[3])
                web_contents[input_path] = (file_tuple, content)
                current_char_count += len(content)
                print(f"Added {'snippet of ' if is_snippet else ''}web content from: {input_path}")
                print_char_count(current_char_count)
        elif os.path.isfile(input_path):
            # Handle files provided directly via command line
            if not is_ignored(input_path, ignore_patterns) and not is_binary(input_path):
                file_size = os.path.getsize(input_path)
                file_size_readable = get_human_readable_size(file_size)
                file_char_estimate = file_size
                language = get_language_for_file(input_path)

                if is_large_file(input_path):
                    print(f"\nFile {input_path} ({file_size_readable}, ~{file_char_estimate} chars) is large.")
                    print("Preview (first ~50 lines or 4KB):")
                    print(get_colored_file_snippet(input_path))
                    print("-" * 40)
                    while True:
                        print_char_count(current_char_count)
                        choice = input(f"How to include large file {input_path}? (f)ull / (s)nippet / (p)atches / (n)o skip: ").lower()
                        if choice == 'f':
                            files_to_include.append((input_path, False, None, language))
                            current_char_count += file_char_estimate
                            print(f"Added full file: {input_path}")
                            break
                        elif choice == 's':
                            snippet_content = get_file_snippet(input_path)
                            files_to_include.append((input_path, True, None, language))
                            current_char_count += len(snippet_content)
                            print(f"Added snippet of file: {input_path}")
                            break
                        elif choice == 'p':
                            chunks, char_count = select_file_patches(input_path)
                            if chunks:
                                files_to_include.append((input_path, False, chunks, language))
                                current_char_count += char_count
                                print(f"Added selected patches from file: {input_path}")
                            else:
                                print(f"No patches selected for {input_path}. Skipping file.")
                            break
                        elif choice == 'n':
                            print(f"Skipped large file: {input_path}")
                            break
                        else:
                            print("Invalid choice. Please enter 'f', 's', 'p', or 'n'.")
                else:
                    # Automatically include non-large files
                    files_to_include.append((input_path, False, None, language))
                    current_char_count += file_char_estimate
                    print(f"Added file: {input_path} ({file_size_readable})")

                # Display current count after processing the file
                print_char_count(current_char_count)

            else:
                if is_ignored(input_path, ignore_patterns):
                    print(f"Ignoring file based on ignore patterns: {input_path}")
                elif is_binary(input_path):
                     print(f"Ignoring binary file: {input_path}")
                else:
                     print(f"Ignoring file: {input_path}") # Should not happen if logic is correct, but fallback.
        elif os.path.isdir(input_path):
            print(f"\nProcessing directory specified directly: {input_path}")
            dir_files, dir_processed, current_char_count = process_directory(input_path, ignore_patterns, current_char_count)
            files_to_include.extend(dir_files)
            processed_dirs.update(dir_processed)
        else:
            print(f"Warning: {input_path} is not a valid file, directory, or URL. Skipping.")

    if not files_to_include and not web_contents:
        print("No files or web content were selected. Exiting.")
        return

    print("\nFile and web content selection complete.")
    print_char_count(current_char_count) # Print final count before prompt generation
    print(f"Summary: Added {len(files_to_include)} files and {len(web_contents)} web sources.")

    added_files_count = len(files_to_include)
    added_dirs_count = len(processed_dirs) # Count unique processed directories
    added_web_count = len(web_contents)
    print(f"Summary: Added {added_files_count} files/patches from {added_dirs_count} directories and {added_web_count} web sources.")

    prompt_template, cursor_position = generate_prompt_template(files_to_include, ignore_patterns, web_contents, env_vars)

    # Logic branching for interactive mode vs. clipboard mode
    if args.interactive:
        print("\nPreparing initial prompt for editing...")
        # Determine the initial content for the editor
        if args.task:
            # Pre-populate the task section if --task was provided
            editor_initial_content = prompt_template[:cursor_position] + args.task + prompt_template[cursor_position:]
            print("Pre-populating editor with task provided via --task argument.")
        else:
            # Use the template as is (user will add task in the editor)
            editor_initial_content = prompt_template
            print("Opening editor for you to add the task instructions.")

        # Always open the editor in interactive mode
        initial_chat_prompt = open_editor_for_input(editor_initial_content, cursor_position)
        print("Editor closed. Starting interactive chat session...")
        start_chat_session(initial_chat_prompt) # Start the chat with the edited prompt    else:
    else:
        # Original non-interactive behavior
        if args.task:
            task_description = args.task
            # Insert task description before "## Task Instructions"
            task_marker = "## Task Instructions\n\n"
            insertion_point = prompt_template.find(task_marker)
            if insertion_point != -1:
                final_prompt = prompt_template[:insertion_point + len(task_marker)] + task_description + "\n\n" + prompt_template[insertion_point + len(task_marker):]
            else: # Fallback if marker not found
                final_prompt = prompt_template[:cursor_position] + task_description + prompt_template[cursor_position:]
            print("\nUsing task description from -t argument.")
        else:
            print("\nOpening editor for task instructions...")
            final_prompt = open_editor_for_input(prompt_template, cursor_position)

        print("\n\nGenerated prompt:")
        print("-" * 80)
        print(final_prompt)
        print("-" * 80)

        # Copy the prompt to clipboard
        try:
            pyperclip.copy(final_prompt)
            separator = "\n" + "=" * 40 + "\n☕🍝       Kopipasta Complete!       🍝☕\n" + "=" * 40 + "\n"
            print(separator)
            final_char_count = len(final_prompt)
            final_token_estimate = final_char_count // 4
            print(f"Prompt has been copied to clipboard. Final size: {final_char_count} characters (~ {final_token_estimate} tokens)")
        except pyperclip.PyperclipException as e:
            print(f"\nWarning: Failed to copy to clipboard: {e}")
            print("You can manually copy the prompt above.")

if __name__ == "__main__":
    main()