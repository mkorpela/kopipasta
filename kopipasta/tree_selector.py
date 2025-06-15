import os
from typing import Dict, List, Optional, Tuple
from rich.console import Console
from rich.tree import Tree
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.layout import Layout
import click

from kopipasta.file import FileTuple, is_binary, is_ignored, get_human_readable_size
from kopipasta.prompt import get_file_snippet, get_language_for_file


class FileNode:
    """Represents a file or directory in the tree"""
    def __init__(self, path: str, is_dir: bool, parent: Optional['FileNode'] = None):
        self.path = os.path.abspath(path)  # Always store absolute paths
        self.is_dir = is_dir
        self.parent = parent
        self.children: List['FileNode'] = []
        self.expanded = False
        self.selected = False
        self.selected_as_snippet = False
        self.size = 0 if is_dir else os.path.getsize(self.path)
        self.is_root = path == "."  # Mark if this is the root node
        
    @property
    def name(self):
        if self.is_root:
            return "."  # Show root as "." instead of directory name
        return os.path.basename(self.path) or self.path
        
    @property
    def relative_path(self):
        if self.is_root:
            return "."
        return os.path.relpath(self.path)


class TreeSelector:
    """Interactive file tree selector using Rich"""
    
    def __init__(self, ignore_patterns: List[str], project_root_abs: str):
        self.console = Console()
        self.ignore_patterns = ignore_patterns
        self.project_root_abs = project_root_abs
        self.selected_files: Dict[str, Tuple[bool, Optional[List[str]]]] = {}  # path -> (is_snippet, chunks)
        self.current_index = 0
        self.nodes: List[FileNode] = []
        self.visible_nodes: List[FileNode] = []
        self.char_count = 0
        self.quit_selection = False
        
    def build_tree(self, paths: List[str]) -> FileNode:
        """Build tree structure from given paths"""
        # Use current directory as root
        root = FileNode(".", True)
        root.expanded = True  # Always expand root
        
        # Process each input path
        for path in paths:
            abs_path = os.path.abspath(path)
            
            if os.path.isfile(abs_path):
                # Single file - add to root
                if not is_ignored(abs_path, self.ignore_patterns) and not is_binary(abs_path):
                    node = FileNode(abs_path, False, root)
                    root.children.append(node)
            elif os.path.isdir(abs_path):
                # If the directory is the current directory, scan its contents directly
                if abs_path == os.path.abspath("."):
                    self._scan_directory(abs_path, root)
                else:
                    # Otherwise add the directory as a child
                    dir_node = FileNode(abs_path, True, root)
                    root.children.append(dir_node)
                    # Auto-expand if it's the only child
                    if len(paths) == 1:
                        dir_node.expanded = True
                        self._scan_directory(abs_path, dir_node)
                
        return root
    
    def _scan_directory(self, dir_path: str, parent_node: FileNode):
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
            if is_ignored(item_path, self.ignore_patterns):
                continue
                
            if os.path.isdir(item_path):
                dirs.append(item)
            elif os.path.isfile(item_path) and not is_binary(item_path):
                files.append(item)
        
        # Add directories first
        for dir_name in sorted(dirs):
            dir_path_full = os.path.join(abs_dir_path, dir_name)
            # Check if this node already exists as a child
            existing = next((child for child in parent_node.children 
                           if os.path.abspath(child.path) == os.path.abspath(dir_path_full)), None)
            if not existing:
                dir_node = FileNode(dir_path_full, True, parent_node)
                parent_node.children.append(dir_node)
            
        # Then add files
        for file_name in sorted(files):
            file_path = os.path.join(abs_dir_path, file_name)
            # Check if this node already exists as a child
            existing = next((child for child in parent_node.children 
                           if os.path.abspath(child.path) == os.path.abspath(file_path)), None)
            if not existing:
                file_node = FileNode(file_path, False, parent_node)
                parent_node.children.append(file_node)
    
    def _flatten_tree(self, node: FileNode, level: int = 0) -> List[Tuple[FileNode, int]]:
        """Flatten tree into a list of (node, level) tuples for display"""
        result = []
        
        # Special handling for root - show its children at top level
        if node.is_root:
            # Don't include the root node itself in the display
            for child in node.children:
                result.extend(self._flatten_tree(child, 0))  # Start children at level 0
        else:
            # Include this node
            result.append((node, level))
            
            if node.is_dir and node.expanded:
                # Load children on demand if not loaded
                if not node.children:
                    self._scan_directory(node.path, node)
                    
                for child in node.children:
                    result.extend(self._flatten_tree(child, level + 1))
                
        return result
    
    def _build_display_tree(self) -> Tree:
        """Build Rich tree for display"""
        tree = Tree("📁 Project Files", guide_style="dim")
        
        # Flatten tree and rebuild visible nodes list
        flat_tree = self._flatten_tree(self.root)
        self.visible_nodes = [node for node, _ in flat_tree]
        
        # Build tree structure - we'll map absolute paths to tree nodes
        node_map = {}
        
        for i, (node, level) in enumerate(flat_tree):
            # Determine style and icon
            is_current = i == self.current_index
            style = "bold cyan" if is_current else ""
            
            if node.is_dir:
                icon = "📂" if node.expanded else "📁"
                size_str = f" ({len(node.children)} items)" if node.children else ""
            else:
                icon = "📄"
                size_str = f" ({get_human_readable_size(node.size)})"
                
            # Selection indicator
            abs_path = os.path.abspath(node.path)
            if abs_path in self.selected_files:
                is_snippet = self.selected_files[abs_path][0]
                if is_snippet:
                    selection = "◐"  # Half-selected (snippet)
                else:
                    selection = "●"  # Fully selected
                style = "green " + style
            else:
                selection = "○"
                
            # Build label
            label = Text()
            label.append(f"{selection} ", style="dim")
            label.append(f"{icon} {node.name}{size_str}", style=style)
            
            # Add to tree at correct position
            # For root-level items, add directly to tree
            if node.parent and node.parent.path == os.path.abspath("."):
                tree_node = tree.add(label)
                node_map[abs_path] = tree_node
            else:
                # Find parent node in map
                parent_abs_path = os.path.abspath(node.parent.path) if node.parent else None
                if parent_abs_path and parent_abs_path in node_map:
                    parent_tree = node_map[parent_abs_path]
                    tree_node = parent_tree.add(label)
                    node_map[abs_path] = tree_node
                else:
                    # Fallback - add to root
                    tree_node = tree.add(label)
                    node_map[abs_path] = tree_node
            
        return tree
    
    def _show_help(self) -> Panel:
        """Create help panel"""
        help_text = """[bold]Navigation:[/bold]
↑/k: Move up     ↓/j: Move down     →/l/Enter: Expand dir     ←/h: Collapse dir

[bold]Selection:[/bold]  
Space: Toggle file/dir     a: Add all in dir     s: Snippet mode

[bold]Actions:[/bold]
g: Grep in directory     d: Show dependencies     q: Quit selection

[bold]Status:[/bold]
Selected: [green]● Full[/green]  [yellow]◐ Snippet[/yellow]  ○ Not selected"""
        
        return Panel(help_text, title="Keyboard Shortcuts", border_style="dim", expand=False)
    
    def _get_status_bar(self) -> str:
        """Create status bar with selection info"""
        # Count selections
        full_count = sum(1 for _, (is_snippet, _) in self.selected_files.items() if not is_snippet)
        snippet_count = sum(1 for _, (is_snippet, _) in self.selected_files.items() if is_snippet)
        
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
        
        # Import here to avoid circular dependency
        from kopipasta.main import grep_files_in_directory, select_from_grep_results
        
        grep_results = grep_files_in_directory(pattern, node.path, self.ignore_patterns)
        if not grep_results:
            self.console.print(f"[yellow]No matches found for '{pattern}'[/yellow]")
            return
            
        # Show results and let user select
        selected_files, new_char_count = select_from_grep_results(grep_results, self.char_count)
        
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
            self.console.print(f"\n[green]Added {added_count} files from grep results[/green]")
        else:
            self.console.print(f"\n[yellow]All selected files were already in selection[/yellow]")
    
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
                if is_snippet:
                    self.char_count -= len(get_file_snippet(node.path))
                else:
                    self.char_count -= node.size
            else:
                # Select
                if snippet_mode or (node.size > 102400 and not self._confirm_large_file(node)):
                    # Use snippet
                    self.selected_files[abs_path] = (True, None)
                    self.char_count += len(get_file_snippet(node.path))
                else:
                    # Use full file
                    self.selected_files[abs_path] = (False, None)
                    self.char_count += node.size
    
    def _toggle_directory(self, node: FileNode):
        """Toggle all files in a directory"""
        if not node.is_dir:
            return
            
        # Ensure children are loaded
        if not node.children:
            self._scan_directory(node.path, node)
            
        # Collect all files recursively
        all_files = []
        
        def collect_files(n: FileNode):
            if n.is_dir:
                for child in n.children:
                    collect_files(child)
            else:
                all_files.append(n)
                
        collect_files(node)
        
        # Check if any are unselected
        any_unselected = any(os.path.abspath(f.path) not in self.selected_files for f in all_files)
        
        if any_unselected:
            # Select all unselected
            for file_node in all_files:
                if file_node.path not in self.selected_files:
                    self._toggle_selection(file_node)
        else:
            # Unselect all
            for file_node in all_files:
                if file_node.path in self.selected_files:
                    self._toggle_selection(file_node)
    
    def _ensure_path_visible(self, file_path: str):
        """Ensure a file path is visible in the tree by expanding parent directories"""
        abs_file_path = os.path.abspath(file_path)
        
        # Build the path from root to the file
        path_components = []
        current = abs_file_path
        
        while current != os.path.abspath(self.project_root_abs) and current != '/':
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
                        self._scan_directory(node.path, node)
                    found = True
                    break
            
            if not found:
                # This shouldn't happen if the tree is properly built
                self.console.print(f"[yellow]Warning: Could not find directory {component_path} in tree[/yellow]")
    
    def _get_all_nodes(self, node: FileNode) -> List[FileNode]:
        """Get all nodes in the tree recursively"""
        nodes = [node]
        for child in node.children:
            nodes.extend(self._get_all_nodes(child))
        return nodes

    def _confirm_large_file(self, node: FileNode) -> bool:
        """Ask user about large file handling"""
        size_str = get_human_readable_size(node.size)
        return click.confirm(f"{node.name} is large ({size_str}). Include full content?", default=False)
    
    def _show_dependencies(self, node: FileNode):
        """Show and optionally add dependencies for a file"""
        if node.is_dir:
            return
            
        self.console.print(f"\nAnalyzing dependencies for {node.relative_path}...")
        
        # Import here to avoid circular dependency  
        from kopipasta.main import _propose_and_add_dependencies
        
        # Create a temporary files list for the dependency analyzer
        files_list = [(path, is_snippet, chunks, get_language_for_file(path)) 
                      for path, (is_snippet, chunks) in self.selected_files.items()]
        
        new_deps, deps_char_count = _propose_and_add_dependencies(
            node.path, self.project_root_abs, files_list, self.char_count
        )
        
        # Add new dependencies to our selection
        for dep_path, is_snippet, chunks, _ in new_deps:
            self.selected_files[dep_path] = (is_snippet, chunks)
            
        self.char_count += deps_char_count
    
    def run(self, initial_paths: List[str]) -> Tuple[List[FileTuple], int]:
        """Run the interactive tree selector"""
        self.root = self.build_tree(initial_paths)
        
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
                if key in ['\x1b[A', 'k']:  # Up arrow or k
                    self.current_index = max(0, self.current_index - 1)
                elif key in ['\x1b[B', 'j']:  # Down arrow or j  
                    self.current_index = min(len(self.visible_nodes) - 1, self.current_index + 1)
                elif key in ['\x1b[C', 'l', '\r']:  # Right arrow, l, or Enter
                    if current_node.is_dir:
                        current_node.expanded = True
                elif key in ['\x1b[D', 'h']:  # Left arrow or h
                    if current_node.is_dir and current_node.expanded:
                        current_node.expanded = False
                    elif current_node.parent:
                        # Jump to parent
                        parent_idx = next((i for i, n in enumerate(self.visible_nodes) 
                                         if n == current_node.parent), None)
                        if parent_idx is not None:
                            self.current_index = parent_idx
                            
                # Handle selection
                elif key == ' ':  # Space - toggle selection
                    self._toggle_selection(current_node)
                elif key == 's':  # Snippet mode
                    if not current_node.is_dir:
                        self._toggle_selection(current_node, snippet_mode=True)
                elif key == 'a':  # Add all in directory
                    if current_node.is_dir:
                        self._toggle_directory(current_node)
                        
                # Handle actions
                elif key == 'g':  # Grep
                    self.console.print()  # Add some space
                    self._handle_grep(current_node)
                    click.pause("Press any key to continue...")
                elif key == 'd':  # Dependencies
                    self.console.print()  # Add some space
                    self._show_dependencies(current_node)
                    click.pause("Press any key to continue...")
                elif key == 'q':  # Quit
                    self.quit_selection = True
                elif key == '\x03':  # Ctrl+C
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
            files_to_include.append((rel_path, is_snippet, chunks, get_language_for_file(abs_path)))
            
        return files_to_include, self.char_count