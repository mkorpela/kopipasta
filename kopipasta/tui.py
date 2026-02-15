"""
Textual-based TUI for kopipasta.
Replaces the manual Rich + click.getchar() render loop in tree_selector.py.
"""

import json
import os
import subprocess
import tempfile
from typing import Any, List, Optional, Tuple

import pyperclip
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Paste
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Static,
    Tree as TextualTree,
)
from textual.widgets.tree import TreeNode

from kopipasta.cache import (
    clear_cache,
    load_task_from_cache,
)
from kopipasta.claude import configure_claude_desktop
from kopipasta.config import read_fix_command
from kopipasta.file import (
    FileTuple,
    is_binary,
    is_ignored,
    get_human_readable_size,
    read_file_contents,
)
from kopipasta.git_utils import add_to_gitignore, check_session_gitignore_status
from kopipasta.ops import estimate_tokens, sanitize_string
from kopipasta.patcher import apply_patches, parse_llm_output, find_paths_in_text
from kopipasta.prompt import (
    generate_extension_prompt,
    generate_fix_prompt,
)
from kopipasta.selection import SelectionManager, FileState
from kopipasta.session import Session, SESSION_FILENAME
from kopipasta.logger import get_logger

ALWAYS_VISIBLE_FILES = {"AI_SESSION.md", "AI_CONTEXT.md"}
RALPH_CONFIG_FILENAME = ".ralph.json"


# ---------------------------------------------------------------------------
# Data node attached to each tree entry
# ---------------------------------------------------------------------------


class NodeData:
    """Data payload attached to each Textual TreeNode."""

    def __init__(self, path: str, is_dir: bool):
        self.path = os.path.abspath(path)
        self.is_dir = is_dir
        self.size = 0 if is_dir else self._safe_size()
        self.scanned = False

    def _safe_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0


# ---------------------------------------------------------------------------
# Paste preview modal
# ---------------------------------------------------------------------------


class PasteModal(ModalScreen[str]):
    """
    Shown when the user pastes content.
    Returns "apply", "edit", or "cancel".
    """

    BINDINGS = [
        Binding("a", "respond_apply", "Apply", show=True),
        Binding("e", "respond_edit", "Edit in $EDITOR", show=True),
        Binding("escape", "respond_cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    PasteModal {
        align: center middle;
    }
    #paste-dialog {
        width: 80;
        max-height: 24;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }
    #paste-snippet {
        height: auto;
        max-height: 14;
        overflow-y: auto;
        margin: 1 0;
        color: $text-muted;
    }
    #paste-buttons {
        height: 3;
        align: center middle;
    }
    #paste-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, snippet: str, total_len: int) -> None:
        super().__init__()
        self.snippet = snippet
        self.total_len = total_len

    def compose(self) -> ComposeResult:
        with Vertical(id="paste-dialog"):
            yield Static(
                f"[bold cyan]📋 Pasted content detected[/bold cyan]  "
                f"({self.total_len:,} chars)",
                id="paste-title",
            )
            yield Static(self.snippet, id="paste-snippet")
            with Horizontal(id="paste-buttons"):
                yield Button("[A]pply", variant="success", id="btn-apply")
                yield Button("[E]dit", variant="primary", id="btn-edit")
                yield Button("[C]ancel", variant="error", id="btn-cancel")

    @on(Button.Pressed, "#btn-apply")
    def on_apply(self) -> None:
        self.dismiss("apply")

    @on(Button.Pressed, "#btn-edit")
    def on_edit(self) -> None:
        self.dismiss("edit")

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self) -> None:
        self.dismiss("cancel")

    def action_respond_apply(self) -> None:
        self.dismiss("apply")

    def action_respond_edit(self) -> None:
        self.dismiss("edit")

    def action_respond_cancel(self) -> None:
        self.dismiss("cancel")


# ---------------------------------------------------------------------------
# Confirm modal (generic y/n)
# ---------------------------------------------------------------------------


class ConfirmModal(ModalScreen[bool]):
    """Simple yes/no confirmation dialog."""

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-dialog {
        width: 60;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }
    #confirm-buttons {
        height: 3;
        align: center middle;
    }
    #confirm-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("y", "yes", "Yes", show=True),
        Binding("n", "no", "No", show=True),
        Binding("escape", "no", "No", show=False),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self.message, id="confirm-msg")
            with Horizontal(id="confirm-buttons"):
                yield Button("[Y]es", variant="success", id="btn-yes")
                yield Button("[N]o", variant="error", id="btn-no")

    @on(Button.Pressed, "#btn-yes")
    def on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-no")
    def on_no(self) -> None:
        self.dismiss(False)

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# File tree widget
# ---------------------------------------------------------------------------


