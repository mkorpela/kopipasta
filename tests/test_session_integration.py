import os
import subprocess
import pytest
from pathlib import Path
from kopipasta.session import Session, SESSION_FILENAME


def run_git(cmd: list, cwd: Path) -> str:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    result = subprocess.run(
        ["git"] + cmd, cwd=cwd, check=True, capture_output=True, text=True, env=env
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path):
    """Sets up a temporary git repo with an initial commit."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Init git
    run_git(["init"], repo_dir)
    run_git(["config", "user.email", "test@example.com"], repo_dir)
    run_git(["config", "user.name", "Test User"], repo_dir)
    run_git(["config", "commit.gpgsign", "false"], repo_dir)

    # Initial commit
    (repo_dir / "main.py").write_text("print('hello')")
    run_git(["add", "."], repo_dir)
    run_git(["commit", "-m", "Initial commit"], repo_dir)

    return repo_dir


def test_session_lifecycle(git_repo):
    """
    Tests the full lifecycle using the Session class.
    """
    # 1. Capture initial hash
    initial_hash = run_git(["rev-parse", "HEAD"], cwd=git_repo)
    initial_branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo)

    # Pre-configure .gitignore to prevent manual steps usually handled by UI
    (git_repo / ".gitignore").write_text(SESSION_FILENAME)

    # 2. Init Session
    session = Session(str(git_repo))
    assert session.is_active is False
    assert session.start() is True
    assert session.is_active is True

    # Verify we are on a sandbox branch
    current_branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo)
    assert current_branch.startswith("kopipasta/session-")

    # 3. Check Metadata
    metadata = session.get_metadata()
    assert metadata is not None
    assert metadata["start_commit"] == initial_hash
    assert metadata["parent_branch"] == initial_branch
    assert metadata["session_branch"] == current_branch

    # 4. Simulate Work
    (git_repo / "feature.py").write_text("print('feature')")
    run_git(["add", "."], git_repo)
    run_git(["commit", "-m", "WIP: feature"], git_repo)

    # Verify we moved forward
    new_hash = run_git(["rev-parse", "HEAD"], cwd=git_repo)
    assert new_hash != initial_hash

    # Create an untracked file to test pre-finish auto-commit logic
    (git_repo / "untracked.py").write_text("print('untracked')")

    # 5. Finish (Squash)
    assert session.finish(squash=True) is True
    assert session.is_active is False

    # Verify HEAD is back to the parent branch
    final_branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo)
    assert final_branch == initial_branch

    # Verify session branch is deleted
    branches = run_git(["branch"], cwd=git_repo)
    assert current_branch not in branches

    # Verify changes are staged
    status = run_git(["status", "--porcelain"], cwd=git_repo)
    assert "A  feature.py" in status
    assert "A  untracked.py" in status


def test_init_session_dirty_check(git_repo):
    """Tests that session fails to start if git is dirty."""
    (git_repo / "main.py").write_text("print('modified')")

    session = Session(str(git_repo))
    # Passing a mock printer to silence output
    assert session.start(console_printer=lambda x: None) is False
    assert session.is_active is False


def test_auto_commit_changes(git_repo):
    """Tests the auto_commit utility."""
    session = Session(str(git_repo))

    # Make a change
    (git_repo / "auto.txt").write_text("auto content")

    # Run auto commit
    assert session.auto_commit(message="Auto commit test") is True

    # Verify clean status
    status = run_git(["status", "--porcelain"], cwd=git_repo)
    assert status == ""
