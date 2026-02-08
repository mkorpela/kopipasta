import os
import re
from typing import List, Union, TypedDict, Tuple, Optional
from difflib import SequenceMatcher

import click
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


class PatchParser:
    """
    State machine for parsing LLM markdown output into patches.
    Handles nested code blocks, various comment styles, and multiple files per block.
    """

    # Explicit comments: # FILE: path/to/file.ext
    FILE_HEADER_REGEX = re.compile(
        r"\s*(?:#|//|--|/\*|<!--)\s*FILE:\s*(.+?)(?:\s|\*\/|-->)*$", re.IGNORECASE
    )
    # Unified Diff Header: @@ -1,2 +1,2 @@
    DIFF_HUNK_HEADER_REGEX = re.compile(
        r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@", re.MULTILINE
    )
    # Markdown Header Heuristic: ### path/to/file.ext
    # Matches "### src/file.py" allowing for optional leading/trailing whitespace.
    MARKDOWN_FILE_HEADER_REGEX = re.compile(r"^#{1,6}\s+([\w\-\./\\]+\.\w+)\s*$")

    # Special Markers
    DELETION_MARKER = "<<<DELETE>>>"
    RESET_MARKER = "<<<RESET>>>"

    def __init__(self, content: str, console: Optional[Console] = None):
        self.lines = content.splitlines()
        self.console = console
        self.patches: List[Patch] = []
        self.blocks_found = 0
        self.current_line_idx = 0
        self.last_block_end_idx = -1

    def parse(self) -> List[Patch]:
        while self.current_line_idx < len(self.lines):
            line = self.lines[self.current_line_idx]

            # Check for RESET (outside of code blocks)
            if line.strip() == self.RESET_MARKER:
                self.patches = []
                self.blocks_found = 0
                self.last_block_end_idx = self.current_line_idx
                self.current_line_idx += 1
                continue

            fence_match = re.match(r"^(\s*)([`~]{3,})(.*)$", line)

            if fence_match:
                self._process_code_block(fence_match)
            else:
                self.current_line_idx += 1

        self._report_diagnostics()
        return self.patches

    def _process_code_block(self, fence_match):
        self.blocks_found += 1
        indent = fence_match.group(1)
        fence_chars = fence_match.group(2)
        info_string = fence_match.group(3)

        # 1. Look for header in info string or preceding lines
        initial_file_path, initial_is_explicit = self._find_header_context(info_string)

        # 2. Extract block content
        self.current_line_idx += 1
        block_lines = self._extract_block_content(indent, fence_chars)

        # Track where this block ended for the next lookback
        self.last_block_end_idx = self.current_line_idx

        # 3. Parse content into patches
        self._parse_block_content(
            block_lines, initial_file_path, initial_is_explicit, info_string
        )

    def _find_header_context(self, info_string: str) -> Tuple[Optional[str], bool]:
        # Check inline: backtick backtick backtick python # FILE: foo.py
        header_match = self.FILE_HEADER_REGEX.search(info_string)
        if header_match:
            return header_match.group(1).strip(), True

        # Check preceding lines
        # ONLY look at lines between this block and the previous block.
        # Never look inside a prior block's content.
        k = self.current_line_idx - 1
        lines_to_check = 5  # Look back limit
        while k >= 0 and (self.current_line_idx - k) <= lines_to_check:
            if k <= self.last_block_end_idx:
                break

            line = self.lines[k].strip()
            if not line:
                k -= 1
                continue

            # Check Explicit Header (# FILE: ...)
            prev_match = self.FILE_HEADER_REGEX.search(line)
            if prev_match:
                return prev_match.group(1).strip(), True

            # Check Markdown Header (### src/file.py)
            md_match = self.MARKDOWN_FILE_HEADER_REGEX.match(line)
            if md_match:
                return md_match.group(1).strip(), False

            k -= 1

        return None, False

    def _extract_block_content(self, indent: str, fence_chars: str) -> List[str]:
        block_lines = []
        fence_char_type = fence_chars[0]
        fence_len = len(fence_chars)

        while self.current_line_idx < len(self.lines):
            line = self.lines[self.current_line_idx]

            # Check for closing fence
            closing_match = re.match(r"^(\s*)([`~]{3,})\s*$", line)
            if (
                closing_match
                and closing_match.group(2)[0] == fence_char_type
                and len(closing_match.group(2)) >= fence_len
            ):
                # Check indentation level relative to start fence
                closing_indent_len = len(closing_match.group(1))
                start_indent_len = len(indent)

                # If the closing fence is MORE indented than the start, it's likely nested content.
                if closing_indent_len > start_indent_len:
                    pass  # Treat as content
                elif not self._is_inner_fence_heuristic(fence_chars):
                    self.current_line_idx += 1
                    break

            # Strip indentation
            if line.startswith(indent):
                block_lines.append(line[len(indent) :])
            else:
                block_lines.append(line)

            self.current_line_idx += 1

        return block_lines

    def _is_inner_fence_heuristic(self, outer_fence: str) -> bool:
        """
        Lookahead to see if this closing fence is actually part of the content
        (nested markdown) rather than the end of the block.
        """
        peek_idx = self.current_line_idx + 1
        lines_to_peek = 5

        while (
            peek_idx < len(self.lines)
            and (peek_idx - self.current_line_idx) <= lines_to_peek
        ):
            line = self.lines[peek_idx].strip()
            if self.FILE_HEADER_REGEX.search(line):
                return (
                    False  # Found a new file header, so the current fence WAS a close.
                )

            # Found another start fence
            fence_match = re.match(r"^[`~]{3,}(.*)$", line)
            if fence_match:
                if fence_match.group(1).strip():
                    return False  # New block start
                return True  # Likely generic fence inside content

            if line:
                return False  # Found regular text, so gap is populated.

            peek_idx += 1
        return False

    def _parse_block_content(
        self,
        lines: List[str],
        initial_path: Optional[str],
        initial_is_explicit: bool,
        info_string: str,
    ):
        current_path = initial_path
        current_lines = []
        valid_headers_found = 0

        if current_path:
            valid_headers_found += 1

        for line in lines:
            match = self.FILE_HEADER_REGEX.match(line)
            if match:
                # If current_path came from a non-explicit source (markdown header)
                # and has no content yet, just override it — don't finalize empty patch.
                if current_path and (initial_is_explicit or current_lines):
                    self._finalize_patch(current_path, current_lines)
                current_path = match.group(1).strip()
                current_lines = []
                valid_headers_found += 1
                initial_is_explicit = True
            else:
                if current_path:
                    current_lines.append(line)

        if current_path:
            self._finalize_patch(current_path, current_lines)

        # Fallback: Try Raw Parsing strategies
        elif valid_headers_found == 0:
            raw_content = "\n".join(lines).strip()

            # Strategy A: Unified Diff
            raw_patches = _parse_raw_unified_diff(raw_content)
            if raw_patches:
                self.patches.extend(raw_patches)
                return

            # Note: Search/Replace blocks (<<<< ==== >>>>) are handled inside _finalize_patch.
            # If we are here (valid_headers_found == 0), it means we have no file context at all,
            # so we can't attach a search/replace block to a file.

            self._log_skip_warning(lines, info_string)

    def _finalize_patch(self, path: str, lines: List[str]):
        if not path:
            return
        content = "\n".join(lines).strip()

        # 1. Check for Deletion Marker
        if content == self.DELETION_MARKER:
            self.patches.append({"file_path": path, "type": "delete", "content": ""})
            return

        # 2. Check for Unified Diff
        if self.DIFF_HUNK_HEADER_REGEX.search(content):
            hunks = _parse_diff_hunks(content)
            if hunks:
                self.patches.append(
                    {"file_path": path, "type": "diff", "content": hunks}
                )
                return

        # 3. Check for Search/Replace Block (<<<< ... ==== ... >>>>)
        search_replace_hunks = _parse_search_replace_block(lines)
        if search_replace_hunks:
            self.patches.append(
                {"file_path": path, "type": "diff", "content": search_replace_hunks}
            )
            return

        # 4. Default to Full File
        self.patches.append({"file_path": path, "type": "full", "content": content})

    def _log_skip_warning(self, lines: List[str], info_string: str):
        if not self.console:
            return

        preview = "\n".join(lines[:2]).strip()
        hint = ""
        if "FILE:" in info_string or any("FILE:" in l for l in lines[:2]):
            hint = " (Check comment syntax?)"
        elif "filename" in info_string.lower():
            hint = " (Use 'FILE:' instead of 'filename')"

        self.console.print(
            f"[dim yellow]⚠ Skipped block near line {self.current_line_idx}: No valid header found.{hint}[/dim yellow]"
        )

    def _report_diagnostics(self):
        if self.console and self.blocks_found == 0:
            self.console.print(
                "[dim yellow]⚠ No markdown code blocks found.[/dim yellow]"
            )
        elif self.console and self.blocks_found > 0 and not self.patches:
            self.console.print(
                f"[bold red]Found {self.blocks_found} blocks but no valid patches.[/bold red]"
            )


