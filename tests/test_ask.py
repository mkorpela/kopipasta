"""`kopipasta ask`, end to end, through the `none` backend — spec §3-§9.

The `none` backend is what makes this possible: it runs selection, the budget
ladder, rendering, the session record and the whole output contract exactly as
a real run does, and hands the assembled payload back instead of calling a
model. Everything below therefore exercises the real code path, with no key,
no network and no bill.

Each test pins a behaviour that is expensive to get wrong at the far end of a
subprocess: a wrong exit code sends a harness to retry a permanent failure, a
silently withheld file produces a confident answer about code nobody read.
"""

import json
import os

import pytest

from kopipasta.core import ask as askmod
from kopipasta.core import budget as budgetmod
from kopipasta.core.errors import EXIT_BUDGET, EXIT_NO_BACKEND, EXIT_OK, EXIT_USAGE
from kopipasta.interaction import EXIT_NO_HUMAN


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A small repo, with git present so the project root resolves to it."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'
    )
    (tmp_path / "src" / "main.py").write_text(
        'from src.calc import add\n\n\ndef main():\n    """Entry point."""\n    print(add(1, 2))\n'
    )
    (tmp_path / "README.md").write_text("# Notes\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    # No test may reach a provider, however the developer's shell is set up.
    for var in (
        "KOPIPASTA_BACKEND",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    # Clear the per-path caches in kopipasta.file, which are process-global and
    # would otherwise carry answers about one tmp_path into the next test.
    from kopipasta import file as filemod

    filemod._is_ignored_cache.clear()
    filemod._is_binary_cache.clear()
    filemod._gitignore_cache.clear()
    return tmp_path


@pytest.fixture
def canned(project):
    """A small, realistic triage answer, so a session's replay stays small.

    Kept outside the project so it cannot join an --all selection and change
    the counts the tests assert on.
    """
    path = project.parent / "canned-answer.json"
    path.write_text(
        json.dumps(
            {
                "relevant_files": [
                    {"path": "src/calc.py", "why": "owns addition", "confidence": 0.9}
                ],
                "hypothesis": "addition is wrong",
                "missing_context": [],
                "suggested_selection": ["src/calc.py"],
            }
        )
    )
    return f"none:{path}"


def run(*argv, expect=EXIT_OK, backend="none"):
    code = askmod.run(["--backend", backend, *argv])
    assert code == expect, f"expected exit {expect}, got {code}"
    return code


def run_json(capsys, *argv, expect=EXIT_OK, backend="none"):
    run(*argv, "--json", expect=expect, backend=backend)
    return json.loads(capsys.readouterr().out)


# -- selection, spec §4 -----------------------------------------------------


def test_roles_are_rendered_in_their_own_zones(project, capsys):
    run("-e", "src/calc.py", "-r", "src/main.py", "-q", "why")
    out = capsys.readouterr().out
    assert "## Active Workspace (Editable)" in out
    assert "## Reference Context (Read-Only)" in out
    assert out.index("Active Workspace") < out.index("src/calc.py")
    assert out.index("Reference Context") < out.index("src/main.py")


def test_the_most_detailed_role_wins_whatever_the_flag_order(project, capsys):
    """Documented as order-independent, so it has to be resolved by precedence."""
    first = run_json(capsys, "-m", "src/*.py", "-e", "src/calc.py", "-q", "x")
    second = run_json(capsys, "-e", "src/calc.py", "-m", "src/*.py", "-q", "x")
    assert first["sent"]["edit"] == second["sent"]["edit"] == 1
    assert first["sent"]["map"] == second["sent"]["map"] == 1


def test_exclude_is_applied_last_and_beats_edit(project, capsys):
    data = run_json(capsys, "-e", "src/*.py", "-x", "src/main.py", "-q", "x")
    assert data["sent"]["edit"] == 1


def test_a_glob_that_matches_nothing_is_reported_but_not_fatal(project, capsys):
    data = run_json(capsys, "-e", "src/calc.py", "-r", "src/nope*.py", "-q", "x")
    assert data["unmatched"] == [{"flag": "-r", "pattern": "src/nope*.py"}]
    assert data["ok"] is True


def test_an_empty_selection_is_an_error_with_a_suggestion(project, capsys):
    """The most dangerous failure in the tool: a typo'd glob selects nothing,
    the model answers from the structure alone, and the answer reads fine."""
    data = run_json(capsys, "-e", "src/calk.py", "-q", "x", expect=EXIT_USAGE)
    assert data["error"] == "empty_selection"
    assert data["retryable"] is False
    assert "src/calc.py" in data["detail"]  # did you mean


def test_no_selectors_at_all_names_the_flags_that_fix_it(project, capsys):
    data = run_json(capsys, "-q", "x", expect=EXIT_USAGE)
    assert data["error"] == "usage"
    assert "--all" in data["hint"] and "-e" in data["hint"]


def test_at_file_expands_into_patterns(project, capsys):
    (project / "sel.txt").write_text(
        "# from a triage answer\nsrc/calc.py\nsrc/main.py\n"
    )
    data = run_json(capsys, "-r", "@sel.txt", "-q", "x")
    assert data["sent"]["ref"] == 2


def test_from_file_closes_the_loop(project, capsys):
    (project / "sel.txt").write_text("src/calc.py\n")
    data = run_json(capsys, "--from-file", "sel.txt", "-q", "x")
    assert data["sent"]["ref"] == 1


def test_gitignored_and_binary_files_never_enter_a_payload(project, capsys):
    (project / "secret.bin").write_bytes(b"\x00\x01binary")
    (project / "build").mkdir()
    (project / "build" / "junk.py").write_text("x = 1\n")
    (project / ".gitignore").write_text("build/\n")
    data = run_json(capsys, "--all", "-q", "x")
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert "junk.py" not in payload  # gitignored: absent even from the tree
    assert "binary" not in payload  # the bytes of a binary file are never read


# -- the payload contains what it says it contains, spec §6 -----------------


def test_the_payload_carries_the_structure_tree_as_json(project, capsys):
    """The tree is the anti-hallucination device: it is how the model knows
    which files exist, and therefore which ones it was *not* given."""
    data = run_json(capsys, "-e", "src/calc.py", "-q", "x")
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert "## Project Structure" in payload
    start = payload.index("```json", payload.index("## Project Structure")) + len(
        "```json"
    )
    tree = json.loads(payload[start : payload.index("```", start)].strip())
    # Every non-ignored file, not just the selected ones.
    assert sorted(tree["src"]) == ["calc.py", "main.py"]
    assert "README.md" in tree
    # A skeleton lives in the tree; a file sent under a zone heading does not
    # repeat its symbols there.
    assert tree["src"]["calc.py"] == []


def test_a_mapped_file_carries_its_skeleton_in_the_tree(project, capsys):
    data = run_json(capsys, "-m", "src/calc.py", "-q", "x")
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert "def add(a, b)" in payload
    assert "Add two numbers." in payload


@pytest.mark.parametrize(
    "name, body",
    [
        ("notes.md", "# Notes\n\nWHOLE_POINT_OF_THE_FILE\n"),  # no AST at all
        ("schema.sql", "CREATE TABLE t (id int); -- WHOLE_POINT_OF_THE_FILE\n"),
        ("src/broken.py", "def WHOLE_POINT_OF_THE_FILE(:\n"),  # will not parse
        ("src/consts.py", "WHOLE_POINT_OF_THE_FILE = 1\n"),  # no top-level defs
    ],
)
def test_a_named_file_with_no_skeleton_still_reaches_the_model(
    project, capsys, name, body
):
    """`-m` is the one role whose rendering does not exist for every file.

    A MAP entry with no symbols contributes nothing at all: it shows in the
    structure tree as `[]`, which the legend defines as "not sent". So this
    reported `map: 1`, wrote the file into selection.json, and sent the model
    zero bytes of it — a selected file the caller was told had been sent.
    """
    (project / name).write_text(body)
    data = run_json(capsys, "-m", name, "-q", "x")
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert "WHOLE_POINT_OF_THE_FILE" in payload
    # Counted as what it actually is, and named rather than left to inference.
    assert data["sent"]["map"] == 0
    assert data["sent"]["snippet"] == 1
    assert data["unmappable"] == [name]


def test_every_selected_file_appears_under_a_zone_heading(project, capsys):
    """The payload's own legend tells the model that a file it cannot find
    under a zone heading was never sent. That has to be true of every file the
    report claims to have sent."""
    (project / "docs").mkdir()
    (project / "docs" / "spec.md").write_text("# Spec\n")
    (project / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    data = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "-r",
        "src/main.py",
        "-m",
        "docs/spec.md",
        "-m",
        "pyproject.toml",
        "-q",
        "x",
    )
    payload = (project / data["request"]).read_text(encoding="utf-8")
    blocks = {
        line.split(":", 1)[1].split("(")[0].strip()
        for line in payload.splitlines()
        if line.startswith("# FILE:")
    }
    assert blocks == {"src/calc.py", "src/main.py", "docs/spec.md", "pyproject.toml"}
    # And the counts add up to what is in the payload, so `sent` can be trusted
    # without opening the request.
    assert sum(data["sent"][r] for r in ("edit", "ref", "map", "snippet")) == len(
        blocks
    )


def test_the_state_directory_can_never_join_an_all_selection(project, capsys):
    """`--all` sends whole files now, so this must not depend on a text file.

    `.kopipasta/` was excluded only by the line kopipasta writes into
    `.gitignore`. Revert that line, keep it in `.git/info/exclude` instead, or
    have the write fail, and `--all` would sweep the whole conversation back
    in as source — every previous prompt and response, in full, growing
    quadratically, with the model reading its own earlier output as code.
    While `--all` meant skeletons this was invisible: a `.md` mapped to `[]`.
    """
    state = project / ".kopipasta" / "sessions" / "old"
    state.mkdir(parents=True)
    (state / "001-response.md").write_text("A PREVIOUS ANSWER FROM THE MODEL\n")
    (project / ".gitignore").write_text("__pycache__/\n")  # no rule for it
    data = run_json(capsys, "--all", "-q", "x")
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert "A PREVIOUS ANSWER FROM THE MODEL" not in payload
    assert "001-response.md" not in payload  # absent even from the tree


# -- the budget ladder, spec §5 --------------------------------------------


def test_over_budget_files_walk_down_the_ladder_instead_of_vanishing(project, capsys):
    big = "\n".join(f"def f{i}():\n    return {i}" for i in range(400))
    (project / "src" / "big.py").write_text(big)
    data = run_json(
        capsys, "-r", "src/*.py", "-e", "src/calc.py", "--budget", "1k", "-q", "x"
    )
    demoted = {d["path"]: d for d in data["demoted"]}
    assert "src/big.py" in demoted
    # -e is never demoted: that is the contract of "editable".
    assert "src/calc.py" not in demoted
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert '"big.py"' in payload  # still named in the structure tree, not gone
    assert "def f399" not in payload  # ... but its body is not


def test_a_demotion_reports_the_rung_the_file_actually_landed_on(project, capsys):
    """The middle rung of the ladder does not exist for every file.

    A `.md` has no skeleton, so demoting it to one renders it to nothing while
    the report says a skeleton was sent. It goes straight to path-only, which
    is both the truth and the rung that frees the budget the caller asked for.
    """
    (project / "big.md").write_text("# Doc\n\n" + "prose prose prose\n" * 400)
    data = run_json(
        capsys, "-r", "big.md", "-e", "src/calc.py", "--budget", "1k", "-q", "x"
    )
    demoted = {d["path"]: d for d in data["demoted"]}
    assert demoted["big.md"]["to"] == "path-only"
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert '"big.md"' in payload  # still named in the structure tree
    assert "prose prose prose" not in payload


def test_there_is_no_budget_unless_one_is_asked_for(project, capsys):
    """Unbudgeted is the default, and nothing may quietly install one.

    The whole product is frontloading, so a cap the caller did not ask for is
    the tool withholding the thing it exists to provide — and withholding it
    silently, since a demotion nobody requested still reads as `ok: true`.
    Pinned because the natural way to break it is a config-file default, and
    argparse would not say a word.
    """
    big = "\n".join(f"def f{i}():\n    return {i}" for i in range(4000))
    (project / "src" / "big.py").write_text(big)
    data = run_json(capsys, "--all", "-q", "x")
    assert "demoted" not in data
    assert data["sent"]["demoted"] == 0
    assert data["sent"]["map"] == 0  # nothing fell to a skeleton
    assert "def f3999" in (project / data["request"]).read_text(encoding="utf-8")


def test_the_unbudgeted_default_survives_the_config_file(project, capsys, monkeypatch):
    """A budget is a per-question decision, not an operator setting. Spec §6
    puts the model in config and leaves the size of the question out of it."""
    from kopipasta.core import config as cfgmod

    cfg = cfgmod.resolve_backend("ask", flag="none")
    assert not hasattr(cfg, "budget"), "a config-resolved budget would apply silently"
    assert askmod.build_parser().get_default("budget") is None


def test_strict_budget_refuses_instead_of_demoting(project, capsys):
    big = "\n".join(f"def f{i}():\n    return {i}" for i in range(400))
    (project / "src" / "big.py").write_text(big)
    data = run_json(
        capsys,
        "-r",
        "src/*.py",
        "--budget",
        "1k",
        "--strict-budget",
        "-q",
        "x",
        expect=EXIT_BUDGET,
    )
    assert data["error"] == "budget_exceeded"
    assert data["demoted"]  # names what it would have demoted


def test_strict_budget_is_enforced_by_the_corrective_pass(project, capsys):
    """The case only the corrective pass can catch, and the one dogfooding found.

    The ladder works from file sizes and cannot see the structure blob or the
    mode instructions, so a selection can fit the estimate and not the
    rendered payload. When demoting would bring it back under, the guard on
    the *rendered* size never fires — so if the corrective pass does not obey
    --strict-budget, the run demotes silently and exits 0, which is precisely
    what the flag exists to prevent.

    The budget is derived from two measured renders rather than hardcoded, so
    the test cannot quietly stop covering this as the templates change.
    """
    body = "\n\n".join(f'def pad{i}():\n    return "{"x" * 60}"' for i in range(12))
    (project / "src" / "pad.py").write_text(body + "\n")
    skeleton = run_json(capsys, "-m", "src/pad.py", "-q", "x")["est_input_tokens"]
    full = run_json(capsys, "-r", "src/pad.py", "-q", "x")["est_input_tokens"]
    budget = (skeleton + full) // 2
    assert skeleton < budget < full, "the fixture no longer sets up the case"

    lenient = run_json(capsys, "-r", "src/pad.py", "--budget", str(budget), "-q", "x")
    assert lenient["sent"]["demoted"] == 1  # without the flag: demote and carry on
    assert lenient["est_input_tokens"] <= budget  # ... and it really does fit

    data = run_json(
        capsys,
        "-r",
        "src/pad.py",
        "--budget",
        str(budget),
        "--strict-budget",
        "-q",
        "x",
        expect=EXIT_BUDGET,
    )
    assert data["error"] == "budget_exceeded"
    assert data["demoted"] == ["src/pad.py"]


def test_a_bad_budget_string_says_what_it_wanted(project, capsys):
    data = run_json(capsys, "--all", "--budget", "lots", "-q", "x", expect=EXIT_USAGE)
    assert "--budget" in data["summary"]


def test_the_estimate_follows_the_provider_that_will_read_it(
    project, capsys, monkeypatch
):
    """One global chars/token cannot serve two tokenizers 50% apart.

    Measured on this repo: Anthropic 2.50 chars/token, Gemini 3.42-3.87. With
    a single pessimistic constant a `--budget 400k` Gemini run shipped ~273k
    real tokens — the ladder demoted about a third of what would have fit, in
    a tool whose entire product is frontloading.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    claude = run_json(capsys, "--all", "-q", "x", "--dry-run", "--backend", "anthropic")
    gemini = run_json(capsys, "--all", "-q", "x", "--dry-run", "--backend", "gemini")

    # Compared as chars-per-token so the assertion survives the two prompt
    # templates being different lengths — the ratio is the thing under test.
    assert claude["payload_chars"] / claude["est_input_tokens"] == pytest.approx(
        2.5, abs=0.02
    )
    assert gemini["payload_chars"] / gemini["est_input_tokens"] == pytest.approx(
        3.4, abs=0.02
    )


def test_a_dry_run_sizes_for_the_real_provider_with_no_backend_configured(
    project, capsys, monkeypatch
):
    """--dry-run must not need a key, and must still answer the question.

    Sizing the payload is the whole point of a dry run, so resolving the
    planned provider cannot be allowed to fail the run — but when it does
    resolve, its tokenizer is the one that matters.
    """
    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    data = run_json(capsys, "--all", "-q", "x", "--dry-run")
    assert data["est_input_tokens"] > 0


def test_strict_budget_refuses_on_a_counted_number_not_a_guess(
    project, capsys, monkeypatch
):
    """The one decision worth a round trip (spec §5).

    Everywhere else the estimate only chooses what to demote. Under
    --strict-budget it chooses between running and exiting 6, and the flag's
    whole promise is "refuse rather than overshoot" — a promise no heuristic
    can keep, whatever its calibration.

    Overshoot only: a run refused before the payload is rendered is refused on
    the estimate, because there is nothing to count yet. The ratio is the
    lowest measured for that reason.
    """
    real_build = askmod.build
    counted = {}

    def fake_build(cfg, **kw):
        backend = real_build(cfg, **kw)
        # A provider whose tokenizer disagrees with the heuristic, loudly.
        backend.count_tokens = lambda text: counted.setdefault("n", 999_999)
        return backend

    monkeypatch.setattr(askmod, "build", fake_build)

    # An estimate far under the budget; the real count far over it.
    data = run_json(
        capsys,
        "-r",
        "src/calc.py",
        "--budget",
        "500k",
        "--strict-budget",
        "-q",
        "x",
        expect=EXIT_BUDGET,
    )
    assert counted["n"] == 999_999, "the count was never asked for"
    assert data["error"] == "budget_exceeded"
    assert data["wanted_tokens"] == 999_999  # the measured number, not the guess


def test_a_backend_that_cannot_count_still_honours_strict_budget(project, capsys):
    """`none` has no tokenizer. The estimate is pessimistic, so falling back
    to it can only refuse early, never overshoot."""
    data = run_json(
        capsys, "-r", "src/calc.py", "--budget", "500k", "--strict-budget", "-q", "x"
    )
    assert data["ok"] is True


def test_the_budget_is_read_in_the_same_currency_as_the_payload(
    project, capsys, monkeypatch
):
    """`--budget 40kc` is a char budget: both sides convert with one ratio."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    data = run_json(
        capsys,
        "--all",
        "-q",
        "x",
        "--dry-run",
        "--backend",
        "gemini",
        "--budget",
        "1000kc",
    )
    assert data["sent"]["demoted"] == 0
    assert data["est_input_tokens"] == int(data["payload_chars"] / 3.4)


# -- the session, spec §7 ---------------------------------------------------


def test_a_turn_leaves_a_readable_record(project, capsys):
    data = run_json(capsys, "-e", "src/calc.py", "-q", "why does add exist?")
    session_dir = project / ".kopipasta" / "sessions" / data["session"]
    assert (session_dir / "001-request.md").exists()
    assert (session_dir / "001-response.md").exists()
    assert (session_dir / "001-meta.json").exists()
    assert (session_dir / "prefix.md").exists()
    transcript = (session_dir / "transcript.jsonl").read_text().strip().splitlines()
    assert json.loads(transcript[0])["question"] == "why does add exist?"


def test_the_state_directory_is_gitignored_on_first_write(project, capsys):
    run("-e", "src/calc.py", "-q", "x")
    assert ".kopipasta/" in (project / ".gitignore").read_text()


def test_an_unchanged_file_is_not_resent_on_the_next_turn(project, capsys, canned):
    run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "one", backend=canned)
    second = run_json(
        capsys, "-e", "src/calc.py", "--session", "s", "-q", "two", backend=canned
    )
    assert second["turn"] == 2
    assert second["sent"]["deduped"] == 1
    body = (project / second["request"]).read_text(encoding="utf-8")
    assert "prefix: prefix.md" in body  # referenced, not repeated
    assert "def add" not in body


