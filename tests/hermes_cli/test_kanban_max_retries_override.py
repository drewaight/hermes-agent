"""Per-task max_retries (failure-breaker trip threshold) override.

Covers kanban_db.set_max_retries() and the `hermes kanban set-max-retries`
CLI command — OPS-KANBAN-RETRY-BUDGET-API-01.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    c = kbc.connect()
    yield c
    c.close()


def _event_kinds(conn, task_id):
    rows = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,),
    ).fetchall()
    return [r["kind"] for r in rows]


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def test_set_and_clear_max_retries(conn):
    tid = kb.create_task(conn, title="t", assignee="worker", initial_status="blocked")
    assert kb.set_max_retries(conn, tid, 1)
    assert kb.get_task(conn, tid).max_retries == 1

    assert kb.set_max_retries(conn, tid, None)
    assert kb.get_task(conn, tid).max_retries is None


def test_rejects_non_positive_value(conn):
    tid = kb.create_task(conn, title="t", assignee="worker", initial_status="blocked")
    with pytest.raises(ValueError):
        kb.set_max_retries(conn, tid, 0)
    with pytest.raises(ValueError):
        kb.set_max_retries(conn, tid, -1)
    # Rejected write must not have landed.
    assert kb.get_task(conn, tid).max_retries is None


def test_refused_while_running(conn):
    tid = kb.create_task(conn, title="t", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    assert kb.get_task(conn, tid).status == "running"
    with pytest.raises(ValueError):
        kb.set_max_retries(conn, tid, 1)
    assert kb.get_task(conn, tid).max_retries is None


def test_refused_on_archived_task(conn):
    tid = kb.create_task(conn, title="t", assignee="worker", initial_status="blocked")
    assert kb.archive_task(conn, tid)
    with pytest.raises(ValueError):
        kb.set_max_retries(conn, tid, 1)


def test_rejects_non_integer_value(conn):
    tid = kb.create_task(conn, title="t", assignee="worker", initial_status="blocked")
    with pytest.raises(ValueError):
        kb.set_max_retries(conn, tid, 1.5)
    with pytest.raises(ValueError):
        kb.set_max_retries(conn, tid, True)
    with pytest.raises(ValueError):
        kb.set_max_retries(conn, tid, "1")
    # None of the rejected calls landed a value or an audit event.
    assert kb.get_task(conn, tid).max_retries is None
    assert "max_retries_set" not in _event_kinds(conn, tid)


def test_unknown_task_returns_false(conn):
    assert kb.set_max_retries(conn, "t_doesnotexist", 1) is False


def test_records_audit_event(conn):
    tid = kb.create_task(conn, title="t", assignee="worker", initial_status="blocked")
    kb.set_max_retries(conn, tid, 1)
    assert "max_retries_set" in _event_kinds(conn, tid)


# ---------------------------------------------------------------------------
# CLI layer — `hermes kanban set-max-retries`
# ---------------------------------------------------------------------------


def test_cli_set_and_clear(conn, capsys):
    tid = kb.create_task(conn, title="t", assignee="worker", initial_status="blocked")

    rc = kanban_cli._cmd_set_max_retries(argparse.Namespace(task_id=tid, value="1"))
    assert rc == 0
    assert "Set max_retries override" in capsys.readouterr().out
    assert kb.get_task(conn, tid).max_retries == 1

    rc = kanban_cli._cmd_set_max_retries(argparse.Namespace(task_id=tid, value="none"))
    assert rc == 0
    assert "Cleared max_retries override" in capsys.readouterr().out
    assert kb.get_task(conn, tid).max_retries is None

    # Omitted value also clears.
    kb.set_max_retries(conn, tid, 2)
    rc = kanban_cli._cmd_set_max_retries(argparse.Namespace(task_id=tid, value=None))
    assert rc == 0
    assert kb.get_task(conn, tid).max_retries is None


def test_cli_rejects_bad_value(conn):
    tid = kb.create_task(conn, title="t", assignee="worker", initial_status="blocked")
    rc = kanban_cli._cmd_set_max_retries(argparse.Namespace(task_id=tid, value="not-a-number"))
    assert rc == 2
    assert kb.get_task(conn, tid).max_retries is None


def test_cli_refuses_while_running(conn):
    tid = kb.create_task(conn, title="t", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    rc = kanban_cli._cmd_set_max_retries(argparse.Namespace(task_id=tid, value="1"))
    assert rc == 2
    assert kb.get_task(conn, tid).max_retries is None


def test_cli_wired_into_parser():
    """The subcommand must actually be reachable via `hermes kanban set-max-retries`."""
    top = argparse.ArgumentParser(prog="hermes-kanban-test")
    sub = top.add_subparsers(dest="top_command")
    kanban_cli.build_parser(sub)
    args = top.parse_args(["kanban", "set-max-retries", "t_abc123", "1"])
    assert args.task_id == "t_abc123"
    assert args.value == "1"
