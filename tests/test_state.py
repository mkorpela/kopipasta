"""Tests for session state directory resolution — spec §7."""

import os
from pathlib import Path

import pytest

from kopipasta.cache import get_project_key
from kopipasta.core.errors import UsageError
from kopipasta.core.state import resolve_state_root


def test_01_default_in_normal_repo_resolves_to_git_dir(tmp_path: Path) -> None:
    """1. Default in a normal repo resolves to <root>/.git/kopipasta."""
    repo = tmp_path / "my-repo"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)

    res = resolve_state_root(str(repo), env={})
    assert res.kind == "git"
    assert res.source == "built-in default"
    assert res.path == os.path.normpath(str(git_dir / "kopipasta"))
    assert res.project_root == str(repo.resolve())


def test_02_display_in_normal_repo_is_repo_relative_forward_slash(
    tmp_path: Path,
) -> None:
    """2. .display for that case is exactly .git/kopipasta."""
    repo = tmp_path / "my-repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)

    res = resolve_state_root(str(repo), env={})
    assert res.display == ".git/kopipasta"


def test_03_in_worktree_true_for_git_and_repo_false_for_xdg_and_temp(
    tmp_path: Path,
) -> None:
    """3. .in_worktree is True for git/repo, False for xdg/temp."""
    repo = tmp_path / "my-repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)

    git_res = resolve_state_root(str(repo), override="git", env={})
    assert git_res.in_worktree is True

    repo_res = resolve_state_root(str(repo), override="repo", env={})
    assert repo_res.in_worktree is True

    xdg_state = tmp_path / "xdg-state"
    xdg_state.mkdir(parents=True, exist_ok=True)
    xdg_res = resolve_state_root(
        str(repo),
        override="xdg",
        env={"XDG_STATE_HOME": str(xdg_state)},
    )
    assert xdg_res.in_worktree is False

    temp_dir = tmp_path / "temp_state"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_res = resolve_state_root(str(repo), override=str(temp_dir), env={})
    assert temp_res.in_worktree is False


def test_04_git_file_with_abs_gitdir_resolves_under_target_path(
    tmp_path: Path,
) -> None:
    """4. A .git FILE containing gitdir: <abs path> resolves under that path."""
    main_repo = tmp_path / "main-repo"
    worktree_git_dir = main_repo / ".git" / "worktrees" / "feature-1"
    worktree_git_dir.mkdir(parents=True, exist_ok=True)

    worktree_root = tmp_path / "worktree-feat1"
    worktree_root.mkdir(parents=True, exist_ok=True)
    dot_git = worktree_root / ".git"
    dot_git.write_text(f"gitdir: {worktree_git_dir}\n", encoding="utf-8")

    res = resolve_state_root(str(worktree_root), env={})
    assert res.kind == "git"
    assert res.path == os.path.normpath(str(worktree_git_dir / "kopipasta"))
    assert res.source == "built-in default"


def test_05_git_file_with_relative_gitdir_resolves_against_project_root(
    tmp_path: Path,
) -> None:
    """5. A .git file with a relative gitdir: resolves against project_root."""
    repo = tmp_path / "submodule-repo"
    repo.mkdir(parents=True, exist_ok=True)
    target_git_dir = tmp_path / "parent-git" / "modules" / "sub"
    target_git_dir.mkdir(parents=True, exist_ok=True)

    rel_path = os.path.relpath(target_git_dir, repo)
    dot_git = repo / ".git"
    dot_git.write_text(f"gitdir: {rel_path}\n", encoding="utf-8")

    res = resolve_state_root(str(repo), env={})
    assert res.kind == "git"
    assert res.path == os.path.normpath(str(target_git_dir / "kopipasta"))


def test_06_git_file_with_invalid_format_falls_through_to_repo(
    tmp_path: Path,
) -> None:
    """6. A .git file with junk is treated as no git directory -> repo."""
    repo = tmp_path / "corrupt-repo"
    repo.mkdir(parents=True, exist_ok=True)
    dot_git = repo / ".git"
    dot_git.write_text("not a valid gitdir reference line\n", encoding="utf-8")

    res = resolve_state_root(str(repo), env={})
    assert res.kind == "repo"
    assert res.path == os.path.normpath(str(repo / ".kopipasta"))
    assert res.source == "built-in default"


def test_07_no_git_directory_defaults_to_repo(tmp_path: Path) -> None:
    """7. No .git at all: default is <root>/.kopipasta, kind repo."""
    repo = tmp_path / "plain-dir"
    repo.mkdir(parents=True, exist_ok=True)

    res = resolve_state_root(str(repo), env={})
    assert res.kind == "repo"
    assert res.path == os.path.normpath(str(repo / ".kopipasta"))
    assert res.source == "built-in default"


def test_08_git_dir_in_env_overrides_dot_git_directory(tmp_path: Path) -> None:
    """8. GIT_DIR in env wins over <root>/.git."""
    repo = tmp_path / "repo-with-git"
    (repo / ".git").mkdir(parents=True, exist_ok=True)

    custom_git = tmp_path / "custom-git-location"
    custom_git.mkdir(parents=True, exist_ok=True)

    res = resolve_state_root(
        str(repo),
        env={"GIT_DIR": str(custom_git)},
    )
    assert res.kind == "git"
    assert res.path == os.path.normpath(str(custom_git / "kopipasta"))
    assert res.source == "built-in default"