def test_a_changed_file_is_resent_and_marked_as_superseding(project, capsys, canned):
    run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "one", backend=canned)
    (project / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b  # fixed\n"
    )
    second = run_json(
        capsys, "-e", "src/calc.py", "--session", "s", "-q", "two", backend=canned
    )
    body = (project / second["request"]).read_text(encoding="utf-8")
    assert second["sent"]["deduped"] == 0
    assert "supersedes" in body and "# fixed" in body


def test_a_role_change_defeats_dedup(project, capsys, canned):
    """Same bytes, different role. Deduping on the hash alone would answer
    `-e file` with the 50-line snippet turn 1 sent, and report it as sent."""
    run_json(capsys, "-s", "src/main.py", "--session", "s", "-q", "one", backend=canned)
    second = run_json(
        capsys, "-e", "src/main.py", "--session", "s", "-q", "two", backend=canned
    )
    assert second["sent"]["deduped"] == 0
    assert "now edit" in (project / second["request"]).read_text(encoding="utf-8")


def test_dedup_only_trusts_the_prefix_not_earlier_suffixes(project, capsys, canned):
    """A file that rode in turn 2's suffix is gone by turn 3: the suffix is not
    replayed. Treating it as still-present would withhold it from the model
    while the record claimed it was sent."""
    run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "one", backend=canned)
    run_json(
        capsys,
        "-e",
        "src/calc.py",
        "-e",
        "src/main.py",
        "--session",
        "s",
        "-q",
        "2",
        backend=canned,
    )
    third = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "-e",
        "src/main.py",
        "--session",
        "s",
        "-q",
        "3",
        backend=canned,
    )
    body = (project / third["request"]).read_text(encoding="utf-8")
    assert third["sent"]["deduped"] == 1  # only the one that is in the prefix
    assert "def main" in body


