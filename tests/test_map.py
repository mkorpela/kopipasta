"""`kopipasta map` — the skeleton, without a model call. Spec §3, §5, §8.

`map` is the one verb with no backend, no session and no network, so every
test here is the real code path. What it has to get right is narrow and easy
to get wrong quietly:

- it renders *one* thing (a skeleton), whatever flag selected the file;
- a file that lost its symbols to `--budget` still appears, because a map that
  omits a file tells the caller the file does not exist;
- the size it reports is the size of what it actually printed.
"""

import json

import pytest

from kopipasta.core import map as mapmod
from kopipasta.core.errors import EXIT_BUDGET, EXIT_OK, EXIT_USAGE


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        'def add(a, b):\n    """Add two numbers."""\n    return a + b\n\n\n'
        'class Calculator:\n    """A calculator."""\n\n    def run(self):\n        pass\n'
    )
    (tmp_path / "src" / "main.py").write_text(
        'def main():\n    """Entry point."""\n    print("hi")\n'
    )
    (tmp_path / "README.md").write_text("# Notes\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    from kopipasta import file as filemod

    filemod._is_ignored_cache.clear()
    filemod._is_binary_cache.clear()
    filemod._gitignore_cache.clear()
    return tmp_path


def run(*argv, expect=EXIT_OK):
    code = mapmod.run(list(argv))
    assert code == expect, f"expected exit {expect}, got {code}"
    return code


def run_json(capsys, *argv, expect=EXIT_OK):
    run(*argv, "--json", expect=expect)
    return json.loads(capsys.readouterr().out)


# -- what it maps -----------------------------------------------------------


def test_no_selectors_maps_the_whole_project(project, capsys):
    """Spec §3 calls it the cheap whole-repo map, so the bare command is the
    common case rather than an empty selection."""
    data = run_json(capsys)
    assert set(data["map"]) == {".gitignore", "README.md", "src/calc.py", "src/main.py"}


def test_symbols_come_out_and_unparseable_files_are_listed_bare(project, capsys):
    data = run_json(capsys)
    assert any("def add(" in s for s in data["map"]["src/calc.py"])
    assert any("class Calculator" in s for s in data["map"]["src/calc.py"])
    # Still listed. A file with no symbols is one line, which is what it is worth.
    assert data["map"]["README.md"] == []


def test_positional_paths_select(project, capsys):
    data = run_json(capsys, "src")
    assert set(data["map"]) == {"src/calc.py", "src/main.py"}


def test_every_role_renders_as_a_skeleton(project, capsys):
    """A verb named for one rendering must not quietly produce three.

    `-e` means full content in `ask`. Here it selects a file and nothing more,
    so `map -e` and `map -m` are the same map.
    """
    edit = run_json(capsys, "-e", "src/calc.py")
    mapped = run_json(capsys, "-m", "src/calc.py")
    assert edit["map"] == mapped["map"]
    assert "return a + b" not in json.dumps(edit["map"])


def test_exclude_still_wins(project, capsys):
    data = run_json(capsys, "src", "-x", "src/main.py")
    assert set(data["map"]) == {"src/calc.py"}


def test_a_pattern_that_matches_nothing_is_a_usage_error(project):
    run("src/nope*.py", expect=EXIT_USAGE)


# -- the budget, spec §5 ----------------------------------------------------


@pytest.fixture
def big(project):
    for i in range(40):
        (project / "src" / f"mod{i}.py").write_text(
            f"def function_number_{i}(argument_one, argument_two):\n"
            f'    """Do the {i}th thing, at some length so the skeleton costs something."""\n'
            f"    return argument_one + argument_two\n"
        )
    return project


def test_over_budget_costs_symbols_and_never_the_path(big, capsys):
    """Nothing vanishes silently. A demoted file keeps its line and loses its
    symbols — the alternative is a map that says a file does not exist."""
    full = run_json(capsys)
    tight = run_json(capsys, "--budget", "300")
    assert set(tight["map"]) == set(full["map"])
    assert tight["path_only"]
    assert all(tight["map"][p] == [] for p in tight["path_only"])
    assert tight["symbols"] < full["symbols"]


def test_the_reported_size_is_the_size_of_what_was_printed(big, capsys):
    """The ladder works from file sizes and cannot see the path line every
    file costs. Without the corrective pass a `--budget 900` map came back at
    ~2,656 tokens and reported it as a success."""
    data = run_json(capsys, "--budget", "900")
    assert data["est_tokens"] <= 900
    run("--budget", "900")
    printed = capsys.readouterr().out
    assert data["chars"] == len(printed.rstrip("\n"))


def test_strict_budget_reports_the_size_before_the_ladder_ran(big, capsys):
    """`demote_to_fit` mutates the selection, so measuring afterwards reports
    the size that *did* fit: "needs ~1,878 tokens, over the 2,000 budget"."""
    run("--budget", "900", "--strict-budget", "--json", expect=EXIT_BUDGET)
    err = json.loads(capsys.readouterr().out)
    assert err["error"] == "budget_exceeded"
    assert err["wanted_tokens"] > err["budget_tokens"] == 900


def test_a_budget_under_the_path_list_still_prints_every_path(big, capsys):
    run("--budget", "1", "--json")
    captured = capsys.readouterr()  # one read: it clears both streams
    data = json.loads(captured.out)
    assert data["symbols"] == 0
    assert len(data["map"]) == data["files"] > 40
    assert "still over" in captured.err


# -- the output contract, spec §8 -------------------------------------------


def test_stdout_is_the_artifact_and_everything_else_is_stderr(project, capsys):
    run()
    captured = capsys.readouterr()
    assert captured.out.startswith(".gitignore\n")
    assert "kopipasta:" not in captured.out
    assert "files, " in captured.err


def test_map_records_nothing(project, capsys):
    """No session, no cache, no network: there is nothing to record, because
    nothing was asked and nothing was spent."""
    run()
    assert not (project / ".kopipasta").exists()
