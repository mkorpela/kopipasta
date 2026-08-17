from typing import List

from kopipasta.patcher import Hunk, Patch


def hunks_of(patch: Patch) -> List[Hunk]:
    """The diff hunks of a diff patch, asserting it really is one."""
    content = patch["content"]
    assert isinstance(content, list), f"expected a diff patch, got {patch['type']!r}"
    return content


def text_of(patch: Patch) -> str:
    """The whole-file content of a full patch, asserting it really is one."""
    content = patch["content"]
    assert isinstance(content, str), f"expected a full patch, got {patch['type']!r}"
    return content