def test_a_skeleton_selected_on_a_later_turn_actually_reaches_the_model(
    project, capsys, canned
):
    """`sent` must never count a file the model did not get.

    Turn 2 only rendered full-text roles into its suffix, so a `-m` file first
    selected on turn 2 was reported under sent["map"] and never sent at all —
    a payload the caller believes contains a file it does not.
    """
    run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "one", backend=canned)
    second = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "-m",
        "src/main.py",
        "--session",
        "s",
        "-q",
        "two",
        backend=canned,
    )
    body = (project / second["request"]).read_text(encoding="utf-8")
    assert second["sent"]["map"] == 1
    assert "src/main.py" in body
    assert "def main()" in body  # the skeleton, not just the name
    assert "print(add(1, 2))" not in body  # ... and only the skeleton


def test_the_prefix_is_byte_identical_across_turns(project, capsys, canned):
    """It is the cache breakpoint. Re-rendering it would miss on every turn."""
    first = run_json(
        capsys, "-e", "src/calc.py", "--session", "s", "-q", "one", backend=canned
    )
    prefix = (
        project / ".kopipasta" / "sessions" / first["session"] / "prefix.md"
    ).read_bytes()
    (project / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b  # changed\n"
    )
    run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "two", backend=canned)
    assert (
        project / ".kopipasta" / "sessions" / first["session"] / "prefix.md"
    ).read_bytes() == prefix