class FileTreeWidget(TextualTree):
    """Project file tree backed by filesystem scanning."""

    DEFAULT_CSS = """
    FileTreeWidget {
        height: 1fr;
    }
    """

    # Prevent the default Tree widget from consuming Space
    BINDINGS = [
        Binding("space", "toggle_node", "Toggle", show=False),
    ]

    def __init__(
        self,
        project_root: str,
        ignore_patterns: List[str],
        manager: SelectionManager,
        **kwargs: Any,
    ) -> None:
        super().__init__("Project Root", **kwargs)
        self.project_root = os.path.abspath(project_root)
        self.ignore_patterns = ignore_patterns
        self.manager = manager
        self.show_root = True

    def on_mount(self) -> None:
        root_data = NodeData(self.project_root, is_dir=True)
        self.root.data = root_data
        self._populate_directory(self.root)
        self.root.expand()

    def action_toggle_node(self) -> None:
        """Override default Space behavior to do file selection instead."""
        node = self.cursor_node
        if node is not None and node.data is not None:
            self.toggle_selection(node)
            self.post_message(self.NodeSelected(node))

    class NodeSelected(TextualTree.NodeSelected):
        """Posted when selection state changes (for status bar refresh)."""

        pass

    def _populate_directory(self, tree_node: TreeNode) -> None:
        """Scan directory and add children to tree node."""
        data: NodeData = tree_node.data
        if data.scanned:
            return
        data.scanned = True

        try:
            items = sorted(os.listdir(data.path))
        except PermissionError:
            return

        dirs: List[str] = []
        files: List[str] = []

        for item in items:
            item_path = os.path.join(data.path, item)
            if item in ALWAYS_VISIBLE_FILES:
                pass
            elif is_ignored(item_path, self.ignore_patterns, self.project_root):
                continue

            if os.path.isdir(item_path):
                dirs.append(item)
            elif os.path.isfile(item_path) and not is_binary(item_path):
                files.append(item)

        for d in sorted(dirs):
            child_path = os.path.join(data.path, d)
            child_data = NodeData(child_path, is_dir=True)
            child_node = tree_node.add(d, data=child_data, allow_expand=True)
            child_node.add_leaf("…", data=None)

        for f in sorted(files):
            child_path = os.path.join(data.path, f)
            child_data = NodeData(child_path, is_dir=False)
            label = self._file_label(child_data)
            tree_node.add_leaf(label, data=child_data)

    def on_tree_node_expanded(self, event: TextualTree.NodeExpanded) -> None:
        node = event.node
        data: Optional[NodeData] = node.data
        if data is None or not data.is_dir:
            return
        if not data.scanned:
            node.remove_children()
            self._populate_directory(node)

    def _file_label(self, data: NodeData) -> Text:
        """Build the label for a file node with selection indicator."""
        state = self.manager.get_state(data.path)
        is_snip = self.manager.is_snippet(data.path)
        name = os.path.basename(data.path)
        size_str = get_human_readable_size(data.size)

        label = Text()
        if state == FileState.DELTA:
            marker = "◐" if is_snip else "●"
            label.append(f"{marker} ", style="green")
            label.append(f"📄 {name}", style="green")
        elif state == FileState.BASE:
            marker = "◐" if is_snip else "●"
            label.append(f"{marker} ", style="cyan")
            label.append(f"📄 {name}", style="cyan")
        else:
            label.append("○ ", style="dim")
            label.append(f"📄 {name}")
        label.append(f" ({size_str})", style="dim")
        return label

    def refresh_node_label(self, node: TreeNode) -> None:
        """Update label of a file node after selection change."""
        data: Optional[NodeData] = node.data
        if data is None or data.is_dir:
            return
        node.set_label(self._file_label(data))

    def toggle_selection(self, node: TreeNode, snippet: bool = False) -> None:
        """Toggle selection for a file or all files in a directory."""
        data: Optional[NodeData] = node.data
        if data is None:
            return

        if data.is_dir:
            self._toggle_directory(node)
        else:
            self.manager.toggle(data.path, is_snippet=snippet)
            self.refresh_node_label(node)

    def _toggle_directory(self, node: TreeNode) -> None:
        """Toggle all files under a directory recursively."""
        file_nodes = self._collect_file_nodes(node)
        any_unselected = any(
            self.manager.get_state(n.data.path) == FileState.UNSELECTED
            for n in file_nodes
            if n.data is not None
        )

        for fn in file_nodes:
            if fn.data is None:
                continue
            if any_unselected:
                if self.manager.get_state(fn.data.path) == FileState.UNSELECTED:
                    self.manager.set_state(fn.data.path, FileState.DELTA)
            else:
                self.manager.set_state(fn.data.path, FileState.UNSELECTED)
            self.refresh_node_label(fn)

    def _collect_file_nodes(self, node: TreeNode) -> List[TreeNode]:
        """Recursively collect all file (leaf) tree nodes under a directory."""
        results: List[TreeNode] = []
        for child in node.children:
            if child.data is None:
                continue
            if child.data.is_dir:
                if not child.data.scanned:
                    child.remove_children()
                    self._populate_directory(child)
                results.extend(self._collect_file_nodes(child))
            else:
                results.append(child)
        return results

    def refresh_all_labels(self) -> None:
        """Refresh labels on all visible file nodes."""
        self._refresh_subtree(self.root)

    def _refresh_subtree(self, node: TreeNode) -> None:
        if node.data is not None and not node.data.is_dir:
            self.refresh_node_label(node)
        for child in node.children:
            self._refresh_subtree(child)

    def get_all_unignored_files(self) -> List[str]:
        """Walk filesystem for path scanning (intelligent import)."""
        all_files: List[str] = []
        for root, dirs, files in os.walk(self.project_root):
            dirs[:] = [
                d
                for d in dirs
                if not is_ignored(
                    os.path.join(root, d), self.ignore_patterns, self.project_root
                )
            ]
            for f in files:
                full_path = os.path.join(root, f)
                if not is_ignored(
                    full_path, self.ignore_patterns, self.project_root
                ) and not is_binary(full_path):
                    all_files.append(os.path.relpath(full_path, self.project_root))
        return all_files

    def ensure_path_visible(self, file_path: str) -> None:
        """Expand parent directories so a file path becomes visible in tree."""
        abs_target = os.path.abspath(file_path)
        self._expand_to_path(self.root, abs_target)

    def _expand_to_path(self, node: TreeNode, target_abs: str) -> bool:
        """Recursively expand tree nodes toward target path. Returns True if found."""
        if node.data is None:
            return False
        if not node.data.is_dir:
            return node.data.path == target_abs
        # Check if target is under this directory
        if not target_abs.startswith(node.data.path):
            return False
        # Ensure scanned
        if not node.data.scanned:
            node.remove_children()
            self._populate_directory(node)
        node.expand()
        for child in node.children:
            if child.data is not None and self._expand_to_path(child, target_abs):
                return True
        return False


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------


