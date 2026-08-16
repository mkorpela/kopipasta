"""What the package promises to install into, checked against what it needs.

`requires-python` is a promise, and the classifiers repeat it three times. A
promise nobody exercises is how `kopipasta config --show` came to be broken on
every 3.10 machine that had not also installed mypy.
"""

import pathlib
import sys

import pytest

PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - depends on interpreter
    tomli = pytest.importorskip("tomli")
    tomllib = tomli


def _project():
    with open(PYPROJECT, "rb") as fh:
        return tomllib.load(fh)["project"]


def test_reading_toml_is_possible_on_every_supported_python():
    """The defect: `tomllib` is 3.11+, `requires-python` is >=3.10, and the
    3.10 fallback (`tomli`) was not a dependency.

    Every development environment had `tomli` anyway, because mypy depends on
    it below 3.11 — so the one configuration that mattered was the only one
    nobody ever ran. A fresh 3.10 install could not read config.toml, which
    means it could not resolve a backend, which means `ask` could not run.
    """
    deps = _project()["dependencies"]
    conditional = [d for d in deps if d.replace(" ", "").startswith("tomli")]
    assert conditional, (
        "python_requires allows 3.10, where `import tomllib` fails and the "
        "config file becomes unreadable"
    )
    assert "python_version" in conditional[0], (
        "pin it to the versions that need it; 3.11+ has tomllib in the stdlib"
    )


def test_toml_actually_loads_here():
    """The end of the chain, not the declaration: whichever interpreter is
    running this must be able to parse the config file format."""
    assert _project()["name"] == "kopipasta"


def test_the_supported_versions_are_stated_once_and_agree():
    """`requires-python` and the classifiers are two hand-maintained lists of
    the same fact, and they drift silently: a classifier is metadata nobody
    installs against, so nothing fails when it lags."""
    project = _project()
    floor = project["requires-python"]
    classified = {
        c.rsplit("::", 1)[1].strip()
        for c in project["classifiers"]
        if c.startswith("Programming Language :: Python :: 3.")
    }
    assert floor.startswith(">=")
    assert floor[2:].strip() == min(
        classified, key=lambda v: tuple(map(int, v.split(".")))
    ), f"requires-python says {floor} and the classifiers start at {sorted(classified)}"