def test_json_mode_never_resumes_someone_elses_conversation(project, capsys):
    """The `current` pointer is racy, so it is human-only (spec §7)."""
    first = run_json(capsys, "-e", "src/calc.py", "-q", "one")
    second = run_json(capsys, "-e", "src/calc.py", "-q", "two")
    assert first["session"] != second["session"]
    assert second["turn"] == 1


def test_the_current_pointer_is_written_even_under_json(project, capsys):
    """Write always, follow never — two rules that were one.

    Spec §1's third workflow is `ask --json` followed by `apply current`, and
    §7 says the pointer is written on every run. Not writing it under --json
    made the flagship agent workflow impossible: the only mode that produces a
    patch artifact was the only mode that left no handle to it.

    The refusal to *follow* it is a separate question and stays as it was —
    the test above pins that half.
    """
    data = run_json(capsys, "-e", "src/calc.py", "-q", "x")
    pointer = project / ".kopipasta" / "current"
    assert pointer.exists(), "no `current` for `apply current` to resolve"
    assert pointer.read_text().strip() == data["session"]


def test_a_follow_up_turn_records_the_context_it_inherited(project, capsys, canned):
    """`apply` reads the latest turn to learn the editable set (spec §11).

    A follow-up turn with no selectors inherits the whole prefix, but recorded
    `files: {}` — which tells `apply` that nothing was editable, so it either
    rejects every patch or treats "empty" as "unrestricted" and allows any.
    """
    run_json(
        capsys,
        "-e",
        "src/calc.py",
        "-r",
        "src/main.py",
        "--session",
        "s",
        "-q",
        "one",
        backend=canned,
    )
    run_json(capsys, "--session", "s", "-q", "two", backend=canned)

    record = json.loads((project / ".kopipasta/sessions/s/selection.json").read_text())
    assert record["2"]["files"], "turn 2 recorded no files at all"
    assert record["2"]["files"]["src/calc.py"]["role"] == "edit"
    assert record["2"]["files"]["src/main.py"]["role"] == "ref"


def test_a_follow_up_turn_records_the_role_it_changed(project, capsys, canned):
    """This turn's role wins over the inherited one, or the record describes
    a context that no longer exists."""
    run_json(capsys, "-r", "src/calc.py", "--session", "s", "-q", "one", backend=canned)
    run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "two", backend=canned)

    record = json.loads((project / ".kopipasta/sessions/s/selection.json").read_text())
    assert record["1"]["files"]["src/calc.py"]["role"] == "ref"
    assert record["2"]["files"]["src/calc.py"]["role"] == "edit"


def test_a_second_ask_does_not_silently_continue_the_first(project, capsys, canned):
    """spec §7: a disposable context oracle is disposable by default.

    Resumption used to be implicit whenever --json was off, so two unrelated
    questions typed a minute apart shared one context — and the second answer
    was shaped by the first question's files. That is the context pollution the
    tool exists to keep out of the caller's window.
    """
    run("-e", "src/calc.py", "-q", "one", backend=canned)
    first = (project / ".kopipasta" / "current").read_text().strip()
    run("-e", "src/calc.py", "-q", "two", backend=canned)
    second = (project / ".kopipasta" / "current").read_text().strip()

    assert first != second
    assert len(list((project / ".kopipasta" / "sessions").iterdir())) == 2