def test_09_precedence_hierarchy_asserts_source_at_all_levels(
    tmp_path: Path,
) -> None:
    """9. Precedence: flag -> env -> config -> default, asserting source."""
    repo = tmp_path / "hierarchy-repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)

    cfg_file = tmp_path / "config" / "config.toml"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text('[state]\ndir = "repo"\n', encoding="utf-8")

    flag_val = str(tmp_path / "from-flag")
    env_val = str(tmp_path / "from-env")

    # Level 1: Flag beats env, config, and default
    res_flag = resolve_state_root(
        str(repo),
        override=flag_val,
        env={"KOPIPASTA_STATE_DIR": env_val},
        config_path=cfg_file,
    )
    assert res_flag.source == "--state-dir"
    assert res_flag.path == os.path.normpath(flag_val)

    # Level 2: Env beats config and default
    res_env = resolve_state_root(
        str(repo),
        override=None,
        env={"KOPIPASTA_STATE_DIR": env_val},
        config_path=cfg_file,
    )
    assert res_env.source == "KOPIPASTA_STATE_DIR"
    assert res_env.path == os.path.normpath(env_val)

    # Level 3: Config beats default
    res_cfg = resolve_state_root(
        str(repo),
        override=None,
        env={},
        config_path=cfg_file,
    )
    assert res_cfg.source == "config.toml [state] dir"
    assert res_cfg.path == os.path.normpath(str(repo / ".kopipasta"))
    assert res_cfg.kind == "repo"

    # Level 4: Default when nothing configured
    empty_cfg = tmp_path / "empty-cfg" / "config.toml"
    empty_cfg.parent.mkdir(parents=True, exist_ok=True)
    empty_cfg.write_text("", encoding="utf-8")

    res_default = resolve_state_root(
        str(repo),
        override=None,
        env={},
        config_path=empty_cfg,
    )
    assert res_default.source == "built-in default"
    assert res_default.path == os.path.normpath(str(repo / ".git" / "kopipasta"))
    assert res_default.kind == "git"


def test_10_empty_or_whitespace_env_falls_through_to_default(
    tmp_path: Path,
) -> None:
    """10. Empty and whitespace string in KOPIPASTA_STATE_DIR fall through."""
    repo = tmp_path / "env-fallback-repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)

    res_empty = resolve_state_root(
        str(repo),
        env={"KOPIPASTA_STATE_DIR": ""},
    )
    assert res_empty.source == "built-in default"
    assert res_empty.path == os.path.normpath(str(repo / ".git" / "kopipasta"))

    res_spaces = resolve_state_root(
        str(repo),
        env={"KOPIPASTA_STATE_DIR": "   \t\n  "},
    )
    assert res_spaces.source == "built-in default"
    assert res_spaces.path == os.path.normpath(str(repo / ".git" / "kopipasta"))


def test_11_explicit_relative_path_resolves_against_project_root_not_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11. Explicit relative path resolves against project_root, not cwd."""
    repo = tmp_path / "repo-root"
    sub_dir = repo / "nested" / "pkg"
    sub_dir.mkdir(parents=True, exist_ok=True)

    other_dir = tmp_path / "unrelated-dir"
    other_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(other_dir)

    res = resolve_state_root(
        str(repo),
        override="custom-state-dir",
        env={},
    )
    assert res.kind == "explicit"
    assert res.path == os.path.normpath(str(repo / "custom-state-dir"))
    assert res.path != os.path.normpath(str(other_dir / "custom-state-dir"))


def test_12_explicit_git_without_git_dir_raises_usage_error(
    tmp_path: Path,
) -> None:
    """12. --state-dir git in a directory with no git raises UsageError."""
    repo = tmp_path / "non-git-dir"
    repo.mkdir(parents=True, exist_ok=True)

    with pytest.raises(UsageError) as exc_info:
        resolve_state_root(str(repo), override="git", env={})

    rendered = exc_info.value.render()
    assert "git" in rendered
    assert exc_info.value.exit_code == 1


def test_13_xdg_honours_xdg_state_home_and_contains_project_key(
    tmp_path: Path,
) -> None:
    """13. xdg honours XDG_STATE_HOME and contains get_project_key."""
    repo = tmp_path / "xdg-repo"
    repo.mkdir(parents=True, exist_ok=True)
    custom_xdg = tmp_path / "custom-xdg-state"
    custom_xdg.mkdir(parents=True, exist_ok=True)

    res = resolve_state_root(
        str(repo),
        override="xdg",
        env={"XDG_STATE_HOME": str(custom_xdg)},
    )
    expected_key = get_project_key(str(repo))
    assert res.kind == "xdg"
    assert expected_key in res.path
    assert str(custom_xdg) in res.path
    assert res.path == os.path.normpath(
        str(custom_xdg / "kopipasta" / "projects" / expected_key)
    )


def test_14_display_is_absolute_when_outside_worktree(
    tmp_path: Path,
) -> None:
    """14. .display is an absolute path when not in the worktree."""
    repo = tmp_path / "repo-root"
    repo.mkdir(parents=True, exist_ok=True)

    outside = tmp_path / "outside-state"
    outside.mkdir(parents=True, exist_ok=True)

    res = resolve_state_root(
        str(repo),
        override=str(outside),
        env={},
    )
    assert res.in_worktree is False
    assert res.display == os.path.normpath(str(outside))
    assert os.path.isabs(res.display)


def test_15_xdg_without_home_or_xdg_env_falls_back_to_temp_with_requested_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """15. Explicit xdg with no home or XDG_STATE_HOME lands on temp."""
    repo = tmp_path / "headless-repo"
    repo.mkdir(parents=True, exist_ok=True)

    def _no_home() -> Path:
        raise RuntimeError("No home directory")

    monkeypatch.setattr(Path, "home", _no_home)
    res = resolve_state_root(
        str(repo),
        override="xdg",
        env={"XDG_STATE_HOME": "", "LOCALAPPDATA": ""},
    )
    assert res.source == "--state-dir"
    assert res.kind == "temp"
    assert "kopipasta-state" in res.path
