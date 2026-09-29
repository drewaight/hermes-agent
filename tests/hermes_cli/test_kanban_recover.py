"""Tests for the kanban `recover` verb — supported triage recovery for a task
the unblock-loop breaker routed there (block_loop_detected), distinct from
`specify`/`decompose` which are for a freshly-created triage task awaiting a
spec.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _looped_triage_task(conn, *, parents_done=True, n_parents=0):
    """Build the scenario: a task routed to triage by the unblock-loop
    breaker (block_kind set, block_recurrences at the limit), same shape
    ``block_task``'s ``_route_block`` produces — via direct SQL, since
    driving the real re-block loop end to end isn't this test's concern.
    """
    parent_ids = [
        kb.create_task(conn, title=f"parent{i}", assignee="setup")
        for i in range(n_parents)
    ]
    task_id = kb.create_task(conn, title="looped", parents=parent_ids, assignee="setup")
    conn.execute(
        "UPDATE tasks SET status = 'triage', block_kind = 'capability', "
        "block_recurrences = 2, consecutive_failures = 3, last_failure_error = 'boom' "
        "WHERE id = ?",
        (task_id,),
    )
    if parents_done:
        for pid in parent_ids:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (pid,))
    return task_id, parent_ids


def test_recover_lands_ready_and_resets_loop_counters(conn):
    task_id, _ = _looped_triage_task(conn, n_parents=0)
    ok, err = kb.recover_triage_task(conn, task_id, actor="tester", reason="qualifying gate is done")
    assert ok and err is None
    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert task.block_kind is None
    assert task.block_recurrences == 0
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None


def test_recover_lands_todo_when_parent_still_open(conn):
    task_id, (parent,) = _looped_triage_task(conn, parents_done=False, n_parents=1)
    ok, err = kb.recover_triage_task(conn, task_id, actor="tester", reason="qualification")
    assert ok and err is None
    assert kb.get_task(conn, task_id).status == "todo"


def test_recover_refuses_without_reason(conn):
    task_id, _ = _looped_triage_task(conn)
    ok, err = kb.recover_triage_task(conn, task_id, actor="tester", reason="")
    assert not ok
    assert "reason" in err
    assert kb.get_task(conn, task_id).status == "triage"


def test_recover_refuses_non_triage_task(conn):
    task_id = kb.create_task(conn, title="not triage", assignee="setup")
    status_before = kb.get_task(conn, task_id).status
    ok, err = kb.recover_triage_task(conn, task_id, actor="tester", reason="qualification")
    assert not ok
    assert "triage" in err
    assert kb.get_task(conn, task_id).status == status_before


def test_recover_refuses_fresh_triage_task_with_no_block_kind(conn):
    # A task parked in triage via `create --triage` for specify/decompose,
    # never routed there by the loop breaker.
    task_id = kb.create_task(conn, title="fresh", assignee="setup", triage=True)
    assert kb.get_task(conn, task_id).status == "triage"
    ok, err = kb.recover_triage_task(conn, task_id, actor="tester", reason="qualification")
    assert not ok
    assert "specify" in err
    assert kb.get_task(conn, task_id).status == "triage"


def test_recover_preserves_task_id_deps_assignee_and_run_history(conn):
    task_id, (parent,) = _looped_triage_task(conn, parents_done=True, n_parents=1)
    before = kb.get_task(conn, task_id)
    runs_before = kb.list_runs(conn, task_id)
    ok, _ = kb.recover_triage_task(conn, task_id, actor="tester", reason="qualification")
    assert ok
    after = kb.get_task(conn, task_id)
    assert after.id == before.id
    assert after.assignee == before.assignee
    assert after.workspace_kind == before.workspace_kind
    assert after.workspace_path == before.workspace_path
    parents = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,),
    ).fetchall()
    assert [p["parent_id"] for p in parents] == [parent]
    assert kb.list_runs(conn, task_id) == runs_before


# ---------------------------------------------------------------------------
# CLI `_cmd_recover`
# ---------------------------------------------------------------------------


def _recover_ns(task_id, *, reason=("qualification",), as_json=False):
    return argparse.Namespace(task_id=task_id, reason=list(reason), json=as_json)


def test_cli_recover_succeeds(kanban_home, capsys):
    with kbc.connect() as c:
        task_id, _ = _looped_triage_task(c, n_parents=0)
    rc = kb_cli._cmd_recover(_recover_ns(task_id))
    assert rc == 0
    out = capsys.readouterr().out
    assert task_id in out and "ready" in out
    with kbc.connect() as c:
        assert kb.get_task(c, task_id).status == "ready"


def test_cli_recover_reports_error_and_nonzero_exit(kanban_home, capsys):
    with kbc.connect() as c:
        task_id = kb.create_task(c, title="not triage", assignee="setup")
    rc = kb_cli._cmd_recover(_recover_ns(task_id))
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot recover" in err


def test_cli_recover_refused_for_delegated_worker(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_whatever")
    rc = kb_cli._cmd_recover(_recover_ns("t_whatever"))
    assert rc != 0


# ---------------------------------------------------------------------------
# Public-command regression: `kanban_command()` (the real `hermes kanban recover`
# path) for inherited-but-unowned env vs. a genuinely fenced delegated child.
# ---------------------------------------------------------------------------


def _recover_command_ns(task_id, *, reason=("qualification",)):
    return argparse.Namespace(
        kanban_action="recover", task_id=task_id, reason=list(reason), json=False, board=None,
    )


def test_recover_command_allowed_for_inherited_unfenced_env(kanban_home, monkeypatch):
    """A non-dispatcher-owned execution (e.g. a delegate_task child or cron run) that merely
    *inherited* HERMES_KANBAN_TASK from a dispatcher-owned parent, with no path fence in play,
    must not be denied -- see t_e608e3c4's review of the bare env-var check."""
    from agent.delegation_context import non_dispatcher_owned_context

    with kbc.connect() as c:
        task_id, _ = _looped_triage_task(c, n_parents=0)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_someone_elses_task")
    with non_dispatcher_owned_context():
        rc = kb_cli.kanban_command(_recover_command_ns(task_id))
    assert rc == 0
    with kbc.connect() as c:
        assert kb.get_task(c, task_id).status == "ready"


def test_recover_command_denied_for_fenced_delegated_child(kanban_home, monkeypatch):
    """A genuinely fenced delegate_task child (in-process delegated context) is still denied,
    via the path-fence guard in `kanban_command()`, regardless of ownership semantics."""
    from agent.delegation_context import delegated_child_context

    with kbc.connect() as c:
        task_id, _ = _looped_triage_task(c, n_parents=0)
    with delegated_child_context():
        rc = kb_cli.kanban_command(_recover_command_ns(task_id))
    assert rc != 0
    with kbc.connect() as c:
        assert kb.get_task(c, task_id).status == "triage"
