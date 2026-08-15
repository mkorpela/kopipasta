import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable, List, Union, TypedDict, Tuple, Optional
from difflib import SequenceMatcher

import click
from rich.console import Console
from structlog.stdlib import BoundLogger

from kopipasta.interaction import human_attached, use_default_without_human


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


# --- What happened when we applied them ---

#: A file whose bytes changed on disk (or would have, under a dry run).
APPLIED = "applied"
#: Written, but with hunks left unapplied. The dangerous one: this used to be
#: indistinguishable from APPLIED, so a half-patched file reported success.
PARTIAL = "partial"
#: Nothing was written. Safe to retry.
FAILED = "failed"
#: Deliberately not attempted — a declined delete, a failed safety check, a
#: path outside the editable zone. A decision, not an error.
SKIPPED = "skipped"


@dataclass
class FileOutcome:
    """What became of one file's patch.

    `hunks_applied` / `hunks_total` are only meaningful for diff patches, and
    they are the whole point of this type: `_apply_diff_patch` has always
    printed "(1/2 hunks applied)" and always thrown the numbers away.
    """

    path: str
    status: str
    action: str = ""  # created | overwritten | diff_applied | deleted
    hunks_applied: int = 0
    hunks_total: int = 0
    reason: str = ""

    @property
    def wrote(self) -> bool:
        return self.status in (APPLIED, PARTIAL)


