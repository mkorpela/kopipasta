import fnmatch
import os
from typing import List, Optional, Tuple


FileTuple = Tuple[str, bool, Optional[List[str]], str]


def read_file_contents(file_path):
    try:
        with open(file_path, 'r') as file:
            return file.read()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return ""


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