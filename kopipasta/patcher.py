import os
import re
from typing import List, Union, TypedDict, Tuple, Optional
from difflib import SequenceMatcher

from rich.console import Console


# --- Data Structures for Parsed Patches ---


class Hunk(TypedDict):
    """Represents a single 'hunk' or block of changes from a diff."""

    original_lines: List[str]
    new_lines: List[str]
    start_line: Optional[int]  # The line number from @@ -N,M ...


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

    # Regex to parse the hunk header: @@ -12,3 +15,5 @@
    hunk_header_regex = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

    for line in lines:
        header_match = hunk_header_regex.match(line)
        if header_match:
            if current_hunk:
                hunks.append(current_hunk)

            start_line = int(header_match.group(1))
            current_hunk = {
                "original_lines": [],
                "new_lines": [],
                "start_line": start_line,
            }
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


def _parse_raw_unified_diff(content: str) -> List[Patch]:
    """
    Attempts to parse content as a multi-file unified diff.
    Looks for `diff --git` or `--- a/` + `+++ b/` headers.
    """
    patches: List[Patch] = []
    
    # Detect chunks starting with `diff --git ...`
    # We assume standard git diff output format
    git_diff_starts = [m.start() for m in re.finditer(r"^diff --git ", content, re.MULTILINE)]
    
    if git_diff_starts:
        # Split by git diff headers
        indices = git_diff_starts + [len(content)]
        for k in range(len(indices) - 1):
            chunk = content[indices[k]:indices[k+1]]
            # Extract filename from `+++ b/...` inside chunk
            # Matches "+++ b/src/main.py" or "+++ src/main.py"
            m = re.search(r"^\+\+\+ (?:b/)?([^\s\n]+)", chunk, re.MULTILINE)
            if m:
                path = m.group(1).strip()
                hunks = _parse_diff_hunks(chunk)
                if hunks:
                    patches.append({"file_path": path, "type": "diff", "content": hunks})
        return patches

    # Fallback: Detect chunks starting with `--- ...` then `+++ ...`
    # This handles non-git unified diffs (e.g. `diff -u file1 file2`)
    # We look for the `---` header at start of line
    unified_starts = [m.start() for m in re.finditer(r"^--- (?:a/)?\S+", content, re.MULTILINE)]
    if unified_starts:
        indices = unified_starts + [len(content)]
        for k in range(len(indices) - 1):
            chunk = content[indices[k]:indices[k+1]]
            # Must have a +++ line
            m = re.search(r"^\+\+\+ (?:b/)?([^\s\n]+)", chunk, re.MULTILINE)
            if m:
                path = m.group(1).strip()
                hunks = _parse_diff_hunks(chunk)
                if hunks:
                    patches.append({"file_path": path, "type": "diff", "content": hunks})
        return patches

    return []


