import os
import subprocess
import platform
import shutil
from typing import Dict, List, Optional, Tuple
from prompt_toolkit import prompt as prompt_toolkit_prompt
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.tree import Tree
from rich.panel import Panel
from rich.text import Text
import pyperclip
import click

from kopipasta.patcher import apply_patches, parse_llm_output
from kopipasta.file import FileTuple, is_binary, is_ignored, get_human_readable_size, read_file_contents
from kopipasta.prompt import get_file_snippet, get_language_for_file
from kopipasta.prompt import generate_fix_prompt
from kopipasta.cache import load_selection_from_cache, clear_cache
from kopipasta.ops import (
    sanitize_string,
    estimate_tokens,
)
from kopipasta.analysis import (
    propose_and_add_dependencies,
    grep_files_in_directory,
    select_from_grep_results,
)
from kopipasta.selection import SelectionManager, FileState
from kopipasta.session import Session, SESSION_FILENAME
from kopipasta.patcher import find_paths_in_text
from kopipasta.config import read_fix_command
from kopipasta.prompt import generate_extension_prompt

ALWAYS_VISIBLE_FILES = {"AI_SESSION.md", "AI_CONTEXT.md"}

class FileNode:
    """Represents a file or directory in the tree"""

    def __init__(
        self,
        path: str,
        is_dir: bool,
        parent: Optional["FileNode"] = None,
        is_scan_root: bool = False,
    ):
        self.path = os.path.abspath(path)
        self.is_dir = is_dir
        self.parent = parent
        self.children: List["FileNode"] = []
        self.expanded = False
        self.is_scan_root = is_scan_root
        # Base size (for files) or initial placeholder (for dirs)
        self.size = 0 if is_dir else os.path.getsize(self.path)
        # New attributes for caching the results of a deep scan
        self.total_size: int = self.size
        self.is_scanned: bool = not self.is_dir

    @property
    def name(self):
        base = os.path.basename(self.path)
        if not base and self.is_scan_root:
            return "Project Root"
        return base or self.path

    @property
    def relative_path(self):
        # os.path.relpath is relative to the current working directory by default
        return os.path.relpath(self.path)


