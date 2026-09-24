import concurrent.futures
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from blackbox._signals import DatabaseIssue
from blackbox.db import connect
from blackbox.ingest import append_claim, ingest
from blackbox.models import Capture, identity
from blackbox.provenance import collect_git
from blackbox.query import claims, integrity, reconstruct, timeline
from blackbox.schema import TABLES, VERSION


@pytest.fixture
def database(tmp_path):
    return tmp_path / "runtime" / "blackbox.sqlite3"


@pytest.fixture
def capture_request():
    return {
        "request_id": "test-001",
        "producer": "test-agent",
        "observations": [
            {"source": "caller", "kind": "test", "name": "unit-tests", "exit_code": 0}
        ],
        "claims": [
            {
                "source": "caller",
                "topic": "tests",
                "statement": "Tests reported as passing",
            }
        ],
        "artifacts": [{"source": "caller", "path": "report.txt", "digest": "0" * 64}],
    }


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return (
            subprocess.check_output(
                ["git", "-C", str(root), *args], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )

    git("init", "-q")
    (root / "tracked.txt").write_text("baseline\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "baseline",
    )
    return root, git


def test_migration_empty_database_and_reopen(database):
    connection = connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == VERSION
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    } == set(TABLES)
    connection.close()
    connect(database).close()
    assert database.stat().st_mode & 0o777 == 0o600
    assert integrity(database) == {"ok": True, "schema_version": VERSION, "errors": []}


def test_capture_reconstruction_and_dedup(database, capture_request):
    result = ingest(database, capture_request)
    before = timeline(database)
    again = ingest(database, capture_request)
    assert again["session_id"] == result["session_id"]
    assert again["duplicate"] is True
    assert timeline(database) == before
    record = reconstruct(database, result["session_id"])
    assert record["status"] == "COMMITTED"
    assert (
        len(record["observations"])
        == len(record["claims"])
        == len(record["artifacts"])
        == 1
    )
    assert record["evidence"][0]["verification"] == "unverified"
    assert record["sources"][0]["authority"] == "caller_asserted"
    assert record["artifacts"][0]["verification"] == "unverified"
    assert integrity(database)["ok"]


def test_conflicting_id_cannot_overwrite(database, capture_request):
    ingest(database, capture_request)
    before = timeline(database)
    capture_request["producer"] = "different"
    with pytest.raises(ValueError, match="different input"):
        ingest(database, capture_request)
    assert timeline(database) == before


def test_claim_corrections_are_append_only_and_temporal(database, capture_request):
    session = ingest(database, capture_request)["session_id"]
    old = claims(database)[0]
    cutoff = timeline(database)[-1]["sequence"]
    correction = {
        "source": "reviewer",
        "topic": "tests",
        "statement": "One test failed",
    }
    successor = append_claim(
        database, session, correction, target=old["id"], relation="supersedes"
    )
    assert successor != old["id"]
    assert claims(database, through=cutoff) == [old]
    assert claims(database)[0]["status"] == "superseded"
    assert claims(database)[0]["statement"] == old["statement"]
    assert (
        append_claim(
            database, session, correction, target=old["id"], relation="supersedes"
        )
        == successor
    )
    correction["statement"] = "Another correction"
    with pytest.raises(sqlite3.IntegrityError):
        append_claim(
            database, session, correction, target=old["id"], relation="supersedes"
        )
    assert len(claims(database)) == 2
    append_claim(database, session, correction, target=successor, relation="contests")
    assert claims(database)[1]["status"] == "contested"


@pytest.mark.parametrize("table", TABLES)
def test_history_rejects_update_delete(database, capture_request, table):
    ingest(database, capture_request)
    connection = connect(database)
    # Even empty tables must have both immutability triggers.
    triggers = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        )
    }
    assert triggers == {f"{table}_no_update", f"{table}_no_delete"}
    if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
        column = connection.execute(f"PRAGMA table_info({table})").fetchone()[1]
        with pytest.raises(sqlite3.IntegrityError, match="immutable history"):
            connection.execute(f"UPDATE {table} SET {column}={column}")
        with pytest.raises(sqlite3.IntegrityError, match="immutable history"):
            connection.execute(f"DELETE FROM {table}")
    connection.close()


