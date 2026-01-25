import os
import subprocess
import pytest
from pathlib import Path
from kopipasta.session import init_session, get_session_metadata, SESSION_FILENAME, auto_commit_changes

def run_git(cmd: list, cwd: Path):
    subprocess.run(["git"] + cmd, cwd=cwd, check=True, capture_output=True)

@pytest.fixture
def git_repo(tmp_path: Path):
    """Sets up a temporary git repo with an initial commit."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    
    # Init git
    run_git(["init"], repo_dir)
    run_git(["config", "user.email", "test@example.com"], repo_dir)
    run_git(["config", "user.name", "Test User"], repo_dir)
    
    # Initial commit
    (repo_dir / "main.py").write_text("print('hello')")
    run_git(["add", "."], repo_dir)
    run_git(["commit", "-m", "Initial commit"], repo_dir)
    
    return repo_dir

def test_session_lifecycle(git_repo):
    """
    Tests the full lifecycle:
    1. Start Session (capture start_commit)
    2. Make changes & commit (simulate work)
    3. Verify metadata
    4. Simulate 'Finish' (git reset --soft)
    """
    # 1. Capture initial hash
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True)
    initial_hash = result.stdout.strip()
    
    # 2. Init Session
    # Change CWD for the session functions relying on os.getcwd() or absolute paths
    assert init_session(str(git_repo)) is True
    
    session_file = git_repo / SESSION_FILENAME
    assert session_file.exists()
    
    # 3. Check Metadata
    metadata = get_session_metadata(str(git_repo))
    assert metadata is not None
    assert metadata["start_commit"] == initial_hash
    
    # 4. Simulate Work (create file, auto-commit logic would usually do this)
    (git_repo / "feature.py").write_text("print('feature')")
    run_git(["add", "."], git_repo)
    run_git(["commit", "-m", "WIP: feature"], git_repo)
    
    # Verify we moved forward
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True)
    new_hash = result.stdout.strip()
    assert new_hash != initial_hash
    
    # 5. Simulate "Finish Task" (Squash/Soft Reset) logic found in tree_selector.py
    # git reset --soft <start_commit>
    run_git(["reset", "--soft", metadata["start_commit"]], git_repo)
    
    # Verify HEAD is back at initial_hash
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True)
    reset_hash = result.stdout.strip()
    assert reset_hash == initial_hash
    
    # Verify changes are staged (status should show added file)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True).stdout
    assert "A  feature.py" in status or "A  AI_SESSION.md" in status

def test_init_session_dirty_check(git_repo):
    """Tests that session fails to start if git is dirty."""
    (git_repo / "main.py").write_text("print('modified')")
    
    # Should fail because of uncommitted changes
    assert init_session(str(git_repo)) is False
    assert not (git_repo / SESSION_FILENAME).exists()

def test_auto_commit_changes(git_repo):
    """Tests the auto_commit_changes utility."""
    # Ensure clean state
    run_git(["status"], git_repo)
    
    # Make a change
    (git_repo / "auto.txt").write_text("auto content")
    
    # Run auto commit
    committed = auto_commit_changes(str(git_repo), message="Auto commit test")
    
    assert committed is True
    
    # Verify git status is clean
    result = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True)
    assert result.stdout.strip() == ""
    
    # Verify commit message
    log = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=git_repo, capture_output=True, text=True).stdout.strip()
    assert log == "Auto commit test"
    
    # Run again with no changes -> should return False
    committed_again = auto_commit_changes(str(git_repo))
    assert committed_again is False