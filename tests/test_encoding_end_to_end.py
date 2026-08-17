"""Encodings, end to end: what actually reaches the model and the worktree.

`tests/test_file.py` pins `decode_text` itself. This module pins the four
places the decision escapes into a run — the payload, the envelope, the
question, the path list — plus the patch reader on the way back.

The origin of every case here is the same: PowerShell 5.1's `>` redirection
writes UTF-16LE with a BOM, so `git diff > changes.txt` on Windows produces a
file a human can open and read that is not UTF-8. The subtlety that made it
expensive is that BOM-less/BOM-ful UTF-16LE ASCII is a sequence of *legal*
UTF-8 bytes — NUL is valid UTF-8 — so a strict utf-8 decode does not raise.
It succeeds, and returns `"k\\x00o\\x00p\\x00i\\x00"`. Nothing failed, nothing
was logged, and a model reviewed the NUL-interleaved mojibake with total
confidence. A unit test on the decoder cannot catch that; only a test that
looks at the bytes leaving the process can.
"""

import codecs
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kopipasta.core import apply as applymod
from kopipasta.core import ask as askmod
from kopipasta.core.errors import EXIT_OK

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def clean_module_state():
    """`kopipasta.file` keeps process-global state; a test must not inherit it.

    `_lossy_reads` is keyed by path and is what both the payload caveat and
    the envelope's `lossy_decode` are read out of. It survives for the life of
    the process, so a file one test decoded lossily would still be in there
    when the negative control asserts the key is *absent* — and that test
    would fail for a reason that has nothing to do with the run it just made.
    `_is_binary_cache` is keyed by path too, and tmp_path names can repeat.
    """
    from kopipasta import file as filemod

    for cache in (
        filemod._lossy_reads,
        filemod._is_binary_cache,
        filemod._is_ignored_cache,
        filemod._gitignore_cache,
    ):
        cache.clear()
    yield


def write_powershell(path: Path, text: str) -> Path:
    """Write `text` exactly as `... > path` writes it in PowerShell 5.1.

    UTF-16LE, a BOM, and CRLF. Reproducing all three matters: the BOM is what
    the decoder is supposed to notice, and the CRLF is what proves the newline
    normalisation text mode used to do is still being done.
    """
    payload = text.replace("\n", "\r\n").encode("utf-16-le")
    path.write_bytes(codecs.BOM_UTF16_LE + payload)
    return path


