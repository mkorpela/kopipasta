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
    for var in ("KOPIPASTA_BACKEND", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
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
    (project / "sel.txt").write_text("# from a triage answer\nsrc/calc.py\nsrc/main.py\n")
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
    payload = (project / data["request"]).read_text()
    assert "junk.py" not in payload  # gitignored: absent even from the tree
    assert "binary" not in payload  # the bytes of a binary file are never read


# -- the budget ladder, spec §5 --------------------------------------------


def test_over_budget_files_walk_down_the_ladder_instead_of_vanishing(project, capsys):
    big = "\n".join(f"def f{i}():\n    return {i}" for i in range(400))
    (project / "src" / "big.py").write_text(big)
    data = run_json(capsys, "-r", "src/*.py", "-e", "src/calc.py", "--budget", "1k", "-q", "x")
    demoted = {d["path"]: d for d in data["demoted"]}
    assert "src/big.py" in demoted
    # -e is never demoted: that is the contract of "editable".
    assert "src/calc.py" not in demoted
    payload = (project / data["request"]).read_text()
    assert '"big.py"' in payload  # still named in the structure tree, not gone
    assert "def f399" not in payload  # ... but its body is not


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
    body = "\n\n".join(
        f'def pad{i}():\n    return "{"x" * 60}"' for i in range(12)
    )
    (project / "src" / "pad.py").write_text(body + "\n")
    skeleton = run_json(capsys, "-m", "src/pad.py", "-q", "x")["est_input_tokens"]
    full = run_json(capsys, "-r", "src/pad.py", "-q", "x")["est_input_tokens"]
    budget = (skeleton + full) // 2
    assert skeleton < budget < full, "the fixture no longer sets up the case"

    lenient = run_json(capsys, "-r", "src/pad.py", "--budget", str(budget), "-q", "x")
    assert lenient["sent"]["demoted"] == 1  # without the flag: demote and carry on
    assert lenient["est_input_tokens"] <= budget  # ... and it really does fit

    data = run_json(capsys, "-r", "src/pad.py", "--budget", str(budget), "--strict-budget",
                    "-q", "x", expect=EXIT_BUDGET)
    assert data["error"] == "budget_exceeded"
    assert data["demoted"] == ["src/pad.py"]


def test_a_bad_budget_string_says_what_it_wanted(project, capsys):
    data = run_json(capsys, "--all", "--budget", "lots", "-q", "x", expect=EXIT_USAGE)
    assert "--budget" in data["summary"]


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
    second = run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "two", backend=canned)
    assert second["turn"] == 2
    assert second["sent"]["deduped"] == 1
    body = (project / second["request"]).read_text()
    assert "prefix: prefix.md" in body  # referenced, not repeated
    assert "def add" not in body


def test_a_changed_file_is_resent_and_marked_as_superseding(project, capsys, canned):
    run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "one", backend=canned)
    (project / "src" / "calc.py").write_text("def add(a, b):\n    return a + b  # fixed\n")
    second = run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "two", backend=canned)
    body = (project / second["request"]).read_text()
    assert second["sent"]["deduped"] == 0
    assert "supersedes" in body and "# fixed" in body


def test_a_role_change_defeats_dedup(project, capsys, canned):
    """Same bytes, different role. Deduping on the hash alone would answer
    `-e file` with the 50-line snippet turn 1 sent, and report it as sent."""
    run_json(capsys, "-s", "src/main.py", "--session", "s", "-q", "one", backend=canned)
    second = run_json(capsys, "-e", "src/main.py", "--session", "s", "-q", "two", backend=canned)
    assert second["sent"]["deduped"] == 0
    assert "now edit" in (project / second["request"]).read_text()


