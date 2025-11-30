import os
import re
from typing import List, Union, TypedDict
from difflib import SequenceMatcher

from rich.console import Console


# --- Data Structures for Parsed Patches ---


class Hunk(TypedDict):
    """Represents a single 'hunk' or block of changes from a diff."""

    original_lines: List[str]
    new_lines: List[str]


PatchContent = Union[str, List[Hunk]]


class Patch(TypedDict):
    """Represents a patch for a single file, either full content or a diff."""

    file_path: str
    type: str  # 'full' or 'diff'
    content: PatchContent


def _parse_diff_hunks(diff_content: str) -> List[Hunk]:
    """Parses the content of a diff block into a list of Hunks."""
    hunks: List[Hunk] = []
    lines = diff_content.splitlines()
    current_hunk: Hunk = None
    hunk_counter = 0

    for line in lines:
        if line.startswith("@@ "):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = {"original_lines": [], "new_lines": []}
            hunk_counter += 1
            continue

        if not current_hunk:
            continue

        if line.startswith("-"):
            current_hunk["original_lines"].append(line[1:])
        elif line.startswith("+"):
            current_hunk["new_lines"].append(line[1:])
        elif line.startswith(" "):
            line_content = line[1:]
            current_hunk["original_lines"].append(line_content)
            current_hunk["new_lines"].append(line_content)
        # Ignore other lines like '---', '+++', '\ No newline at end of file'

    if current_hunk:
        hunks.append(current_hunk)

    return hunks


def parse_llm_output(content: str, console: Console = None) -> List[Patch]:
    """
    Parses LLM markdown output to find file patches.
    Handles:
    - Indented code blocks.
    - Multiple files in a single block.
    - Various comment styles (#, //, --, <!--).
    - Nested backticks (by matching fence length).
    - File headers on the fence line.
    Returns a list of structured Patch objects.
    """
    patches: List[Patch] = []
    blocks_found = 0
    blocks_with_valid_headers = 0

    lines = content.splitlines()
    i = 0

    # Regex for file headers:
    # - Allow leading whitespace (^\s*)
    # - Support #, //, --, /*, <!--
    # - Capture filename lazily (.+?)
    # - Handle trailing comment closers like */ or -->
    file_header_regex = re.compile(
        r"\s*(?:#|//|--|/\*|<!--)\s*FILE:\s*(.+?)(?:\s|\*\/|-->)*$",
        re.IGNORECASE
    )

    # Regex to detect unified diff hunks
    diff_hunk_header_regex = re.compile(
        r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@", re.MULTILINE
    )

    while i < len(lines):
        line = lines[i]

        # Detect start of code block: indentation + ``` + info_string
        fence_match = re.match(r"^(\s*)(`{3,})(.*)$", line)

        if fence_match:
            blocks_found += 1
            indent = fence_match.group(1)
            fence_len = len(fence_match.group(2))
            info_string = fence_match.group(3)

            block_lines = []

            # Check if fence line has a file header (e.g. ```python # FILE: foo.py)
            current_file_path = None
            header_match = file_header_regex.search(info_string)
            if header_match:
                current_file_path = header_match.group(1).strip()
                blocks_with_valid_headers += 1

            i += 1

            # Capture block content until closing fence
            while i < len(lines):
                block_line = lines[i]
                # Check for closing fence of same or greater length
                closing_match = re.match(r"^(\s*)(`{3,})\s*$", block_line)
                if closing_match and len(closing_match.group(2)) >= fence_len:
                    break

                # Strip indentation if it matches the fence's indentation
                if block_line.startswith(indent):
                    block_lines.append(block_line[len(indent) :])
                else:
                    block_lines.append(block_line)
                i += 1

            # Inner function to finalize a collected patch
            def finalize(path, collected_lines):
                if not path:
                    return
                content_str = "\n".join(collected_lines).strip()
                if diff_hunk_header_regex.search(content_str):
                    hunks = _parse_diff_hunks(content_str)
                    if hunks:
                        patches.append({"file_path": path, "type": "diff", "content": hunks})
                else:
                    patches.append({"file_path": path, "type": "full", "content": content_str})

            # Process lines to split multiple files
            current_content_lines = []
            for bline in block_lines:
                match = file_header_regex.match(bline)
                if match:
                    if current_file_path:
                        finalize(current_file_path, current_content_lines)
                    current_file_path = match.group(1).strip()
                    blocks_with_valid_headers += 1
                    current_content_lines = []
                else:
                    if current_file_path:
                        current_content_lines.append(bline)

            # Save the last file in the block
            if current_file_path:
                finalize(current_file_path, current_content_lines)
            
            # --- DIAGNOSTICS: If block ended but we never found a file path ---
            elif console:
                # Check for near misses to give helpful hints
                preview = "\n".join(block_lines[:2]).strip()
                hint = ""
                if "FILE:" in info_string or any("FILE:" in l for l in block_lines[:2]):
                    hint = " (Found 'FILE:' keyword but syntax was incorrect. Check comment style?)"
                elif "filename" in info_string.lower() or any("filename" in l.lower() for l in block_lines[:2]):
                    hint = " (Found 'filename' keyword. Please use 'FILE:' instead.)"

                console.print(f"[dim yellow]⚠ Skipped code block at line {i}: No valid '# FILE: path' header found.{hint}[/dim yellow]")
                if preview:
                    console.print(f"[dim]   Preview: {preview}[/dim]")

        i += 1

    if blocks_found == 0 and console:
        console.print("[dim yellow]⚠ No markdown code blocks (```) found in the pasted content.[/dim yellow]")
    elif blocks_found > 0 and len(patches) == 0 and console:
        console.print(f"[bold red]Found {blocks_found} code blocks, but none contained valid file headers.[/bold red]")

    return patches


