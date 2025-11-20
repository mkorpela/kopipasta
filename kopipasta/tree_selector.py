import os
import shutil
from typing import Dict, List, Optional, Tuple
from prompt_toolkit import prompt as prompt_toolkit_prompt
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.tree import Tree
from rich.panel import Panel
from rich.text import Text
import click

from kopipasta.patcher import apply_patches, parse_llm_output
from kopipasta.file import FileTuple, is_binary, is_ignored, get_human_readable_size
from kopipasta.prompt import get_file_snippet, get_language_for_file
from kopipasta.cache import load_selection_from_cache
from kopipasta.ops import propose_and_add_dependencies, grep_files_in_directory, select_from_grep_results


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
        return os.path.basename(self.path) or self.path

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
        self.selected_files: Dict[
            str, Tuple[bool, Optional[List[str]]]
        ] = {}  # path -> (is_snippet, chunks)
        self.current_index = 0
        self.nodes: List[FileNode] = []
        self.visible_nodes: List[FileNode] = []
        self.char_count = 0
        self.quit_selection = False
        self.viewport_offset = 0  # First visible item index
        self._metrics_cache: Dict[str, Tuple[int, int]] = {}

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
                if child.path in self.selected_files:
                    is_snippet, _ = self.selected_files[child.path]
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

        for path in paths:
            abs_path = os.path.abspath(path)
            node = None
            if os.path.isfile(abs_path):
                if not is_ignored(
                    abs_path, self.ignore_patterns, self.project_root_abs
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
            if is_ignored(item_path, self.ignore_patterns, self.project_root_abs):
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

        # If it's the special root node, don't display it. Display its children at the top level.
        if node.is_scan_root:
            for child in node.children:
                result.extend(self._flatten_tree(child, 0))
        else:
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
        tree_title = "📁 Project Files"
        if self.viewport_offset > 0:
            tree_title += f" ↑ ({self.viewport_offset} more)"

        tree = Tree(tree_title)

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
                if abs_path in self.selected_files:
                    is_snippet, _ = self.selected_files[abs_path]
                    selection = "◐" if is_snippet else "●"
                    style = "green " + style
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
        help_text = """[bold]Navigation:[/bold]  ↑/k: Up  ↓/j: Down  →/l/Enter: Expand  ←/h: Collapse
[bold]Selection:[/bold]  Space: Toggle selection  a: Add all in dir     s: Snippet mode
[bold]Actions:[/bold]    r: Reuse last selection  g: Grep in directory   p: Apply patch
q: Quit and finalize"""

        return Panel(
            help_text, title="Keyboard Controls", border_style="dim", expand=False
        )

    def _get_status_bar(self) -> str:
        """Create status bar with selection info"""
        # Count selections
        full_count = sum(
            1 for _, (is_snippet, _) in self.selected_files.items() if not is_snippet
        )
        snippet_count = sum(
            1 for _, (is_snippet, _) in self.selected_files.items() if is_snippet
        )

        # Current item info
        if self.visible_nodes and 0 <= self.current_index < len(self.visible_nodes):
            current = self.visible_nodes[self.current_index]
            current_info = f"[dim]Current:[/dim] {current.relative_path}"
        else:
            current_info = "No selection"

        selection_info = f"[dim]Selected:[/dim] {full_count} full, {snippet_count} snippets | ~{self.char_count:,} chars (~{self.char_count//4:,} tokens)"

        return f"\n{current_info} | {selection_info}\n"

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
        selected_files, new_char_count = select_from_grep_results(
            grep_results, self.char_count
        )

        # Add selected files
        added_count = 0
        for file_tuple in selected_files:
            file_path, is_snippet, chunks, _ = file_tuple
            abs_path = os.path.abspath(file_path)

            # Check if already selected
            if abs_path not in self.selected_files:
                self.selected_files[abs_path] = (is_snippet, chunks)
                added_count += 1
                # Ensure the file is visible in the tree
                self._ensure_path_visible(abs_path)

        self.char_count = new_char_count

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
            if not content.strip():
                self.console.print("\n[yellow]No content pasted. Aborting.[/yellow]")
                return

            patches = parse_llm_output(content)
            apply_patches(patches)
            self.console.print("\n[bold]Review the changes above with `git diff` before committing.[/bold]")

        except KeyboardInterrupt:
            self.console.print("\n[red]Patch application cancelled.[/red]")


    def _toggle_selection(self, node: FileNode, snippet_mode: bool = False):
        """Toggle selection of a file or directory"""
        if node.is_dir:
            # For directories, toggle all children
            self._toggle_directory(node)
        else:
            abs_path = os.path.abspath(node.path)
            # For files, toggle individual selection
            if abs_path in self.selected_files:
                # Unselect
                is_snippet, _ = self.selected_files[abs_path]
                del self.selected_files[abs_path]
                self.char_count -= (
                    len(get_file_snippet(node.path)) if is_snippet else node.size
                )
            else:
                # Select
                if snippet_mode or (
                    node.size > 102400 and not self._confirm_large_file(node)
                ):
                    # Use snippet
                    self.selected_files[abs_path] = (True, None)
                    self.char_count += len(get_file_snippet(node.path))
                else:
                    # Use full file
                    self.selected_files[abs_path] = (False, None)
                    self.char_count += node.size

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
            os.path.abspath(f.path) not in self.selected_files for f in all_files
        )

        if any_unselected:
            # Select all unselected files
            for file_node in all_files:
                abs_path = os.path.abspath(file_node.path)
                if abs_path not in self.selected_files:
                    self.selected_files[abs_path] = (False, None)
                    self.char_count += file_node.size
        else:
            # Unselect all files
            for file_node in all_files:
                abs_path = os.path.abspath(file_node.path)
                if abs_path in self.selected_files:
                    is_snippet, _ = self.selected_files[abs_path]
                    del self.selected_files[abs_path]
                    if is_snippet:
                        self.char_count -= len(get_file_snippet(file_node.path))
                    else:
                        self.char_count -= file_node.size

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

            if abs_path in self.selected_files:
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
            if os.path.isfile(abs_path) and abs_path not in self.selected_files:
                file_size = os.path.getsize(abs_path)
                self.selected_files[abs_path] = (False, None)
                self.char_count += file_size
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
        files_list = [
            (path, is_snippet, chunks, get_language_for_file(path))
            for path, (is_snippet, chunks) in self.selected_files.items()
        ]

        # Use imported function from ops
        new_deps, deps_char_count = propose_and_add_dependencies(
            node.path, self.project_root_abs, files_list, self.char_count
        )

        # Add new dependencies to our selection
        for dep_path, is_snippet, chunks, _ in new_deps:
            self.selected_files[dep_path] = (is_snippet, chunks)

        self.char_count += deps_char_count

    def _preselect_files(self, files_to_preselect: List[str]):
        """Pre-selects a list of files passed from the command line."""
        if not files_to_preselect:
            return

        added_count = 0
        for file_path in files_to_preselect:
            abs_path = os.path.abspath(file_path)
            if abs_path in self.selected_files:
                continue

            # This check is simpler than a full tree walk and sufficient here
            if os.path.isfile(abs_path) and not is_binary(abs_path):
                file_size = os.path.getsize(abs_path)
                self.selected_files[abs_path] = (
                    False,
                    None,
                )  # (is_snippet=False, chunks=None)
                self.char_count += file_size
                added_count += 1
                self._ensure_path_visible(abs_path)

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

                if not self.visible_nodes:
                    continue

                current_node = self.visible_nodes[self.current_index]

                # Handle navigation
                if key in ["\x1b[A", "\xe0H", "k"]:  # Up arrow or k
                    self.current_index = max(0, self.current_index - 1)
                elif key in ["\x1b[B", "\xe0P", "j"]:  # Down arrow or j
                    self.current_index = min(
                        len(self.visible_nodes) - 1, self.current_index + 1
                    )
                elif key == "\x1b[5~":  # Page Up
                    term_width, term_height = shutil.get_terminal_size()
                    page_size = max(1, term_height - 15)
                    self.current_index = max(0, self.current_index - page_size)
                elif key == "\x1b[6~":  # Page Down
                    term_width, term_height = shutil.get_terminal_size()
                    page_size = max(1, term_height - 15)
                    self.current_index = min(
                        len(self.visible_nodes) - 1, self.current_index + page_size
                    )
                elif key == "\x1b[H":  # Home - go to top
                    self.current_index = 0
                elif key == "\x1b[F":  # End - go to bottom
                    self.current_index = len(self.visible_nodes) - 1
                elif key == "G":  # Shift+G - go to bottom (vim style)
                    self.current_index = len(self.visible_nodes) - 1
                elif key in ["\x1b[C", "l", "\r", "\xe0M"]:  # Right arrow, l, or Enter
                    if current_node.is_dir:
                        current_node.expanded = True
                elif key in ["\x1b[D", "h", "\xe0K"]:  # Left arrow or h
                    if current_node.is_dir and current_node.expanded:
                        current_node.expanded = False
                    elif current_node.parent:
                        # Jump to parent
                        parent_idx = next(
                            (
                                i
                                for i, n in enumerate(self.visible_nodes)
                                if n == current_node.parent
                            ),
                            None,
                        )
                        if parent_idx is not None:
                            self.current_index = parent_idx

                # Handle selection
                elif key == " ":  # Space - toggle selection
                    self._toggle_selection(current_node)
                elif key == "s":  # Snippet mode
                    if not current_node.is_dir:
                        self._toggle_selection(current_node, snippet_mode=True)
                elif key == "a":  # Add all in directory
                    if current_node.is_dir:
                        self._toggle_directory(current_node)

                # Handle actions
                elif key == "r":  # Reuse last selection
                    self._propose_and_apply_last_selection()
                elif key == "g":  # Grep
                    self.console.print()  # Add some space
                    self._handle_grep(current_node)
                elif key == "p": # Apply Patches
                    self._handle_apply_patches()
                    click.pause("Press any key to return to the file selector...")
                elif key == "d":  # Dependencies
                    self.console.print()  # Add some space
                    self._show_dependencies(current_node)
                    click.pause("Press any key to continue...")
                elif key == "q":  # Quit
                    self.quit_selection = True
                elif key == "\x03":  # Ctrl+C
                    raise KeyboardInterrupt()

            except Exception as e:
                self.console.print(f"[red]Error: {e}[/red]")
                click.pause("Press any key to continue...")

        # Clear screen one more time
        self.console.clear()

        # Convert selections to FileTuple format
        files_to_include = []
        for abs_path, (is_snippet, chunks) in self.selected_files.items():
            # Convert back to relative path for the output
            rel_path = os.path.relpath(abs_path)
            files_to_include.append(
                (rel_path, is_snippet, chunks, get_language_for_file(abs_path))
            )

        return files_to_include, self.char_count