class PatchResult(List[str]):
    """The list of modified paths, plus what the list alone cannot say.

    Subclasses `list` deliberately. `tree_selector._handle_apply_patches` and
    `spike/oracle` both treat the return value as a list of modified files, and
    neither should have to change to gain access to the failures. Iteration,
    equality and `len()` behave exactly as before.

    The list holds every file whose bytes changed — including PARTIAL ones,
    because a caller about to revert or commit needs to know they are dirty.
    Under `dry_run` it holds the files that *would* change and nothing has been
    written; check `.dry_run` before reporting a diffstat as fact.
    """

    def __init__(self, iterable: Iterable[str] = (), dry_run: bool = False):
        super().__init__(iterable)
        self.outcomes: List[FileOutcome] = []
        self.dry_run = dry_run

    def record(self, outcome: FileOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.wrote:
            self.append(outcome.path)

    def _paths(self, status: str) -> List[str]:
        return [o.path for o in self.outcomes if o.status == status]

    @property
    def applied(self) -> List[str]:
        """Cleanly applied — every hunk landed."""
        return self._paths(APPLIED)

    @property
    def partial(self) -> List[str]:
        return self._paths(PARTIAL)

    @property
    def failed(self) -> List[str]:
        return self._paths(FAILED)

    @property
    def skipped(self) -> List[str]:
        return self._paths(SKIPPED)

    @property
    def changed(self) -> bool:
        """Did the worktree move? Spec §8's exit 5 says "worktree untouched"."""
        return any(o.wrote for o in self.outcomes)

    @property
    def ok(self) -> bool:
        """Everything the model asked for landed, in full."""
        return not self.partial and not self.failed and not self.skipped


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
        self.last_parsed_path: Optional[str] = None

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
        current_lines: List[str] = []
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
            self.last_parsed_path = current_path

        # Fallback: Try Raw Parsing strategies
        elif valid_headers_found == 0:
            raw_content = "\n".join(lines).strip()

            # Strategy A: Unified Diff
            raw_patches = _parse_raw_unified_diff(raw_content)
            if raw_patches:
                self.patches.extend(raw_patches)
                return

            # Strategy B: Search/Replace Block
            search_replace_hunks = _parse_search_replace_block(lines)
            if search_replace_hunks and self.last_parsed_path:
                self.patches.append(
                    {
                        "file_path": self.last_parsed_path,
                        "type": "diff",
                        "content": search_replace_hunks,
                    }
                )
                return

            # Strategy C: Diff Hunks without header but with last_parsed_path
            if self.last_parsed_path and self.DIFF_HUNK_HEADER_REGEX.search(
                raw_content
            ):
                hunks = _parse_diff_hunks(raw_content)
                if hunks:
                    self.patches.append(
                        {
                            "file_path": self.last_parsed_path,
                            "type": "diff",
                            "content": hunks,
                        }
                    )
                    return

            self._log_skip_warning(lines, info_string)

    def _finalize_patch(self, path: str, lines: List[str]) -> bool:
        if not path:
            return False
        content = "\n".join(lines).strip()

        if not content:
            return False

        # 1. Check for Deletion Marker
        if content == self.DELETION_MARKER:
            self.patches.append({"file_path": path, "type": "delete", "content": ""})
            return True

        # 2. Check for Unified Diff
        if self.DIFF_HUNK_HEADER_REGEX.search(content):
            hunks = _parse_diff_hunks(content)
            if hunks:
                self.patches.append(
                    {"file_path": path, "type": "diff", "content": hunks}
                )
                return True

        # 3. Check for Search/Replace Block (<<<< ... ==== ... >>>>)
        search_replace_hunks = _parse_search_replace_block(lines)
        if search_replace_hunks:
            self.patches.append(
                {"file_path": path, "type": "diff", "content": search_replace_hunks}
            )
            return True

        # 4. Default to Full File
        self.patches.append({"file_path": path, "type": "full", "content": content})
        return True

    def _log_skip_warning(self, lines: List[str], info_string: str):
        if not self.console:
            return

        hint = ""
        if "FILE:" in info_string or any("FILE:" in line for line in lines[:2]):
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


def parse_llm_output(content: str, console: Optional[Console] = None) -> List[Patch]:
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

    # Remove trailing empty lines to prevent them from being parsed as
    # trimmed blank context lines when padding exists at the end of a block.
    while lines and lines[-1] == "":
        lines.pop()
    current_hunk: Optional[Hunk] = None

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
        elif line.startswith(" ") or line == "":
            line_content = line[1:] if line.startswith(" ") else ""
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
    current_orig: List[str] = []
    current_new: List[str] = []

    # Regex for markers (allow 3 or more chars, plus optional trailing text like 'SEARCH')
    re_start = re.compile(r"^<{3,}.*$")
    re_mid = re.compile(r"^={3,}.*$")
    re_end = re.compile(r"^>{3,}.*$")

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


def _detect_indent(lines: List[str]) -> str:
    """Return the common leading whitespace of the first non-empty line."""
    for line in lines:
        if line.strip():
            return line[: len(line) - len(line.lstrip())]
    return ""


def _reindent_lines(lines: List[str], old_indent: str, new_indent: str) -> List[str]:
    """
    Re-indent a list of lines: replace the leading `old_indent` prefix with
    `new_indent`, preserving additional relative indentation.
    """
    if old_indent == new_indent:
        return lines
    result = []
    for line in lines:
        if line.startswith(old_indent):
            rest = line[len(old_indent) :]
            result.append(new_indent + rest)
        else:
            # Can't strip old_indent; leave as-is
            result.append(line)
    return result


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


@dataclass
class _DiffOutcome:
    """The result of matching hunks against a file, before anything is written.

    Separating "did it match" from "was it written" is what makes `dry_run`
    honest: the preview has to run the same matching the real thing does, or it
    is a different answer to a different question.
    """

    applied: int
    total: int
    content: Optional[str] = None  # None when nothing could be applied
    written: bool = False

    @property
    def ok(self) -> bool:
        return self.content is not None

    @property
    def complete(self) -> bool:
        return self.ok and self.applied == self.total


def _apply_diff_patch(
    file_path: str,
    original_content: str,
    hunks: List[Hunk],
    console: Console,
    dry_run: bool = False,
) -> _DiffOutcome:
    """Applies a list of diff hunks to the original file content.

    Returns the counts rather than a bare bool. A file where some but not all
    hunks matched is still written — reverting the ones that landed would be a
    bigger surprise than reporting the shortfall — but the caller has to be
    able to see it, because that is spec §8's exit 4.
    """
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
            new_lines = hunk["new_lines"]
            new_len = len(new_lines)

            # --- Validation: Already Applied Check (Insert-Only) ---
            if (
                original_lines[target_line_index : target_line_index + new_len]
                == new_lines
            ):
                console.print(
                    f"  - [dim]Skipping hunk #{i + 1}:[/dim] Already applied."
                )
                hunks_applied_count += 1
                continue

            replacements.append((target_line_index, target_line_index, new_lines))
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
                # We found a partial match. Apply the hunk changes carefully.
                # Bug 1 fix: re-indent new_lines to match the file's indentation.
                # Bug 2 fix: only replace file lines that were actually matched;
                #            never delete file lines outside the matched window.

                # Determine indentation delta using the matched lines as reference.
                file_indent = _detect_indent(
                    original_lines[match.a : match.a + match.size]
                )
                hunk_indent = _detect_indent(
                    hunk_original[match.b : match.b + match.size]
                )

                # Compute the net diff between hunk_original and hunk_new_lines.
                # From this diff we know which hunk_original lines are 'deleted'
                # and which new lines are 'inserted'.
                hunk_new = hunk["new_lines"]
                sm = SequenceMatcher(None, hunk_original, hunk_new, autojunk=False)
                opcodes = sm.get_opcodes()

                # Build a replacement that covers exactly the matched file window
                # [match.a, match.a + match.size], applying:
                #   - 'equal' ops within matched range  -> keep actual file line
                #   - 'replace'/'delete' within matched range -> use hunk_new lines
                #   - 'insert' ops whose hunk position falls inside matched range
                #     -> inject new lines at correct position
                # Hunk positions OUTSIDE the matched range are ignored (Bug 2 fix).

                # Map each hunk_original index to its file index:
                #   file_idx = match.a + (hunk_idx - match.b)
                # This is valid only when match.b <= hunk_idx < match.b + match.size.

                result_lines_fuzzy: List[str] = []
                file_start = match.a
                file_end = match.a + match.size

                for tag, i1, i2, j1, j2 in opcodes:
                    # Clamp the hunk_original range to the matched window.
                    clamped_i1 = max(i1, match.b)
                    clamped_i2 = min(i2, match.b + match.size)

                    if tag == "equal":
                        # Use actual file lines for the clamped range.
                        for hunk_idx in range(clamped_i1, clamped_i2):
                            file_idx = match.a + (hunk_idx - match.b)
                            result_lines_fuzzy.append(original_lines[file_idx])

                    elif tag in ("replace", "delete"):
                        if clamped_i1 < clamped_i2:
                            # Proportionally map to new_lines range.
                            frac_start = (clamped_i1 - i1) / max(i2 - i1, 1)
                            frac_end = (clamped_i2 - i1) / max(i2 - i1, 1)
                            mapped_j1 = j1 + round(frac_start * (j2 - j1))
                            mapped_j2 = j1 + round(frac_end * (j2 - j1))
                            inserted = hunk_new[mapped_j1:mapped_j2]
                            if file_indent != hunk_indent:
                                inserted = _reindent_lines(
                                    inserted, hunk_indent, file_indent
                                )
                            result_lines_fuzzy.extend(inserted)
                        elif (
                            i1 == match.b + match.size and tag == "replace" and j1 < j2
                        ):
                            # The replace op starts exactly at the end of the matched window.
                            # The hunk_original lines it references don't exist in the file,
                            # but we should still emit the new_lines (as an insert after context).
                            inserted = hunk_new[j1:j2]
                            if file_indent != hunk_indent:
                                inserted = _reindent_lines(
                                    inserted, hunk_indent, file_indent
                                )
                            result_lines_fuzzy.extend(inserted)

                    elif tag == "insert":
                        # 'insert' has i1==i2; attach it after the closest matched line.
                        # Include it if i1 (insertion point in hunk_original) falls
                        # within [match.b, match.b + match.size].
                        if match.b <= i1 <= match.b + match.size:
                            inserted = hunk_new[j1:j2]
                            if file_indent != hunk_indent:
                                inserted = _reindent_lines(
                                    inserted, hunk_indent, file_indent
                                )
                            result_lines_fuzzy.extend(inserted)

                start_idx = file_start
                end_idx = file_end

                # --- Validation: Already Applied Check (Fuzzy) ---
                new_len = len(result_lines_fuzzy)
                if (
                    original_lines[start_idx : start_idx + new_len]
                    == result_lines_fuzzy
                ):
                    console.print(
                        f"  - [dim]Skipping hunk #{i + 1}:[/dim] Already applied."
                    )
                    hunks_applied_count += 1
                    continue

                is_overlapping = any(
                    max(start_idx, r_start) < min(end_idx, r_end)
                    for r_start, r_end, _ in replacements
                )
                if is_overlapping:
                    console.print(
                        f"  - [yellow]Skipping hunk #{i + 1}:[/yellow] Overlaps with a previous hunk."
                    )
                    continue

                replacements.append((start_idx, end_idx, result_lines_fuzzy))
                hunks_applied_count += 1
                match_type = f"fuzzy ({match_ratio:.2f})"
                continue  # Skip the standard overlap check / append below
            else:
                console.print(
                    f"  - [yellow]Skipping hunk #{i + 1}:[/yellow] Could not find a match."
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

        # --- Validation: Already Applied Check ---
        # If applying this hunk would produce no change (the file already
        # contains new_lines where original_lines are), skip it to prevent
        # double-apply of append-only or context-only hunks.
        start_idx = selected_index
        end_idx = selected_index + len(hunk_original)  # exact/loose: full hunk size

        candidate_new = hunk["new_lines"]
        if match_type == "loose" and candidate_new and hunk_original:
            file_indent = _detect_indent(original_lines[start_idx:end_idx])
            hunk_indent = _detect_indent(hunk_original)
            if file_indent != hunk_indent:
                candidate_new = _reindent_lines(candidate_new, hunk_indent, file_indent)

        new_len = len(candidate_new)
        if original_lines[start_idx : start_idx + new_len] == candidate_new:
            console.print(f"  - [dim]Skipping hunk #{i + 1}:[/dim] Already applied.")
            hunks_applied_count += 1
            continue

        is_overlapping = any(
            max(start_idx, r_start) < min(end_idx, r_end)
            for r_start, r_end, _ in replacements
        )

        if is_overlapping:
            console.print(
                f"  - [yellow]Skipping hunk #{i + 1}:[/yellow] Overlaps with a previous hunk."
            )
            continue

        # Success!
        # For loose matches, reindent new_lines to match the file's actual indentation.
        new_lines_to_apply = hunk["new_lines"]
        if match_type == "loose" and new_lines_to_apply and hunk_original:
            file_indent = _detect_indent(original_lines[selected_index:end_idx])
            hunk_indent = _detect_indent(hunk_original)
            if file_indent != hunk_indent:
                new_lines_to_apply = _reindent_lines(
                    new_lines_to_apply, hunk_indent, file_indent
                )
        replacements.append((start_idx, end_idx, new_lines_to_apply))
        hunks_applied_count += 1

    if hunks_applied_count == 0:
        console.print(
            f"❌ [bold red]Failed to apply patch to {file_path}:[/bold red] No applicable hunks found."
        )
        return _DiffOutcome(applied=0, total=len(hunks))

    # --- Application Phase ---

    # Sort replacements by start index in reverse to apply patches without shifting indices
    replacements.sort(key=lambda x: x[0], reverse=True)

    for start, end, new_lines in replacements:
        final_lines[start:end] = new_lines

    final_content = "\n".join(final_lines)
    if original_content.endswith("\n") and not final_content.endswith("\n"):
        final_content += "\n"

    if not dry_run:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(final_content)

    verb = "Would patch" if dry_run else "Patched"
    shortfall = (
        ""
        if hunks_applied_count == len(hunks)
        else "  [yellow]— the rest did not match[/yellow]"
    )
    console.print(
        f"✅ {verb} [green]{file_path}[/green] "
        f"({hunks_applied_count}/{len(hunks)} hunks applied){shortfall}"
    )
    return _DiffOutcome(
        applied=hunks_applied_count,
        total=len(hunks),
        content=final_content,
        written=not dry_run,
    )


def _confirm_destructive(
    prompt: str, *, opted_in: bool, what: str, default_desc: str
) -> bool:
    """Ask a human to confirm something destructive, or apply policy instead.

    Three cases, and the middle one is the one worth being careful about:

      - human attached      -> ask, exactly as before. No behaviour change.
      - no human, opted in  -> proceed WITHOUT asking. The caller already
        answered on the command line; re-prompting would be the stall.
      - no human, not opted -> decline, and say so on stderr.

    `opted_in` deliberately does not suppress the prompt when a human IS
    present: --allow-delete says "this run may delete", not "delete without
    telling me".
    """
    if human_attached():
        return click.confirm(prompt, default=False)
    if opted_in:
        print(f"kopipasta: {what} — permitted by flag, no human to ask.", file=sys.stderr)
        return True
    use_default_without_human(what, default_desc)
    return False


def normalise_path(path: str) -> str:
    """One spelling for one file.

    A selection record says `kopipasta/patcher.py`, git says the same, and a
    model writes `./kopipasta/patcher.py` or uses backslashes. Every place that
    compares a model-supplied path against a path from anywhere else goes
    through here — `apply.revert()` learned this the hard way, where the raw
    comparison meant "was this file already dirty?" answered no and
    `git checkout --` went over the caller's uncommitted work.
    """
    return os.path.normpath(path).replace("\\", "/")


def _normalise_zone(paths: Optional[Iterable[str]]) -> Optional[set]:
    """The editable zone, spelled the way the patcher spells paths."""
    if paths is None:
        return None
    return {normalise_path(p) for p in paths}


def apply_patches(
    patches: List[Patch],
    logger: Optional[BoundLogger] = None,
    allow_delete: bool = False,
    force: bool = False,
    dry_run: bool = False,
    allowed_files: Optional[Iterable[str]] = None,
) -> PatchResult:
    """
    Applies a list of patches to the filesystem.
    Dispatches between full-file replacement and diff-based patching.
    Returns a `PatchResult` — a list of the file paths that were modified,
    carrying the per-file outcomes the bare list could never express.

    `dry_run` runs the whole matching pass and writes nothing, so the preview
    is the same computation as the real run rather than a second guess at it.

    `allowed_files` is the Active Workspace of spec §11: a patch against
    anything else is recorded as SKIPPED. `None` means no restriction, which is
    what the interactive TUI path has always done.

    With no human attached, the two confirmation prompts below become policy
    (spec §11/§12) rather than questions: destructive actions are declined
    unless the caller opted in via `allow_delete` / `force`. Both already
    default to False and skip on decline, so the headless answer is the answer
    a careful human would have given anyway.

    Note this must NOT raise for the missing human, and neither must the
    editable-zone refusal: the per-file body is wrapped in a broad
    `except Exception` that would swallow either and report it as a mangled
    patch error. Injecting the decision is the only shape that survives that.
    """
    console = Console()
    result = PatchResult(dry_run=dry_run)
    zone = _normalise_zone(allowed_files)
    if not patches:
        console.print(
            "[yellow]No valid file patches found in the pasted content.[/yellow]"
        )
        return result

    verb = "Previewing" if dry_run else "Applying"
    console.print(f"\n[bold]{verb} {len(patches)} patch(es)...[/bold]")
    for patch in patches:
        file_path = patch["file_path"]
        patch_type = patch["type"]
        patch_content: PatchContent = patch["content"]

        # --- The Editable Zone (spec §11) ---
        # Checked before the try, not inside it: this is a policy decision and
        # the broad `except Exception` below would turn it into "corrupt patch",
        # sending the caller to debug something that was fine.
        if zone is not None and normalise_path(file_path) not in zone:
            console.print(
                f"   [yellow]Refused {file_path}: not in the editable set.[/yellow]"
            )
            result.record(
                FileOutcome(
                    path=file_path,
                    status=SKIPPED,
                    reason="not in the editable set for this session",
                )
            )
            if logger:
                logger.info(
                    "patch_skipped", file_path=file_path, reason="outside_editable_zone"
                )
            continue

        # --- Logging Original State (Forensics) ---
        original_content_log: Optional[str] = None
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    original_content_log = f.read()
            except IOError:
                original_content_log = "<IOError: Could not read original>"
        else:
            original_content_log = "<New File>"

        if logger:
            logger.info(
                "patch_attempt",
                file_path=file_path,
                patch_type=patch_type,
                original_content=original_content_log,
                patch_content=patch_content,
            )

        try:
            # --- Deletion Handling ---
            if patch_type == "delete":
                if os.path.exists(file_path):
                    if _confirm_destructive(
                        f"🗑️  Delete {file_path}?",
                        opted_in=allow_delete,
                        what=f"Deleting {file_path}",
                        default_desc="refusing the delete (pass --allow-delete to permit it)",
                    ):
                        try:
                            if not dry_run:
                                os.remove(file_path)
                            result.record(
                                FileOutcome(
                                    path=file_path, status=APPLIED, action="deleted"
                                )
                            )
                            console.print(
                                f"✅ {'Would delete' if dry_run else 'Deleted'} "
                                f"[red]{file_path}[/red]"
                            )
                            if logger:
                                logger.info(
                                    "patch_success",
                                    file_path=file_path,
                                    action="deleted",
                                )
                        except OSError as e:
                            console.print(
                                f"❌ [bold red]Failed to delete {file_path}: {e}[/bold red]"
                            )
                            result.record(
                                FileOutcome(
                                    path=file_path, status=FAILED, reason=str(e)
                                )
                            )
                            if logger:
                                logger.error(
                                    "patch_failed", file_path=file_path, error=str(e)
                                )
                    else:
                        console.print(f"   [dim]Skipped deletion of {file_path}[/dim]")
                        result.record(
                            FileOutcome(
                                path=file_path,
                                status=SKIPPED,
                                action="deleted",
                                reason="the delete was declined",
                            )
                        )
                        if logger:
                            logger.info(
                                "patch_skipped",
                                file_path=file_path,
                                reason="user_cancelled",
                            )
                else:
                    console.print(
                        f"   [yellow]File {file_path} not found, skipping delete.[/yellow]"
                    )
                    result.record(
                        FileOutcome(
                            path=file_path,
                            status=SKIPPED,
                            action="deleted",
                            reason="file not found",
                        )
                    )
                    if logger:
                        logger.warning(
                            "patch_skipped",
                            file_path=file_path,
                            reason="file_not_found",
                        )
                continue

            # If file doesn't exist, it's a simple creation.
            if not os.path.exists(file_path):
                if patch_type == "diff" and isinstance(patch_content, list):
                    # For a new file, a diff is just the content to be added.
                    full_content = "\n".join(
                        line for hunk in patch_content for line in hunk["new_lines"]
                    )
                elif isinstance(patch_content, str):
                    full_content = patch_content
                else:
                    result.record(
                        FileOutcome(
                            path=file_path,
                            status=FAILED,
                            reason="patch content was neither a diff nor file content",
                        )
                    )
                    continue

                if not dry_run:
                    parent_dir = os.path.dirname(file_path)
                    if parent_dir:
                        os.makedirs(parent_dir, exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(full_content)
                result.record(
                    FileOutcome(path=file_path, status=APPLIED, action="created")
                )
                console.print(
                    f"✅ {'Would create' if dry_run else 'Created'} [green]{file_path}[/green]"
                )
                if logger:
                    logger.info("patch_success", file_path=file_path, action="created")
                continue

            # File exists, so we apply a patch.
            with open(file_path, "r", encoding="utf-8") as f:
                original_content = f.read()

            if patch_type == "diff" and isinstance(patch_content, list):
                diff = _apply_diff_patch(
                    file_path, original_content, patch_content, console, dry_run=dry_run
                )
                if diff.ok:
                    # complete vs partial is the exit-4 distinction. Both wrote,
                    # so both are in the modified list; only one of them is fine.
                    result.record(
                        FileOutcome(
                            path=file_path,
                            status=APPLIED if diff.complete else PARTIAL,
                            action="diff_applied",
                            hunks_applied=diff.applied,
                            hunks_total=diff.total,
                            reason=""
                            if diff.complete
                            else f"{diff.total - diff.applied} of {diff.total} hunks did not match",
                        )
                    )
                    if logger:
                        logger.info(
                            "patch_success",
                            file_path=file_path,
                            action="diff_applied",
                            hunks_applied=diff.applied,
                            hunks_total=diff.total,
                        )
                else:
                    result.record(
                        FileOutcome(
                            path=file_path,
                            status=FAILED,
                            action="diff_applied",
                            hunks_applied=0,
                            hunks_total=diff.total,
                            reason="no hunk matched the file",
                        )
                    )
                    if logger:
                        logger.error(
                            "patch_failed",
                            file_path=file_path,
                            error="diff_application_failed",
                        )

            elif isinstance(patch_content, str):  # 'full'
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
                            f"   • File shrinking significantly: {original_len} -> {new_len} chars (-{100 - int(new_len / original_len * 100)}%)"
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

                    if not _confirm_destructive(
                        f"   Are you sure you want to overwrite {file_path}?",
                        opted_in=force,
                        what=f"Overwriting {file_path} despite the safety check",
                        default_desc="skipping the file (pass --force to overwrite anyway)",
                    ):
                        console.print(f"   [red]Skipped {file_path}[/red]")
                        result.record(
                            FileOutcome(
                                path=file_path,
                                status=SKIPPED,
                                action="overwritten",
                                reason="declined by the safety check",
                            )
                        )
                        if logger:
                            logger.info(
                                "patch_skipped",
                                file_path=file_path,
                                reason="safety_check_declined",
                            )
                        continue

                if not dry_run:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(final_content)

                result.record(
                    FileOutcome(
                        path=file_path, status=APPLIED, action="overwritten"
                    )
                )
                console.print(
                    f"✅ {'Would overwrite' if dry_run else 'Overwrote'} "
                    f"[green]{file_path}[/green] (Full Content)"
                )
                if logger:
                    logger.info(
                        "patch_success", file_path=file_path, action="overwritten"
                    )

        except Exception as e:
            console.print(f"❌ [bold red]Error processing {file_path}: {e}[/bold red]")
            result.record(
                FileOutcome(path=file_path, status=FAILED, reason=str(e))
            )
            if logger:
                logger.error("patch_failed", file_path=file_path, error=str(e))

    return result