def test_continue_resumes_the_pointer(project, capsys, canned):
    """The replacement for implicit resumption: you have to ask for it."""
    run("-e", "src/calc.py", "-q", "one", backend=canned)
    first = (project / ".kopipasta" / "current").read_text().strip()
    run("--continue", "-q", "two", backend=canned)

    assert (project / ".kopipasta" / "current").read_text().strip() == first
    record = json.loads(
        (project / ".kopipasta" / "sessions" / first / "selection.json").read_text()
    )
    assert "2" in record, "the follow-up did not land in the same session"


def test_continue_is_honoured_under_json_because_it_is_explicit(
    project, capsys, canned
):
    """--json refuses to *guess* at `current`; it does not refuse to be told.

    The raciness argument is about an agent that omitted --session inheriting
    someone else's conversation. An agent that passed --continue asked for it.
    """
    first = run_json(capsys, "-e", "src/calc.py", "-q", "one", backend=canned)
    second = run_json(capsys, "--continue", "-q", "two", backend=canned)
    assert second["session"] == first["session"]
    assert second["turn"] == 2


def test_continue_with_no_previous_session_is_a_usage_error(project, capsys):
    """Nothing to continue is a wrong command line, not a fresh session:
    silently starting one would answer a follow-up question with no context
    and look exactly like success."""
    data = run_json(
        capsys, "-e", "src/calc.py", "--continue", "-q", "x", expect=EXIT_USAGE
    )
    assert data["error"] == "usage"


def test_continue_and_session_cannot_both_be_given(project, capsys):
    from kopipasta.core.errors import UsageError

    with pytest.raises(UsageError):
        askmod.build_parser().parse_args(["--continue", "--session", "s", "-q", "x"])


# -- patch mode: two different failures that look alike --------------------


def _canned(project, text, name="canned.md"):
    path = project.parent / name
    path.write_text(text)
    return f"none:{path}"


def test_a_response_with_no_patch_content_blames_the_backend(project, capsys):
    """The `claude -p` failure: it reached for its own edit tool instead of
    emitting a patch. The fix is to disable the backend's tools."""
    backend = _canned(project, "I have made the change using my editing tool.")
    data = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "--mode",
        "patch",
        "-q",
        "x",
        backend=backend,
        expect=3,
    )
    assert data["error"] == "backend_not_a_completion"


def test_an_unfenced_patch_is_applied_rather_than_rejected(project, capsys):
    """Found by dogfooding: gemini returned a perfectly good search/replace
    patch with no ``` fence, and was told to disable its file and shell tools.

    It has none — it is a raw API call. The response was not a refusal, it was
    exactly what was asked for in a format the parser could not find the edges
    of. Demanding the fence in the template fixed the symptom by asking the
    model to be careful; the parser reading it is the fix (spec §14).
    """
    backend = _canned(
        project,
        "# FILE: src/calc.py\n"
        "<<<<<<< SEARCH\n    return a + b\n=======\n    return a + b + 0\n"
        ">>>>>>> REPLACE\n",
    )
    data = run_json(
        capsys, "-e", "src/calc.py", "--mode", "patch", "-q", "x", backend=backend
    )
    assert data["patches_proposed"] == 1


# -- proposing is not applying, and the envelope has to say which -----------


def _one_patch(project):
    return _canned(
        project,
        "# FILE: src/calc.py\n"
        "<<<<<<< SEARCH\n    return a + b\n=======\n    return a + b + 0\n"
        ">>>>>>> REPLACE\n",
    )


def test_a_proposed_patch_is_counted_as_proposed_not_applied(project, capsys):
    """Field report 2.5.

    `"patches": 3` beside `"ok": true` reads as "3 patches applied". Nothing
    had been written and `git status` was clean. The separation of proposal
    from application is the best thing about this tool; the field name was
    undercutting it. `patches_applied` is the same type as `patches_proposed`
    so the two can be compared without knowing which verb produced them —
    `applied` is already a list of paths over in `apply`, and reusing the name
    for a boolean would be worse than the ambiguity it fixed.
    """
    data = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "--mode",
        "patch",
        "-q",
        "x",
        backend=_one_patch(project),
    )
    assert data["patches_proposed"] == 1
    assert data["patches_applied"] == 0
    assert "patches" not in data, (
        "the ambiguous name must not linger beside the clear one"
    )
    assert (project / "src" / "calc.py").read_text().endswith("return a + b\n"), (
        "ask wrote to the worktree"
    )


def test_a_proposed_patch_says_what_would_apply_it(project, capsys):
    """An agent that has just been handed a proposal needs one thing next, and
    it is a command, not a field."""
    data = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "--mode",
        "patch",
        "-q",
        "x",
        backend=_one_patch(project),
    )
    assert data["next"].startswith("kopipasta apply")
    assert data["session"] in data["next"]


def test_a_prose_answer_has_no_patch_counts_at_all(project, capsys, canned):
    """Absent is a clearer answer than zero for a mode that cannot propose."""
    data = run_json(capsys, "-e", "src/calc.py", "-q", "x", backend=canned)
    assert "patches_proposed" not in data
    assert "patches_applied" not in data


# -- the output budget has to fit what the mode is asked to produce --------


def test_the_output_budget_is_not_a_relic_of_a_smaller_era(project, capsys, canned):
    """Field report 2.4.

    The first patch call in the field died at the 8192 default having produced
    9,340 characters — one wasted call, and the failure arrives only after the
    whole payload has been sent and billed. 8192 was never a considered
    number: Google's own console defaults this model to 65536, and reasoning
    tokens spend the same allowance, so the shipped ceiling was eight times
    below what the vendor treats as ordinary.

    A cap is not a bill. Providers charge for tokens produced, not permitted,
    so the only cost of the higher number is the ceiling on a runaway — and
    that is bounded by `--timeout` and the deadline anyway.
    """
    data = run_json(
        capsys, "-e", "src/calc.py", "--mode", "answer", "-q", "x", backend=canned
    )
    assert data["max_tokens"] == 65536


def test_every_mode_gets_the_same_room_to_answer_in(project, capsys, canned):
    """No per-mode floor. It was written to compensate for a global default
    that was simply wrong, and machinery that exists to work around a bad
    constant is worse than fixing the constant."""
    prose = run_json(
        capsys, "-e", "src/calc.py", "--mode", "answer", "-q", "x", backend=canned
    )
    patch = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "--mode",
        "patch",
        "-q",
        "x",
        backend=_one_patch(project),
    )
    assert prose["max_tokens"] == patch["max_tokens"] == 65536