# -- ask -------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A small repo, with git present so the project root resolves to it."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'
    )
    (tmp_path / "src" / "main.py").write_text(
        'from src.calc import add\n\n\ndef main():\n    """Entry point."""\n'
        "    print(add(1, 2))\n"
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
    return tmp_path


def run(*argv, expect=EXIT_OK, backend="none"):
    code = askmod.run(["--backend", backend, *argv])
    assert code == expect, f"expected exit {expect}, got {code}"
    return code


def run_json(capsys, *argv, expect=EXIT_OK, backend="none"):
    run(*argv, "--json", expect=expect, backend=backend)
    return json.loads(capsys.readouterr().out)


DIFF = (
    "diff --git a/src/calc.py b/src/calc.py\n"
    "--- a/src/calc.py\n"
    "+++ b/src/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-    return a + b\n"
    "+    return a - b\n"
)


def test_a_utf16_file_reaches_the_model_as_the_text_it_actually_holds(project, capsys):
    """The reported failure, in one assertion.

    `git diff > changes.txt` in PowerShell 5.1 is UTF-16LE with a BOM. Read as
    UTF-8 the bytes decode *successfully* — NUL is a legal UTF-8 codepoint —
    into `"d\\x00i\\x00f\\x00f\\x00"`, with only the two BOM bytes becoming
    U+FFFD. So the payload was well-formed, the run was clean, the envelope
    said the file was sent, and the model reviewed a diff nobody could read.

    The NUL assertion is the one that matters: it is the shape of the damage,
    and it is the shape a strict utf-8 decode cannot report.
    """
    write_powershell(project / "changes.txt", DIFF)
    data = run_json(capsys, "--pin", "changes.txt", "-q", "review this", "--dry-run")
    payload = (project / data["request"]).read_text(encoding="utf-8")

    assert "diff --git a/src/calc.py b/src/calc.py" in payload
    assert "+    return a - b" in payload
    assert "\x00" not in payload, "the file was decoded as utf-8 and is NUL-interleaved"
    assert "\ufffd" not in payload, "the file was decoded destructively"


def test_a_utf16_file_is_selectable_at_all(project, capsys):
    """A `.rst` has no entry in TEXT_EXTENSIONS, so `is_binary` sniffs it.

    UTF-16 text is more than half NUL bytes, and the null-byte test that
    decides binary-ness saw exactly that: the file was classified as a blob
    and dropped from the selection with no message anywhere. The failure mode
    is the worst one this tool has — a file the caller named, silently absent
    from a payload whose counts still look healthy.
    """
    write_powershell(project / "notes.rst", "The expiry deadline lives in calc.\n")
    data = run_json(capsys, "-r", "notes.rst", "-q", "x", "--dry-run")

    assert data["sent"]["ref"] == 1, "the file was dropped as binary"
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert "The expiry deadline lives in calc." in payload


def test_a_file_that_could_not_be_decoded_carries_its_caveat_to_the_model(
    project, capsys
):
    """The model is the one reasoning about the bytes, so the model gets told.

    0x97 is a cp1252 em-dash: a real byte from a real file, not recoverable as
    UTF-8 under any sniff. The read is still worth doing — the rest of the
    prose survives — but handing over a lossy transcription with no caveat is
    how a confident answer gets written about text that was never there. A
    warning on stderr does not reach the model; the block header does.
    """
    (project / "prose.md").write_bytes(b"# Title\n\nx \x97 y, and more prose.\n")
    data = run_json(capsys, "-r", "prose.md", "-q", "x", "--dry-run")
    payload = (project / data["request"]).read_text(encoding="utf-8")

    header = next(
        line
        for line in payload.splitlines()
        if line.startswith("# FILE:") and "prose.md" in line
    )
    assert "decoded as" in header
    assert "unreadable character" in header
    assert "and more prose" in payload, "the readable part must still be sent"


def test_the_envelope_names_the_files_it_could_not_decode(project, capsys):
    """The machine-readable half of the same caveat.

    A caller deciding whether the answer is worth having cannot be made to
    grep the rendered payload for a parenthetical. `lossy_decode` names the
    file and says what it cost, in the one place an agent already parses.
    """
    (project / "prose.md").write_bytes(b"x \x97 y\n")
    data = run_json(capsys, "-r", "prose.md", "-q", "x", "--dry-run")

    assert isinstance(data["lossy_decode"], list)
    entry = data["lossy_decode"][0]
    assert entry["file"] == "prose.md"
    assert "unreadable character" in entry["detail"]


def test_a_clean_run_says_nothing_about_decoding(project, capsys):
    """The negative control, and it has to be absence rather than emptiness.

    An always-present `lossy_decode: []` trains every reader to skip the key,
    which is precisely the reader who then misses the run where it is not
    empty. Absent means "nothing to say"; present means "read this".
    """
    data = run_json(capsys, "-r", "src/calc.py", "-q", "x", "--dry-run")

    assert "lossy_decode" not in data


def test_the_question_itself_can_be_a_utf16_file(project, capsys):
    """`-q @file` is the flag for a question too long to quote in a shell —
    which on Windows is usually a question that was *redirected* into a file,
    and therefore UTF-16LE. This path read it as strict UTF-8, so it raised
    UnicodeDecodeError and refused the run over a perfectly readable file.
    """
    write_powershell(project / "task.txt", "Where does the expiry deadline live?\n")
    data = run_json(capsys, "-r", "src/calc.py", "-q", "@task.txt", "--dry-run")
    payload = (project / data["request"]).read_text(encoding="utf-8")

    assert "Where does the expiry deadline live?" in payload
    assert "\x00" not in payload


def test_a_utf16_path_list_still_names_files(project, capsys):
    """`--from-file` closes the triage loop, and a mojibake path matches nothing.

    Every line decoded to `"s\\x00r\\x00c\\x00/..."`, so every path in the list
    "matched no files" and the run failed as an ordinary empty selection —
    with a suggestion list, a usage exit code, and nothing anywhere pointing at
    the encoding. The caller sees a tool that cannot find files it can see.
    """
    write_powershell(project / "sel.txt", "src/calc.py\nsrc/main.py\n")
    data = run_json(capsys, "--from-file", "sel.txt", "-q", "x", "--dry-run")

    assert data["sent"]["ref"] == 2
    payload = (project / data["request"]).read_text(encoding="utf-8")
    assert "def add" in payload and "def main" in payload


# -- apply -----------------------------------------------------------------


ORIGINAL = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"

UTF16_PATCH = """Here is the change.

```python
# FILE: app.py
<<<<<<< SEARCH
def a():
    return 1
=======
def a():
    return 100
>>>>>>> REPLACE
```
"""


def _git_bin() -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError(
            "git is required for this test helper but was not found on PATH"
        )
    return git


def _git(cwd, *args):
    subprocess.run([_git_bin(), *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo, committed clean, as `apply`'s own tests build one."""
    work = tmp_path / "repo"
    work.mkdir()
    (work / "app.py").write_text(ORIGINAL)
    (work / ".gitignore").write_text(".kopipasta/\n")
    if shutil.which("git"):
        _git(work, "init", "-q")
        _git(work, "config", "user.email", "t@example.com")
        _git(work, "config", "user.name", "t")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "init")
    monkeypatch.chdir(work)
    monkeypatch.setenv("KOPIPASTA_NONINTERACTIVE", "1")
    return work


def apply_json(capsys, *argv, expect=EXIT_OK):
    code = applymod.run([*argv, "--json"])
    assert code == expect, f"expected exit {expect}, got {code}"
    return json.loads(capsys.readouterr().out)


@needs_git
def test_a_utf16_patch_file_applies(repo, capsys):
    """The return leg of the same bug, and the one that looked like a refusal.

    `kopipasta ask --mode patch > patch.txt` in PowerShell 5.1 writes UTF-16LE.
    Decoded as UTF-8 every ``` fence, every `# FILE:` marker and every
    SEARCH/REPLACE line is NUL-interleaved, so the parser finds no patches at
    all and `apply` exits with `no_patches` — which reads as "the model
    declined to make a change" and sends the caller back to re-ask a question
    that was already answered correctly.
    """
    patch = write_powershell(repo.parent / "patch.txt", UTF16_PATCH)
    data = apply_json(capsys, str(patch))

    assert data["applied"] == ["app.py"]
    assert "return 100" in (repo / "app.py").read_text(encoding="utf-8")
