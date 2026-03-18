from unittest.mock import patch, MagicMock
from pathlib import Path
from kopipasta.main import KopipastaApp


def test_finalize_and_output_reloads_state(tmp_path: Path, monkeypatch):
    """
    Ensures that AI_SESSION.md and AI_CONTEXT.md are re-read from disk
    right before the final prompt is generated, preventing stale state
    if they were patched during the interactive loop.
    """
    # Isolate global profile to the temp directory
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    app = KopipastaApp()
    app.project_root_abs = str(tmp_path)

    # Provide a task via args to bypass the interactive user prompt
    app.args.task = "Refactor logic"

    # 1. Setup initial disk state
    session_file = tmp_path / "AI_SESSION.md"
    session_file.write_text("OLD SESSION")

    context_file = tmp_path / "AI_CONTEXT.md"
    context_file.write_text("OLD CONTEXT")

    # 2. Load configuration (simulates startup)
    app._load_configuration()
    assert app.session_state == "OLD SESSION"
    assert app.project_context == "OLD CONTEXT"

    # 3. Simulate the user applying a patch that alters the memory files on disk
    session_file.write_text("NEW SESSION STATE")
    context_file.write_text("NEW CONTEXT STATE")

    # Prevent interactive output during test
    app.console = MagicMock()

    with (
        patch("kopipasta.main.save_selection_to_cache"),
        patch("kopipasta.main.save_map_to_cache"),
        patch("kopipasta.main.pyperclip.copy") as mock_copy,
        patch("builtins.print"),
    ):
        # 4. Trigger prompt generation
        app._finalize_and_output()

        # 5. Verify the generated prompt uses the newly patched state
        assert mock_copy.called
        copied_text = mock_copy.call_args[0][0]

        assert "NEW SESSION STATE" in copied_text
        assert "NEW CONTEXT STATE" in copied_text
        assert "OLD SESSION" not in copied_text
        assert "OLD CONTEXT" not in copied_text
