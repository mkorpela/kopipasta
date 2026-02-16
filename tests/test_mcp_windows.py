"""
Tests for Windows-specific command execution in the MCP server.

These tests protect against regressions where:
- PowerShell (.ps1) scripts hang or silently fail when invoked via cmd.exe
- subprocess stdin inheritance causes MCP server hangs in headless contexts
- Command normalization breaks cross-platform execution
"""

import platform
import subprocess
from unittest.mock import patch, MagicMock
from pathlib import Path

import pytest

from kopipasta.mcp_server import _prepare_command, _run_cmd


# ---------------------------------------------------------------------------
# _prepare_command: PS1 wrapping
# ---------------------------------------------------------------------------

class TestPrepareCommandWindows:
    """Tests that run regardless of host OS by mocking platform.system()."""

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_ps1_bare_dotslash(self, _mock: MagicMock) -> None:
        result = _prepare_command(".\\apply_ai_changes.ps1")
        assert result == 'powershell -NoProfile -ExecutionPolicy Bypass -File ".\\apply_ai_changes.ps1"'

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_ps1_forward_slash(self, _mock: MagicMock) -> None:
        result = _prepare_command("./apply_ai_changes.ps1")
        assert result == 'powershell -NoProfile -ExecutionPolicy Bypass -File "./apply_ai_changes.ps1"'

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_ps1_absolute_path(self, _mock: MagicMock) -> None:
        result = _prepare_command("C:\\scripts\\verify.ps1")
        assert result == 'powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\scripts\\verify.ps1"'

    @patch("kopipasta.mcp_server.platform.system", return_value="Windows")
    def test_ps1_with_arguments(self, _mock: MagicMock) -> None:
        result = _prepare_command(".\\verify.ps1 -Verbose -Strict")
        assert result == 'powershell -NoProfile -ExecutionPolicy Bypass -File ".\\verify.ps1" -Verbose -Strict'

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
    """Verify that _run_cmd passes the right kwargs to subprocess.run."""

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server._prepare_command", side_effect=lambda c: c)
    @patch("kopipasta.mcp_server.subprocess.run")
    def test_stdin_is_devnull(
        self,
        mock_run: MagicMock,
        _mock_prepare: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        """stdin must be DEVNULL to prevent hangs in headless MCP context."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        _run_cmd("echo hello", Path("/tmp"))
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("stdin") == subprocess.DEVNULL

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server._prepare_command", side_effect=lambda c: c)
    @patch("kopipasta.mcp_server.subprocess.run")
    def test_capture_output_enabled(
        self,
        mock_run: MagicMock,
        _mock_prepare: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        _run_cmd("echo hello", Path("/tmp"))
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("capture_output") is True

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server._prepare_command", side_effect=lambda c: c)
    @patch("kopipasta.mcp_server.subprocess.run")
    def test_timeout_present(
        self,
        mock_run: MagicMock,
        _mock_prepare: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        _run_cmd("echo hello", Path("/tmp"))
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("timeout") == 300

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server.subprocess.run")
    def test_ps1_command_is_prepared(
        self,
        mock_run: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        """End-to-end: a .ps1 command on Windows should arrive wrapped."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="passed", stderr=""
        )
        with patch(
            "kopipasta.mcp_server.platform.system", return_value="Windows"
        ):
            result = _run_cmd(".\\verify.ps1", Path("C:\\project"))

        actual_cmd = mock_run.call_args.args[0]
        assert actual_cmd.startswith("powershell")
        assert ".\\verify.ps1" in actual_cmd
        assert "Exit Code: 0" in result


# ---------------------------------------------------------------------------
# _run_cmd: output formatting
# ---------------------------------------------------------------------------

class TestRunCmdOutput:

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server._prepare_command", side_effect=lambda c: c)
    @patch("kopipasta.mcp_server.subprocess.run")
    def test_output_format(
        self,
        mock_run: MagicMock,
        _mock_prepare: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="FAIL", stderr="error detail"
        )
        result = _run_cmd("test_cmd", Path("/tmp"))
        assert "$ test_cmd" in result
        assert "Exit Code: 1" in result
        assert "FAIL" in result
        assert "error detail" in result

    @patch("kopipasta.mcp_server._get_shell_env", return_value={})
    @patch("kopipasta.mcp_server._prepare_command", side_effect=lambda c: c)
    @patch("kopipasta.mcp_server.subprocess.run", side_effect=Exception("boom"))
    def test_exception_handling(
        self,
        _mock_run: MagicMock,
        _mock_prepare: MagicMock,
        _mock_env: MagicMock,
    ) -> None:
        result = _run_cmd("bad_cmd", Path("/tmp"))
        assert "Error running command" in result
        assert "boom" in result