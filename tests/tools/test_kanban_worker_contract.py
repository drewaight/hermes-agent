"""Deterministic Lifecycle-02 contract; not a provider-backed dispatch receipt."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_swarm import SwarmWorkerSpec, create_swarm
from tools import kanban_tools  # register the native handlers
from tools.registry import registry


def call(name, **args):
    return json.loads(registry.dispatch(name, args))


@pytest.mark.parametrize("terminal,status", [("kanban_request_review", "review"), ("kanban_block", "blocked")])
def test_worker_verifier_synthesizer_handoff(tmp_path, monkeypatch, terminal, status):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    with kbc.connect_closing() as conn:
        swarm = create_swarm(conn, goal="test handoff", workers=[SwarmWorkerSpec(
            profile="worker", title="work", body="report findings")],
            verifier_assignee="verifier", synthesizer_assignee="synthesizer")
        roles = [(swarm.worker_ids[0], "worker"), (swarm.verifier_id, "verifier"),
                 (swarm.synthesizer_id, "synthesizer")]
        for tid, role in roles:
            assert kb.get_task(conn, tid).status == "ready"
            assert kb.claim_task(conn, tid, claimer=role)
            task = kb.get_task(conn, tid)
            with monkeypatch.context() as ctx:
                ctx.setenv("HERMES_KANBAN_TASK", tid)
                ctx.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
                ctx.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
                ctx.setenv("HERMES_PROFILE", role)
                assert call("kanban_show")["task"]["id"] == tid
                assert call("kanban_heartbeat", note=role)["ok"]
                before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
                assert "orchestrator-only" in call("kanban_create", title="forbidden", assignee="worker")["error"]
                assert "orchestrator-only" in call("kanban_link", parent_id=swarm.root_id, child_id=tid)["error"]
                assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before
                assert "error" in call("kanban_complete", task_id=swarm.root_id, summary="forbidden")
                board = call("kanban_show", task_id=swarm.root_id)
                if role == "verifier":
                    assert any(c["body"] == "worker findings" and c["author"] == "worker" for c in board["comments"])
                if role == "synthesizer":
                    assert {"worker findings", "verifier verdict"} <= {c["body"] for c in board["comments"]}
                body = {"worker": "worker findings", "verifier": "verifier verdict", "synthesizer": "synthesis"}[role]
                assert "error" in call("kanban_comment", task_id=swarm.root_id, body=body, author="forged")
                assert call("kanban_comment", task_id=swarm.root_id, body=body)["ok"]
                if role == "synthesizer":
                    args = {"summary": body} if terminal == "kanban_request_review" else {"reason": "needs decision", "kind": "needs_input"}
                    assert call(terminal, **args)["ok"]
                else:
                    assert call("kanban_complete", summary=body)["ok"]
            assert kb.get_task(conn, tid).current_run_id is None
        assert kb.get_task(conn, swarm.synthesizer_id).status == status
        # No task-scoped identity: routing returns to the orchestrator.
        created = call("kanban_create", title="follow-up", assignee="worker")
        assert created["ok"]
        assert call("kanban_link", parent_id=swarm.synthesizer_id, child_id=created["task_id"])["ok"]
        assert call("kanban_comment", task_id=swarm.root_id, body="orchestrator handoff")["ok"]
        authors_and_bodies = {(c["author"], c["body"]) for c in call("kanban_show", task_id=swarm.root_id)["comments"]}
        assert {("worker", "worker findings"), ("verifier", "verifier verdict"), ("synthesizer", "synthesis")} <= authors_and_bodies
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
