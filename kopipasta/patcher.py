import os
import re
from typing import List, Tuple
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


def apply_patches(patches: List[Tuple[str, str]]) -> None:
    """
    Applies a list of patches to the filesystem.
    For existing files, it finds the most similar block and replaces it.
    For new files, it creates them.
    """
    console = Console()
    is_debug = os.environ.get("KOPIPASTA_DEBUG")
    if not patches:
        console.print("[yellow]No valid file patches found in the pasted content.[/yellow]")
        return

    console.print(f"\n[bold]Applying {len(patches)} patch(es)...[/bold]")
    for file_path, patch_content in patches:
        try:
            # If file doesn't exist, it's a simple creation.
            if not os.path.exists(file_path):
                parent_dir = os.path.dirname(file_path)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(patch_content)
                console.print(f"✅ Created [green]{file_path}[/green]")
                continue

            # File exists, so we apply an intelligent patch.
            with open(file_path, "r", encoding="utf-8") as f:
                original_content = f.read()

            original_lines = original_content.splitlines()
            patch_lines = patch_content.splitlines()

            if not patch_lines:
                console.print(f"⚠️ Skipped empty patch for [yellow]{file_path}[/yellow]")
                continue
            
            # Find the longest contiguous matching block (the anchor).
            matcher = SequenceMatcher(None, original_lines, patch_lines, autojunk=False)
            match = matcher.find_longest_match(0, len(original_lines), 0, len(patch_lines))

            if match.size == 0:
                 # If there's no common ground, we can't patch.
                console.print(f"❌ [bold red]Failed to apply patch to {file_path}:[/bold red] No common content found. File left unchanged.")
                continue

            # Determine the block to replace in the original file based on the anchor.
            original_replace_start = max(0, match.a - match.b)
            # The length of the block to replace should be relative to the original file's size
            # based on how much the patch covers before and after the anchor.
            lines_before_anchor_in_patch = match.b
            lines_after_anchor_in_patch = len(patch_lines) - (match.b + match.size)
            original_replace_end = min(len(original_lines), match.a + match.size + lines_after_anchor_in_patch)

            # Perform a final confidence check on the identified block.
            original_block_to_replace = original_lines[original_replace_start:original_replace_end]
            confidence_matcher = SequenceMatcher(None, original_block_to_replace, patch_lines, autojunk=False)
            confidence_ratio = confidence_matcher.ratio()
            
            # --- DEBUG BLOCK ---
            if is_debug:
                original_block_text = "\n".join(original_block_to_replace)
                patch_text = "\n".join(patch_lines)
                console.print(f"\n[DEBUG] --- Patch Analysis for: {file_path} ---")
                console.print(f"[DEBUG] Final confidence score: {confidence_ratio*100:.1f}%")
                console.print(f"[DEBUG] --- Original Block (lines {original_replace_start+1}-{original_replace_end}) ---")
                console.print(original_block_text)
                console.print("[DEBUG] --- Patch Snippet ---")
                console.print(patch_text)
                console.print("[DEBUG] --- End Analysis ---\n")
            # --- END DEBUG BLOCK ---

            if confidence_ratio < 0.3: # Lowered threshold
                console.print(
                    f"❌ [bold red]Failed to apply patch to {file_path}:[/bold red] Snippet did not match content confidently (Similarity: {confidence_ratio*100:.1f}%). File left unchanged."
                )
                continue

            # Construct the new file content by replacing the identified block.
            final_lines = original_lines[:original_replace_start] + patch_lines + original_lines[original_replace_end:]
            final_content = "\n".join(final_lines)
            
            if original_content.endswith('\n'):
                final_content += '\n'

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(final_content)

            console.print(f"✅ Patched [green]{file_path}[/green]")

        except Exception as e:
            console.print(f"❌ [bold red]Error processing {file_path}: {e}[/bold red]")