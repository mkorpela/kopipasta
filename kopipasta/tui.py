"""
Textual-based TUI for kopipasta.
Replaces the manual Rich + click.getchar() render loop in tree_selector.py.
"""
import os
import subprocess
import tempfile
from typing import List, Optional, Tuple

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

from kopipasta.file import (
    FileTuple,
    is_binary,
    is_ignored,
    get_human_readable_size,
)
from kopipasta.ops import estimate_tokens, sanitize_string
from kopipasta.patcher import apply_patches, parse_llm_output, find_paths_in_text
from kopipasta.selection import SelectionManager, FileState
from kopipasta.session import Session, SESSION_FILENAME
from kopipasta.prompt import get_file_snippet
from kopipasta.logger import get_logger

ALWAYS_VISIBLE_FILES = {"AI_SESSION.md", "AI_CONTEXT.md"}


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
# File tree widget
# ---------------------------------------------------------------------------

class FileTreeWidget(TextualTree):
    """Project file tree backed by filesystem scanning."""

    DEFAULT_CSS = """
    FileTreeWidget {
        height: 1fr;
    }
    """

    def __init__(
        self,
        project_root: str,
        ignore_patterns: List[str],
        manager: SelectionManager,
        **kwargs,
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

        dirs = []
        files = []

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
            # Add placeholder so Textual shows expand arrow
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
            # Remove placeholder
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
                # Ensure scanned
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
                d for d in dirs
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
    # Paste handling (the core new feature)
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
            self._apply_paste(self._paste_buffer)
        elif result == "edit":
            self._edit_and_apply()
        # "cancel" — do nothing

    @work(thread=True)
    def _apply_paste(self, content: str) -> None:
        """Apply pasted content as patches or intelligent import."""
        from rich.console import Console

        console = Console()

        patches = parse_llm_output(content, console)
        if patches:
            self.logger.info("paste_patches_found", count=len(patches))
            modified_files = apply_patches(patches, logger=self.logger)

            self.app.call_from_thread(self._post_patch, modified_files)
            return

        # Fallback: Intelligent Import (path scanning)
        all_paths = self.tree_widget.get_all_unignored_files()
        found_paths = find_paths_in_text(content, all_paths)

        if found_paths:
            self.logger.info("paste_paths_found", count=len(found_paths))
            self.app.call_from_thread(self._import_paths, found_paths)
        else:
            self.logger.info("paste_no_match")
            self.app.call_from_thread(
                self.notify,
                "No patches or project paths detected in pasted content.",
                severity="warning",
            )

    def _post_patch(self, modified_files: List[str]) -> None:
        """Called on main thread after patches applied."""
        self.manager.promote_all_to_base()
        for path in modified_files:
            self.manager.mark_as_delta(path)
        if self.session.is_active:
            self.session.auto_commit()
        self.tree_widget.refresh_all_labels()
        self.status_bar.refresh()
        self.notify(f"Applied patches to {len(modified_files)} file(s).", severity="information")

    def _import_paths(self, found_paths: List[str]) -> None:
        """Called on main thread to add found paths to Delta."""
        for path in found_paths:
            abs_p = os.path.abspath(os.path.join(self.project_root, path))
            if os.path.isfile(abs_p):
                if self.manager.get_state(abs_p) == FileState.UNSELECTED:
                    self.manager.set_state(abs_p, FileState.DELTA)
                elif self.manager.get_state(abs_p) == FileState.BASE:
                    self.manager.mark_as_delta(abs_p)
        self.tree_widget.refresh_all_labels()
        self.status_bar.refresh()
        self.notify(f"Imported {len(found_paths)} file(s) to Delta.", severity="information")

    def _edit_and_apply(self) -> None:
        """Suspend TUI, open $EDITOR on paste buffer, then apply."""
        editor = os.environ.get("EDITOR", "nano")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(self._paste_buffer)
            temp_path = tf.name

        def _do_edit() -> None:
            subprocess.run([editor, temp_path])
            try:
                with open(temp_path, "r", encoding="utf-8") as f:
                    edited = f.read()
            finally:
                os.unlink(temp_path)
            self._paste_buffer = edited
            self._apply_paste(edited)

        self.suspend()
        # After suspend returns, the screen is restored.
        # We need to run the editor synchronously during suspend.
        # Textual's suspend() is a context manager in newer versions.
        # For compatibility we use the action pattern:
        _do_edit()

    # ------------------------------------------------------------------
    # Key-bound actions
    # ------------------------------------------------------------------

    def action_toggle_selection(self) -> None:
        node = self.tree_widget.cursor_node
        if node is not None and node.data is not None:
            self.tree_widget.toggle_selection(node)
            self.status_bar.refresh()

    def action_toggle_snippet(self) -> None:
        node = self.tree_widget.cursor_node
        if node is not None and node.data is not None and not node.data.is_dir:
            self.tree_widget.toggle_selection(node, snippet=True)
            self.status_bar.refresh()

    def action_add_all_in_dir(self) -> None:
        node = self.tree_widget.cursor_node
        if node is None or node.data is None:
            return
        target = node if node.data.is_dir else node.parent
        if target is not None and target.data is not None:
            self.tree_widget.toggle_selection(target)
            self.status_bar.refresh()

    def action_extend_context(self) -> None:
        """Generate extension prompt with Delta files and copy to clipboard."""
        import pyperclip
        from kopipasta.prompt import generate_extension_prompt

        delta_files = self.manager.get_delta_files()
        if not delta_files:
            self.notify("No Delta files to extend.", severity="warning")
            return

        prompt_text = generate_extension_prompt(delta_files, {})
        try:
            pyperclip.copy(prompt_text)
            self.manager.promote_delta_to_base()
            self.tree_widget.refresh_all_labels()
            self.status_bar.refresh()
            self.notify(f"Extended context ({len(delta_files)} files) copied!", severity="information")
        except Exception:
            self.notify("Failed to copy to clipboard.", severity="error")

    def action_clear_selection(self) -> None:
        self.manager.clear_all()
        self.tree_widget.refresh_all_labels()
        self.status_bar.refresh()
        self.notify("Selection cleared.")

    def action_quit_and_finalize(self) -> None:
        self.manager.promote_all_to_base()
        self.result_files = self.manager.get_selected_files()
        self.result_char_count = self.manager.char_count
        self.logger.info("tui_quit", file_count=len(self.result_files))
        self.exit()

    def action_force_quit(self) -> None:
        self.exit()


# ---------------------------------------------------------------------------
# Public entry point (matches old TreeSelector.run interface)
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