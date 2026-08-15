"""The return value of `apply_patches` — spec §8 exit codes 4 and 5.

Found by dogfooding: `_apply_diff_patch` returned `False` only when *zero*
hunks matched, so a file where 1 of 3 hunks applied was written to disk and
reported as a success. `apply_patches` returned a bare `List[str]` of modified
files, so the caller could not see it.

That is precisely the distinction spec §8 hangs exit 4 ("patch **partially**
applied — worktree dirty, inspect `failed`") on, and its absence meant
`kopipasta apply` would have exited 0 on a half-patched file. The counts were
already computed — `_apply_diff_patch` prints "(1/2 hunks applied)" — and then
thrown away at the return.

`PatchResult` subclasses `list` on purpose: `tree_selector` and `spike/oracle`
both use the old return value as a list of modified paths, and neither should
have to change to learn something new is available.
"""

import os
from pathlib import Path

import pytest

from kopipasta.patcher import apply_patches, parse_llm_output

TWO_HUNKS_ONE_MATCHES = """
### app.py

```python
<<<<
def a():
    return 1
====
def a():
    return 100
>>>>
<<<<
def zzz_nonexistent():
    raise SystemExit
====
def zzz_nonexistent():
    pass
>>>>
```
"""

NOTHING_MATCHES = """
### app.py

```python
<<<<
def zzz_nonexistent():
    raise SystemExit
====
def zzz_nonexistent():
    pass
>>>>
```
"""

ORIGINAL = "def a():\n    return 1\n\ndef b():\n    return 2\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(ORIGINAL)
    (tmp_path / "other.py").write_text("def c():\n    return 3\n")
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(original_cwd)


def test_partially_applied_file_is_not_reported_as_success(project: Path):
    """1 of 2 hunks applied: the file changed, so this is exit 4, not exit 0."""
    result = apply_patches(parse_llm_output(TWO_HUNKS_ONE_MATCHES))

    # The write happened, so the worktree is dirty and the path must still be
    # in the list — a caller reverting or committing needs to know it changed.
    assert (project / "app.py").read_text() != ORIGINAL
    assert "app.py" in result

    # But it is not clean, and that must be visible without re-reading the file.
    assert result.partial == ["app.py"]
    assert result.ok is False
    outcome = result.outcomes[0]
    assert outcome.status == "partial"
    assert (outcome.hunks_applied, outcome.hunks_total) == (1, 2)


def test_totally_failed_file_leaves_the_worktree_untouched(project: Path):
    """No hunk applied: exit 5 — nothing was written, so a retry is safe."""
    result = apply_patches(parse_llm_output(NOTHING_MATCHES))

    assert (project / "app.py").read_text() == ORIGINAL
    assert list(result) == []
    assert result.failed == ["app.py"]
    assert result.changed is False
    assert result.ok is False


def test_dry_run_reports_the_same_outcome_but_writes_nothing(project: Path):
    """spec §11: --dry-run renders what would be applied and touches nothing."""
    result = apply_patches(parse_llm_output(TWO_HUNKS_ONE_MATCHES), dry_run=True)

    assert (project / "app.py").read_text() == ORIGINAL, "dry run wrote to disk"
    assert result.dry_run is True
    # The verdict must be identical to the real run, or --dry-run is useless
    # as a preview of it.
    assert result.partial == ["app.py"]
    assert result.ok is False
    assert result.outcomes[0].hunks_applied == 1


def test_dry_run_does_not_create_new_files(project: Path):
    result = apply_patches(
        parse_llm_output("```python\n# FILE: brand_new.py\nprint('hi')\n```"),
        dry_run=True,
    )

    assert not (project / "brand_new.py").exists()
    assert result.ok is True
    assert result.outcomes[0].action == "created"


def test_dry_run_does_not_delete(project: Path):
    result = apply_patches(
        parse_llm_output("```\n# FILE: other.py\n<<<DELETE>>>\n```"),
        allow_delete=True,
        dry_run=True,
    )

    assert (project / "other.py").exists(), "dry run deleted a file"
    assert result.outcomes[0].action == "deleted"


def test_patch_outside_the_editable_zone_is_refused(project: Path):
    """spec §11: only files under Active Workspace (Editable) may be modified.

    The refusal must be a recorded decision, not an exception: the per-file
    body of apply_patches catches broad exceptions, so raising here would be
    swallowed and misreported as a corrupt patch.
    """
    result = apply_patches(
        parse_llm_output(TWO_HUNKS_ONE_MATCHES), allowed_files=["other.py"]
    )

    assert (project / "app.py").read_text() == ORIGINAL
    assert result.skipped == ["app.py"]
    assert result.changed is False
    assert "editable" in result.outcomes[0].reason.lower()


def test_allowed_files_permits_a_file_inside_the_zone(project: Path):
    result = apply_patches(
        parse_llm_output(TWO_HUNKS_ONE_MATCHES), allowed_files=["app.py"]
    )

    assert (project / "app.py").read_text() != ORIGINAL
    assert result.skipped == []


def test_allowed_files_normalises_separators_and_dot_slash(project: Path):
    """A selection record and a model's prose spell the same path differently."""
    result = apply_patches(
        parse_llm_output(TWO_HUNKS_ONE_MATCHES), allowed_files=["./app.py"]
    )

    assert result.skipped == []
    assert (project / "app.py").read_text() != ORIGINAL


def test_result_is_still_a_list_of_modified_paths(project: Path):
    """tree_selector.py and spike/oracle.py use it as a plain list."""
    result = apply_patches(parse_llm_output(NOTHING_MATCHES))
    assert result == []

    result = apply_patches(parse_llm_output(TWO_HUNKS_ONE_MATCHES))
    assert result == ["app.py"]
    assert len(result) == 1
    assert [p for p in result] == ["app.py"]


def test_clean_full_file_overwrite_is_ok(project: Path):
    result = apply_patches(
        parse_llm_output("```python\n# FILE: app.py\ndef a():\n    return 100\n```")
    )
    assert result.ok is True
    assert result.applied == ["app.py"]
    assert result.partial == []
    assert result.changed is True
