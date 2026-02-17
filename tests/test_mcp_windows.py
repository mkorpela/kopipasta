"""
Tests for Windows-specific command execution in the MCP server.

These tests protect against regressions where:
- PowerShell (.ps1) scripts hang or silently fail when invoked via cmd.exe
- subprocess stdin inheritance causes MCP server hangs in headless contexts
- Command normalization breaks cross-platform execution
- read_context/apply_edits bypass _run_cmd with inline subprocess calls
"""

import subprocess
from unittest.mock import patch, MagicMock
from pathlib import Path


from kopipasta.mcp_server import (
    _prepare_command,
    _run_cmd,
    read_context,
    apply_edits,
    SAFE_CLIENT_TIMEOUT,
)


def _mock_popen_factory(returncode=0, stdout_text="", stderr_text=""):
    """Return a (mock_proc, side_effect) for patching subprocess.Popen.

    The side_effect writes *stdout_text* / *stderr_text* into the real file
    handles that _run_cmd passes as stdout= / stderr= so the output can be
    read back from the temp files.
    """
    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.pid = 12345
    mock_proc.wait = MagicMock(return_value=returncode)

    def side_effect(*args, **kwargs):
        stdout_fh = kwargs.get("stdout")
        stderr_fh = kwargs.get("stderr")
        if stdout_fh and hasattr(stdout_fh, "write"):
            stdout_fh.write(stdout_text)
        if stderr_fh and hasattr(stderr_fh, "write"):
            stderr_fh.write(stderr_text)
        return mock_proc

    return mock_proc, side_effect


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_CONFIG = {
    "project_root": "C:\\fake\\project",
    "verification_command": ".\\verify.ps1",
    "editable_files": ["app.py"],
    "task_description": "test task",
}


# ---------------------------------------------------------------------------
# _prepare_command: PS1 wrapping
# ---------------------------------------------------------------------------


class TestPrepareCommandWindows:
    """Tests that run regardless of host OS by mocking platform.system()."""

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_ps1_bare_dotslash(self, _mock: MagicMock) -> None:
        result = _prepare_command(".\\apply_ai_changes.ps1")
        assert (
            result
            == 'powershell -NoProfile -ExecutionPolicy Bypass -File ".\\apply_ai_changes.ps1"'
        )

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_ps1_forward_slash(self, _mock: MagicMock) -> None:
        result = _prepare_command("./apply_ai_changes.ps1")
        assert (
            result
            == 'powershell -NoProfile -ExecutionPolicy Bypass -File "./apply_ai_changes.ps1"'
        )

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_ps1_absolute_path(self, _mock: MagicMock) -> None:
        result = _prepare_command("C:\\scripts\\verify.ps1")
        assert (
            result
            == 'powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\scripts\\verify.ps1"'
        )

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_ps1_with_arguments(self, _mock: MagicMock) -> None:
        result = _prepare_command(".\\verify.ps1 -Verbose -Strict")
        assert (
            result
            == 'powershell -NoProfile -ExecutionPolicy Bypass -File ".\\verify.ps1" -Verbose -Strict'
        )

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_ps1_already_prefixed_powershell(self, _mock: MagicMock) -> None:
        """Don't double-wrap if user already specified powershell."""
        cmd = "powershell -File .\\verify.ps1"
        result = _prepare_command(cmd)
        assert result == cmd  # unchanged

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_ps1_already_prefixed_pwsh(self, _mock: MagicMock) -> None:
        cmd = "pwsh -File .\\verify.ps1"
        result = _prepare_command(cmd)
        assert result == cmd  # unchanged

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_bat_passthrough(self, _mock: MagicMock) -> None:
        cmd = ".\\verify.bat"
        result = _prepare_command(cmd)
        assert result == cmd

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_bare_command_passthrough(self, _mock: MagicMock) -> None:
        cmd = "uv run pytest"
        result = _prepare_command(cmd)
        assert result == cmd

    @patch("kopipasta.mcp_server.platform.system", return_value="Darwin")
    def test_ps1_on_mac_passthrough(self, _mock: MagicMock) -> None:
        """On non-Windows, .ps1 is not special — pass through."""
        cmd = "./verify.ps1"
        result = _prepare_command(cmd)
        assert result == cmd

    @patch("kopipasta.mcp_server.platform.system", return_value="Linux")
    def test_ps1_on_linux_passthrough(self, _mock: MagicMock) -> None:
        cmd = "./verify.ps1"
        result = _prepare_command(cmd)
        assert result == cmd