def parse_llm_output(content: str, console: Console = None) -> List[Patch]:
    """
    Parses LLM markdown output to find file patches.
    Handles:
    - Indented code blocks.
    - Multiple files in a single block.
    - Various comment styles (#, //, --, <!--).
    - Nested backticks (by matching fence length).
    - File headers on the fence line.
    - File headers immediately preceding the code block.
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
        r"\s*(?:#|//|--|/\*|<!--)\s*FILE:\s*(.+?)(?:\s|\*\/|-->)*$", re.IGNORECASE
    )

    # Regex to detect unified diff hunks
    diff_hunk_header_regex = re.compile(
        r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@", re.MULTILINE
    )

    while i < len(lines):
        line = lines[i]

        # Detect start of code block: indentation + (``` OR ~~~) + info_string
        fence_match = re.match(r"^(\s*)([`~]{3,})(.*)$", line)

        if fence_match:
            blocks_found += 1
            indent = fence_match.group(1)
            fence_chars = fence_match.group(2)
            fence_char_type = fence_chars[0]
            fence_len = len(fence_chars)
            # Track if this block successfully yields explicit headers
            initial_valid_headers_count = blocks_with_valid_headers
            info_string = fence_match.group(3)

            block_lines = []
            current_file_path = None

            # 1. Check if fence line itself has a file header
            #    e.g. ```python # FILE: foo.py
            header_match = file_header_regex.search(info_string)
            if header_match:
                current_file_path = header_match.group(1).strip()
                blocks_with_valid_headers += 1
            else:
                # 2. Check preceding lines for a file header
                #    e.g. # FILE: foo.py
                #         ```python
                k = i - 1
                while k >= 0 and not lines[k].strip():
                    k -= 1
                if k >= 0:
                    prev_match = file_header_regex.search(lines[k])
                    if prev_match:
                        current_file_path = prev_match.group(1).strip()
                        blocks_with_valid_headers += 1

            i += 1

            # Capture block content until closing fence
            while i < len(lines):
                block_line = lines[i]
                # Check for closing fence of same or greater length
                closing_match = re.match(r"^(\s*)([`~]{3,})\s*$", block_line)
                
                is_potential_close = (
                    closing_match 
                    and closing_match.group(2)[0] == fence_char_type 
                    and len(closing_match.group(2)) >= fence_len
                )

                if is_potential_close:
                    # Lookahead Heuristic: 
                    # If we find ANOTHER fence within a short window, and it looks like a closure,
                    # we assume the current one is inner content.
                    is_inner = False
                    peek_idx = i + 1
                    lines_to_peek = 5 
                    
                    while peek_idx < len(lines) and (peek_idx - i) <= lines_to_peek:
                        peek_line = lines[peek_idx].strip()
                        
                        # If we see a File Header in the gap, it's definitely a new block/file
                        if file_header_regex.search(peek_line):
                            break

                        # If we hit another fence
                        peek_fence = re.match(r"^[`~]{3,}(.*)$", peek_line)
                        if peek_fence:
                            # If the next fence has an info string (e.g. ```python), it's a start of new block
                            if peek_fence.group(1).strip():
                                break
                            
                            # It's a generic fence. Likely the real closer.
                            is_inner = True
                            break
                        
                        # If we hit any non-empty text that is NOT a fence, the gap is populated.
                        # We assume this text belongs to the chat, not the code block.
                        if peek_line:
                            break
                        
                        peek_idx += 1
                    
                    if not is_inner:
                        break # Real close found

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
                        patches.append(
                            {"file_path": path, "type": "diff", "content": hunks}
                        )
                else:
                    patches.append(
                        {"file_path": path, "type": "full", "content": content_str}
                    )

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

            # If we didn't find any explicit FILE: headers, try parsing as a raw unified diff
            elif blocks_with_valid_headers == initial_valid_headers_count:
                # Re-assemble block content to check for raw diffs
                raw_block_content = "\n".join(block_lines).strip()
                raw_diff_patches = _parse_raw_unified_diff(raw_block_content)
                
                if raw_diff_patches:
                    patches.extend(raw_diff_patches)
                elif console:
                    # --- DIAGNOSTICS: Failed to parse anything ---
                    # Check for near misses to give helpful hints
                    preview = "\n".join(block_lines[:2]).strip()
                    hint = ""
                    if "FILE:" in info_string or any("FILE:" in l for l in block_lines[:2]):
                        hint = " (Found 'FILE:' keyword but syntax was incorrect. Check comment style?)"
                    elif "filename" in info_string.lower() or any(
                        "filename" in l.lower() for l in block_lines[:2]
                    ):
                        hint = " (Found 'filename' keyword. Please use 'FILE:' instead.)"

                    console.print(
                        f"[dim yellow]⚠ Skipped code block at line {i}: No valid '# FILE: path' header found.{hint}[/dim yellow]"
                    )
                    if preview:
                        console.print(f"[dim]   Preview: {preview}[/dim]")

        i += 1

    if blocks_found == 0 and console:
        console.print(
            "[dim yellow]⚠ No markdown code blocks (```) found in the pasted content.[/dim yellow]"
        )
    elif blocks_found > 0 and len(patches) == 0 and console:
        console.print(
            f"[bold red]Found {blocks_found} code blocks, but none contained valid file headers.[/bold red]"
        )

    return patches


def _find_all_sublist_indices(
    full_list: List[str], sub_list: List[str], loose: bool = False
) -> List[int]:
    """
    Finds starting indices of all occurrences of sub_list in full_list.
    If loose is True, compares strings after stripping whitespace.
    """
    if not sub_list:
        return []

    n = len(full_list)
    m = len(sub_list)
    indices = []

    if loose:
        # Pre-process for performance
        full_normalized = [s.strip() for s in full_list]
        sub_normalized = [s.strip() for s in sub_list]

        for i in range(n - m + 1):
            if full_normalized[i : i + m] == sub_normalized:
                indices.append(i)
    else:
        for i in range(n - m + 1):
            if full_list[i : i + m] == sub_list:
                indices.append(i)

    return indices


def _apply_diff_patch(
    file_path: str, original_content: str, hunks: List[Hunk], console: Console
) -> bool:
    """Applies a list of diff hunks to the original file content."""
    original_lines = original_content.splitlines()
    # If the file ended with a newline, splitlines() drops it.
    # We work with lines and join them later.

    final_lines = original_lines[:]

    # We collect all planned replacements (start_idx, end_idx, new_lines)
    # Indices refer to the 'original_lines' array.
    replacements: List[Tuple[int, int, List[str]]] = []
    hunks_applied_count = 0

    # Sort hunks to process them top-to-bottom for reporting,
    # though application order will be handled by sorting replacements later.
    # Note: If LLM outputs disordered hunks, we might have issues, but usually they are ordered.

    for i, hunk in enumerate(hunks):
        hunk_original = hunk["original_lines"]
        target_line = hunk.get("start_line", 1) - 1  # 0-indexed

        if not hunk_original:
            # An insert-only hunk (no context lines).
            # These are rare in unified diffs without context, usually @@ ... @@ is followed by lines.
            # If context is strictly empty, we can only rely on line number.
            # However, standard unified diffs usually provide context.
            # If we really have no context, we insert at the line number provided.
            replacements.append((target_line, target_line, hunk["new_lines"]))
            hunks_applied_count += 1
            continue

        # --- Phase 1: Exact Match ---
        candidates = _find_all_sublist_indices(
            original_lines, hunk_original, loose=False
        )
        match_type = "exact"

        # --- Phase 2: Loose Match (Whitespace Agnostic) ---
        if not candidates:
            candidates = _find_all_sublist_indices(
                original_lines, hunk_original, loose=True
            )
            match_type = "loose"

        # --- Phase 3: Disambiguation / Selection ---
        selected_index = -1

        if not candidates:
            # --- Phase 4: Fuzzy Fallback (difflib) ---
            # This handles cases where comments changed slightly, or variable names changed.
            # It's risky but better than failing if the LLM wasn't perfect.
            matcher = SequenceMatcher(
                None, original_lines, hunk_original, autojunk=False
            )
            match = matcher.find_longest_match(
                0, len(original_lines), 0, len(hunk_original)
            )

            # Calculate match ratio based on the hunk size
            match_ratio = match.size / len(hunk_original) if hunk_original else 0

            if match.size > 0 and match_ratio >= 0.6:
                # We found a partial match
                selected_index = match.a
                match_type = f"fuzzy ({match_ratio:.2f})"
            else:
                console.print(
                    f"  - [yellow]Skipping hunk #{i+1}:[/yellow] Could not find a match."
                )
                preview = "\n".join([f"      | {line}" for line in hunk_original[:3]])
                console.print(f"    [dim]Expected context:\n{preview}[/dim]")
                continue

        else:
            # We have 1 or more exact/loose matches.
            # If multiple, pick the one closest to the target_line reported in diff header.
            if len(candidates) == 1:
                selected_index = candidates[0]
            else:
                # Find candidate with minimum distance to target_line
                best_cand = min(candidates, key=lambda idx: abs(idx - target_line))
                selected_index = best_cand
                if match_type == "exact":
                    match_type = "exact (disambiguated)"
                else:
                    match_type = "loose (disambiguated)"

        # --- Validation: Check Overlaps ---
        # The range we propose to replace in the original file:
        start_idx = selected_index
        end_idx = selected_index + len(hunk_original)

        is_overlapping = any(
            max(start_idx, r_start) < min(end_idx, r_end)
            for r_start, r_end, _ in replacements
        )

        if is_overlapping:
            console.print(
                f"  - [yellow]Skipping hunk #{i+1}:[/yellow] Overlaps with a previous hunk."
            )
            continue

        # Success!
        replacements.append((start_idx, end_idx, hunk["new_lines"]))
        hunks_applied_count += 1

    if hunks_applied_count == 0:
        console.print(
            f"❌ [bold red]Failed to apply patch to {file_path}:[/bold red] No applicable hunks found."
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
                final_content = patch_content
                if original_content.endswith("\n") and not final_content.endswith("\n"):
                    final_content += "\n"

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(final_content)

                console.print(f"✅ Overwrote [green]{file_path}[/green] (Full Content)")

        except Exception as e:
            console.print(f"❌ [bold red]Error processing {file_path}: {e}[/bold red]")