def test_dedup_only_trusts_the_prefix_not_earlier_suffixes(project, capsys, canned):
    """A file that rode in turn 2's suffix is gone by turn 3: the suffix is not
    replayed. Treating it as still-present would withhold it from the model
    while the record claimed it was sent."""
    run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "one", backend=canned)
    run_json(capsys, "-e", "src/calc.py", "-e", "src/main.py", "--session", "s", "-q", "2",
             backend=canned)
    third = run_json(capsys, "-e", "src/calc.py", "-e", "src/main.py", "--session", "s", "-q", "3",
                     backend=canned)
    body = (project / third["request"]).read_text()
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
    second = run_json(capsys, "-e", "src/calc.py", "-m", "src/main.py", "--session", "s",
                      "-q", "two", backend=canned)
    body = (project / second["request"]).read_text()
    assert second["sent"]["map"] == 1
    assert "src/main.py" in body
    assert "def main()" in body  # the skeleton, not just the name
    assert "print(add(1, 2))" not in body  # ... and only the skeleton


def test_the_prefix_is_byte_identical_across_turns(project, capsys, canned):
    """It is the cache breakpoint. Re-rendering it would miss on every turn."""
    first = run_json(capsys, "-e", "src/calc.py", "--session", "s", "-q", "one", backend=canned)
    prefix = (project / ".kopipasta" / "sessions" / first["session"] / "prefix.md").read_bytes()
    (project / "src" / "calc.py").write_text("def add(a, b):\n    return a + b  # changed\n")
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
    run_json(capsys, "-e", "src/calc.py", "-r", "src/main.py",
             "--session", "s", "-q", "one", backend=canned)
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


def test_continue_is_honoured_under_json_because_it_is_explicit(project, capsys, canned):
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
    data = run_json(capsys, "-e", "src/calc.py", "--continue", "-q", "x",
                    expect=EXIT_USAGE)
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
    data = run_json(capsys, "-e", "src/calc.py", "--mode", "patch", "-q", "x",
                    backend=backend, expect=3)
    assert data["error"] == "backend_not_a_completion"


def test_an_unfenced_patch_is_a_format_problem_not_a_misconfigured_backend(project, capsys):
    """Found by dogfooding: gemini returned a perfectly good search/replace
    patch with no ``` fence, and was told to disable its file and shell tools.

    It has none — it is a raw API call. Following that hint cannot help, and
    the response was not an agent's refusal but exactly what was asked for in
    a format the parser could not find the edges of. The two need different
    slugs because they send the caller to different places.
    """
    backend = _canned(
        project,
        "# FILE: src/calc.py\n"
        "<<<<<<< SEARCH\n    return a + b\n=======\n    return a + b + 0\n"
        ">>>>>>> REPLACE\n",
    )
    data = run_json(capsys, "-e", "src/calc.py", "--mode", "patch", "-q", "x",
                    backend=backend, expect=5)
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
    data = run_json(capsys, "-e", "src/calc.py", "--session", "../../etc", "-q", "x",
                    expect=EXIT_USAGE)
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
    assert "Where does expiry live?" in (project / data["request"]).read_text()


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
        ["--backend", f"none:{project / 'answer.json'}", "-e", "src/calc.py", "-q", "x", "--json"]
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
        ["--backend", f"none:{project / 'answer.txt'}", "-e", "src/calc.py", "-q", "x", "--json"]
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
        ["--backend", f"none:{project / 'answer.txt'}", "-e", "src/calc.py", "-q", "x", "--json"]
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
    payload = (project / data["request"]).read_text()
    assert "sk-live-abcdefghijklmnop" not in payload
    assert "*" * 10 in payload


# -- the project constitution ----------------------------------------------


def test_ai_context_is_offered_to_the_oracle_and_can_be_declined(project, capsys):
    (project / "AI_CONTEXT.md").write_text("# Rules\nNever use threads.\n")
    with_ctx = run_json(capsys, "-e", "src/calc.py", "-q", "x")
    assert "Never use threads." in (project / with_ctx["request"]).read_text()
    without = run_json(capsys, "-e", "src/calc.py", "-q", "x", "--no-project-context")
    assert "Never use threads." not in (project / without["request"]).read_text()


# -- git selectors ----------------------------------------------------------


def test_changed_needs_git_and_says_so_when_it_is_missing(project, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    data = run_json(capsys, "--changed", "-q", "x", expect=EXIT_USAGE)
    assert "git" in data["summary"]


@pytest.mark.skipif(os.name == "nt", reason="git plumbing differs enough to be its own test")
def test_changed_selects_the_working_tree(project, capsys):
    import subprocess

    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
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