def parse_llm_output(content: str, console: Console = None) -> List[Patch]:
    parser = PatchParser(content, console)
    return parser.parse()


def find_paths_in_text(text: str, valid_paths: List[str]) -> List[str]:
    """
    Scans text for occurrences of valid project paths.
    Returns a list of matching relative paths.
    """
    found = []
    # Normalize input text slashes for cross-platform matching
    normalized_text = text.replace("\\", "/")

    # Sort by length descending to prevent sub-path shadowing
    sorted_paths = sorted(valid_paths, key=len, reverse=True)

    for path in sorted_paths:
        # Normalize the project path to forward slashes for the search
        search_path = path.replace("\\", "/")

        # Match path surrounded by quotes, whitespace, or delimiters
        pattern = re.compile(
            rf'(?:^|[\s"\'`\(\)\[\]:;,])({re.escape(search_path)})(?:$|[\s"\'`\(\)\[\]:;,])'
        )
        if pattern.search(normalized_text):
            found.append(path)
    return found


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


def _parse_search_replace_block(lines: List[str]) -> List[Hunk]:
    """
    Parses a block using <<<< ==== >>>> markers (Aider style).
    Returns a list of Hunks where start_line is None (pure content matching).
    """
    hunks: List[Hunk] = []

    # State constants
    S_TEXT = 0
    S_ORIG = 1
    S_NEW = 2

    state = S_TEXT
    current_orig = []
    current_new = []

    # Regex for markers (allow 4 or more chars)
    re_start = re.compile(r"^<{4,}\s*$")
    re_mid = re.compile(r"^={4,}\s*$")
    re_end = re.compile(r"^>{4,}\s*$")

    for line in lines:
        if state == S_TEXT:
            if re_start.match(line):
                state = S_ORIG
                current_orig = []
            # Ignore text outside of blocks
        elif state == S_ORIG:
            if re_mid.match(line):
                state = S_NEW
                current_new = []
            else:
                current_orig.append(line)
        elif state == S_NEW:
            if re_end.match(line):
                # End of block, finalize hunk
                hunks.append(
                    {
                        "original_lines": current_orig,
                        "new_lines": current_new,
                        "start_line": None,  # Signal to use content-matching only
                    }
                )
                state = S_TEXT
                current_orig = []
                current_new = []
            elif re_start.match(line):
                # Error: Unexpected start marker inside new block.
                # Treat as content to be safe.
                current_new.append(line)
            else:
                current_new.append(line)

    return hunks