def test_an_explicit_max_tokens_still_wins(project, capsys):
    """The floor is a better default, not a policy. Someone who names a number
    has a reason, and overruling it would make the flag a lie."""
    data = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "--mode",
        "patch",
        "--max-tokens",
        "1000",
        "-q",
        "x",
        backend=_one_patch(project),
    )
    assert data["max_tokens"] == 1000


def _with_config(monkeypatch, tmp_path, body):
    cfg = tmp_path / "config.toml"
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.setattr("kopipasta.core.config.config_path", lambda: cfg)
    return cfg


def test_max_tokens_named_in_the_modes_own_section_wins(
    project, capsys, tmp_path, monkeypatch
):
    """The reason to lower it is a provider that cannot go this high.

    Anthropic and OpenAI hard-error above their model's output ceiling, so
    `[patch] max_tokens = 32000` is the documented remedy, and it has to
    actually win.
    """
    _with_config(
        monkeypatch,
        tmp_path,
        '[ask]\nprovider = "none"\nmodel = ""\n[patch]\nmax_tokens = 4096\n',
    )
    data = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "--mode",
        "patch",
        "-q",
        "x",
        backend=_one_patch(project),
    )
    assert data["max_tokens"] == 4096


def test_the_shipped_config_does_not_pin_the_default_it_documents(tmp_path):
    """Writing the built-in default into the file turns a default into a
    decision, and nothing downstream can tell the two apart afterwards — most
    concretely, `config --show` then reports a number nobody chose as
    configured, and the next person to raise the default cannot reach anyone
    who has ever run `--edit-config`."""
    from kopipasta.core.config import DEFAULT_CONFIG

    assert "max_tokens  = 8192" not in DEFAULT_CONFIG
    assert "max_tokens" in DEFAULT_CONFIG, "still worth documenting as a knob"


# -- the mode picks the config section, as the file has always claimed -----


def test_a_mode_reads_its_own_config_section(project, capsys, tmp_path, monkeypatch):
    """`[patch] provider = "anthropic"` is in the shipped config file and in
    the README, and `ask` resolved `[ask]` unconditionally — so the one
    documented reason to have per-section config did nothing at all.

    A coordinated patch wants the strongest model; triage over a 400k
    frontload wants the big cheap window. That is the entire argument for
    sections existing.
    """
    _with_config(
        monkeypatch,
        tmp_path,
        '[ask]\nprovider = "none"\nmodel = ""\ntimeout_s = 900\n'
        '[patch]\nprovider = "exec"\nmodel = "echo hi"\n',
    )
    data = run_json(capsys, "-e", "src/calc.py", "-q", "x")
    assert data["backend"].startswith("none")

    # Resolved for the mode, not for the verb. `--backend` still overrides.
    from kopipasta.core.config import resolve_backend

    assert resolve_backend("patch", path=tmp_path / "config.toml").provider == "exec"


def test_a_mode_without_a_section_still_falls_back_to_ask(
    project, capsys, tmp_path, monkeypatch
):
    _with_config(
        monkeypatch, tmp_path, '[ask]\nprovider = "none"\nmodel = ""\ntimeout_s = 42\n'
    )
    data = run_json(capsys, "-e", "src/calc.py", "--mode", "explain", "-q", "x")
    assert data["backend"].startswith("none")


# -- the tool must not edit tracked files without saying so ----------------


def test_writing_the_gitignore_rule_is_announced(project, capsys):
    """Field report 2.6.

    The first run in a repo appends `.kopipasta/` to a tracked `.gitignore`.
    It is the right default — session records are not source — but it was the
    only tree change that run produced, and it happened in silence. A tool
    that edits a tracked file without saying so is one `git diff` away from
    looking like a bug in something else.
    """
    assert ".kopipasta/" not in (project / ".gitignore").read_text()
    run("-e", "src/calc.py", "-q", "x")
    err = capsys.readouterr().err
    # A specific sentence, not just the two words appearing somewhere: every
    # run mentions `.gitignore` and `.kopipasta/` in passing, so a loose
    # assertion here would pass without the feature existing.
    assert "added '.kopipasta/' to .gitignore" in err
    assert ".kopipasta/" in (project / ".gitignore").read_text()


def test_the_announcement_is_not_repeated_once_the_rule_is_there(project, capsys):
    """Narration that fires on every run is narration nobody reads."""
    run("-e", "src/calc.py", "-q", "x")
    capsys.readouterr()
    run("-e", "src/calc.py", "-q", "x")
    assert "added '.kopipasta/'" not in capsys.readouterr().err


def test_a_response_with_no_patch_is_a_format_problem_not_a_misconfigured_backend(
    project, capsys
):
    """ "It never tried" and "it tried and the format was wrong" are
    indistinguishable from a patch count of zero and need opposite responses.
    Sending a caller to reconfigure a backend that behaved correctly costs it
    the one thing it cannot get back."""
    # Markers, but no path to apply them to — the model described the change
    # and never said which file it belongs in. It tried; the format is wrong.
    backend = _canned(
        project,
        "Here is the fix:\n\n"
        "<<<<<<< SEARCH\n    return a + b\n=======\n    return a + b + 0\n"
        ">>>>>>> REPLACE\n",
    )
    data = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "--mode",
        "patch",
        "-q",
        "x",
        backend=backend,
        expect=5,
    )
    assert data["error"] == "unparseable_patch"
    assert data["retryable"] is True
    assert "```" in data["hint"], "the hint must name the thing that was missing"


def test_the_patch_template_asks_for_the_fence_the_parser_requires(project):
    """The template and the parser disagreed: it said "every code block starts
    with a path comment" and never said to fence it, so a compliant model
    produced output the parser skipped entirely."""
    from kopipasta.core import modes

    assert "```" in modes.PATCH.instructions


def test_a_session_id_cannot_escape_the_sessions_directory(project, capsys):
    data = run_json(
        capsys,
        "-e",
        "src/calc.py",
        "--session",
        "../../etc",
        "-q",
        "x",
        expect=EXIT_USAGE,
    )
    assert data["error"] == "usage"


# -- the output contract, spec §8 ------------------------------------------


def test_json_mode_puts_exactly_one_object_on_stdout(project, capsys):
    run("--all", "-q", "x", "--json")
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)  # would fail on any stray narration
    assert parsed["ok"] is True
    assert ".gitignore detected." not in captured.out  # library narration
    assert captured.err  # ... which is on stderr, where narration belongs


def test_the_artifact_reaches_stdout_when_reached_through_main(project, capsys):
    """`ask` establishes the output contract itself so it holds however it was
    reached — and `main` has already established it. A second scope that
    re-captured the handle would save the *redirected* stdout and send every
    artifact to stderr, with the exit code still reporting success. That is
    what `kopipasta ask --json > out.json` producing an empty file looks like.
    """
    from kopipasta import main as mainmod

    mainmod.main(["ask", "--backend", "none", "-e", "src/calc.py", "-q", "x", "--json"])
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert captured.err  # narration went the other way, as it should


