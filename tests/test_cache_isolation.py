"""The cache used to be a single global directory shared by every project.

Because it stores *relative* paths, that was not a mere clobber: two repos
that both have a `src/main.py` would silently inherit each other's selection,
and the `os.path.exists()` filter hid it. These tests are written around that
specific failure, not around the implementation that replaced it.
"""

import json
import os
import sys

import pytest

from kopipasta import cache


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate the machine-wide cache root so tests never touch the real one."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(cache.Path, "home", staticmethod(lambda: fake_home))
    return fake_home


def make_repo(tmp_path, name):
    """Two repos with an IDENTICAL relative layout — the dangerous case."""
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.py").write_text(f"# {name}\n", encoding="utf-8")
    (repo / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    return repo


def sel(*paths):
    return [(p, False, None, "text") for p in paths]


# -- the bug ---------------------------------------------------------------


def test_a_selection_does_not_leak_into_another_repo(home, tmp_path, monkeypatch):
    a, b = make_repo(tmp_path, "repo_a"), make_repo(tmp_path, "repo_b")

    monkeypatch.chdir(a)
    cache.save_selection_to_cache(sel(os.path.join("src", "main.py")))

    monkeypatch.chdir(b)
    # The old code returned repo A's list here, because 'src/main.py' also
    # exists in repo B and therefore survived the existence filter.
    assert cache.load_selection_from_cache() == []


def test_a_task_description_does_not_leak_into_another_repo(
    home, tmp_path, monkeypatch
):
    """The worst of the four: task text is prose, often confidential, and it
    was loaded as the default task in whatever repo you opened next."""
    a, b = make_repo(tmp_path, "repo_a"), make_repo(tmp_path, "repo_b")

    monkeypatch.chdir(a)
    cache.save_task_to_cache("port the acme.com billing integration")

    monkeypatch.chdir(b)
    assert cache.load_task_from_cache() is None


def test_a_map_does_not_leak_into_another_repo(home, tmp_path, monkeypatch):
    a, b = make_repo(tmp_path, "repo_a"), make_repo(tmp_path, "repo_b")

    monkeypatch.chdir(a)
    cache.save_map_to_cache([os.path.join("src", "main.py")])

    monkeypatch.chdir(b)
    assert cache.load_map_from_cache() == []


def test_clearing_one_repo_does_not_wipe_another(home, tmp_path, monkeypatch):
    """clear_cache() removed the shared files, so ending a session in one repo
    silently destroyed every other repo's saved state."""
    a, b = make_repo(tmp_path, "repo_a"), make_repo(tmp_path, "repo_b")

    monkeypatch.chdir(a)
    cache.save_selection_to_cache(sel("README.md"))
    monkeypatch.chdir(b)
    cache.save_selection_to_cache(sel("README.md"))

    cache.clear_cache()
    assert cache.load_selection_from_cache() == []

    monkeypatch.chdir(a)
    assert cache.load_selection_from_cache() == ["README.md"]


# -- the isolation must not be so strong that the feature stops working ----


def test_the_same_repo_still_gets_its_selection_back(home, tmp_path, monkeypatch):
    a = make_repo(tmp_path, "repo_a")
    monkeypatch.chdir(a)

    cache.save_selection_to_cache(sel("README.md", os.path.join("src", "main.py")))
    assert cache.load_selection_from_cache() == [
        "README.md",
        os.path.join("src", "main.py"),
    ]


def test_deleted_files_are_still_filtered_out(home, tmp_path, monkeypatch):
    a = make_repo(tmp_path, "repo_a")
    monkeypatch.chdir(a)

    cache.save_selection_to_cache(sel("README.md", "gone.py"))
    assert cache.load_selection_from_cache() == ["README.md"]


# -- keying ----------------------------------------------------------------


def test_two_repos_get_different_keys(tmp_path):
    a, b = make_repo(tmp_path, "repo_a"), make_repo(tmp_path, "repo_b")
    assert cache.get_project_key(str(a)) != cache.get_project_key(str(b))


def test_the_key_is_stable_across_calls(tmp_path):
    a = make_repo(tmp_path, "repo_a")
    assert cache.get_project_key(str(a)) == cache.get_project_key(str(a))


def test_the_key_survives_a_trailing_slash_or_dot_segment(tmp_path):
    a = make_repo(tmp_path, "repo_a")
    assert cache.get_project_key(str(a)) == cache.get_project_key(str(a) + os.sep)
    assert cache.get_project_key(str(a)) == cache.get_project_key(
        os.path.join(str(a), "src", "..")
    )


@pytest.mark.skipif(
    sys.platform not in ("win32", "darwin"),
    reason="Linux paths are genuinely case-sensitive; folding there would merge real projects",
)
def test_case_differences_do_not_split_one_repo_into_two_caches(tmp_path):
    """C:\\Foo and c:\\foo are the same directory on Windows, and so are
    /Users/me/Repo and /Users/me/repo on a default macOS filesystem. Hashing
    the raw string would hand one project two caches depending on how it was
    typed. os.path.normcase covers Windows only — it is a no-op on POSIX —
    which is why cache.py folds explicitly on darwin."""
    a = make_repo(tmp_path, "repo_a")
    assert cache.get_project_key(str(a)) == cache.get_project_key(str(a).upper())


def test_the_key_is_readable_and_filesystem_safe(tmp_path):
    a = make_repo(tmp_path, "repo_a")
    key = cache.get_project_key(str(a))
    assert key.startswith("repo_a-")
    assert not set(key) & set('<>:"/\\|?*')


def test_the_cache_dir_records_which_path_it_belongs_to(home, tmp_path, monkeypatch):
    a = make_repo(tmp_path, "repo_a")
    monkeypatch.chdir(a)

    stamp = json.loads(
        (cache.get_cache_file_path() / "project.json").read_text(encoding="utf-8")
    )
    assert os.path.normcase(stamp["root"]) == os.path.normcase(str(a.resolve()))


def test_the_cache_lives_outside_the_project(home, tmp_path, monkeypatch):
    """Storing it in the repo would need a .gitignore entry everywhere and
    breaks on read-only checkouts."""
    a = make_repo(tmp_path, "repo_a")
    monkeypatch.chdir(a)

    cache.save_selection_to_cache(sel("README.md"))
    assert not (a / ".kopipasta").exists()
    assert str(a.resolve()) not in str(cache.get_cache_file_path())


# -- legacy files ----------------------------------------------------------


def test_pre_upgrade_global_files_are_never_read(home, tmp_path, monkeypatch):
    """Attributing them to whichever repo runs first would recreate the bug."""
    a = make_repo(tmp_path, "repo_a")
    root = cache.get_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "last_selection.json").write_text('["README.md"]', encoding="utf-8")
    (root / "last_task.txt").write_text("someone else's task", encoding="utf-8")

    monkeypatch.chdir(a)
    assert cache.load_selection_from_cache() == []
    assert cache.load_task_from_cache() is None