class TreeSelector:
    """Interactive file tree selector using Rich"""

    def __init__(self, ignore_patterns: List[str], project_root_abs: str):
        self.console = Console()
        self.ignore_patterns = ignore_patterns
        self.project_root_abs = project_root_abs
        self.manager = SelectionManager()
        
        self.current_index = 0
        self.nodes: List[FileNode] = []
        self.visible_nodes: List[FileNode] = []
        self.session = Session(project_root_abs)
        self.quit_selection = False
        self.viewport_offset = 0  # First visible item index
        self._metrics_cache: Dict[str, Tuple[int, int]] = {}
        self._init_key_bindings()

    def _calculate_directory_metrics(self, node: FileNode) -> Tuple[int, int]:
        """Recursively calculate total and selected size for a directory."""
        if not node.is_dir:
            return 0, 0

        # If the directory itself is ignored, don't explore it.
        if is_ignored(node.path, self.ignore_patterns, self.project_root_abs):
            return 0, 0

        # Use instance cache for this render cycle
        if node.path in self._metrics_cache:
            return self._metrics_cache[node.path]

        total_size = 0
        selected_size = 0

        # Ensure directory is scanned
        if not node.children:
            self._deep_scan_directory_and_calc_size(node.path, node)

        for child in node.children:
            if child.is_dir:
                child_total, child_selected = self._calculate_directory_metrics(child)
                total_size += child_total
                selected_size += child_selected
            else:  # It's a file
                total_size += child.size
                state = self.manager.get_state(child.path)
                if state != FileState.UNSELECTED:
                    is_snippet = self.manager.is_snippet(child.path)
                    if is_snippet:
                        selected_size += len(get_file_snippet(child.path))
                    else:
                        selected_size += child.size

        self._metrics_cache[node.path] = (total_size, selected_size)
        return total_size, selected_size

    def build_tree(self, paths: List[str]) -> FileNode:
        """Build tree structure from given paths."""
        # If one directory is given, make its contents the top level of the tree.
        if len(paths) == 1 and os.path.isdir(paths[0]):
            root_path = os.path.abspath(paths[0])
            root = FileNode(root_path, True, is_scan_root=True)
            root.expanded = True
            self._deep_scan_directory_and_calc_size(root_path, root)
            return root

        # Otherwise, create a virtual root to hold multiple items (e.g., `kopipasta file.py dir/`).
        # This virtual root itself won't be displayed.
        virtual_root_path = os.path.join(
            self.project_root_abs, "__kopipasta_virtual_root__"
        )
        root = FileNode(virtual_root_path, True, is_scan_root=True)
        root.expanded = True
        # Assign a meaningful name to the virtual root for display
        # (FileNode uses basename, but virtual path ends in __kopipasta_virtual_root__)
        # The name property logic handles empty basename, but let's ensure it's nice.

        for path in paths:
            abs_path = os.path.abspath(path)
            node = None
            basename = os.path.basename(abs_path)
            if os.path.isfile(abs_path):
                if basename in ALWAYS_VISIBLE_FILES or (
                    not is_ignored(abs_path, self.ignore_patterns, self.project_root_abs)
                    and not is_binary(abs_path)
                ) and not is_binary(abs_path):
                    node = FileNode(abs_path, False, root)
            elif os.path.isdir(abs_path):
                node = FileNode(abs_path, True, root)

            if node:
                root.children.append(node)

        return root

    def _deep_scan_directory_and_calc_size(self, dir_path: str, parent_node: FileNode):
        """Recursively scan directory and build tree"""
        abs_dir_path = os.path.abspath(dir_path)

        # Check if we've already scanned this directory
        if parent_node.children:
            return

        try:
            items = sorted(os.listdir(abs_dir_path))
        except PermissionError:
            return

        # Separate and sort directories and files
        dirs = []
        files = []

        for item in items:
            item_path = os.path.join(abs_dir_path, item)
            if item in ALWAYS_VISIBLE_FILES:
                # Explicitly allow key memory files
                pass
            elif is_ignored(item_path, self.ignore_patterns, self.project_root_abs):
                continue

            if os.path.isdir(item_path):
                dirs.append(item)
            elif os.path.isfile(item_path) and not is_binary(item_path):
                files.append(item)

        # Add directories first
        for dir_name in sorted(dirs):
            dir_path_full = os.path.join(abs_dir_path, dir_name)
            # Check if this node already exists as a child
            existing = next(
                (
                    child
                    for child in parent_node.children
                    if os.path.abspath(child.path) == os.path.abspath(dir_path_full)
                ),
                None,
            )
            if not existing:
                dir_node = FileNode(dir_path_full, True, parent_node)
                parent_node.children.append(dir_node)

        # Then add files
        for file_name in sorted(files):
            file_path = os.path.join(abs_dir_path, file_name)
            # Check if this node already exists as a child
            existing = next(
                (
                    child
                    for child in parent_node.children
                    if os.path.abspath(child.path) == os.path.abspath(file_path)
                ),
                None,
            )
            if not existing:
                file_node = FileNode(file_path, False, parent_node)
                parent_node.children.append(file_node)

    def _flatten_tree(
        self, node: FileNode, level: int = 0
    ) -> List[Tuple[FileNode, int]]:
        """Flatten tree into a list of (node, level) tuples for display."""
        result = []
        
        result.append((node, level))
        if node.is_dir and node.expanded:
            if not node.children:
                self._deep_scan_directory_and_calc_size(node.path, node)
            for child in node.children:
                result.extend(self._flatten_tree(child, level + 1))

        return result

    def _build_display_tree(self) -> Tree:
        """Build Rich tree for display with viewport"""
        self._metrics_cache = {}  # Clear cache for each new render

        # Get terminal size
        _, term_height = shutil.get_terminal_size()

        # Reserve space for header, help panel, and status
        reserved_space = 12
        available_height = term_height - reserved_space
        available_height = max(5, available_height)  # Minimum height

        # Flatten tree to get all visible nodes
        flat_tree = self._flatten_tree(self.root)
        self.visible_nodes = [node for node, _ in flat_tree]

        # Calculate viewport
        if self.visible_nodes:
            # Ensure current selection is visible
            if self.current_index < self.viewport_offset:
                self.viewport_offset = self.current_index
            elif self.current_index >= self.viewport_offset + available_height:
                self.viewport_offset = self.current_index - available_height + 1

            # Clamp viewport to valid range
            max_offset = max(0, len(self.visible_nodes) - available_height)
            self.viewport_offset = max(0, min(self.viewport_offset, max_offset))
        else:
            self.viewport_offset = 0

        # Create tree with scroll indicators
        tree = Tree("root", hide_root=True)
        if self.viewport_offset > 0:
            tree.add(Text(f"↑ ({self.viewport_offset} more items)", style="dim italic"))


        # Build tree structure - only for visible portion
        viewport_end = min(len(flat_tree), self.viewport_offset + available_height)

        # Track what level each visible item is at for proper tree structure
        level_stacks = {}  # level -> stack of tree nodes

        for i in range(self.viewport_offset, viewport_end):
            node, level = flat_tree[i]

            # Determine style and icon
            is_current = i == self.current_index
            style = "bold cyan" if is_current else ""

            label = Text()

            if node.is_dir:
                icon = "📂" if node.expanded else "📁"
                total_size, selected_size = self._calculate_directory_metrics(node)
                if total_size > 0:
                    size_str = f" ({get_human_readable_size(selected_size)} / {get_human_readable_size(total_size)})"
                else:
                    size_str = ""  # Don't show size for empty dirs

                # Omit the selection circle for directories
                label.append(f"{icon} {node.name}{size_str}", style=style)

            else:  # It's a file
                icon = "📄"
                size_str = f" ({get_human_readable_size(node.size)})"

                # File selection indicator
                abs_path = os.path.abspath(node.path)
                state = self.manager.get_state(abs_path)
                if state != FileState.UNSELECTED:
                    is_snippet = self.manager.is_snippet(abs_path)
                    selection = "◐" if is_snippet else "●"
                    if state == FileState.DELTA:
                        style = "green " + style
                    else: # BASE
                        style = "cyan " + style
                else:
                    selection = "○"

                label.append(f"{selection} ", style="dim")
                label.append(f"{icon} {node.name}{size_str}", style=style)

            # Add to tree at correct level
            if level == 0:
                tree_node = tree.add(label)
                level_stacks[0] = tree_node
            else:
                # Find parent at previous level
                parent_level = level - 1
                if parent_level in level_stacks:
                    parent_tree = level_stacks[parent_level]
                    tree_node = parent_tree.add(label)
                    level_stacks[level] = tree_node
                else:
                    # Fallback - add to root with indentation indicator
                    indent_text = "  " * level
                    if not node.is_dir:
                        # Re-add file selection marker for indented fallback
                        selection_char = "○"
                        if node.path in self.selected_files:
                            selection_char = (
                                "◐" if self.selected_files[node.path][0] else "●"
                            )
                        indent_text += f"{selection_char} "

                    # Create a new label with proper indentation for this edge case
                    fallback_label_text = f"{indent_text}{label.plain}"
                    tree_node = tree.add(Text(fallback_label_text, style=style))
                    level_stacks[level] = tree_node

        # Add scroll indicator at bottom if needed
        if viewport_end < len(self.visible_nodes):
            remaining = len(self.visible_nodes) - viewport_end
            tree.add(Text(f"↓ ({remaining} more items)", style="dim italic"))

        return tree

    def _show_help(self) -> Panel:
        """Create help panel"""
        
        action_list = ["r: Reuse", "g: Grep", "p: Patch", "x: Fix"]
        if self.manager.delta_count > 0:
            action_list.append("e: Extend")
        action_list.append("d: Deps")

        actions = "   ".join(action_list)
        if self.session.is_active:
            actions += "   u: Update Session   f: Finish Task"
        else:
            actions += "   n: Start Session"

        help_text = f"""[bold]Navigation:[/bold]  ↑/k: Up  ↓/j: Down  →/l/Enter: Expand  ←/h: Collapse
[bold]Selection:[/bold]  Space: Toggle selection  a: Add all in dir     s: Snippet mode
[bold]Actions:[/bold]    {actions}
q: Quit and finalize"""

        return Panel(
            help_text, title="Keyboard Controls", border_style="dim", expand=False
        )

    def _get_status_bar(self) -> str:
        """Create status bar with selection info"""
        # Current item info
        if self.visible_nodes and 0 <= self.current_index < len(self.visible_nodes):
            current = self.visible_nodes[self.current_index]
            current_info = f"[dim]Current:[/dim] {current.relative_path}"
        else:
            current_info = "No selection"

        session_indicator = "[bold green]SESSION ON[/bold green]" if self.session.is_active else "[dim]Session Off[/dim]"

        selection_info = f"[dim]Selected:[/dim] {self.manager.delta_count} delta, {self.manager.base_count} base | ~{self.manager.char_count:,} chars (~{estimate_tokens(self.manager.char_count):,} tokens)"

        return f"\n{current_info} | {selection_info} | {session_indicator}\n"

    def _handle_grep(self, node: FileNode):
        """Handle grep search in directory"""
        if not node.is_dir:
            self.console.print("[red]Grep only works on directories[/red]")
            return

        pattern = click.prompt("Enter search pattern")
        if not pattern:
            return

        self.console.print(f"Searching for '{pattern}' in {node.relative_path}...")

        grep_results = grep_files_in_directory(pattern, node.path, self.ignore_patterns)
        if not grep_results:
            self.console.print(f"[yellow]No matches found for '{pattern}'[/yellow]")
            return

        # Show results and let user select
        selected_files, _ = select_from_grep_results(
            grep_results, self.manager.char_count
        )

        # Add selected files
        added_count = 0
        for file_tuple in selected_files:
            file_path, is_snippet, chunks, _ = file_tuple
            abs_path = os.path.abspath(file_path)

            # Check if already selected
            if self.manager.get_state(abs_path) == FileState.UNSELECTED:
                self.manager.set_state(abs_path, FileState.DELTA, is_snippet=is_snippet, chunks=chunks)
                added_count += 1
                # Ensure the file is visible in the tree
                self._ensure_path_visible(abs_path)

        # Show summary of what was added
        if added_count > 0:
            self.console.print(
                f"\n[green]Added {added_count} files from grep results[/green]"
            )
        else:
            self.console.print(
                f"\n[yellow]All selected files were already in selection[/yellow]"
            )

    def _handle_apply_patches(self):
        """Handles the 'p' keypress to apply patches from pasted text."""
        self.console.clear()
        self.console.print(
            Panel(
                "[bold cyan]📝 Paste the LLM's markdown response below.[/bold cyan]\n\n"
                "   - Press [bold]Meta+Enter[/bold] or [bold]Esc[/bold] then [bold]Enter[/bold] to submit.\n"
                "   - Press [bold]Ctrl-C[/bold] to cancel.",
                title="Apply Patches",
                border_style="cyan",
            )
        )

        style = Style.from_dict({"": "#ffffff"})
        try:
            content = prompt_toolkit_prompt(
                "> ",
                multiline=True,
                prompt_continuation="  ",
                style=style,
            )
            content = sanitize_string(content)
            if not content.strip():
                self.console.print("\n[yellow]No content pasted. Aborting.[/yellow]")
                return

            patches = parse_llm_output(content, self.console)
            if patches:
                modified_files = apply_patches(patches)
                
                # Promote patched files to Delta and everything else to Base
                self.manager.promote_all_to_base()
                for path in modified_files:
                    self.manager.mark_as_delta(path)

                # --- Auto-Commit Logic ---
                if self.session.is_active:
                    self.session.auto_commit()

                self.console.print(
                    "\n[bold]Review the changes above with `git diff` before committing.[/bold]"
                )
                return

            # --- No patches found: Fallback to Path Scanning (Intelligent Import) ---
            all_paths = self._get_all_unignored_files()
            found_paths = find_paths_in_text(content, all_paths)

            if found_paths:
                self.console.print(f"\n[bold cyan]🔍 Found {len(found_paths)} project paths in text.[/bold cyan]")
                for p in sorted(found_paths)[:10]:
                    self.console.print(f"  • {p}")
                if len(found_paths) > 10:
                    self.console.print(f"  ... and {len(found_paths)-10} more.")

                choice = click.prompt(
                    "\n[A]ppend to current selection, [R]eplace selection, or [C]ancel?",
                    type=click.Choice(['a', 'r', 'c'], case_sensitive=False),
                    default='a'
                )

                if choice == 'c':
                    return
                if choice == 'r':
                    self.manager.clear_all()

                for path in found_paths:
                    abs_p = os.path.abspath(path)
                    self._ensure_path_visible(abs_p)
                    self.manager.set_state(abs_p, FileState.DELTA)
                
                self.console.print(f"\n[green]Successfully added {len(found_paths)} files to Delta focus.[/green]")
            else:
                self.console.print("\n[yellow]No patches or valid project paths detected in pasted content.[/yellow]")
        except KeyboardInterrupt:
            self.console.print("\n[red]Patch application cancelled.[/red]")

    def _handle_fix(self):
        """
        Handles the 'x' keypress: Run a fix command, capture errors,
        detect affected files, generate a diagnostic prompt, and copy to clipboard.
        """
        fix_cmd = read_fix_command(self.project_root_abs)

        self.console.clear()
        self.console.print(
            Panel(
                f"[bold cyan]🔧 Fix Workflow[/bold cyan]\n\n"
                f"   Command: [bold]{fix_cmd}[/bold]\n\n"
                f"   Press [bold]Enter[/bold] to run, or [bold]Ctrl-C[/bold] to cancel.\n"
                f"   [dim]Configure via AI_CONTEXT.md: <!-- KOPIPASTA_FIX_CMD: your command -->[/dim]",
                title="Fix",
                border_style="yellow",
            )
        )

        try:
            click.pause()
        except KeyboardInterrupt:
            self.console.print("\n[red]Fix cancelled.[/red]")
            return

        # --- Run the command ---
        self.console.print(f"\n[bold]Running:[/bold] {fix_cmd}\n")
        try:
            # Use shell=True for complex commands (pipes, &&, etc.)
            # Set cwd to project root for consistent behavior
            result = subprocess.run(
                fix_cmd,
                shell=True,
                capture_output=True,
                text=True,
                cwd=self.project_root_abs,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            self.console.print("[bold red]Command timed out after 120 seconds.[/bold red]")
            return
        except Exception as e:
            self.console.print(f"[bold red]Failed to run command: {e}[/bold red]")
            return

        combined_output = ""
        if result.stdout:
            combined_output += result.stdout
        if result.stderr:
            combined_output += result.stderr

        if result.returncode == 0:
            self.console.print("[bold green]✅ Command succeeded! No errors to fix.[/bold green]")
            if combined_output.strip():
                # Show truncated output even on success
                preview_lines = combined_output.strip().splitlines()[:10]
                self.console.print("[dim]" + "\n".join(preview_lines) + "[/dim]")
            return

        # --- Command failed: show output ---
        self.console.print(f"[bold yellow]⚠ Command exited with code {result.returncode}[/bold yellow]\n")
        output_lines = combined_output.strip().splitlines()
        # Show a reasonable preview
        for line in output_lines[:30]:
            self.console.print(f"  [dim]{line}[/dim]")
        if len(output_lines) > 30:
            self.console.print(f"  [dim]... ({len(output_lines) - 30} more lines)[/dim]")

        # --- Detect affected files from error output ---
        all_project_files = self._get_all_unignored_files()
        found_paths = find_paths_in_text(combined_output, all_project_files)

        if found_paths:
            self.console.print(f"\n[bold cyan]🔍 Detected {len(found_paths)} affected file(s):[/bold cyan]")
            for p in sorted(found_paths)[:15]:
                self.console.print(f"  • {p}")
            if len(found_paths) > 15:
                self.console.print(f"  ... and {len(found_paths) - 15} more.")

            # Add to Delta
            for path in found_paths:
                abs_p = os.path.abspath(os.path.join(self.project_root_abs, path))
                if os.path.isfile(abs_p):
                    self._ensure_path_visible(abs_p)
                    if self.manager.get_state(abs_p) == FileState.UNSELECTED:
                        self.manager.set_state(abs_p, FileState.DELTA)
                    elif self.manager.get_state(abs_p) == FileState.BASE:
                        self.manager.mark_as_delta(abs_p)
        else:
            self.console.print("\n[yellow]Could not auto-detect affected files from error output.[/yellow]")

        # --- Capture git diff ---
        git_diff = ""
        try:
            diff_result = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True, text=True,
                cwd=self.project_root_abs, timeout=30,
            )
            git_diff = diff_result.stdout.strip()
        except Exception:
            pass  # Non-fatal: diff is optional context

        # --- Generate and copy prompt ---
        affected_file_tuples = self.manager.get_delta_files()
        prompt_text = generate_fix_prompt(
            command=fix_cmd,
            error_output=combined_output.strip(),
            git_diff=git_diff,
            affected_files=affected_file_tuples,
            env_vars={},  # env_vars are handled at the prompt layer
        )

        try:
            pyperclip.copy(prompt_text)
            self.console.print(f"\n[bold green]📋 Fix prompt copied to clipboard![/bold green]")
            self.console.print(f"[dim]Contains: error output + git diff + {len(affected_file_tuples)} file(s)[/dim]")
        except pyperclip.PyperclipException:
            self.console.print("\n[red]Failed to copy to clipboard. Prompt printed above.[/red]")

    def _get_all_unignored_files(self) -> List[str]:
        """Walks the project to find all non-binary, non-ignored files for path scanning."""
        all_files = []
        for root, dirs, files in os.walk(self.project_root_abs):
            dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d), self.ignore_patterns, self.project_root_abs)]
            for f in files:
                full_path = os.path.join(root, f)
                if not is_ignored(full_path, self.ignore_patterns, self.project_root_abs) and not is_binary(full_path):
                    all_files.append(os.path.relpath(full_path, self.project_root_abs))
        return all_files

    def _action_extend(self):
        """
        Handles the 'e' key: Generates a minimal prompt with only Delta files,
        copies to clipboard, and promotes Delta -> Base.
        """
        delta_files = self.manager.get_delta_files()
        
        if not delta_files:
            return

        # Generate minimal prompt
        prompt_text = generate_extension_prompt(delta_files, {})
        
        try:
            pyperclip.copy(prompt_text)
            self.console.print(f"\n[green]📋 Extended context ({len(delta_files)} files) copied to clipboard![/green]")
            
            # Transition Delta to Base
            self.manager.promote_delta_to_base()
            self.console.print("[dim]Selected files moved from Green (Delta) to Cyan (Base).[/dim]")
            
        except pyperclip.PyperclipException:
            self.console.print("\n[red]Failed to copy to clipboard.[/red]")
            
        click.pause("Press any key to return...")

    def _handle_session_update(self):
        """Handles 'u' key: Updates AI_SESSION.md (Handover/Checkpoint)."""
        if not self.session.is_active:
            self.console.print("[yellow]No active session to update.[/yellow]")
            click.pause("Press any key to continue...")
            return
        
        # READ CONTENT TO INJECT
        session_content = self.session.content

        prompt_text = (
            "# Session Handover / Checkpoint\n\n"
            "We are wrapping up this context window. Update `AI_SESSION.md` to preserve our mental state for the next session.\n\n"
            "## Current Session State\n"
            f"```markdown\n{session_content}\n```\n\n"
            "## Instructions\n"
            "1. **Consolidate State**: Merge new findings into `AI_SESSION.md`. Keep it concise but lossless.\n"
            "2. **Track Decisions**: Add a '## Architecture & Decisions' section if new patterns were established.\n"
            "3. **Update Next Steps**: Check off completed items. Add new ones based on recent discoveries.\n"
            "4. **Preserve Context**: Do not remove info that is still relevant for the immediate next steps.\n"
            "5. **Preserve Metadata**: You MUST keep the `<!-- KOPIPASTA_METADATA ... -->` line at the top verbatim.\n\n"
            "## Required Output Format\n"
            "Return the **FULL** content of the new `AI_SESSION.md` inside a Markdown code block.\n"
            "Use this exact format:\n"
            "````markdown\n"
            "```markdown\n"
            "<!-- FILE: AI_SESSION.md -->\n"
            "<!-- KOPIPASTA_METADATA ... -->\n"
            "# Current Working Session\n"
            "...\n"
            "```\n"
            "````"
        )
        self._run_gardener_cycle(prompt_text, "Update Session / Handover")

    def _handle_task_completion(self):
        """Handles 'f' key: Merges session to context and clears session (Harvest)."""
        if not self.session.is_active:
            self.console.print("[yellow]No active session to finish.[/yellow]")
            click.pause("Press any key to continue...")
            return
        
        # READ CONTENTS TO INJECT
        session_content = self.session.content
        
        context_path = os.path.join(self.project_root_abs, "AI_CONTEXT.md")
        context_content = ""
        if os.path.exists(context_path):
            context_content = read_file_contents(context_path)
        else:
            context_content = "(File does not exist yet)"

        metadata = self.session.get_metadata()
        start_commit = metadata.get("start_commit") if metadata else "NO_GIT"
        
        prompt_text = (
            "# Task Completion & Harvest\n\n"
            "## Session Data (AI_SESSION.md)\n"
            f"```markdown\n{session_content}\n```\n\n"
            "## Project Context (AI_CONTEXT.md)\n"
            f"```markdown\n{context_content}\n```\n\n"
            "# Instructions\n"
            "The task is complete. We need to consolidate knowledge.\n"
            "1. Review the Session Data for architectural decisions, constraints, or new patterns.\n"
            "2. Generate a Unified Diff patch to update `AI_CONTEXT.md` (or create it if missing) with these learnings.\n"
            "3. DO NOT patch AI_SESSION.md; it will be deleted locally."
        )
        self._run_gardener_cycle(prompt_text, "Finish Task / Harvest")

        # Post-patch cleanup: Check if file still exists (user might have deleted it via patch, but unlikely)
        if self.session.is_active:
            if click.confirm("\n🗑️  Delete `AI_SESSION.md` and finish session?", default=True):
                clear_cache()
                
                should_squash = False
                if start_commit and start_commit != "NO_GIT":
                    self.console.print(f"\n[bold]Session started at commit: {start_commit[:7]}[/bold]")
                    should_squash = click.confirm("Squash session commits (soft reset) to this point?", default=True)

                if self.session.finish(squash=should_squash, console_printer=self.console.print):
                    self.console.print("[green]Deleted AI_SESSION.md[/green]")
                    if should_squash:
                         self.console.print("[green]Commits squashed. Changes are staged.[/green]")
                         self.console.print("Run `git commit` to finalize the feature.")

    def _run_gardener_cycle(self, prompt_text: str, title: str):
        """Helper to copy prompt, pause, and run patcher."""
        self.console.clear()
        self.console.print(
            Panel(
                f"[bold]{prompt_text}[/bold]",
                title=f"🌱 Gardener: {title}",
                border_style="green",
            )
        )
        
        try:
            pyperclip.copy(prompt_text)
            self.console.print("\n[green]📋 Prompt copied to clipboard![/green]")
        except pyperclip.PyperclipException:
            self.console.print("\n[yellow]Could not copy to clipboard. Please copy the text above manually.[/yellow]")

        self.console.print("\n1. Paste this into your LLM.")
        self.console.print("2. Copy the LLM's Markdown response (Diffs/Code blocks).")
        self.console.print("3. Press [bold]Enter[/bold] here to paste and apply patches.")
        
        click.pause()
        self._handle_apply_patches()
        click.pause("Press any key to return to file selector...")

    def _toggle_selection(self, node: FileNode, snippet_mode: bool = False):
        """Toggle selection of a file or directory"""
        if node.is_dir:
            # For directories, toggle all children
            self._toggle_directory(node)
        else:
            abs_path = os.path.abspath(node.path)
            
            # Selection logic hook for large files
            if self.manager.get_state(abs_path) == FileState.UNSELECTED and not snippet_mode:
                if node.size > 102400 and not self._confirm_large_file(node):
                    snippet_mode = True
            
            self.manager.toggle(abs_path, is_snippet=snippet_mode)

    def _toggle_directory(self, node: FileNode):
        """Toggle all files in a directory, now fully recursive."""
        if not node.is_dir:
            return

        # Ensure children are loaded
        if not node.children:
            self._deep_scan_directory_and_calc_size(node.path, node)

        # Collect all files recursively
        all_files = []

        def collect_files(n: FileNode):
            if n.is_dir:
                # CRITICAL FIX: Ensure sub-directory children are loaded before recursing
                if not n.children:
                    self._deep_scan_directory_and_calc_size(n.path, n)
                for child in n.children:
                    collect_files(child)
            else:
                all_files.append(n)

        collect_files(node)

        # Check if any are unselected
        any_unselected = any(
            self.manager.get_state(f.path) == FileState.UNSELECTED for f in all_files
        )

        if any_unselected:
            # Select all unselected files
            for file_node in all_files:
                if self.manager.get_state(file_node.path) == FileState.UNSELECTED:
                    self.manager.set_state(file_node.path, FileState.DELTA)
        else:
            # Unselect all files
            for file_node in all_files:
                self.manager.set_state(file_node.path, FileState.UNSELECTED)

    def _propose_and_apply_last_selection(self):
        """Loads paths from cache, shows a confirmation dialog, and applies the selection if confirmed."""
        cached_paths = load_selection_from_cache()

        if not cached_paths:
            self.console.print(
                Panel(
                    "[yellow]No cached selection found to reuse.[/yellow]",
                    title="Info",
                    border_style="dim",
                )
            )
            click.pause("Press any key to continue...")
            return

        # Categorize cached paths for the preview
        files_to_add = []
        files_already_selected = []
        files_not_found = []

        for rel_path in cached_paths:
            abs_path = os.path.abspath(rel_path)
            if not os.path.isfile(abs_path):
                files_not_found.append(rel_path)
                continue

            state = self.manager.get_state(abs_path)
            if state != FileState.UNSELECTED:
                files_already_selected.append(rel_path)
            else:
                files_to_add.append(rel_path)

        # Build the rich text for the confirmation panel
        preview_text = Text()
        if files_to_add:
            preview_text.append("The following files will be ADDED:\n", style="bold")
            for path in sorted(files_to_add):
                preview_text.append("  ")
                preview_text.append("+", style="cyan")
                preview_text.append(f" {path}\n")

        if files_already_selected:
            preview_text.append("\nAlready selected (no change):\n", style="bold dim")
            for path in sorted(files_already_selected):
                preview_text.append(f"  ✓ {path}\n")

        if files_not_found:
            preview_text.append(
                "\nNot found on disk (will be skipped):\n", style="bold dim"
            )
            for path in sorted(files_not_found):
                preview_text.append("  ")
                preview_text.append("-", style="red")
                preview_text.append(f" {path}\n")

        # Display the confirmation panel and prompt
        self.console.clear()
        self.console.print(
            Panel(
                preview_text,
                title="[bold cyan]Reuse Last Selection?",
                border_style="cyan",
                padding=(1, 2),
            )
        )

        if not files_to_add:
            self.console.print(
                "\n[yellow]No new files to add from the last selection.[/yellow]"
            )
            click.pause("Press any key to continue...")
            return

        # Use click.confirm for a simple and effective y/n prompt
        if not click.confirm(
            f"\nAdd {len(files_to_add)} file(s) to your current selection?",
            default=True,
        ):
            return

        # If confirmed, apply the changes
        for rel_path in files_to_add:
            abs_path = os.path.abspath(rel_path)
            if os.path.isfile(abs_path) and self.manager.get_state(abs_path) == FileState.UNSELECTED:
                self.manager.set_state(abs_path, FileState.DELTA)
                self._ensure_path_visible(abs_path)

    def _ensure_path_visible(self, file_path: str):
        """Ensure a file path is visible in the tree by expanding parent directories"""
        abs_file_path = os.path.abspath(file_path)

        # Build the path from root to the file
        path_components = []
        current = abs_file_path

        while current != os.path.abspath(self.project_root_abs) and current != "/":
            path_components.append(current)
            parent = os.path.dirname(current)
            if parent == current:  # Reached root
                break
            current = parent

        # Reverse to go from root to file
        path_components.reverse()

        # Find and expand each directory in the path
        for component_path in path_components[:-1]:  # All except the file itself
            # Search through all nodes to find this path
            found = False
            for node in self._get_all_nodes(self.root):
                if os.path.abspath(node.path) == component_path and node.is_dir:
                    if not node.expanded:
                        node.expanded = True
                    # Ensure children are loaded
                    if not node.children:
                        self._deep_scan_directory_and_calc_size(node.path, node)
                    found = True
                    break

            if not found:
                # This shouldn't happen if the tree is properly built
                self.console.print(
                    f"[yellow]Warning: Could not find directory {component_path} in tree[/yellow]"
                )

    def _get_all_nodes(self, node: FileNode) -> List[FileNode]:
        """Get all nodes in the tree recursively"""
        nodes = [node]
        for child in node.children:
            nodes.extend(self._get_all_nodes(child))
        return nodes

    def _confirm_large_file(self, node: FileNode) -> bool:
        """Ask user about large file handling"""
        size_str = get_human_readable_size(node.size)
        return click.confirm(
            f"{node.name} is large ({size_str}). Include full content?", default=False
        )

    def _show_dependencies(self, node: FileNode):
        """Show and optionally add dependencies for a file"""
        if node.is_dir:
            return

        self.console.print(f"\nAnalyzing dependencies for {node.relative_path}...")

        # Create a temporary files list for the dependency analyzer
        files_list = self.manager.get_selected_files()

        # Use imported function from ops
        new_deps, _ = propose_and_add_dependencies(
            node.path, self.project_root_abs, files_list, self.manager.char_count
        )

    def _preselect_files(self, files_to_preselect: List[str]):
        """Pre-selects a list of files passed from the command line."""
        if not files_to_preselect:
            return

        added_count = 0
        for file_path in files_to_preselect:
            abs_path = os.path.abspath(file_path)
            if self.manager.get_state(abs_path) != FileState.UNSELECTED:
                continue

            # This check is simpler than a full tree walk and sufficient here
            if os.path.isfile(abs_path) and not is_binary(abs_path):
                # Files coming from CLI/Cache are 'BASE' (already known to LLM/User)
                self.manager.set_state(abs_path, FileState.BASE)
                added_count += 1
                self._ensure_path_visible(abs_path)

    def _init_key_bindings(self):
        """Initializes the keyboard command dispatch table."""
        self.key_map = {}
        
        # Helper to register multiple keys for one action
        def bind(keys: List[str], action):
            for key in keys:
                self.key_map[key] = action

        # Navigation
        bind(["\x1b[A", "\xe0H", "k"], self._nav_up)
        bind(["\x1b[B", "\xe0P", "j"], self._nav_down)
        bind(["\x1b[5~"], self._nav_page_up)
        bind(["\x1b[6~"], self._nav_page_down)
        bind(["\x1b[H"], self._nav_home)
        bind(["\x1b[F", "G"], self._nav_end)
        bind(["\x1b[C", "l", "\r", "\xe0M"], self._nav_expand)
        bind(["\x1b[D", "h", "\xe0K"], self._nav_collapse)

        # Selection
        bind([" "], self._action_toggle)
        bind(["s"], self._action_snippet)
        bind(["a"], self._action_add_all)

        # Actions
        bind(["r"], self._propose_and_apply_last_selection)
        bind(["g"], self._action_grep)
        bind(["p"], self._action_patch)
        bind(["e"], self._action_extend)
        bind(["d"], self._action_deps)
        bind(["x"], self._action_fix)
        bind(["n"], self._action_session_start)
        bind(["c"], self._action_clear_base)
        bind(["u"], self._handle_session_update)
        bind(["f"], self._handle_task_completion)
        
        # Meta
        bind(["q"], self._action_quit)
        bind(["\x03"], self._action_interrupt) # Ctrl+C

    def _get_current_node(self) -> Optional[FileNode]:
        if self.visible_nodes and 0 <= self.current_index < len(self.visible_nodes):
            return self.visible_nodes[self.current_index]
        return None

    # --- Navigation Actions ---
    
    def _nav_up(self):
        self.current_index = max(0, self.current_index - 1)

    def _nav_down(self):
        self.current_index = min(len(self.visible_nodes) - 1, self.current_index + 1)

    def _nav_page_up(self):
        _, term_height = shutil.get_terminal_size()
        page_size = max(1, term_height - 15)
        self.current_index = max(0, self.current_index - page_size)

    def _nav_page_down(self):
        _, term_height = shutil.get_terminal_size()
        page_size = max(1, term_height - 15)
        self.current_index = min(len(self.visible_nodes) - 1, self.current_index + page_size)

    def _nav_home(self):
        self.current_index = 0

    def _nav_end(self):
        self.current_index = len(self.visible_nodes) - 1

    def _nav_expand(self):
        node = self._get_current_node()
        if node and node.is_dir:
            node.expanded = True

    def _nav_collapse(self):
        node = self._get_current_node()
        if not node:
            return
        if node.is_dir and node.expanded:
            node.expanded = False
        elif node.parent:
            # Jump to parent
            parent_idx = next(
                (i for i, n in enumerate(self.visible_nodes) if n == node.parent),
                None
            )
            if parent_idx is not None:
                self.current_index = parent_idx

    # --- Interaction Actions ---

    def _action_toggle(self):
        node = self._get_current_node()
        if node:
            self._toggle_selection(node)

    def _action_snippet(self):
        node = self._get_current_node()
        if node and not node.is_dir:
            self._toggle_selection(node, snippet_mode=True)

    def _action_add_all(self):
        node = self._get_current_node()
        if not node:
            return
        target_node = node if node.is_dir else node.parent
        if target_node:
            self._toggle_directory(target_node)

    def _action_grep(self):
        node = self._get_current_node()
        if node:
            self.console.print()
            self._handle_grep(node)

    def _action_patch(self):
        self._handle_apply_patches()
        click.pause("Press any key to return to the file selector...")

    def _action_fix(self):
        self._handle_fix()
        click.pause("Press any key to return to the file selector...")

    def _action_deps(self):
        node = self._get_current_node()
        if node:
            self.console.print()
            self._show_dependencies(node)
            click.pause("Press any key to continue...")

    def _action_clear_base(self):
        self.manager.clear_base()

    def _action_session_start(self):
        # Pre-check for gitignore to be interactive
        from kopipasta.git_utils import check_session_gitignore_status, add_to_gitignore
        
        if not check_session_gitignore_status(self.project_root_abs):
            self.console.print(f"\n[bold yellow]⚠ {SESSION_FILENAME} is NOT ignored by git.[/bold yellow]")
            if click.confirm(f"Add {SESSION_FILENAME} to .gitignore now?", default=True):
                add_to_gitignore(self.project_root_abs, SESSION_FILENAME)
            else:
                return

        if self.session.start(console_printer=self.console.print):
            if self.session.is_active:
                self._ensure_path_visible(self.session.path)
        click.pause("Press any key to continue...")

    def _action_quit(self):
        # Before quitting, promote all Delta to Base
        # to mark them as "processed" for the clipboard output turn.
        self.manager.promote_all_to_base()
        self.quit_selection = True

    def _action_interrupt(self):
        raise KeyboardInterrupt()

    def run(
        self, initial_paths: List[str], files_to_preselect: Optional[List[str]] = None
    ) -> Tuple[List[FileTuple], int]:
        """Run the interactive tree selector"""
        self.root = self.build_tree(initial_paths)

        if files_to_preselect:
            self._preselect_files(files_to_preselect)

        # Don't use Live mode, instead manually control the display
        while not self.quit_selection:
            # Clear and redraw
            self.console.clear()

            # Draw tree
            tree = self._build_display_tree()
            self.console.print(tree)

            # Draw help
            self.console.print(self._show_help())

            # Draw status bar
            self.console.print(self._get_status_bar())

            try:
                # Get keyboard input
                key = click.getchar()

                if key in self.key_map:
                    self.key_map[key]()

            except Exception as e:
                self.console.print(f"[red]Error: {e}[/red]")
                click.pause("Press any key to continue...")

        # Clear screen one more time
        self.console.clear()

        # Convert selections to FileTuple format
        files_to_include = self.manager.get_selected_files()
        return files_to_include, self.manager.char_count
