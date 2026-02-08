import pytest
from kopipasta.selection import SelectionManager, FileState


@pytest.fixture
def manager():
    return SelectionManager()


def test_initial_state(manager):
    assert manager.char_count == 0
    assert manager.delta_count == 0
    assert manager.base_count == 0


def test_set_and_get_state(manager, tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    path = str(f)

    manager.set_state(path, FileState.DELTA)
    assert manager.get_state(path) == FileState.DELTA
    assert manager.char_count == 5
    assert manager.delta_count == 1

    manager.set_state(path, FileState.BASE)
    assert manager.get_state(path) == FileState.BASE
    assert manager.char_count == 5
    assert manager.base_count == 1
    assert manager.delta_count == 0


def test_toggle_cycle(manager, tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("content")
    path = str(f)

    # 1. Unselected -> Delta
    manager.toggle(path)
    assert manager.get_state(path) == FileState.DELTA

    # 2. Delta -> Unselected
    manager.toggle(path)
    assert manager.get_state(path) == FileState.UNSELECTED

    # Setup for Base
    manager.set_state(path, FileState.BASE)

    # 3. Base -> Delta
    manager.toggle(path)
    assert manager.get_state(path) == FileState.DELTA


def test_promote_to_base(manager, tmp_path):
    f1 = tmp_path / "f1.txt"
    f1.write_text("one")
    f2 = tmp_path / "f2.txt"
    f2.write_text("two")

    manager.set_state(str(f1), FileState.DELTA)
    manager.set_state(str(f2), FileState.DELTA)

    manager.promote_all_to_base()

    assert manager.get_state(str(f1)) == FileState.BASE
    assert manager.get_state(str(f2)) == FileState.BASE
    assert manager.delta_count == 0
    assert manager.base_count == 2


def test_clear_base(manager, tmp_path):
    f1 = tmp_path / "f1.txt"
    f1.write_text("one")
    f2 = tmp_path / "f2.txt"
    f2.write_text("two")

    manager.set_state(str(f1), FileState.BASE)
    manager.set_state(str(f2), FileState.DELTA)

    manager.clear_base()

    assert manager.get_state(str(f1)) == FileState.UNSELECTED
    assert manager.get_state(str(f2)) == FileState.DELTA
    assert manager.char_count == 3  # only f2 remains


def test_snippet_handling(manager, tmp_path):
    # Create a large-ish file
    f = tmp_path / "large.txt"
    content = "x" * 1000
    f.write_text(content)
    path = str(f)

    # SelectionManager doesn't calculate snippet itself, it relies on get_file_snippet
    # but we test that is_snippet flag is preserved.
    manager.set_state(path, FileState.DELTA, is_snippet=True)
    assert manager.is_snippet(path) is True
    # The size for snippet in SelectionManager uses get_file_snippet which usually returns 50 lines.
    # Our test file is 1 line, so it should be the same, but we verify the manager tracks it.
    assert manager.char_count == 1000


def test_delta_retrieval_and_promotion(manager, tmp_path):
    f1 = tmp_path / "base.py"
    f1.write_text("base")
    f2 = tmp_path / "delta.py"
    f2.write_text("delta")

    # Setup: One Base, One Delta
    manager.set_state(str(f1), FileState.BASE)
    manager.set_state(str(f2), FileState.DELTA)

    assert manager.delta_count == 1
    assert manager.base_count == 1

    # Test retrieval
    deltas = manager.get_delta_files()
    assert len(deltas) == 1
    assert deltas[0][0] == str(f2)

    # Test selective promotion
    manager.promote_delta_to_base()

    assert manager.get_state(str(f1)) == FileState.BASE
    assert manager.get_state(str(f2)) == FileState.BASE
    assert manager.delta_count == 0
    assert manager.base_count == 2