def _apply_diff_patch(
    file_path: str, original_content: str, hunks: List[Hunk], console: Console
) -> bool:
    """Applies a list of diff hunks to the original file content."""
    original_lines = original_content.splitlines()
    final_lines = original_lines[:]

    replacements = []
    hunks_applied_count = 0

    # --- Hunk Analysis Phase ---
    for i, hunk in enumerate(hunks):
        hunk_original = hunk["original_lines"]
        if not hunk_original:
            console.print(
                f"  - Skipping hunk #{i+1}: Cannot apply a patch without any context lines to match against."
            )
            continue

        matcher = SequenceMatcher(None, original_lines, hunk_original, autojunk=False)
        match = matcher.find_longest_match(
            0, len(original_lines), 0, len(hunk_original)
        )

        match_ratio = match.size / len(hunk_original) if hunk_original else 0

        # A higher threshold is better for preventing incorrect patches.
        if match.size == 0 or match_ratio < 0.6:
            console.print(
                f"  - Skipping hunk #{i+1}: Could not find a confident match (best ratio: {match_ratio:.2f})."
            )
            # Clean up context for display (limit to first 3 lines)
            preview = "\n".join([f"      | {line}" for line in hunk_original[:3]])
            console.print(f"    [dim]Expected context starts with:\n{preview}[/dim]")
            continue

        start_index = match.a - match.b
        end_index = start_index + len(hunk_original)

        is_overlapping = any(
            max(start_index, r_start) < min(end_index, r_end)
            for r_start, r_end, _ in replacements
        )
        if is_overlapping:
            console.print(
                f"  - Skipping hunk #{i+1}: Match overlaps with another hunk's changes."
            )
            continue

        replacements.append((start_index, end_index, hunk["new_lines"]))
        hunks_applied_count += 1

    if hunks_applied_count == 0:
        console.print(
            f"❌ [bold red]Failed to apply patch to {file_path}:[/bold red] No applicable hunks found. File left unchanged."
        )
        return False

    # --- Application Phase ---

    # Sort replacements by start index in reverse to apply patches without shifting indices
    replacements.sort(key=lambda x: x[0], reverse=True)

    for start, end, new_lines in replacements:
        final_lines[start:end] = new_lines

    final_content = "\n".join(final_lines)
    if original_content.endswith("\n") and not final_content.endswith("\n"):
        final_content += "\n"

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(final_content)

    console.print(
        f"✅ Patched [green]{file_path}[/green] ({hunks_applied_count}/{len(hunks)} hunks applied)"
    )
    return True


def apply_patches(patches: List[Patch]) -> None:
    """
    Applies a list of patches to the filesystem.
    Dispatches between full-file replacement and diff-based patching.
    """
    console = Console()
    if not patches:
        console.print(
            "[yellow]No valid file patches found in the pasted content.[/yellow]"
        )
        return

    console.print(f"\n[bold]Applying {len(patches)} patch(es)...[/bold]")
    for patch in patches:
        file_path = patch["file_path"]
        patch_type = patch["type"]
        patch_content = patch["content"]

        try:
            # If file doesn't exist, it's a simple creation.
            if not os.path.exists(file_path):
                if patch_type == "diff":
                    # For a new file, a diff is just the content to be added.
                    full_content = "\n".join(
                        line for hunk in patch_content for line in hunk["new_lines"]
                    )
                else:  # 'full'
                    full_content = patch_content

                parent_dir = os.path.dirname(file_path)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(full_content)
                console.print(f"✅ Created [green]{file_path}[/green]")
                continue

            # File exists, so we apply a patch.
            with open(file_path, "r", encoding="utf-8") as f:
                original_content = f.read()

            if patch_type == "diff":
                _apply_diff_patch(file_path, original_content, patch_content, console)

            else:  # 'full'
                # For non-diff blocks, we treat them as full file overwrites.
                # The prompt instructs the LLM to provide full content if not using patches.
                # We rely on git diff for the user to verify safety.

                final_content = patch_content
                if original_content.endswith("\n") and not final_content.endswith("\n"):
                    final_content += "\n"

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(final_content)

                console.print(f"✅ Overwrote [green]{file_path}[/green] (Full Content)")

        except Exception as e:
            console.print(f"❌ [bold red]Error processing {file_path}: {e}[/bold red]")