def test_an_explicit_clear_sweeps_the_legacy_files(home, tmp_path, monkeypatch):
    a = make_repo(tmp_path, "repo_a")
    root = cache.get_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "last_selection.json").write_text('["README.md"]', encoding="utf-8")

    monkeypatch.chdir(a)
    cache.clear_cache()
    assert not (root / "last_selection.json").exists()


# -- running from a subdirectory -------------------------------------------


def make_git_repo(tmp_path, name):
    repo = make_repo(tmp_path, name)
    (repo / ".git").mkdir()
    return repo


def test_a_subdirectory_shares_the_repo_cache(home, tmp_path, monkeypatch):
    """Keying on cwd alone gave `repo/` and `repo/src/` separate caches, so
    running one directory down silently looked like a first run."""
    repo = make_git_repo(tmp_path, "repo_a")

    monkeypatch.chdir(repo)
    cache.save_selection_to_cache(sel(os.path.join("src", "main.py")))

    monkeypatch.chdir(repo / "src")
    assert cache.get_project_key() == cache.get_project_key(str(repo))
    # ...and the paths must still resolve, from a different cwd.
    assert cache.load_selection_from_cache() == ["main.py"]


def test_paths_saved_in_a_subdirectory_resolve_at_the_root(home, tmp_path, monkeypatch):
    repo = make_git_repo(tmp_path, "repo_a")

    monkeypatch.chdir(repo / "src")
    cache.save_selection_to_cache(sel("main.py"))

    monkeypatch.chdir(repo)
    assert cache.load_selection_from_cache() == [os.path.join("src", "main.py")]


