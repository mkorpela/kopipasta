import os
from enum import Enum, auto
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from kopipasta.core.render import get_file_snippet, get_language_for_file

if TYPE_CHECKING:  # pragma: no cover - import cycle: core.context imports prompt
    from kopipasta.core.resolver import Selection


class FileState(Enum):
    UNSELECTED = auto()
    BASE = auto()  # Blue/Cyan: Previously synced context
    DELTA = auto()  # Green: New changes or active focus
    MAP = auto()  # Yellow: Skeletonized version shown in prompt; not "selected"


class SelectionManager:
    def __init__(self):
        # path -> (state, is_snippet, chunks)
        self._files: Dict[str, Tuple[FileState, bool, Optional[List[str]]]] = {}
        self.char_count: int = 0

    def get_state(self, path: str) -> FileState:
        abs_path = os.path.abspath(path)
        if abs_path not in self._files:
            return FileState.UNSELECTED
        return self._files[abs_path][0]

    def is_snippet(self, path: str) -> bool:
        abs_path = os.path.abspath(path)
        return self._files.get(abs_path, (None, False, None))[1]

    def set_state(
        self,
        path: str,
        state: FileState,
        is_snippet: bool = False,
        chunks: Optional[List[str]] = None,
    ):
        abs_path = os.path.abspath(path)

        # Subtract old size if it existed
        if abs_path in self._files:
            self.char_count -= self._calculate_file_size(abs_path)

        if state == FileState.UNSELECTED:
            self._files.pop(abs_path, None)
        else:
            self._files[abs_path] = (state, is_snippet, chunks)
            self.char_count += self._calculate_file_size(abs_path)

    def toggle(self, path: str, is_snippet: bool = False):
        """
        Implements the 3-state cycle for Space:
        1. Unselected -> Delta
        2. Delta -> Unselected
        3. Base -> Delta (Promote to focus)
        """
        current_state = self.get_state(path)

        if current_state == FileState.UNSELECTED:
            self.set_state(path, FileState.DELTA, is_snippet=is_snippet)
        elif current_state == FileState.DELTA:
            self.set_state(path, FileState.UNSELECTED)
        elif current_state in (FileState.BASE, FileState.MAP):
            self.set_state(path, FileState.DELTA, is_snippet=is_snippet)

    def promote_all_to_base(self):
        """Transitions all DELTA files to BASE (used on Patch or Quit)."""
        self.promote_delta_to_base()

    def promote_delta_to_base(self):
        """Transitions only DELTA files to BASE (used on Extend Context)."""
        for path in list(self._files.keys()):
            state, is_snippet, chunks = self._files[path]
            if state == FileState.DELTA:
                self._files[path] = (FileState.BASE, is_snippet, chunks)

    def get_delta_files(self) -> List[Tuple[str, bool, Optional[List[str]], str]]:
        """Returns only files in DELTA state."""
        return [
            (p, s[1], s[2], get_language_for_file(p))
            for p, s in self._files.items()
            if s[0] == FileState.DELTA
        ]

    def get_base_files(self) -> List[Tuple[str, bool, Optional[List[str]], str]]:
        """Returns only files in BASE state."""
        return [
            (p, s[1], s[2], get_language_for_file(p))
            for p, s in self._files.items()
            if s[0] == FileState.BASE
        ]

    def mark_as_delta(self, path: str):
        """Promotes a file to DELTA state (e.g. after a patch)."""
        _, is_snippet, chunks = self._files.get(
            os.path.abspath(path), (None, False, None)
        )
        self.set_state(path, FileState.DELTA, is_snippet=is_snippet, chunks=chunks)

    def get_selected_files(self) -> List[Tuple[str, bool, Optional[List[str]], str]]:
        """Returns files in the format expected by the prompt generator.

        Excludes MAP files; use get_map_files() to retrieve those separately.
        """
        results = []
        for path, (state, is_snippet, chunks) in self._files.items():
            if state not in (FileState.UNSELECTED, FileState.MAP):
                results.append((path, is_snippet, chunks, get_language_for_file(path)))
        return results

    def _calculate_file_size(self, path: str) -> int:
        abs_path = os.path.abspath(path)
        if abs_path not in self._files:
            return 0
        state, is_snippet, chunks = self._files[abs_path]
        if state == FileState.MAP:
            return 0
        if chunks:
            return sum(len(c) for c in chunks)
        if is_snippet:
            return len(get_file_snippet(path))
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def toggle_map(self, path: str) -> None:
        """Toggle MAP state: Unselected -> MAP -> Unselected.

        Only applies to .py files.
        Does not affect files in BASE or DELTA state.
        MAP files do not contribute to char_count.
        """
        if not path.endswith(".py"):
            return
        abs_path = os.path.abspath(path)
        current_state = self.get_state(abs_path)
        if current_state == FileState.UNSELECTED:
            self.set_state(abs_path, FileState.MAP)
        elif current_state == FileState.MAP:
            self.set_state(abs_path, FileState.UNSELECTED)
        # BASE and DELTA are intentionally left unchanged.

    def get_map_files(self) -> List[str]:
        """Returns paths of files currently in MAP state."""
        return [p for p, (s, _, _) in self._files.items() if s == FileState.MAP]

    def to_selection(self, root: str) -> "Selection":
        """This selection as the core's role model, for the shared renderer.

        The three-state engine and the CLI's flags are the same idea in two
        vocabularies, and they line up one to one:

            DELTA  ->  pin       the working set; attention belongs here
            BASE   ->  ref       synced context; read for dependencies
            MAP    ->  map       skeleton only
            snippet -> snippet   only the first lines were sent, whatever
                                 state selected it

        That correspondence was already load-bearing — the Ralph loop hands
        the agent Delta as its editable set — but the prompt flattened it into
        one undifferentiated list, so the pasting window never saw the
        distinction the rest of the tool tracks.
        """
        from kopipasta.core.resolver import MAP, PIN, REF, SNIPPET, Entry, Selection

        state_role = {FileState.DELTA: PIN, FileState.BASE: REF, FileState.MAP: MAP}
        selection = Selection(root=root)
        for path, (state, is_snippet, chunks) in self._files.items():
            role = state_role.get(state)
            if role is None:
                continue
            # A partial file belongs in the zone that says so. `-s` is its own
            # role in the CLI for the same reason: "you have the first 50
            # lines of this" is the caveat that has to travel with it, and it
            # outranks which state put it there.
            if is_snippet and role != MAP:
                role = SNIPPET
            selection.entries[path] = Entry(
                path=path,
                rel=os.path.relpath(path, root).replace(os.sep, "/"),
                role=role,
                bulk=False,
                chunks=chunks,
            )
        return selection

    def clear_base(self):
        """Removes all files in BASE state, keeping DELTA."""
        to_remove = [p for p, (s, _, _) in self._files.items() if s == FileState.BASE]
        for p in to_remove:
            self.set_state(p, FileState.UNSELECTED)

    def clear_all(self):
        """Removes all files from selection."""
        self._files.clear()
        self.char_count = 0

    @property
    def delta_count(self) -> int:
        return sum(1 for s, _, _ in self._files.values() if s == FileState.DELTA)

    @property
    def base_count(self) -> int:
        return sum(1 for s, _, _ in self._files.values() if s == FileState.BASE)