def test_stdout_stays_empty_when_a_run_fails_without_json(project, capsys):
    """A partial artifact is worse than none: the caller cannot tell."""
    run("-e", "does-not-exist.py", "-q", "x", expect=EXIT_USAGE)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no files matched" in captured.err


def test_the_answer_is_the_artifact_and_the_bookkeeping_is_narration(project, capsys):
    run("-e", "src/calc.py", "-q", "x")
    captured = capsys.readouterr()
    assert "# Project Overview" in captured.out
    assert "session" in captured.err
    assert "session" not in captured.out.split("## Task Instructions")[0].lower()


def test_the_report_carries_pointers_not_payloads(project, capsys):
    data = run_json(capsys, "--all", "-q", "x")
    for key in ("session", "turn", "request", "response", "sent", "est_input_tokens"):
        assert key in data
    assert (project / data["request"]).exists()
    assert (project / data["response"]).exists()


def test_a_dry_run_says_so_rather_than_implying_an_answer(project, capsys):
    data = run_json(capsys, "-e", "src/calc.py", "-q", "x")
    assert data["dry_run"] is True
    assert "triage" not in data  # nothing answered, so nothing is claimed


# -- backend and configuration, spec §6/§9 ---------------------------------


def test_a_missing_key_names_the_provider_and_where_it_resolved_from(project, capsys):
    code = askmod.run(["--backend", "gemini:some-model", "--all", "-q", "x", "--json"])
    assert code == EXIT_NO_BACKEND
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "no_api_key"
    assert data["missing_env"] == "GEMINI_API_KEY"
    assert data["retryable"] is False


def test_an_unknown_provider_suggests_the_right_one(project, capsys):
    code = askmod.run(["--backend", "gemni:x", "--all", "-q", "x", "--json"])
    assert code == EXIT_USAGE
    assert "gemini" in json.loads(capsys.readouterr().out)["hint"]


def test_no_question_and_no_human_refuses_with_its_own_exit_code(project, capsys):
    """Exit 8, not 1: the caller needs a different invocation, not a fix."""
    code = askmod.run(["--backend", "none", "--all", "--json"])
    assert code == EXIT_NO_HUMAN
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "interaction_required"
    assert data["retryable"] is False


def test_the_question_can_come_from_a_file(project, capsys):
    (project / "task.md").write_text("Where does expiry live?\n")
    data = run_json(capsys, "-e", "src/calc.py", "-q", "@task.md")
    assert "Where does expiry live?" in (project / data["request"]).read_text(
        encoding="utf-8"
    )


def test_a_canned_response_drives_the_parsing_path(project, capsys):
    """`none:<file>` answers with a file, which is how triage parsing and, in
    time, patch application get exercised without a provider."""
    (project / "answer.json").write_text(
        json.dumps(
            {
                "relevant_files": [
                    {"path": "src/calc.py", "why": "owns addition", "confidence": 0.9}
                ],
                "hypothesis": "addition is wrong",
                "missing_context": [],
                "suggested_selection": ["src/calc.py"],
            }
        )
    )
    code = askmod.run(
        [
            "--backend",
            f"none:{project / 'answer.json'}",
            "-e",
            "src/calc.py",
            "-q",
            "x",
            "--json",
        ]
    )
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["triage"]["relevant_files"][0]["path"] == "src/calc.py"
    assert data["files_cited"] == ["src/calc.py"]


