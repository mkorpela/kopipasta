from kopipasta.skeleton import skeletonize_python


def test_skeletonize_strips_function_body():
    source = "def foo():\n    return 42\n"
    result = skeletonize_python(source)
    assert "def foo():" in result
    assert "return 42" not in result
    assert "    ..." in result


def test_skeletonize_preserves_class_and_method_signature():
    source = "class Foo:\n    def bar(self):\n        pass\n"
    result = skeletonize_python(source)
    assert "class Foo:" in result
    assert "def bar(self):" in result
    assert "        ..." in result
    assert "pass" not in result


def test_skeletonize_multiline_function_body():
    source = "def foo():\n    x = 1\n    y = 2\n    return x + y\n"
    result = skeletonize_python(source)
    assert "def foo():" in result
    assert "x = 1" not in result
    assert "return x + y" not in result
    assert "    ..." in result


def test_skeletonize_preserves_function_signature_with_params():
    source = "def greet(name: str, age: int = 0) -> str:\n    return f'hello {name}'\n"
    result = skeletonize_python(source)
    assert "def greet(name: str, age: int = 0) -> str:" in result
    assert "return" not in result
    assert "    ..." in result


def test_skeletonize_invalid_python_returned_as_is():
    source = "def foo( <- not valid python"
    result = skeletonize_python(source)
    assert result == source


def test_skeletonize_multiple_methods():
    source = (
        "class MyClass:\n"
        "    def __init__(self):\n"
        "        self.x = 0\n"
        "    def compute(self):\n"
        "        return self.x * 2\n"
    )
    result = skeletonize_python(source)
    assert "class MyClass:" in result
    assert "def __init__(self):" in result
    assert "def compute(self):" in result
    assert "self.x = 0" not in result
    assert "return self.x * 2" not in result
    assert "        ..." in result


def test_skeletonize_top_level_and_class():
    source = (
        "def helper(x):\n"
        "    return x + 1\n"
        "\n"
        "class Foo:\n"
        "    def run(self):\n"
        "        pass\n"
    )
    result = skeletonize_python(source)
    assert "def helper(x):" in result
    assert "return x + 1" not in result
    assert "class Foo:" in result
    assert "def run(self):" in result
    assert "pass" not in result