def test_read_only_handle_and_foreign_keys(database, capture_request):
    session = ingest(database, capture_request)["session_id"]
    connection = connect(database, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO sessions VALUES ('a','b','c','d','e')")
    connection.close()
    with pytest.raises(ValueError, match="correction target"):
        append_claim(
            database,
            session,
            capture_request["claims"][0],
            target="missing",
            relation="contests",
        )
    assert len(claims(database)) == 1


def test_corrections_preserve_both_originating_sessions(database, capture_request):
    first = ingest(database, capture_request)["session_id"]
    target = claims(database)[0]["id"]
    before = reconstruct(database, first)
    second = ingest(database, {**capture_request, "request_id": "test-002"})[
        "session_id"
    ]
    key = append_claim(
        database,
        second,
        capture_request["claims"][0],
        target=target,
        relation="contests",
    )
    assert len(claims(database)) == 3
    assert reconstruct(database, first) == before
    assert next(c for c in claims(database) if c["id"] == key)["session_id"] == second
    assert (
        next(c for c in claims(database) if c["id"] == target)["status"] == "contested"
    )


def test_failed_capture_retries_atomically(database, capture_request, monkeypatch):
    module = importlib.import_module("blackbox.ingest")
    original = module.claim_row

    def fail(*args, **kwargs):
        raise OSError("sensitive internal failure detail")

    monkeypatch.setattr(module, "claim_row", fail)
    with pytest.raises(OSError):
        ingest(database, capture_request)
    session = identity("ses", capture_request["request_id"])
    failed = reconstruct(database, session)
    assert failed["status"] == "FAILED_RETRYABLE"
    assert failed["observations"] == failed["evidence"] == failed["claims"] == []
    assert "sensitive internal" not in json.dumps(failed)
    monkeypatch.setattr(module, "claim_row", original)
    ingest(database, capture_request)
    assert reconstruct(database, session)["status"] == "COMMITTED"
    assert len(reconstruct(database, session)["failures"]) == 1


def test_process_crash_releases_transaction_and_allows_retry(database, capture_request):
    code = """
import importlib,json,os,sys
m=importlib.import_module('blackbox.ingest')
m.claim_row=lambda *a,**kw: os._exit(73)
m.ingest(sys.argv[1],json.loads(sys.argv[2]))
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(database), json.dumps(capture_request)],
        check=False,
    )
    assert child.returncode == 73
    session = identity("ses", capture_request["request_id"])
    record = reconstruct(database, session)
    assert record["status"] == "RESERVED"
    assert record["observations"] == []
    ingest(database, capture_request)
    assert reconstruct(database, session)["status"] == "COMMITTED"
    assert integrity(database)["ok"]


def test_concurrent_duplicate_delivery(database, capture_request):
    connect(database).close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ingest(database, capture_request), range(8)))
    assert sum(not item["duplicate"] for item in results) == 1
    assert len({item["session_id"] for item in results}) == 1
    assert len(claims(database)) == 1


def test_concurrent_supersede_one_winner(database, capture_request):
    session = ingest(database, capture_request)["session_id"]
    target = claims(database)[0]["id"]

    def write(number):
        try:
            append_claim(
                database,
                session,
                {
                    "source": "caller",
                    "topic": "tests",
                    "statement": f"Correction {number}",
                },
                target=target,
                relation="supersedes",
            )
            return True
        except sqlite3.IntegrityError:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(write, range(2))) == 1


def test_git_baseline_does_not_lose_dirty_or_untracked_files(repo):
    root, git = repo
    baseline = git("rev-parse", "HEAD")
    (root / "committed.txt").write_text("committed")
    git("add", "committed.txt")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "change",
    )
    (root / "tracked.txt").write_text("dirty")
    (root / "new\nfile.txt").write_text("untracked")
    result = collect_git(root, baseline)
    assert result["committed_delta"] == ["committed.txt"]
    assert result["working_tree_delta"] == ["tracked.txt"]
    assert result["untracked_files"] == ["new\nfile.txt"]


def test_staged_and_unstaged_cancellation_is_visible(repo):
    root, git = repo
    (root / "tracked.txt").write_text("staged")
    git("add", "tracked.txt")
    (root / "tracked.txt").write_text("baseline\n")
    result = collect_git(root, git("rev-parse", "HEAD"))
    assert result["working_tree_delta"] == ["tracked.txt"]
    assert result["staged_delta"] == result["unstaged_delta"] == ["tracked.txt"]


def test_observed_repository_config_cannot_run_or_blind_the_git_observer(
    repo, tmp_path, monkeypatch
):
    root, git = repo
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    # fsmonitor v2 hook: return a token and claim that no paths changed.
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf 'token\\0'\n")
    hook.chmod(0o700)
    git("config", "core.fsmonitor", str(hook))
    git("config", "core.fsmonitorHookVersion", "2")
    git("update-index", "--fsmonitor")
    git("status")
    (root / "tracked.txt").write_text("hidden from a trusting observer")
    assert git("diff", "--name-only") == ""
    marker.unlink()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    result = collect_git(root)
    assert not marker.exists()
    assert result["unstaged_delta"] == result["working_tree_delta"] == ["tracked.txt"]


def commit_all(git, message):
    git("add", "-A")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        message,
    )


def test_unstaged_detection_matches_git_diff_without_filters(repo):
    root, git = repo
    for name in ("edited", "deleted", "chmod", "retargeted", "became-link", "same"):
        (root / name).write_text(name + "\n")
    (root / "new\nline").write_text("tracked name with a newline\n")
    (root / "target").write_text("target\n")
    (root / "link").symlink_to("same")
    commit_all(git, "variety")
    (root / "edited").write_text("changed\n")
    (root / "deleted").unlink()
    (root / "chmod").chmod(0o755)
    (root / "link").unlink()
    (root / "link").symlink_to("target")
    (root / "became-link").unlink()
    (root / "became-link").symlink_to("target")
    (root / "new\nline").write_text("changed too\n")
    (root / "same").write_text("same\n")  # rewritten with identical bytes
    git_diff = sorted(
        p
        for p in subprocess.check_output(
            ["git", "-C", str(root), "diff", "--name-only", "-z"]
        )
        .decode()
        .split("\0")
        if p
    )
    assert collect_git(root)["unstaged_delta"] == git_diff
    assert git_diff == sorted(
        ["edited", "deleted", "chmod", "link", "became-link", "new\nline"]
    )


def test_repository_clean_filter_never_runs_and_cannot_hide_changes(repo, tmp_path):
    root, git = repo
    marker = tmp_path / "filter-ran"
    # Hostile clean filter: reports the committed content for any input.
    hook = tmp_path / "clean.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat >/dev/null\necho baseline\n")
    hook.chmod(0o700)
    git("config", "filter.hide.clean", str(hook))
    git("config", "filter.hide.required", "true")
    (root / ".git" / "info" / "attributes").write_text("* filter=hide\n")
    (root / "tracked.txt").write_text("hidden from a trusting observer\n")
    assert git("diff", "--name-only") == ""  # plain Git is blinded
    marker.unlink()
    result = collect_git(root)
    assert not marker.exists()
    assert result["unstaged_delta"] == ["tracked.txt"]


def test_assume_unchanged_cannot_hide_changes(repo):
    root, git = repo
    git("update-index", "--assume-unchanged", "tracked.txt")
    (root / "tracked.txt").write_text("changed behind the index\n")
    assert git("diff", "--name-only") == ""
    assert collect_git(root)["unstaged_delta"] == ["tracked.txt"]


def test_unmerged_paths_are_unstaged_changes(repo):
    root, git = repo
    git("checkout", "-qb", "other")
    (root / "tracked.txt").write_text("other\n")
    commit_all(git, "other")
    git("checkout", "-q", "-")
    (root / "tracked.txt").write_text("main\n")
    commit_all(git, "main")
    with pytest.raises(subprocess.CalledProcessError):
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "merge",
            "-q",
            "other",
        )
    assert git("ls-files", "--unmerged")  # stopped on the conflict, not earlier
    assert collect_git(root)["unstaged_delta"] == ["tracked.txt"]


def test_sha256_repositories_are_hashed_with_sha256(tmp_path):
    root = tmp_path / "sha256"
    subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(root)], check=True
    )

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args]).decode()

    (root / "file.txt").write_text("one\n")
    (root / "kept.txt").write_text("kept\n")
    commit_all(git, "sha256")
    (root / "file.txt").write_text("two\n")
    result = collect_git(root)
    assert len(result["commit_after"]) == 64
    assert result["unstaged_delta"] == ["file.txt"]


def test_submodule_counts_as_changed_only_on_a_different_commit(repo, tmp_path):
    root, git = repo
    sub = root / "sub"
    sub.mkdir()

    def subgit(*args):
        return subprocess.check_output(["git", "-C", str(sub), *args]).decode().strip()

    subgit("init", "-q")
    (sub / "a.txt").write_text("a\n")
    commit_all(subgit, "first")
    first = subgit("rev-parse", "HEAD")
    git("update-index", "--add", "--cacheinfo", f"160000,{first},sub")
    commit_all(git, "add gitlink")
    (sub / "a.txt").write_text("dirty\n")
    assert collect_git(root)["unstaged_delta"] == []
    commit_all(subgit, "second")
    assert collect_git(root)["unstaged_delta"] == ["sub"]


def git_diff_names(root):
    listing = subprocess.check_output(
        ["git", "-C", str(root), "diff", "--name-only", "-z"]
    )
    return sorted(p for p in listing.decode().split("\0") if p)


def test_subfolder_observation_is_repo_wide_and_ignores_core_worktree(
    repo, tmp_path
):
    root, git = repo
    (root / "sub").mkdir()
    (root / "sub" / "f").write_text("f\n")
    (root / "sub" / "staged").write_text("staged\n")
    (root / "committed").write_text("before\n")
    commit_all(git, "subfolder baseline")
    baseline = git("rev-parse", "HEAD")

    (root / "committed").write_text("after\n")
    commit_all(git, "top-level committed change")
    (root / "sub" / "staged").write_text("index change\n")
    git("add", "sub/staged")
    (root / "sub" / "f").write_text("dirty in sub\n")
    (root / "tracked.txt").write_text("dirty at top\n")
    (root / "sub" / "new").write_text("untracked\n")

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "decoy.txt").write_text("not the worktree\n")
    git("config", "core.worktree", str(decoy))

    from_root = collect_git(root, baseline)
    from_subfolder = collect_git(root / "sub", baseline)
    assert from_subfolder == from_root
    assert from_subfolder["committed_delta"] == ["committed"]
    assert from_subfolder["staged_delta"] == ["sub/staged"]
    assert from_subfolder["unstaged_delta"] == ["sub/f", "tracked.txt"]
    assert from_subfolder["working_tree_delta"] == [
        "sub/f",
        "sub/staged",
        "tracked.txt",
    ]
    assert from_subfolder["untracked_files"] == ["sub/new"]


def test_symlinked_parent_is_not_followed_out_of_the_repository(repo, tmp_path):
    root, git = repo
    (root / "dir").mkdir()
    (root / "dir" / "f").write_text("inside\n")
    commit_all(git, "dir")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("inside\n")  # identical bytes, outside the repo
    for child in (root / "dir").iterdir():
        child.unlink()
    (root / "dir").rmdir()
    (root / "dir").symlink_to(outside)
    assert git_diff_names(root) == ["dir/f"]  # Git reports it deleted
    assert collect_git(root)["unstaged_delta"] == ["dir/f"]


def test_fifo_in_place_of_a_tracked_file_cannot_block_capture(repo):
    import threading

    root, _ = repo
    (root / "tracked.txt").unlink()
    os.mkfifo(root / "tracked.txt")
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(collect_git(root)), daemon=True
    )
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "collect_git blocked opening a FIFO"
    assert result["unstaged_delta"] == ["tracked.txt"]


def test_sparse_checkout_is_not_a_flood_of_deletions(repo):
    root, git = repo
    for directory in ("keep", "drop"):
        (root / directory).mkdir()
        (root / directory / "f").write_text(directory + "\n")
    commit_all(git, "sparse")
    git("sparse-checkout", "set", "keep")
    assert not (root / "drop").exists()
    assert git_diff_names(root) == []
    assert collect_git(root)["unstaged_delta"] == []
    # A present skip-worktree file is still compared: the flag cannot hide an edit.
    (root / "drop").mkdir()
    (root / "drop" / "f").write_text("edited under skip-worktree\n")
    assert collect_git(root)["unstaged_delta"] == ["drop/f"]


@pytest.mark.parametrize("index", ["--no-sparse-index", "--sparse-index"])
def test_cone_sparse_checkout_leaves_absent_files_unchanged(repo, index):
    root, git = repo
    for name in ("kept", "dropped"):
        (root / name).mkdir()
        (root / name / "file").write_text(name + "\n")
    commit_all(git, "sparse")
    git("sparse-checkout", "init", "--cone", index)
    git("sparse-checkout", "set", "kept")
    assert not (root / "dropped").exists()
    assert git_diff_names(root) == []
    assert collect_git(root)["unstaged_delta"] == []
    (root / "kept" / "file").write_text("edited\n")
    assert collect_git(root)["unstaged_delta"] == ["kept/file"]


def test_skip_worktree_flag_cannot_hide_edits(repo):
    root, git = repo
    git("update-index", "--skip-worktree", "tracked.txt")
    (root / "tracked.txt").write_text("changed behind the index\n")
    assert git_diff_names(root) == []  # plain Git is blinded
    assert collect_git(root)["unstaged_delta"] == ["tracked.txt"]
    # Documented limit: an absent skip-worktree file looks exactly like sparse
    # checkout, so deleting a flagged file is not observed.
    (root / "tracked.txt").unlink()
    assert collect_git(root)["unstaged_delta"] == []


def test_observing_from_inside_git_dir_uses_the_real_worktree(repo, tmp_path):
    root, git = repo
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "tracked.txt").write_text("baseline\n")
    (decoy / "decoy-only").write_text("x\n")
    (root / "tracked.txt").write_text("edited in the real working tree\n")
    (root / "real-only").write_text("x\n")
    git("config", "core.worktree", str(decoy))
    assert git("diff", "--name-only") == ""  # plain Git now looks at the decoy
    result = collect_git(root / ".git")
    assert result == collect_git(root)
    assert result["unstaged_delta"] == ["tracked.txt"]
    assert result["untracked_files"] == ["real-only"]


def test_submodule_without_a_commit_does_not_abort_capture(repo):
    root, git = repo
    source = root.parent / "submodule-source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "a.txt").write_text("a\n")

    def srcgit(*args):
        return (
            subprocess.check_output(["git", "-C", str(source), *args]).decode().strip()
        )

    commit_all(srcgit, "first")
    git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{srcgit('rev-parse', 'HEAD')},sub",
    )
    # Commit the gitlink alone: `add -A` would stage its removal (no sub/ yet).
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "gitlink",
    )
    (root / "sub").mkdir()
    subprocess.run(["git", "init", "-q", str(root / "sub")], check=True)
    assert collect_git(root)["unstaged_delta"] == ["sub"]


def test_many_directories_do_not_exhaust_file_descriptors(repo):
    root, git = repo
    for index in range(300):
        (root / f"d{index:03}").mkdir()
        (root / f"d{index:03}" / "f").write_text(f"{index}\n")
    commit_all(git, "many directories")
    (root / "d150" / "f").write_text("changed\n")
    script = (
        "import resource, sys\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))\n"
        "from blackbox.provenance import collect_git\n"
        "print(collect_git(sys.argv[1])['unstaged_delta'])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-300:]
    assert result.stdout.strip() == "['d150/f']"


def replace_commit(git, original, tree):
    """Substitute another commit for `original` through refs/replace/."""
    fake = git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit-tree",
        tree,
        "-m",
        "substitute",
    )
    git("replace", original, fake)


def test_replaced_baseline_cannot_hide_committed_changes(repo):
    root, git = repo
    baseline = git("rev-parse", "HEAD")
    (root / "tracked.txt").write_text("agent change\n")
    (root / "added.txt").write_text("added\n")
    commit_all(git, "agent work")
    replace_commit(git, baseline, git("rev-parse", "HEAD^{tree}"))
    assert git("diff", "--name-only", baseline, "HEAD") == ""  # plain Git is fooled
    result = collect_git(root, baseline)
    assert result["commit_before"] == baseline
    assert result["committed_delta"] == ["added.txt", "tracked.txt"]


def test_replaced_head_cannot_hide_staged_changes(repo):
    root, git = repo
    (root / "tracked.txt").write_text("staged edit\n")
    git("add", "tracked.txt")
    replace_commit(git, git("rev-parse", "HEAD"), git("write-tree"))
    assert git("diff", "--cached", "--name-only") == ""  # plain Git is fooled
    result = collect_git(root)
    assert result["staged_delta"] == result["working_tree_delta"] == ["tracked.txt"]


def test_symlinked_git_directory_is_a_repository_like_in_git(repo, tmp_path):
    root, git = repo
    store = tmp_path / "shared-store.git"
    (root / ".git").rename(store)
    (root / ".git").symlink_to(store)
    (root / "tracked.txt").write_text("edited\n")
    assert git("rev-parse", "--show-toplevel") == str(root)  # Git accepts it
    result = collect_git(root)
    assert result["commit_after"] == git("rev-parse", "HEAD")
    assert result["unstaged_delta"] == ["tracked.txt"]


def test_dangling_git_symlink_is_not_a_repository(repo, tmp_path):
    root, _ = repo
    (root / ".git").rename(tmp_path / "moved.git")
    (root / ".git").symlink_to(tmp_path / "missing.git")
    with pytest.raises(ValueError):
        collect_git(root)


def add_submodule(repo):
    """Commit a gitlink at `sub`; return the baseline and a function that stages
    the gitlink at a new submodule commit."""
    root, git = repo
    sub = root / "sub"
    sub.mkdir()

    def subgit(*args):
        return subprocess.check_output(["git", "-C", str(sub), *args]).decode().strip()

    def advance(content):
        (sub / "a.txt").write_text(content)
        commit_all(subgit, content)
        # Newer Git's `add` skips a submodule whose .gitmodules says ignore=all.
        commit = subgit("rev-parse", "HEAD")
        git("update-index", "--cacheinfo", f"160000,{commit},sub")
        assert git("ls-files", "--stage", "sub").split()[1] == commit

    subgit("init", "-q")
    (sub / "a.txt").write_text("a\n")
    commit_all(subgit, "first")
    first = subgit("rev-parse", "HEAD")
    git("update-index", "--add", "--cacheinfo", f"160000,{first},sub")
    commit_all(git, "add gitlink")
    return git("rev-parse", "HEAD"), advance


@pytest.mark.parametrize("setting", ["config", "gitmodules"])
def test_submodule_ignore_settings_cannot_hide_gitlink_changes(repo, setting):
    root, git = repo
    baseline, advance = add_submodule(repo)
    if setting == "config":
        git("config", "diff.ignoreSubmodules", "all")
    else:
        git("config", "-f", ".gitmodules", "submodule.sub.path", "sub")
        git("config", "-f", ".gitmodules", "submodule.sub.ignore", "all")
    advance("second")
    commit_all(git, "bump gitlink")
    advance("third")
    # plain Git is fooled
    assert git("diff", "--cached", "--name-only", "HEAD") == ""
    assert "sub" not in git("diff", "--name-only", baseline, "HEAD").split()
    result = collect_git(root, baseline)
    assert "sub" in result["committed_delta"]
    assert result["staged_delta"] == ["sub"]


def test_core_filemode_false_cannot_hide_executable_bit_changes(repo):
    root, git = repo
    git("config", "core.fileMode", "false")
    (root / "tracked.txt").chmod(0o755)
    assert git("diff", "--name-only") == ""  # plain Git is fooled
    result = collect_git(root)
    assert result["unstaged_delta"] == result["working_tree_delta"] == ["tracked.txt"]


def test_untracked_files_cannot_hide_behind_invisible_ignore_rules(repo, tmp_path):
    root, git = repo
    (root / "info-excluded.txt").write_text("hidden\n")
    (root / ".git" / "info" / "exclude").write_text("info-excluded.txt\n")
    (root / "globally-excluded.txt").write_text("hidden\n")
    global_excludes = tmp_path / "global-excludes"
    global_excludes.write_text("globally-excluded.txt\n")
    git("config", "core.excludesFile", str(global_excludes))
    (root / "d").mkdir()
    (root / "d" / "new.txt").write_text("hidden\n")
    (root / "d" / ".gitignore").write_text("*\n")  # ignores itself too
    (root / "build.log").write_text("ignored\n")
    (root / ".gitignore").write_text("*.log\n")
    commit_all(git, "track .gitignore")
    (root / "build.log").write_text("ignored\n")  # commit_all -A skipped it
    assert git("ls-files", "--others", "--exclude-standard") == ""  # Git hides all
    result = collect_git(root)
    assert result["untracked_files"] == [
        "d/.gitignore",
        "globally-excluded.txt",
        "info-excluded.txt",
    ]


def test_local_observer_authority_cannot_be_claimed_by_input(
    database, capture_request, repo
):
    root, _ = repo
    capture_request["observations"][0]["source"] = "blackbox.git"
    session = ingest(database, capture_request, repo=root)["session_id"]
    record = reconstruct(database, session)
    assert {row["verification"] for row in record["evidence"]} == {
        "unverified",
        "locally_observed",
    }
    assert {row["authority"] for row in record["sources"]} == {
        "caller_asserted",
        "local_git",
    }


@pytest.mark.parametrize(
    "extra",
    ["password", "payload", "stdout", "verified", "observer_authority", "raw_response"],
)
def test_unknown_persisted_fields_rejected(database, capture_request, extra):
    capture_request["observations"][0][extra] = "not-persisted"
    with pytest.raises(ValueError):
        ingest(database, capture_request)
    assert not database.exists()


@pytest.mark.parametrize(
    "value",
    [
        "password=synthetic-only",
        "Bearer synthetic-only",
        "postgresql://synthetic.invalid/db",
        "-----BEGIN " + "PRIVATE KEY-----",
    ],
)
def test_sensitive_shaped_content_rejected_before_storage(
    database, capture_request, value
):
    capture_request["claims"][0]["statement"] = value
    with pytest.raises(ValueError):
        ingest(database, capture_request)
    assert not database.exists()


def test_unknown_schema_is_rejected(database):
    connection = connect(database)
    connection.execute("PRAGMA user_version=999")
    connection.close()
    with pytest.raises(ValueError, match="unsupported"):
        connect(database)


def test_cli_end_to_end_and_safe_errors(database, capture_request, tmp_path):
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps(capture_request))

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "blackbox.cli", "--database", str(database), *args],
            capture_output=True,
            check=False,
            text=True,
        )

    result = cli("capture", "--input", str(input_file))
    assert result.returncode == 0, result.stderr
    session = json.loads(result.stdout)["session_id"]
    assert json.loads(cli("show", session).stdout)["status"] == "COMMITTED"
    assert json.loads(cli("check").stdout)["ok"]
    input_file.write_text(
        json.dumps({**capture_request, "password": "synthetic-private-value"})
    )
    result = cli("capture", "--input", str(input_file))
    assert result.returncode == 1
    assert "synthetic-private-value" not in result.stdout + result.stderr


def test_model_instance_cannot_bypass_validation(database):
    malicious = Capture.model_construct(request_id="x", producer="password=synthetic")
    with pytest.raises(ValueError):
        ingest(database, malicious)


@pytest.mark.parametrize("mode", [0o777, 0o1777, 0o770, 0o707])
def test_database_directory_writable_by_others_is_refused(tmp_path, mode):
    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(mode)
    with pytest.raises(DatabaseIssue):
        connect(directory / "blackbox.sqlite3")
    assert not (directory / "blackbox.sqlite3").exists()


@pytest.mark.parametrize("mode, allowed", [(0o777, False), (0o1777, True)])
def test_database_ancestor_may_be_shared_only_with_the_sticky_bit(
    tmp_path, mode, allowed
):
    ancestor = tmp_path / "ancestor"
    (ancestor / "private").mkdir(parents=True, mode=0o700)
    ancestor.chmod(mode)
    database = ancestor / "private" / "blackbox.sqlite3"
    if allowed:
        connect(database).close()
    else:
        with pytest.raises(DatabaseIssue):
            connect(database)


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to chown")
def test_database_directory_owned_by_another_user_is_refused(tmp_path):
    directory = tmp_path / "theirs"
    directory.mkdir(mode=0o700)
    os.chown(directory, 12345, 12345)
    with pytest.raises(DatabaseIssue):
        connect(directory / "blackbox.sqlite3")


def test_foreign_database_not_adopted(tmp_path):
    path = tmp_path / "foreign.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE important(value TEXT)")
    db.close()
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="non-BlackBox"):
        connect(path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()


def test_machine_readable_schema_matches_model(capture_request):
    import jsonschema

    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/capture.schema.json").read_text()
    )
    assert schema == Capture.model_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(capture_request, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**capture_request, "payload": {}}, schema)