# ---------------------------------------------------------------------------
# _run_cmd: stdin=DEVNULL (prevents MCP hangs)
# ---------------------------------------------------------------------------


class TestRunCmdSubprocessArgs:
    """Verify that _run_cmd passes the right kwargs to subprocess.Popen."""

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server._prepare_command", side_effect=lambda c: c)
    @patch("kopipasta.mcp_server.subprocess.Popen")
    def test_stdin_is_devnull(
        self,
        mock_popen: MagicMock,
        _mock_prepare: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        """stdin must be DEVNULL to prevent hangs in headless MCP context."""
        mock_proc, side_effect = _mock_popen_factory(returncode=0, stdout_text="ok")
        mock_popen.side_effect = side_effect
        _run_cmd("echo hello", Path("/tmp"))
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args
        assert call_kwargs.kwargs.get("stdin") == subprocess.DEVNULL

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server._prepare_command", side_effect=lambda c: c)
    @patch("kopipasta.mcp_server.subprocess.Popen")
    def test_output_is_captured_via_files(
        self,
        mock_popen: MagicMock,
        _mock_prepare: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        """stdout/stderr must be captured (via temp files) so output is not lost."""
        mock_proc, side_effect = _mock_popen_factory(returncode=0, stdout_text="ok")
        mock_popen.side_effect = side_effect
        result = _run_cmd("echo hello", Path("/tmp"))
        # Popen receives file handles for stdout/stderr (not PIPE, not None)
        call_kwargs = mock_popen.call_args
        assert call_kwargs.kwargs.get("stdout") is not None
        assert call_kwargs.kwargs.get("stderr") is not None
        # And the output is present in the result
        assert "ok" in result

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server._prepare_command", side_effect=lambda c: c)
    @patch("kopipasta.mcp_server.subprocess.Popen")
    def test_timeout_present(
        self,
        mock_popen: MagicMock,
        _mock_prepare: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        """proc.wait() must be called with the safe client timeout."""
        mock_proc, side_effect = _mock_popen_factory(returncode=0)
        mock_popen.side_effect = side_effect
        _run_cmd("echo hello", Path("/tmp"))
        mock_proc.wait.assert_called_once_with(timeout=SAFE_CLIENT_TIMEOUT)

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server.subprocess.Popen")
    def test_ps1_command_is_prepared(
        self,
        mock_popen: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        """End-to-end: a .ps1 command on Windows should arrive wrapped."""
        mock_proc, side_effect = _mock_popen_factory(returncode=0, stdout_text="passed")
        mock_popen.side_effect = side_effect
        with patch("kopipasta.mcp_server.platform.system", return_value="Windows"):
            result = _run_cmd(".\\verify.ps1", Path("/tmp"))

        actual_cmd = mock_popen.call_args.args[0]
        assert actual_cmd.startswith("powershell")
        assert ".\\verify.ps1" in actual_cmd
        assert "Exit Code: 0" in result


# ---------------------------------------------------------------------------
# _run_cmd: output formatting
# ---------------------------------------------------------------------------


class TestRunCmdOutput:
    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server._prepare_command", side_effect=lambda c: c)
    @patch("kopipasta.mcp_server.subprocess.Popen")
    def test_output_format(
        self,
        mock_popen: MagicMock,
        _mock_prepare: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        mock_proc, side_effect = _mock_popen_factory(
            returncode=1, stdout_text="FAIL", stderr_text="error detail"
        )
        mock_popen.side_effect = side_effect
        result = _run_cmd("test_cmd", Path("/tmp"))
        assert "$ test_cmd" in result
        assert "Exit Code: 1" in result
        assert "FAIL" in result
        assert "error detail" in result

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server._prepare_command", side_effect=lambda c: c)
    @patch("kopipasta.mcp_server.subprocess.Popen", side_effect=Exception("boom"))
    def test_exception_handling(
        self,
        _mock_popen: MagicMock,
        _mock_prepare: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        result = _run_cmd("bad_cmd", Path("/tmp"))
        assert "Error starting command" in result
        assert "boom" in result


# ---------------------------------------------------------------------------
# read_context / apply_edits: must delegate to _run_cmd, not inline subprocess
# ---------------------------------------------------------------------------


class TestNoInlineSubprocess:
    """Guard against re-introducing inline subprocess.run calls that
    bypass _prepare_command and stdin=DEVNULL."""

    @patch("kopipasta.mcp_server._load_config", return_value=MOCK_CONFIG)
    @patch("kopipasta.mcp_server.list_files", return_value=iter(["app.py"]))
    @patch(
        "kopipasta.mcp_server._run_cmd",
        return_value="$ verify\nExit Code: 0\n--- STDOUT ---\nok\n--- STDERR ---\n",
    )
    def test_read_context_delegates_to_run_cmd(
        self,
        mock_run_cmd: MagicMock,
        _mock_files: MagicMock,
        _mock_config: MagicMock,
    ) -> None:
        """read_context must call _run_cmd for verification, not subprocess.run directly."""
        result = read_context()
        mock_run_cmd.assert_called_once_with(".\\verify.ps1", Path("C:\\fake\\project"))
        assert "Exit Code: 0" in result

    @patch("kopipasta.mcp_server._load_config", return_value=MOCK_CONFIG)
    @patch("kopipasta.mcp_server.list_files", return_value=iter(["app.py"]))
    @patch("kopipasta.mcp_server._run_cmd")
    @patch("kopipasta.mcp_server.subprocess.run")
    def test_read_context_never_calls_subprocess_directly(
        self,
        mock_subprocess: MagicMock,
        mock_run_cmd: MagicMock,
        _mock_files: MagicMock,
        _mock_config: MagicMock,
    ) -> None:
        """subprocess.run must not be called directly from read_context."""
        mock_run_cmd.return_value = (
            "$ cmd\nExit Code: 0\n--- STDOUT ---\n\n--- STDERR ---\n"
        )
        read_context()
        mock_subprocess.assert_not_called()

    @patch(
        "kopipasta.mcp_server._load_config",
        return_value={
            **MOCK_CONFIG,
            "verification_command": None,
        },
    )
    @patch("kopipasta.mcp_server.list_files", return_value=iter(["app.py"]))
    @patch("kopipasta.mcp_server._run_cmd")
    def test_read_context_no_verification_skips_run(
        self,
        mock_run_cmd: MagicMock,
        _mock_files: MagicMock,
        _mock_config: MagicMock,
    ) -> None:
        """When no verification_command is set, _run_cmd should not be called."""
        result = read_context()
        mock_run_cmd.assert_not_called()
        assert "No verification command configured" in result

    @patch(
        "kopipasta.mcp_server._run_cmd",
        return_value="$ verify\nExit Code: 0\n--- STDOUT ---\nok\n--- STDERR ---\n",
    )
    def test_apply_edits_delegates_verification_to_run_cmd(
        self,
        mock_run_cmd: MagicMock,
        tmp_path: Path,
    ) -> None:
        """apply_edits must use _run_cmd for post-edit verification."""
        # Use a real temp directory to avoid Path.resolve() issues on Unix
        # with Windows-style paths like C:\fake\project
        app_file = tmp_path / "app.py"
        app_file.write_text("old content")

        config = {
            "project_root": str(tmp_path),
            "verification_command": ".\\verify.ps1",
            "editable_files": ["app.py"],
        }

        with patch("kopipasta.mcp_server._load_config", return_value=config):
            from kopipasta.mcp_server import EditBlock

            apply_edits(
                edits=[
                    EditBlock(
                        file_path="app.py", search="old content", replace="new content"
                    )
                ]
            )
        mock_run_cmd.assert_called_once_with(".\\verify.ps1", tmp_path.resolve())
