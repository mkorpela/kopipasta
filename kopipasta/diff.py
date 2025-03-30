from dataclasses import dataclass
from typing import List, Optional
import difflib

@dataclass
class Hunk:
    context_before: List[str]
    removals: List[str]
    additions: List[str]
    context_after: List[str]

def parse_unified_diff(diff_text: str) -> List[Hunk]:
    hunks = []
    current_hunk = None
    for line in diff_text.splitlines():
        if line.startswith('@@'):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = Hunk(context_before=[], removals=[], additions=[], context_after=[])
            continue
        if current_hunk is None:
            continue  # Skip file header lines
        prefix = line[:1]
        content = line[1:]
        if prefix == ' ':
            if current_hunk.removals or current_hunk.additions:
                current_hunk.context_after.append(content)
            else:
                current_hunk.context_before.append(content)
        elif prefix == '-':
            current_hunk.removals.append(content)
        elif prefix == '+':
            current_hunk.additions.append(content)
    if current_hunk:
        hunks.append(current_hunk)
    return hunks

def lines_similar(a: List[str], b: List[str], threshold: float = 0.75) -> bool:
    if len(a) != len(b):
        return False
    normalize = lambda lines: "\n".join(line.strip() for line in lines)
    seq = difflib.SequenceMatcher(None, normalize(a), normalize(b))
    return seq.ratio() >= threshold

def find_hunk_position_fuzzy(file_lines: List[str], hunk: Hunk) -> Optional[int]:
    pattern = hunk.context_before + hunk.context_after
    pat_len = len(pattern)
    best_match_idx = None
    best_score = 0.0
    for i in range(0, len(file_lines) - pat_len + 1):
        window = file_lines[i : i + pat_len]
        if window == pattern or lines_similar(window, pattern):
            score = sum(1 for j in range(pat_len) if window[j].strip() == pattern[j].strip()) / pat_len
            if score > best_score:
                best_score = score
                best_match_idx = i
    return best_match_idx + len(hunk.context_before) if best_match_idx is not None else None

def apply_hunk_at(file_lines: List[str], hunk: Hunk, pos: int) -> bool:
    for i, rem_line in enumerate(hunk.removals):
        if pos + i >= len(file_lines) or file_lines[pos + i].strip() != rem_line.strip():
            return False
    file_lines[pos : pos + len(hunk.removals)] = hunk.additions
    return True

# Example target file and diff for testing
target_code = [
    "def greet():",
    "    print(\"Hello\")",
    "    return"
]

patch_text = """@@ -1,3 +1,4 @@
 def greet():
     print("Hello")
+    print("Welcome")
     return
"""

# Run the patch process
hunks = parse_unified_diff(patch_text)
file_lines = target_code.copy()

results = []
for hunk in hunks:
    pos = find_hunk_position_fuzzy(file_lines, hunk)
    if pos is not None:
        success = apply_hunk_at(file_lines, hunk, pos)
        results.append((pos, success))
    else:
        results.append((None, False))

patched_code = "\n".join(file_lines)
results, patched_code
