from pathlib import Path
from kopipasta.file import extract_symbols


def test_extract_symbols_from_python_class(tmp_path: Path):
    """extract_symbols returns class name with public method names; private methods omitted."""
    py_file = tmp_path / "example.py"
    py_file.write_text(
        "class Foo:\n    def __init__(self): pass\n    def bar(self): pass\n"
    )
    result = extract_symbols(str(py_file))
    assert result == ["class Foo(bar)"]


def test_extract_symbols_private_methods_omitted(tmp_path: Path):
    """Methods starting with '_' (including dunders) are omitted."""
    py_file = tmp_path / "example.py"
    py_file.write_text(
        "class Foo:\n"
        "    def __init__(self): pass\n"
        "    def __repr__(self): pass\n"
        "    def _private(self): pass\n"
    )
    result = extract_symbols(str(py_file))
    assert result == ["class Foo"]


def test_extract_symbols_private_top_level_function_omitted(tmp_path: Path):
    """Top-level functions starting with '_' are omitted."""
    py_file = tmp_path / "example.py"
    py_file.write_text(
        "def public(): pass\n"
        "def _private(): pass\n"
        "def __dunder(): pass\n"
    )
    result = extract_symbols(str(py_file))
    assert result == ["def public"]


def test_extract_symbols_top_level_function(tmp_path: Path):
    """extract_symbols includes top-level function definitions."""
    py_file = tmp_path / "example.py"
    py_file.write_text("def helper():\n    pass\n")
    result = extract_symbols(str(py_file))
    assert result == ["def helper"]


def test_extract_symbols_async_function(tmp_path: Path):
    """extract_symbols includes async top-level functions."""
    py_file = tmp_path / "example.py"
    py_file.write_text("async def fetch():\n    pass\n")
    result = extract_symbols(str(py_file))
    assert result == ["def fetch"]


def test_extract_symbols_non_python_returns_empty(tmp_path: Path):
    """Non-Python files return an empty list."""
    js_file = tmp_path / "script.js"
    js_file.write_text("function foo() {}")
    assert extract_symbols(str(js_file)) == []


def test_extract_symbols_invalid_python_returns_empty(tmp_path: Path):
    """Invalid Python syntax returns an empty list."""
    py_file = tmp_path / "bad.py"
    py_file.write_text("def foo( <- invalid syntax")
    assert extract_symbols(str(py_file)) == []


def test_extract_symbols_mixed(tmp_path: Path):
    """extract_symbols handles a mix of top-level classes and functions."""
    py_file = tmp_path / "example.py"
    py_file.write_text(
        "def standalone(): pass\nclass Bar:\n    def method(self): pass\n"
    )
    result = extract_symbols(str(py_file))
    assert result == ["def standalone", "class Bar(method)"]


def test_extract_symbols_empty_class(tmp_path: Path):
    """A class with no methods returns just the class name."""
    py_file = tmp_path / "example.py"
    py_file.write_text("class Empty:\n    pass\n")
    result = extract_symbols(str(py_file))
    assert result == ["class Empty"]


def test_extract_symbols_empty_file_returns_empty(tmp_path: Path):
    """An empty Python file returns an empty list."""
    py_file = tmp_path / "empty.py"
    py_file.write_text("")
    assert extract_symbols(str(py_file)) == []