def _parse_raw_unified_diff(content: str) -> List[Patch]:
    """
    Attempts to parse content as a multi-file unified diff.
    Looks for `diff --git` or `--- a/` + `+++ b/` headers.
    """
    patches: List[Patch] = []

    # Detect chunks starting with `diff --git ...`
    # We assume standard git diff output format
    git_diff_starts = [
        m.start() for m in re.finditer(r"^diff --git ", content, re.MULTILINE)
    ]

    if git_diff_starts:
        # Split by git diff headers
        indices = git_diff_starts + [len(content)]
        for k in range(len(indices) - 1):
            chunk = content[indices[k] : indices[k + 1]]
            # Extract filename from `+++ b/...` inside chunk
            # Matches "+++ b/src/main.py" or "+++ src/main.py"
            m = re.search(r"^\+\+\+ (?:b/)?([^\s\n]+)", chunk, re.MULTILINE)
            if m:
                path = m.group(1).strip()
                hunks = _parse_diff_hunks(chunk)
                if hunks:
                    patches.append(
                        {"file_path": path, "type": "diff", "content": hunks}
                    )
        return patches

    # Fallback: Detect chunks starting with `--- ...` then `+++ ...`
    # This handles non-git unified diffs (e.g. `diff -u file1 file2`)
    # We look for the `---` header at start of line
    unified_starts = [
        m.start() for m in re.finditer(r"^--- (?:a/)?\S+", content, re.MULTILINE)
    ]
    if unified_starts:
        indices = unified_starts + [len(content)]
        for k in range(len(indices) - 1):
            chunk = content[indices[k] : indices[k + 1]]
            # Must have a +++ line
            m = re.search(r"^\+\+\+ (?:b/)?([^\s\n]+)", chunk, re.MULTILINE)
            if m:
                path = m.group(1).strip()
                hunks = _parse_diff_hunks(chunk)
                if hunks:
                    patches.append(
                        {"file_path": path, "type": "diff", "content": hunks}
                    )
        return patches

    return []


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

        # Get hint from hunk header, default to 1 if None (Search/Replace style)
        target_line_hint = hunk.get("start_line") or 1
        target_line_index = target_line_hint - 1  # 0-indexed

        if not hunk_original:
            # An insert-only hunk (no context lines).
            replacements.append(
                (target_line_index, target_line_index, hunk["new_lines"])
            )
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
            elif hunk.get("start_line") is not None:
                # Find candidate with minimum distance to target_line
                best_cand = min(
                    candidates, key=lambda idx: abs(idx - target_line_index)
                )
                selected_index = best_cand
                match_type += " (disambiguated by line #)"
            else:
                # No line number hint (Search/Replace) and multiple matches.
                # Default to the first one for deterministic behavior.
                selected_index = candidates[0]
                match_type += " (first occurrence)"

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


