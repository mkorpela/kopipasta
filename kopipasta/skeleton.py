"""Skeletonization: strip function/method bodies, preserving signatures."""

import ast
from typing import List, Tuple, Union


def _add_body_replacement(
    node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
    lines: List[str],
    replacements: List[Tuple[int, int, str]],
) -> None:
    body = node.body
    body_start = body[0].lineno
    body_end = body[-1].end_lineno or body_start
    func_line = lines[node.lineno - 1]
    func_indent = len(func_line) - len(func_line.lstrip())
    body_indent = " " * (func_indent + 4)
    replacements.append((body_start, body_end, body_indent))


def skeletonize_python(source: str) -> str:
    """Strip implementation bodies from Python source, keeping only signatures.

    Processes top-level functions and class methods. Processes bottom-to-top
    so that line number indices remain valid across successive replacements.

    Returns source unchanged on SyntaxError.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    lines = source.splitlines()
    replacements: List[Tuple[int, int, str]] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _add_body_replacement(node, lines, replacements)
        elif isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _add_body_replacement(child, lines, replacements)

    # Process from bottom to top to keep earlier line numbers intact.
    replacements.sort(key=lambda x: x[0], reverse=True)

    for body_start, body_end, body_indent in replacements:
        lines[body_start - 1 : body_end] = [f"{body_indent}..."]

    return "\n".join(lines)
