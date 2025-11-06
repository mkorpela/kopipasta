import os
import re
from typing import List, Tuple, Optional
from difflib import SequenceMatcher

from rich.console import Console


def parse_llm_output(content: str) -> List[Tuple[str, str]]:
    """
    Parses LLM markdown output to find file patches.
    Looks for fenced code blocks with a special `// FILE: path/to/file.ext` comment.
    Returns a list of (file_path, new_content) tuples.
    """
    patches = []
    # Regex to find fenced code blocks, optionally with a language hint
    code_block_regex = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
    # Regex to find the file path comment, supporting various comment styles
    file_path_regex = re.compile(
        r"^(?:#|//|\/\*)\s*FILE:\s*(\S+)\s*(?:\*\/)?\s*\n?", re.MULTILINE
    )

    for match in code_block_regex.finditer(content):
        block_content = match.group(1)
        file_path_match = file_path_regex.search(block_content)
        if file_path_match:
            file_path = file_path_match.group(1).strip()
            # The actual code is what's left after the file path comment
            code_content = file_path_regex.sub("", block_content, count=1).lstrip("\n")
            patches.append((file_path, code_content))

    return patches


def _find_best_match_location(
    original_lines: List[str], patch_lines: List[str]
) -> Optional[Tuple[int, int, float]]:
    """
    Finds the best matching contiguous block for patch_lines within original_lines.
    Returns (start_index_in_original, end_index_in_original, best_ratio).
    """
    if not original_lines or not patch_lines:
        return None

    s = SequenceMatcher(None, original_lines, patch_lines, autojunk=False)
    match = s.find_longest_match(0, len(original_lines), 0, len(patch_lines))

    # To determine the full span of the patch in the original file, we can
    # use the opcodes. The section of the original file to be replaced
    # corresponds to the range covered by the opcodes when applied to the
    # patch block.
    opcodes = s.get_opcodes()
    if not opcodes:
        return None

    first_opcode = opcodes[0]
    last_opcode = opcodes[-1]

    original_start = first_opcode[1]
    original_end = last_opcode[2]

    # The ratio should be calculated against the corresponding slice of the original
    # not the entire file. This gives a better sense of local similarity.
    original_slice = original_lines[original_start:original_end]
    s_local = SequenceMatcher(None, original_slice, patch_lines, autojunk=False)
    local_ratio = s_local.ratio()

    return original_start, original_end, local_ratio


def apply_patches(patches: List[Tuple[str, str]]) -> None:
    """
    Applies a list of patches to the filesystem.
    For existing files, it attempts to apply the content as a patch.
    For new files, it creates them.
    """
    console = Console()
    if not patches:
        console.print("[yellow]No valid file patches found in the pasted content.[/yellow]")
        return

    console.print(f"\n[bold]Applying {len(patches)} patch(es)...[/bold]")
    for file_path, patch_content in patches:
        try:
            if not os.path.exists(file_path):
                # File is new, create it directly
                parent_dir = os.path.dirname(file_path)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(patch_content)
                console.print(f"✅ Created [green]{file_path}[/green]")
                continue

            # File exists, apply as a patch
            with open(file_path, "r", encoding="utf-8") as f:
                original_content = f.read()

            original_lines = original_content.splitlines(keepends=True)
            patch_lines = patch_content.splitlines(keepends=True)

            match = _find_best_match_location(original_lines, patch_lines)

            if match is None or match[2] < 0.6:
                console.print(
                    f"❌ [bold red]Failed to apply patch to {file_path}:[/bold red] Snippet did not match content confidently. (Similarity: {match[2]*100:.1f}%). File left unchanged."
                )
                continue

            start, end, _ = match
            new_lines = original_lines[:start] + patch_lines + original_lines[end:]
            new_content = "".join(new_lines)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            console.print(f"✅ Patched [green]{file_path}[/green]")

        except Exception as e:
            console.print(f"❌ [bold red]Error processing {file_path}: {e}[/bold red]")