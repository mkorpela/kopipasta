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


def parse_llm_output(content: str) -> List[Patch]:
    """
    Parses LLM markdown output to find file patches.
    Detects whether a patch is a full file or a diff.
    Returns a list of structured Patch objects.
    """
    patches: List[Patch] = []
    # Regex to find fenced code blocks, optionally with a language hint
    code_block_regex = re.compile(r"```(?:[a-zA-Z0-9\.\-]+)?\n(.*?)\n```", re.DOTALL)
    # Regex to find the file path comment, supporting various comment styles
    file_path_regex = re.compile(
        r"^(?:#|//|\/\*)\s*FILE:\s*(\S+)\s*(?:\*\/)?\s*\n?", re.MULTILINE
    )
    # REGEX to robustly detect a unified diff hunk header
    diff_hunk_header_regex = re.compile(
        r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@", re.MULTILINE
    )

    for match in code_block_regex.finditer(content):
        block_content = match.group(1)
        file_path_match = file_path_regex.search(block_content)
        if file_path_match:
            file_path = file_path_match.group(1).strip()
            # The actual code is what's left after the file path comment
            code_content = file_path_regex.sub("", block_content, count=1).lstrip("\n")

            # Use the robust regex to detect if the content is a unified diff
            if diff_hunk_header_regex.search(code_content):
                hunks = _parse_diff_hunks(code_content)
                if hunks:
                    patches.append(
                        {"file_path": file_path, "type": "diff", "content": hunks}
                    )
            else:
                patches.append(
                    {"file_path": file_path, "type": "full", "content": code_content}
                )

    return patches


def _apply_diff_patch(
    file_path: str, original_content: str, hunks: List[Hunk], console: Console
) -> bool:
    """Applies a list of diff hunks to the original file content."""
    original_lines = original_content.splitlines()
    final_lines = original_lines[:]

    replacements = []
    hunks_applied_count = 0

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