def apply_patches(patches: List[Patch]) -> List[str]:
    """
    Applies a list of patches to the filesystem.
    Dispatches between full-file replacement and diff-based patching.
    Returns a list of file paths that were successfully modified.
    """
    console = Console()
    modified_files = []
    if not patches:
        console.print(
            "[yellow]No valid file patches found in the pasted content.[/yellow]"
        )
        return []

    console.print(f"\n[bold]Applying {len(patches)} patch(es)...[/bold]")
    for patch in patches:
        file_path = patch["file_path"]
        patch_type = patch["type"]
        patch_content = patch["content"]

        try:
            # --- Deletion Handling ---
            if patch_type == "delete":
                if os.path.exists(file_path):
                    if click.confirm(f"🗑️  Delete {file_path}?", default=False):
                        try:
                            os.remove(file_path)
                            modified_files.append(file_path)
                            console.print(f"✅ Deleted [red]{file_path}[/red]")
                        except OSError as e:
                            console.print(
                                f"❌ [bold red]Failed to delete {file_path}: {e}[/bold red]"
                            )
                    else:
                        console.print(f"   [dim]Skipped deletion of {file_path}[/dim]")
                else:
                    console.print(
                        f"   [yellow]File {file_path} not found, skipping delete.[/yellow]"
                    )
                continue

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
                modified_files.append(file_path)
                console.print(f"✅ Created [green]{file_path}[/green]")
                continue

            # File exists, so we apply a patch.
            with open(file_path, "r", encoding="utf-8") as f:
                original_content = f.read()

            if patch_type == "diff":
                if _apply_diff_patch(
                    file_path, original_content, patch_content, console
                ):
                    modified_files.append(file_path)

            else:  # 'full'
                # For non-diff blocks, we treat them as full file overwrites.
                final_content = patch_content
                if original_content.endswith("\n") and not final_content.endswith("\n"):
                    final_content += "\n"

                # --- Safety Check: Suspicious Overwrite ---
                original_len = len(original_content)
                new_len = len(final_content)

                # Heuristics:
                # 1. Significant size reduction (> 200 chars originally, < 50% new size)
                # 2. Diff markers in a full file block (LLM likely meant a diff but messed up format)
                is_shrinkage = original_len > 200 and new_len < (original_len * 0.5)
                has_diff_markers = bool(
                    re.search(r"^@@\s+-\d", final_content, re.MULTILINE)
                )

                if is_shrinkage or has_diff_markers:
                    console.print(
                        f"\n[bold yellow]⚠️  Safety Check for {file_path}[/bold yellow]"
                    )
                    if is_shrinkage:
                        console.print(
                            f"   • File shrinking significantly: {original_len} -> {new_len} chars (-{100 - int(new_len/original_len*100)}%)"
                        )
                    if has_diff_markers:
                        console.print(
                            "   • Content looks like a Diff/Patch but was parsed as a Full File."
                        )

                    console.print(
                        "   [dim]Preview (first 3 lines):[/dim]\n"
                        + "\n".join(
                            f"   | {line}" for line in final_content.splitlines()[:3]
                        )
                    )

                    if not click.confirm(
                        f"   Are you sure you want to overwrite {file_path}?",
                        default=False,
                    ):
                        console.print(f"   [red]Skipped {file_path}[/red]")
                        continue

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(final_content)

                modified_files.append(file_path)
                console.print(f"✅ Overwrote [green]{file_path}[/green] (Full Content)")

        except Exception as e:
            console.print(f"❌ [bold red]Error processing {file_path}: {e}[/bold red]")

    return modified_files
