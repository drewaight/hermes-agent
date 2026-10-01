"""Regression test for NousResearch/hermes-agent#86574.

"Kanban project worktrees start from stale local HEAD instead of fetched
origin/main": ``_ensure_git_worktree`` based a brand-new task branch on the
primary checkout's local ``HEAD``, which nobody keeps up to date between
dispatches. A new worktree now fetches ``origin`` and bases on the verified
``origin/main`` (falling back to the remote's actual default branch, then to
local ``HEAD`` for a local-only repo with no reachable remote). Reusing an
EXISTING branch is untouched — reviews, reworks and handoff recoveries keep
exactly the history the owner left.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


def _git(*args: str, cwd: str) -> str:
    result = subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


def _rev_parse(cwd: Path, ref: str) -> str:
    return _git("rev-parse", ref, cwd=str(cwd)).strip()


def test_new_worktree_branch_bases_on_origin_main_not_stale_local_head(tmp_path):
    """Bare origin; remote main advances; the primary checkout's local main
    advances independently and is never fetched before the worktree is made.
    A brand-new task branch must come up at origin/main, not the stale local HEAD."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(origin), cwd=str(tmp_path))

    # The primary checkout (what the dispatcher's repo_root points at).
    primary = tmp_path / "primary"
    _git("clone", str(origin), str(primary), cwd=str(tmp_path))
    for cfg in (("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
        _git(*cfg, cwd=str(primary))
    (primary / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", "README.md", cwd=str(primary))
    _git("commit", "-m", "init", cwd=str(primary))
    _git("push", "origin", "main", cwd=str(primary))

    # Remote advances via a second, independent clone (simulates a teammate's push).
    other = tmp_path / "other"
    _git("clone", str(origin), str(other), cwd=str(tmp_path))
    for cfg in (("config", "user.email", "t2@example.com"), ("config", "user.name", "t2")):
        _git(*cfg, cwd=str(other))
    (other / "README.md").write_text("remote advanced\n", encoding="utf-8")
    _git("commit", "-am", "remote advance", cwd=str(other))
    _git("push", "origin", "main", cwd=str(other))
    origin_main_sha = _rev_parse(other, "HEAD")

    # The primary checkout's local main ALSO advances independently, and is
    # never fetched/pulled — exactly the staleness in the issue (dd4483a).
    (primary / "README.md").write_text("local advanced independently\n", encoding="utf-8")
    _git("commit", "-am", "local-only advance", cwd=str(primary))
    stale_local_head = _rev_parse(primary, "HEAD")
    assert stale_local_head != origin_main_sha  # sanity: the two histories genuinely differ

    target = primary / ".worktrees" / "t_new"
    kbw._ensure_git_worktree(primary, target, "wt/t_new")

    assert _rev_parse(target, "HEAD") == origin_main_sha
    assert _rev_parse(target, "HEAD") != stale_local_head


def test_existing_branch_is_reused_unchanged(tmp_path):
    """A branch that already exists (review, rework, handoff recovery) must
    keep its own history exactly as the owner left it — never rebased onto
    origin/main by the worktree-creation path."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(origin), cwd=str(tmp_path))
    primary = tmp_path / "primary"
    _git("clone", str(origin), str(primary), cwd=str(tmp_path))
    for cfg in (("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
        _git(*cfg, cwd=str(primary))
    (primary / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", "README.md", cwd=str(primary))
    _git("commit", "-m", "init", cwd=str(primary))
    _git("push", "origin", "main", cwd=str(primary))

    # Pre-existing task branch with its own commit, independent of main.
    _git("branch", "wt/existing", "main", cwd=str(primary))
    worktree_a = primary / ".worktrees" / "t_a"
    kbw._ensure_git_worktree(primary, worktree_a, "wt/existing")
    (worktree_a / "review.txt").write_text("under review\n", encoding="utf-8")
    _git("add", "review.txt", cwd=str(worktree_a))
    for cfg in (("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
        _git(*cfg, cwd=str(worktree_a))
    _git("commit", "-m", "review work", cwd=str(worktree_a))
    existing_head = _rev_parse(worktree_a, "HEAD")
    _git("worktree", "remove", "--force", str(worktree_a), cwd=str(primary))

    # Origin advances in the meantime.
    other = tmp_path / "other"
    _git("clone", str(origin), str(other), cwd=str(tmp_path))
    for cfg in (("config", "user.email", "t2@example.com"), ("config", "user.name", "t2")):
        _git(*cfg, cwd=str(other))
    (other / "README.md").write_text("remote advanced\n", encoding="utf-8")
    _git("commit", "-am", "remote advance", cwd=str(other))
    _git("push", "origin", "main", cwd=str(other))

    # Re-materializing the SAME existing branch must not touch its history.
    worktree_b = primary / ".worktrees" / "t_b"
    kbw._ensure_git_worktree(primary, worktree_b, "wt/existing")

    assert _rev_parse(worktree_b, "HEAD") == existing_head


def test_local_only_repo_falls_back_to_head(tmp_path):
    """No remote at all: new branches still come up at local HEAD, same as before."""
    local_repo = tmp_path / "local_only"
    local_repo.mkdir()
    _git("init", "-b", "main", str(local_repo), cwd=str(tmp_path))
    for cfg in (("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
        _git(*cfg, cwd=str(local_repo))
    (local_repo / "README.md").write_text("local only\n", encoding="utf-8")
    _git("add", "README.md", cwd=str(local_repo))
    _git("commit", "-m", "init", cwd=str(local_repo))
    local_head = _rev_parse(local_repo, "HEAD")

    target = local_repo / ".worktrees" / "t_local"
    kbw._ensure_git_worktree(local_repo, target, "wt/t_local")

    assert _rev_parse(target, "HEAD") == local_head


def _git_init_project(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    project.mkdir()
    _git("init", "-b", "main", str(project), cwd=str(tmp_path))
    for cfg in (("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
        _git(*cfg, cwd=str(project))
    (project / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", "README.md", cwd=str(project))
    _git("commit", "-m", "init", cwd=str(project))
    return project


def test_fetch_failure_falls_back_to_head_and_warns(tmp_path, monkeypatch, caplog):
    """``git fetch origin`` failing (bad remote, no connectivity, credential
    problem) must fall back to local HEAD exactly like a local-only repo —
    never raise, never hang — and log a warning naming the repo and reason."""
    primary = _git_init_project(tmp_path, "primary")
    # A remote named "origin" that cannot be fetched (no such path).
    _git("remote", "add", "origin", str(tmp_path / "does-not-exist.git"), cwd=str(primary))
    local_head = _rev_parse(primary, "HEAD")

    target = primary / ".worktrees" / "t_new"
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        kbw._ensure_git_worktree(primary, target, "wt/t_new")

    assert _rev_parse(target, "HEAD") == local_head
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("wt/t_new" in r.getMessage() and str(primary) in r.getMessage() for r in warnings)
    assert any("fetch" in r.getMessage().lower() for r in warnings)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_fetch_failure_records_event_on_card(kanban_home, tmp_path):
    """When a conn + task_id are available, the HEAD fallback is also
    recorded as a durable event on the card."""
    primary = _git_init_project(tmp_path, "primary")
    _git("remote", "add", "origin", str(tmp_path / "does-not-exist.git"), cwd=str(primary))

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="worktree card", assignee="alice",
            workspace_kind="worktree", workspace_path=str(primary), branch_name="wt/card",
        )
        target = primary / ".worktrees" / tid
        kbw._ensure_git_worktree(primary, target, "wt/card", conn=conn, task_id=tid)

        rows = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
        ).fetchall()
        fallback_events = [r for r in rows if r["kind"] == "worktree_new_branch_head_fallback"]
        assert len(fallback_events) == 1
        payload = json.loads(fallback_events[0]["payload"])
        assert payload["branch"] == "wt/card"
        assert "fetch" in payload["reason"].lower()