class StatusBar(Static):
    """Bottom status bar showing selection counts and session state."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $primary-background;
        color: $text;
        padding: 0 1;
    }
    """

    def __init__(self, manager: SelectionManager, session: Session) -> None:
        super().__init__()
        self.manager = manager
        self.session = session

    def render(self) -> Text:
        tokens = estimate_tokens(self.manager.char_count)
        session_label = (
            Text(" SESSION ON ", style="bold white on green")
            if self.session.is_active
            else Text(" Session Off ", style="dim")
        )

        bar = Text()
        bar.append(
            f" Δ {self.manager.delta_count}  ◉ {self.manager.base_count}  "
            f"~{self.manager.char_count:,} chars (~{tokens:,} tokens)  "
        )
        bar.append_text(session_label)
        return bar


# ---------------------------------------------------------------------------
# Main TUI application
# ---------------------------------------------------------------------------


class KopipastaTUI(App):
    """Textual app for kopipasta file selection and paste workflow."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #tree-container {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit_and_finalize", "Quit & Copy", show=True),
        Binding("space", "toggle_selection", "Toggle", show=True),
        Binding("s", "toggle_snippet", "Snippet", show=True),
        Binding("a", "add_all_in_dir", "Add All", show=True),
        Binding("e", "extend_context", "Extend", show=True),
        Binding("p", "manual_paste", "Patch", show=True),
        Binding("x", "fix_workflow", "Fix", show=True),
        Binding("g", "grep_search", "Grep", show=True),
        Binding("d", "show_deps", "Deps", show=True),
        Binding("n", "session_start", "New Session", show=True),
        Binding("u", "session_update", "Update Session", show=True),
        Binding("f", "session_finish", "Finish Task", show=True),
        Binding("r", "ralph_setup", "Ralph", show=True),
        Binding("c", "clear_selection", "Clear", show=True),
        Binding("ctrl+c", "force_quit", "Force Quit", show=False),
    ]

    def __init__(
        self,
        project_root: str,
        ignore_patterns: List[str],
        files_to_preselect: Optional[List[str]] = None,
    ) -> None:
        super().__init__()
        self.project_root = os.path.abspath(project_root)
        self.ignore_patterns = ignore_patterns
        self.manager = SelectionManager()
        self.session = Session(self.project_root)
        self.logger = get_logger()
        self._files_to_preselect = files_to_preselect or []
        self._paste_buffer: str = ""

        # Result slot — set before exit
        self.result_files: List[FileTuple] = []
        self.result_char_count: int = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield FileTreeWidget(
            self.project_root,
            self.ignore_patterns,
            self.manager,
            id="file-tree",
        )
        yield StatusBar(self.manager, self.session)
        yield Footer()

    def on_mount(self) -> None:
        self._preselect_files()
        self.tree_widget.refresh_all_labels()

    @property
    def tree_widget(self) -> FileTreeWidget:
        return self.query_one("#file-tree", FileTreeWidget)

    @property
    def status_bar(self) -> StatusBar:
        return self.query_one(StatusBar)

    def _refresh_ui(self) -> None:
        """Convenience to refresh tree labels and status bar together."""
        self.tree_widget.refresh_all_labels()
        self.status_bar.refresh()

    # ------------------------------------------------------------------
    # Pre-selection
    # ------------------------------------------------------------------

    def _preselect_files(self) -> None:
        for file_path in self._files_to_preselect:
            abs_path = os.path.abspath(file_path)
            if (
                os.path.isfile(abs_path)
                and not is_binary(abs_path)
                and self.manager.get_state(abs_path) == FileState.UNSELECTED
            ):
                self.manager.set_state(abs_path, FileState.BASE)

    # ------------------------------------------------------------------
    # Paste handling (direct paste)
    # ------------------------------------------------------------------

    def on_paste(self, event: Paste) -> None:
        """Intercept terminal paste and show preview modal."""
        text = event.text
        if not text or not text.strip():
            return
        self._paste_buffer = sanitize_string(text)
        self.logger.info("paste_detected", length=len(self._paste_buffer))

        snippet = self._paste_buffer[:500]
        if len(self._paste_buffer) > 500:
            snippet += "\n…"

        self.push_screen(
            PasteModal(snippet, len(self._paste_buffer)),
            callback=self._on_paste_modal_dismissed,
        )

    def _on_paste_modal_dismissed(self, result: str) -> None:
        if result == "apply":
            self._apply_paste_suspended(self._paste_buffer)
        elif result == "edit":
            self._edit_and_apply()

    def _apply_paste_suspended(self, content: str) -> None:
        """Apply pasted content with full terminal output via suspend."""
        from rich.console import Console

        modified_files: list[str] = []
        found_paths: list[str] = []
        had_patches = False

        def _do_apply() -> None:
            nonlocal modified_files, found_paths, had_patches
            console = Console()

            patches = parse_llm_output(content, console)
            if patches:
                had_patches = True
                self.logger.info("paste_patches_found", count=len(patches))
                modified_files.extend(apply_patches(patches, logger=self.logger))
                if modified_files:
                    console.print(
                        f"\n[bold green]✅ Applied patches to {len(modified_files)} file(s).[/bold green]"
                    )
                else:
                    console.print(
                        "\n[bold yellow]⚠ Patches were found but none could be applied.[/bold yellow]"
                    )
                console.print(
                    "\n[bold]Review the changes with `git diff` before committing.[/bold]"
                )
                input("\nPress Enter to return to file selector...")
                return

            # Fallback: Intelligent Import (path scanning)
            all_paths = self.tree_widget.get_all_unignored_files()
            found_paths.extend(find_paths_in_text(content, all_paths))

            if found_paths:
                self.logger.info("paste_paths_found", count=len(found_paths))
                console.print(
                    f"\n[bold cyan]🔍 Found {len(found_paths)} project paths in text.[/bold cyan]"
                )
                for p in sorted(found_paths)[:10]:
                    console.print(f"  • {p}")
                if len(found_paths) > 10:
                    console.print(f"  ... and {len(found_paths) - 10} more.")
                input("\nPress Enter to add them to Delta...")
            else:
                self.logger.info("paste_no_match")
                console.print(
                    "\n[yellow]No patches or valid project paths detected in pasted content.[/yellow]"
                )
                input("\nPress Enter to return to file selector...")

        with self.suspend():
            _do_apply()

        # Post-processing on main thread
        if had_patches and modified_files:
            self._post_patch(modified_files)
        elif found_paths:
            self._import_paths(found_paths)

    def _post_patch(self, modified_files: List[str]) -> None:
        """Called on main thread after patches applied."""
        self.manager.promote_all_to_base()
        for path in modified_files:
            self.manager.mark_as_delta(path)
        if self.session.is_active:
            self.session.auto_commit()
        self._refresh_ui()
        self.notify(
            f"Applied patches to {len(modified_files)} file(s).",
            severity="information",
        )

    def _import_paths(self, found_paths: List[str]) -> None:
        """Called on main thread to add found paths to Delta."""
        for path in found_paths:
            abs_p = os.path.abspath(os.path.join(self.project_root, path))
            if os.path.isfile(abs_p):
                if self.manager.get_state(abs_p) == FileState.UNSELECTED:
                    self.manager.set_state(abs_p, FileState.DELTA)
                elif self.manager.get_state(abs_p) == FileState.BASE:
                    self.manager.mark_as_delta(abs_p)
                self.tree_widget.ensure_path_visible(abs_p)
        self._refresh_ui()
        self.notify(
            f"Imported {len(found_paths)} file(s) to Delta.", severity="information"
        )

    def _edit_and_apply(self) -> None:
        """Suspend TUI, open $EDITOR on paste buffer, then apply."""
        editor = os.environ.get("EDITOR", "nano")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(self._paste_buffer)
            temp_path = tf.name

        with self.suspend():
            subprocess.run([editor, temp_path])

        try:
            with open(temp_path, "r", encoding="utf-8") as f:
                self._paste_buffer = f.read()
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

        if self._paste_buffer.strip():
            self._apply_paste_suspended(self._paste_buffer)

    # ------------------------------------------------------------------
    # Manual paste (p key) — same as old workflow, suspend + prompt
    # ------------------------------------------------------------------

    def action_manual_paste(self) -> None:
        """Manual paste via suspend — for terminals where on_paste doesn't work."""
        self.logger.info("action_p_start", mode="manual_paste")

        def _do_manual_paste() -> None:
            from rich.console import Console as RichConsole

            console = RichConsole()
            console.print(
                "\n[bold cyan]📝 Paste the LLM's markdown response below.[/bold cyan]"
            )
            console.print(
                "   Press [bold]Ctrl-D[/bold] on an empty line to submit."
            )
            console.print("   Press [bold]Ctrl-C[/bold] to cancel.\n")

            lines = []
            try:
                while True:
                    line = input()
                    lines.append(line)
            except EOFError:
                pass
            except KeyboardInterrupt:
                console.print("\n[red]Cancelled.[/red]")
                return

            if lines:
                content = sanitize_string("\n".join(lines))
                if content.strip():
                    self._paste_buffer = content

        with self.suspend():
            _do_manual_paste()

        if self._paste_buffer.strip():
            self._apply_paste_content(self._paste_buffer)

    # ------------------------------------------------------------------
    # Fix workflow (x key)
    # ------------------------------------------------------------------

    def action_fix_workflow(self) -> None:
        """Run fix command, detect errors, generate diagnostic prompt."""
        self.logger.info("action_x_start")
        fix_cmd = read_fix_command(self.project_root)

        def _run_fix() -> Optional[Tuple[str, int, str]]:
            """Runs in suspended terminal. Returns (output, return_code, git_diff) or None."""
            from rich.console import Console as RichConsole
            from rich.panel import Panel

            console = RichConsole()
            console.print(
                Panel(
                    f"[bold cyan]🔧 Fix Workflow[/bold cyan]\n\n"
                    f"   Command: [bold]{fix_cmd}[/bold]\n\n"
                    f"   Press [bold]Enter[/bold] to run, or [bold]Ctrl-C[/bold] to cancel.",
                    title="Fix",
                    border_style="yellow",
                )
            )

            try:
                input()  # Wait for Enter
            except (KeyboardInterrupt, EOFError):
                console.print("\n[red]Fix cancelled.[/red]")
                return None

            # Session-aware git state
            session_active = self.session.is_active
            start_commit = None
            current_head = None

            if session_active:
                metadata = self.session.get_metadata()
                start_commit = metadata.get("start_commit") if metadata else None
                if start_commit and start_commit != "NO_GIT":
                    try:
                        result = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            capture_output=True,
                            text=True,
                            cwd=self.project_root,
                            check=True,
                        )
                        current_head = result.stdout.strip()
                    except subprocess.CalledProcessError:
                        current_head = None

                    if current_head:
                        try:
                            subprocess.run(
                                ["git", "reset", "--soft", start_commit],
                                cwd=self.project_root,
                                check=True,
                                capture_output=True,
                            )
                        except subprocess.CalledProcessError:
                            current_head = None

            console.print(f"\n[bold]Running:[/bold] {fix_cmd}\n")
            combined_output = ""
            return_code = -1

            try:
                try:
                    process = subprocess.Popen(
                        fix_cmd,
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        cwd=self.project_root,
                    )
                    if process.stdout:
                        for line in process.stdout:
                            console.print(f"  [dim]{line.rstrip()}[/dim]")
                            combined_output += line
                    return_code = process.wait(timeout=120)
                finally:
                    if session_active and current_head:
                        try:
                            subprocess.run(
                                ["git", "reset", "--soft", current_head],
                                cwd=self.project_root,
                                check=True,
                                capture_output=True,
                            )
                        except subprocess.CalledProcessError:
                            console.print(
                                f"[bold red]CRITICAL: Could not restore git state! "
                                f"Run: git reset --soft {current_head}[/bold red]"
                            )
            except subprocess.TimeoutExpired:
                console.print("[bold red]Command timed out after 120s.[/bold red]")
                if process:
                    process.kill()
                return None
            except Exception as e:
                console.print(f"[bold red]Failed to run command: {e}[/bold red]")
                return None

            if return_code == 0:
                console.print(
                    "[bold green]✅ Command succeeded! No errors to fix.[/bold green]"
                )
                if session_active:
                    self.session.auto_commit("kopipasta: auto-format fixes")
                input("\nPress Enter to continue...")
                return None

            console.print(
                f"\n[bold yellow]⚠ Command exited with code {return_code}[/bold yellow]"
            )

            # Capture git diff
            git_diff = ""
            try:
                diff_ref = "HEAD"
                if session_active and start_commit and start_commit != "NO_GIT":
                    diff_ref = start_commit
                diff_result = subprocess.run(
                    ["git", "diff", diff_ref],
                    capture_output=True,
                    text=True,
                    cwd=self.project_root,
                    timeout=30,
                )
                git_diff = diff_result.stdout.strip()
            except Exception:
                pass

            if session_active:
                self.session.auto_commit("kopipasta: auto-format fixes")

            input("\nPress Enter to continue...")
            return (combined_output, return_code, git_diff)

        fix_result: Optional[Tuple[str, int, str]] = None
        with self.suspend():
            fix_result = _run_fix()

        if fix_result is None:
            return

        combined_output, return_code, git_diff = fix_result

        # Detect affected files
        all_project_files = self.tree_widget.get_all_unignored_files()
        found_paths = find_paths_in_text(combined_output, all_project_files)

        if found_paths:
            for path in found_paths:
                abs_p = os.path.abspath(os.path.join(self.project_root, path))
                if os.path.isfile(abs_p):
                    self.tree_widget.ensure_path_visible(abs_p)
                    if self.manager.get_state(abs_p) == FileState.UNSELECTED:
                        self.manager.set_state(abs_p, FileState.DELTA)
                    elif self.manager.get_state(abs_p) == FileState.BASE:
                        self.manager.mark_as_delta(abs_p)

        # Generate and copy fix prompt
        affected_file_tuples = self.manager.get_delta_files()
        prompt_text = generate_fix_prompt(
            command=fix_cmd,
            error_output=combined_output.strip(),
            git_diff=git_diff,
            affected_files=affected_file_tuples,
            env_vars={},
        )

        try:
            pyperclip.copy(prompt_text)
            self.notify(
                f"Fix prompt copied! ({len(affected_file_tuples)} files)",
                severity="information",
            )
        except Exception:
            self.notify("Failed to copy fix prompt to clipboard.", severity="error")

        self._refresh_ui()
        self.logger.info("action_x_complete", prompt_len=len(prompt_text))

    # ------------------------------------------------------------------
    # Grep (g key) — suspend to interactive terminal
    # ------------------------------------------------------------------

    def action_grep_search(self) -> None:
        """Grep search in project — suspends to terminal for ag interaction."""
        self.logger.info("action_g_start")
        node = self.tree_widget.cursor_node
        if node is None or node.data is None:
            return

        search_dir = (
            node.data.path if node.data.is_dir else os.path.dirname(node.data.path)
        )

        found_files: List[str] = []

        def _do_grep() -> None:
            from kopipasta.analysis import (
                grep_files_in_directory,
                select_from_grep_results,
            )
            from rich.console import Console as RichConsole

            console = RichConsole()
            try:
                pattern = input("Enter search pattern: ")
            except (KeyboardInterrupt, EOFError):
                return
            if not pattern:
                return

            console.print(f"Searching for '{pattern}'...")
            grep_results = grep_files_in_directory(
                pattern, search_dir, self.ignore_patterns
            )
            if not grep_results:
                console.print(f"[yellow]No matches found for '{pattern}'[/yellow]")
                input("\nPress Enter to continue...")
                return

            selected, _ = select_from_grep_results(
                grep_results, self.manager.char_count
            )
            for file_tuple in selected:
                found_files.append(file_tuple[0])

            input("\nPress Enter to continue...")

        with self.suspend():
            _do_grep()

        # Add found files to Delta
        for file_path in found_files:
            abs_path = os.path.abspath(file_path)
            if self.manager.get_state(abs_path) == FileState.UNSELECTED:
                self.manager.set_state(abs_path, FileState.DELTA)
                self.tree_widget.ensure_path_visible(abs_path)

        if found_files:
            self.notify(
                f"Added {len(found_files)} files from grep.", severity="information"
            )
        self._refresh_ui()

    # ------------------------------------------------------------------
    # Dependencies (d key) — suspend to terminal
    # ------------------------------------------------------------------

    def action_show_deps(self) -> None:
        """Show and optionally add dependencies for current file."""
        node = self.tree_widget.cursor_node
        if node is None or node.data is None or node.data.is_dir:
            self.notify("Select a file to analyze dependencies.", severity="warning")
            return

        file_path = node.data.path
        new_deps: List[FileTuple] = []

        def _do_deps() -> None:
            from kopipasta.analysis import propose_and_add_dependencies

            files_list = self.manager.get_selected_files()
            deps, _ = propose_and_add_dependencies(
                file_path, self.project_root, files_list, self.manager.char_count
            )
            new_deps.extend(deps)
            input("\nPress Enter to continue...")

        with self.suspend():
            _do_deps()

        # Add new deps to selection
        for dep_tuple in new_deps:
            abs_path = os.path.abspath(dep_tuple[0])
            if self.manager.get_state(abs_path) == FileState.UNSELECTED:
                self.manager.set_state(abs_path, FileState.DELTA)
                self.tree_widget.ensure_path_visible(abs_path)

        if new_deps:
            self.notify(f"Added {len(new_deps)} dependencies.", severity="information")
        self._refresh_ui()

    # ------------------------------------------------------------------
    # Session management (n/u/f keys)
    # ------------------------------------------------------------------

    def action_session_start(self) -> None:
        """Start a new session (n key)."""
        self.logger.info("action_n_start")

        if not check_session_gitignore_status(self.project_root):

            def _on_gitignore_confirm(add_it: bool) -> None:
                if add_it:
                    add_to_gitignore(self.project_root, SESSION_FILENAME)
                self._do_session_start()

            self.push_screen(
                ConfirmModal(
                    f"[bold yellow]⚠ {SESSION_FILENAME} is NOT in .gitignore.[/bold yellow]\n\n"
                    f"Add it now?"
                ),
                callback=_on_gitignore_confirm,
            )
        else:
            self._do_session_start()

    def _do_session_start(self) -> None:
        def _start() -> bool:
            from rich.console import Console as RichConsole

            console = RichConsole()
            return self.session.start(console_printer=console.print)

        success = False
        with self.suspend():
            success = _start()

        if success and self.session.is_active:
            self.tree_widget.ensure_path_visible(self.session.path)
            self.notify("Session started.", severity="information")
            self.logger.info("session_started")
        self._refresh_ui()

    def action_session_update(self) -> None:
        """Update session / handover (u key)."""
        self.logger.info("action_u_start")
        if not self.session.is_active:
            self.notify("No active session to update.", severity="warning")
            return

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

    def action_session_finish(self) -> None:
        """Finish task / harvest (f key)."""
        self.logger.info("action_f_start")
        if not self.session.is_active:
            self.notify("No active session to finish.", severity="warning")
            return

        session_content = self.session.content
        context_path = os.path.join(self.project_root, "AI_CONTEXT.md")
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

        # Post-harvest cleanup
        if self.session.is_active:

            def _on_delete_confirm(do_delete: bool) -> None:
                if not do_delete:
                    return
                clear_cache()

                if start_commit and start_commit != "NO_GIT":

                    def _on_squash_confirm(do_squash: bool) -> None:
                        from rich.console import Console as RichConsole

                        console = RichConsole()
                        if self.session.finish(
                            squash=do_squash, console_printer=console.print
                        ):
                            self.notify("Session finished.", severity="information")
                            self.logger.info("session_finished", squashed=do_squash)
                        self._refresh_ui()

                    self.push_screen(
                        ConfirmModal(
                            f"Session started at commit: {start_commit[:7]}\n\n"
                            "Squash session commits (soft reset)?"
                        ),
                        callback=_on_squash_confirm,
                    )
                else:
                    from rich.console import Console as RichConsole

                    console = RichConsole()
                    self.session.finish(console_printer=console.print)
                    self.notify("Session finished.", severity="information")
                    self._refresh_ui()

            self.push_screen(
                ConfirmModal("🗑️  Delete `AI_SESSION.md` and finish session?"),
                callback=_on_delete_confirm,
            )

    def _run_gardener_cycle(self, prompt_text: str, title: str) -> None:
        """Copy gardener prompt, suspend for paste, apply patches."""
        try:
            pyperclip.copy(prompt_text)
            self.notify(f"🌱 {title} prompt copied!", severity="information")
        except Exception:
            self.notify("Could not copy to clipboard.", severity="error")

        def _do_gardener() -> None:
            from rich.console import Console as RichConsole
            from rich.panel import Panel

            console = RichConsole()
            console.print(
                Panel(
                    f"[bold]🌱 Gardener: {title}[/bold]\n\n"
                    "1. Paste this into your LLM.\n"
                    "2. Copy the LLM's Markdown response.\n"
                    "3. Press Enter here to paste and apply patches.",
                    border_style="green",
                )
            )

            try:
                input()
            except (KeyboardInterrupt, EOFError):
                return

            # Now do manual paste
            from prompt_toolkit import prompt as pt_prompt
            from prompt_toolkit.styles import Style as PtStyle

            console.print("[bold cyan]📝 Paste the LLM's response below.[/bold cyan]")
            console.print("   Press Meta+Enter or Esc then Enter to submit.\n")
            style = PtStyle.from_dict({"": "#ffffff"})
            try:
                content = pt_prompt(
                    "> ",
                    multiline=True,
                    prompt_continuation="  ",
                    style=style,
                )
                content = sanitize_string(content)
                if content.strip():
                    patches = parse_llm_output(content, console)
                    if patches:
                        modified = apply_patches(patches, logger=self.logger)
                        self.manager.promote_all_to_base()
                        for path in modified:
                            self.manager.mark_as_delta(path)
                        if self.session.is_active:
                            self.session.auto_commit()
                    else:
                        console.print("[yellow]No patches found in response.[/yellow]")
            except KeyboardInterrupt:
                console.print("\n[red]Cancelled.[/red]")

            input("\nPress Enter to return to file selector...")

        with self.suspend():
            _do_gardener()

        self._refresh_ui()

    # ------------------------------------------------------------------
    # Ralph (r key) — MCP agent integration
    # ------------------------------------------------------------------

    def action_ralph_setup(self) -> None:
        """Configure MCP environment for Ralph Loop."""
        self.logger.info("action_ralph_start")

        delta_files = [
            os.path.relpath(f[0], self.project_root)
            for f in self.manager.get_delta_files()
        ]
        base_files = [
            os.path.relpath(f[0], self.project_root)
            for f in self.manager.get_base_files()
        ]

        total_files = len(delta_files) + len(base_files)
        if total_files == 0:
            self.notify("No files selected. Select context first.", severity="warning")
            return

        def _do_ralph() -> None:
            from rich.console import Console as RichConsole
            from rich.panel import Panel
            import click

            console = RichConsole()

            # .gitignore check
            gitignore_path = os.path.join(self.project_root, ".gitignore")
            ralph_ignored = False
            if os.path.exists(gitignore_path):
                try:
                    with open(gitignore_path, "r", encoding="utf-8") as f:
                        ralph_ignored = RALPH_CONFIG_FILENAME in f.read().splitlines()
                except IOError:
                    pass
            if not ralph_ignored:
                if click.confirm(
                    f"Add {RALPH_CONFIG_FILENAME} to .gitignore?", default=True
                ):
                    add_to_gitignore(self.project_root, RALPH_CONFIG_FILENAME)

            console.print(
                Panel(
                    f"[bold cyan]🤖 Ralph Setup (MCP)[/bold cyan]\n\n"
                    f"   [green]Editable Files (Delta):[/green] {len(delta_files)}\n"
                    f"   [cyan]Read-Only Files (Base):[/cyan] {len(base_files)}\n",
                    title="Ralph Loop",
                    border_style="cyan",
                )
            )

            # Verification command
            default_cmd = read_fix_command(self.project_root) or "pytest"
            config_path = os.path.join(self.project_root, RALPH_CONFIG_FILENAME)
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                        if existing.get("verification_command"):
                            default_cmd = existing["verification_command"]
                except Exception:
                    pass

            verification_cmd = click.prompt(
                "Verification Command (e.g. pytest)", default=default_cmd
            )

            task = load_task_from_cache() or "Solve the current issue."

            config = {
                "project_root": self.project_root,
                "verification_command": verification_cmd,
                "task_description": task,
                "editable_files": delta_files,
                "readable_files": base_files,
            }

            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2)
                console.print(
                    f"\n[green]✅ Configuration saved to {RALPH_CONFIG_FILENAME}[/green]"
                )
            except IOError as e:
                console.print(f"\n[red]Failed to save config: {e}[/red]")
                input("\nPress Enter to continue...")
                return

            use_local = click.confirm(
                "Use local dev mode? (Yes = current Python, No = uvx production)",
                default=True,
            )
            configure_claude_desktop(
                project_root=self.project_root,
                local=use_local,
                console=console,
            )

            input("\nPress Enter to return to file selector...")

        with self.suspend():
            _do_ralph()

    # ------------------------------------------------------------------
    # Selection actions
    # ------------------------------------------------------------------

    def action_toggle_selection(self) -> None:
        node = self.tree_widget.cursor_node
        if node is not None and node.data is not None:
            self.tree_widget.toggle_selection(node)
            self._refresh_ui()

    def action_toggle_snippet(self) -> None:
        node = self.tree_widget.cursor_node
        if node is not None and node.data is not None and not node.data.is_dir:
            self.tree_widget.toggle_selection(node, snippet=True)
            self._refresh_ui()

    def action_add_all_in_dir(self) -> None:
        node = self.tree_widget.cursor_node
        if node is None or node.data is None:
            return
        target = node if node.data.is_dir else node.parent
        if target is not None and target.data is not None:
            self.tree_widget.toggle_selection(target)
            self._refresh_ui()

    def action_extend_context(self) -> None:
        """Generate extension prompt with Delta files and copy to clipboard."""
        delta_files = self.manager.get_delta_files()
        if not delta_files:
            self.notify("No Delta files to extend.", severity="warning")
            return

        prompt_text = generate_extension_prompt(delta_files, {})
        try:
            pyperclip.copy(prompt_text)
            self.manager.promote_delta_to_base()
            self._refresh_ui()
            self.notify(
                f"Extended context ({len(delta_files)} files) copied!",
                severity="information",
            )
        except Exception:
            self.notify("Failed to copy to clipboard.", severity="error")

    def action_clear_selection(self) -> None:
        self.manager.clear_all()
        self._refresh_ui()
        self.notify("Selection cleared.")

    # ------------------------------------------------------------------
    # Quit
    # ------------------------------------------------------------------

    def action_quit_and_finalize(self) -> None:
        self.manager.promote_all_to_base()
        self.result_files = self.manager.get_selected_files()
        self.result_char_count = self.manager.char_count
        self.logger.info("tui_quit", file_count=len(self.result_files))
        self.exit()

    def action_force_quit(self) -> None:
        self.exit()

    # ------------------------------------------------------------------
    # Event handlers for status bar refresh
    # ------------------------------------------------------------------

    def on_tree_node_selected(self, event: TextualTree.NodeSelected) -> None:
        """Refresh status bar when tree cursor moves."""
        self.status_bar.refresh()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_tui(
    project_root: str,
    ignore_patterns: List[str],
    files_to_preselect: Optional[List[str]] = None,
) -> Tuple[List[FileTuple], int]:
    """
    Launch the Textual TUI and return selected files.
    Drop-in replacement for TreeSelector.run().
    """
    app = KopipastaTUI(
        project_root=project_root,
        ignore_patterns=ignore_patterns,
        files_to_preselect=files_to_preselect,
    )
    app.run()
    return app.result_files, app.result_char_count