def test_a_git_worktree_file_is_still_a_root(home, tmp_path, monkeypatch):
    """.git is a FILE in worktrees and submodules, not a directory."""
    repo = make_repo(tmp_path, "repo_a")
    (repo / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")

    assert cache.find_project_root(str(repo / "src")) == repo.resolve()


def test_two_repos_under_one_parent_stay_separate(home, tmp_path, monkeypatch):
    """The root walk must stop at the nearest .git, not the outermost."""
    a, b = make_git_repo(tmp_path, "repo_a"), make_git_repo(tmp_path, "repo_b")
    assert cache.get_project_key(str(a)) != cache.get_project_key(str(b))


# -- project root discovery ------------------------------------------------


def test_pyproject_toml_is_root_from_subdirectory(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    sub = project / "src" / "pkg"
    sub.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'proj'\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(sub)
    assert cache.find_project_root() == project.resolve()


@pytest.mark.parametrize("manifest", ["package.json", "go.mod", "Cargo.toml"])
def test_project_manifest_is_root_from_subdirectory(tmp_path, monkeypatch, manifest):
    project = tmp_path / "proj"
    sub = project / "src" / "pkg"
    sub.mkdir(parents=True)
    (project / manifest).write_text("", encoding="utf-8")

    monkeypatch.chdir(sub)
    assert cache.find_project_root() == project.resolve()


@pytest.mark.parametrize("vcs_marker", [".hg", ".jj", ".svn"])
def test_other_vcs_roots_are_recognised(tmp_path, monkeypatch, vcs_marker):
    repo = tmp_path / "repo"
    sub = repo / "src"
    sub.mkdir(parents=True)
    (repo / vcs_marker).mkdir()

    monkeypatch.chdir(sub)
    assert cache.find_project_root() == repo.resolve()


def test_git_file_worktree_marker_is_recognised(tmp_path, monkeypatch):
    """A .git file (created by git worktrees and submodules) must be recognised
    as a VCS root. Checking .is_dir() instead of .exists() would pass the
    directory-based tests but fail this one, breaking worktrees and submodules."""
    repo = tmp_path / "worktree_repo"
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    (repo / ".git").write_text(
        "gitdir: /path/to/main/repo/.git/worktrees/worktree_repo\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(sub)
    assert cache.find_project_root() == repo.resolve()


def test_git_repo_takes_precedence_over_nested_package_manifest_in_monorepo(
    tmp_path, monkeypatch
):
    """In a JS/Python monorepo (repo/.git with repo/packages/a/package.json),
    running from packages/a must resolve to repo (the VCS root), NOT packages/a.
    A single-pass search would stop at package.json and split session stores,
    caches, and provider keys for monorepo users."""
    root = tmp_path / "monorepo"
    pkg = root / "packages" / "a"
    pkg.mkdir(parents=True)
    (root / ".git").mkdir()
    (pkg / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.chdir(pkg)
    assert cache.find_project_root() == root.resolve()


def test_no_marker_anywhere_falls_back_to_cwd(tmp_path, monkeypatch):
    plain_dir = tmp_path / "plain" / "sub"
    plain_dir.mkdir(parents=True)

    monkeypatch.chdir(plain_dir)
    assert cache.find_project_root() == plain_dir.resolve()


# -- degradation: a cache must never be why the tool fails ------------------


def test_an_unusable_home_does_not_crash(tmp_path, monkeypatch):
    def no_home():
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(cache.Path, "home", staticmethod(no_home))
    assert cache.get_cache_root()  # falls back to temp rather than raising


def test_an_unwritable_cache_root_degrades_to_a_warning(
    home, tmp_path, monkeypatch, capsys
):
    a = make_repo(tmp_path, "repo_a")
    monkeypatch.chdir(a)

    def denied(*args, **kwargs):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(cache.Path, "mkdir", denied)

    assert cache.load_selection_from_cache() == []
    assert cache.load_task_from_cache() is None
    cache.save_selection_to_cache(sel("README.md"))  # must not raise
    assert "Warning" in capsys.readouterr().err


def test_cache_warnings_go_to_stderr_not_stdout(home, tmp_path, monkeypatch, capsys):
    """Narration on stdout corrupts the --json contract (spec 11.2b)."""
    a = make_repo(tmp_path, "repo_a")
    monkeypatch.chdir(a)
    cache.get_selection_cache_file().write_text("{not json", encoding="utf-8")

    assert cache.load_selection_from_cache() == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Warning" in captured.err


def test_a_failed_write_leaves_no_temp_file_behind(home, tmp_path, monkeypatch):
    a = make_repo(tmp_path, "repo_a")
    monkeypatch.chdir(a)
    cache_dir = cache.get_cache_file_path()

    monkeypatch.setattr(
        cache.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    cache.save_selection_to_cache(sel("README.md"))

    assert [p.name for p in cache_dir.glob("*.tmp")] == []


def test_a_transient_windows_lock_is_retried_not_lost(home, tmp_path, monkeypatch):
    """os.replace fails with PermissionError while another process holds the
    destination open. The window is short; giving up would drop the write."""
    a = make_repo(tmp_path, "repo_a")
    monkeypatch.chdir(a)

    real_replace = os.replace
    cache.get_cache_file_path()  # settle the project.json stamp first
    calls = []

    def flaky(src, dst):
        if not str(dst).endswith("last_selection.json"):
            return real_replace(src, dst)
        calls.append(src)
        if len(calls) < 3:
            raise PermissionError("being used by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(cache.os, "replace", flaky)
    cache.save_selection_to_cache(sel("README.md"))

    assert len(calls) == 3
    assert cache.load_selection_from_cache() == ["README.md"]


# -- concurrency -----------------------------------------------------------


def test_a_concurrent_write_never_leaves_a_half_written_file(
    home, tmp_path, monkeypatch
):
    """Agents run things in parallel. A torn write shows up as a corruption
    warning on the next read, which reads as data loss to the user."""
    import threading

    a = make_repo(tmp_path, "repo_a")
    monkeypatch.chdir(a)
    cache.save_selection_to_cache(sel("README.md"))

    stop = threading.Event()
    torn = []

    def writer():
        while not stop.is_set():
            cache.save_selection_to_cache(
                sel("README.md", os.path.join("src", "main.py"))
            )

    def reader():
        while not stop.is_set():
            try:
                text = cache.get_selection_cache_file().read_text(encoding="utf-8")
            except (FileNotFoundError, PermissionError):
                continue
            try:
                json.loads(text)
            except json.JSONDecodeError:
                torn.append(text)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    threading.Event().wait(0.5)
    stop.set()
    for t in threads:
        t.join()

    assert torn == []