def test_a_mode_that_promised_json_and_got_prose_is_a_failed_call(project, capsys):
    """`ok: true` beside `triage: null` sends a caller that branched on `ok`
    into the rest of its task with no answer."""
    (project / "answer.txt").write_text("I could not say, really.")
    code = askmod.run(
        [
            "--backend",
            f"none:{project / 'answer.txt'}",
            "-e",
            "src/calc.py",
            "-q",
            "x",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    assert code != EXIT_OK
    assert data["ok"] is False
    assert data["error"] == "schema_invalid"
    assert data["retryable"] is True


def test_a_failure_still_reports_what_was_sent(project, capsys):
    """When the oracle is wrong the caller inherits the answer with none of
    the evidence, so the request path travels with the failure too."""
    (project / "answer.txt").write_text("nope")
    askmod.run(
        [
            "--backend",
            f"none:{project / 'answer.txt'}",
            "-e",
            "src/calc.py",
            "-q",
            "x",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    assert (project / data["request"]).exists()
    assert data["sent"]["edit"] == 1


def test_prose_modes_return_prose(project, capsys):
    (project / "answer.txt").write_text("It adds two numbers; see src/calc.py, above.")
    code = askmod.run(
        [
            "--backend",
            f"none:{project / 'answer.txt'}",
            "-e",
            "src/calc.py",
            "--mode",
            "explain",
            "-q",
            "x",
            "--json",
        ]
    )
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["answer_head"].startswith("It adds two numbers")
    assert data["files_cited"] == ["src/calc.py"]


def test_an_unknown_mode_lists_the_real_ones(project, capsys):
    data = run_json(capsys, "--all", "--mode", "triaje", "-q", "x", expect=EXIT_USAGE)
    assert "triage" in data["detail"]


def test_the_deadline_caps_the_whole_invocation(project, capsys, monkeypatch):
    data = run_json(capsys, "--all", "-q", "x", "--deadline", "0", expect=3)
    assert data["error"] == "deadline_exceeded"
    assert data["retryable"] is False


# -- secrets, spec §14 ------------------------------------------------------


def test_env_values_are_masked_without_asking(project, capsys):
    """This payload is on its way to a third-party API and the caller may be a
    process that cannot answer. Masking leaks nothing; a prompt would hang."""
    (project / ".env").write_text("API_TOKEN=sk-live-abcdefghijklmnop\n")
    (project / "src" / "conf.py").write_text('TOKEN = "sk-live-abcdefghijklmnop"\n')
    data = run_json(capsys, "-e", "src/conf.py", "-q", "x")
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert "sk-live-abcdefghijklmnop" not in payload
    assert "*" * 10 in payload


# -- the project constitution ----------------------------------------------


def test_ai_context_is_offered_to_the_oracle_and_can_be_declined(project, capsys):
    (project / "AI_CONTEXT.md").write_text("# Rules\nNever use threads.\n")
    with_ctx = run_json(capsys, "-e", "src/calc.py", "-q", "x")
    assert "Never use threads." in (project / with_ctx["request"]).read_text(
        encoding="utf-8"
    )
    without = run_json(capsys, "-e", "src/calc.py", "-q", "x", "--no-project-context")
    assert "Never use threads." not in (project / without["request"]).read_text(
        encoding="utf-8"
    )


# -- git selectors ----------------------------------------------------------


def test_changed_needs_git_and_says_so_when_it_is_missing(project, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    data = run_json(capsys, "--changed", "-q", "x", expect=EXIT_USAGE)
    assert "git" in data["summary"]


@pytest.mark.skipif(
    os.name == "nt", reason="git plumbing differs enough to be its own test"
)
def test_changed_selects_the_working_tree(project, capsys):
    import subprocess

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    (project / ".git").rmdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=project, check=True, env=env)
    (project / "src" / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    (project / "src" / "brand_new.py").write_text("x = 1\n")
    data = run_json(capsys, "--changed", "-q", "x")
    # Untracked files count: a file you just created is the file you are
    # working on, and leaving it out answers from a stale tree.
    assert data["sent"]["edit"] == 2


# -- a payload with nothing in it, spec §5 ----------------------------------


@pytest.fixture
def rust(project):
    """A project in a language kopipasta cannot skeleton.

    `extract_symbols` handles Python and the TS/JS family. Rust, Go, Java, C,
    Ruby and PHP all extract to nothing, and every one of them meets `--all`.
    """
    for path in ("src/calc.py", "src/main.py"):
        (project / path).unlink()
    (project / "src" / "auth.rs").write_text(
        "pub fn validate(t: &Token) -> bool {\n    t.expires_at <= now()\n}\n"
    )
    (project / "src" / "store.rs").write_text("pub struct Store {}\n")
    (project / "Cargo.toml").write_text("[package]\nname = 'tokensvc'\n")
    return project


def test_a_budget_with_no_room_for_one_file_says_the_payload_is_empty(rust, capsys):
    """The remaining route to a payload of pure directory listing.

    `--all` sends whole files now, so the way to end up with nothing is a
    budget too small for even one of them — and on a language with no
    skeletons there is no middle rung to land on, so the ladder takes every
    file straight to path-only and the counts still read as healthy.
    """
    data = run_json(
        capsys, "--all", "--budget", "60", "-q", "which file expires tokens?"
    )
    assert data["no_file_contents"] is True
    payload = (rust / data["request"]).read_text(encoding="utf-8")
    assert "expires_at" not in payload  # the tree names it; nothing shows it


def test_the_empty_payload_warning_is_loud_and_names_the_way_out(rust, capsys):
    run("--all", "--budget", "60", "-q", "x")
    err = capsys.readouterr().err
    assert "no file contents" in err
    assert "--budget 400k" in err  # raise the cap
    assert "-e / -r FILE" in err  # or ask a narrower question


def test_a_payload_with_contents_does_not_cry_wolf(project, capsys):
    data = run_json(capsys, "--all", "-q", "x")
    assert "no_file_contents" not in data
    err = capsys.readouterr().err
    assert "no file contents" not in err


# -- --all means the whole repository, in full ------------------------------


def test_all_sends_whole_files_not_skeletons(project, capsys):
    """The product is a large context window with the codebase inside it.

    `--all` used to start every file at the skeleton rung, which spent the
    ladder's entire range before the caller asked for anything and left the
    oracle reasoning about signatures. Full content is the default; `--budget`
    is the throttle.
    """
    data = run_json(capsys, "--all", "-q", "x")
    assert data["sent"]["ref"] == 4  # every non-ignored file
    assert data["sent"]["map"] == 0
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert "return a + b" in payload  # the body, not just the signature
    assert "## Reference Context (Read-Only)" in payload


def test_all_reads_a_language_it_cannot_skeleton(rust, capsys):
    """The Rust case, now answered by sending the code rather than warning
    that there is none."""
    data = run_json(capsys, "--all", "-q", "which file expires tokens?")
    assert data["sent"]["ref"] == 5
    assert "no_file_contents" not in data
    assert "path_only" not in data
    payload = (rust / data["request"]).read_text(encoding="utf-8")
    assert "t.expires_at <= now()" in payload


def test_an_explicit_role_still_beats_all(project, capsys):
    """`--all -e src/calc.py` is the shape the whole tool is for: the repo as
    read-only background, one file as the workspace."""
    data = run_json(capsys, "--all", "-e", "src/calc.py", "-q", "x")
    assert data["sent"]["edit"] == 1
    assert data["sent"]["ref"] == 3
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert payload.index("Active Workspace") < payload.index("src/calc.py")


def test_the_ladder_demotes_what_all_dragged_in_before_what_you_named(project, capsys):
    """Someone who typed a path meant that path.

    Now that `--all` produces reference files rather than skeletons, that rule
    has to hold inside the reference role too: a small file the caller named
    must outlive a large one nobody chose.
    """
    (project / "src" / "bulk.py").write_text(
        "\n".join(f"# bulk {i}" for i in range(4000))
    )
    (project / "src" / "named.py").write_text(
        "\n".join(f"# named {i}" for i in range(200))
    )
    # Budget chosen so that dropping the bulk file alone is enough: if the
    # order were wrong, the named file would go first and the bulk one would
    # survive. A tighter budget exhausts every rung and proves nothing.
    data = run_json(capsys, "--all", "-r", "src/named.py", "--budget", "2k", "-q", "x")
    demoted = {d["path"] for d in data["demoted"]}
    assert "src/bulk.py" in demoted
    assert "src/named.py" not in demoted
    assert "# named 199" in (project / data["request"]).read_text(encoding="utf-8")


def test_a_budget_still_brings_a_whole_repo_down_to_size(project, capsys):
    big = "\n".join(f"def f{i}():\n    return {i}" for i in range(400))
    (project / "src" / "big.py").write_text(big)
    data = run_json(capsys, "--all", "--budget", "1k", "-q", "x")
    assert data["demoted"], "nothing demoted despite a budget far under the repo"
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert "def f399" not in payload  # the body is gone
    assert '"big.py"' in payload  # the name is not


def test_a_whole_repo_with_no_budget_says_how_big_it_got(project, capsys):
    """`--all` now assembles everything, so the one number that decides the
    bill should not have to be dug out of --json."""
    # Sized off the threshold rather than a magic number, so tuning the
    # threshold does not silently turn this test into a no-op.
    lines = int(askmod.LOUD_TOKENS * budgetmod.CHARS_PER_TOKEN / 6) + 1000
    (project / "src" / "big.py").write_text("x = 1\n" * lines)
    run("--all", "-q", "x")
    err = capsys.readouterr().err
    assert "--budget" in err
    assert "no budget" in err.lower()


def test_a_payload_under_the_threshold_stays_quiet(project, capsys):
    """The warning has to be rare enough to mean something."""
    run("--all", "-q", "x")
    assert "no budget" not in capsys.readouterr().err.lower()
