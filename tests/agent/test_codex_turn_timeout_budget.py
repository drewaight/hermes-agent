"""Regressions for OPS-CODEX-TURN-DEADLINE-01: codex app-server turn_timeout must track the
kanban card's real runtime budget (HERMES_KANBAN_DEADLINE_EPOCH) instead of a flat 600s that is
disconnected from a much longer card max_runtime_seconds (#t_e6a07577 Fault B)."""

from __future__ import annotations

import time

import pytest

from agent.codex_runtime import (
    _CODEX_TURN_TIMEOUT_GRACE_SECONDS,
    _DEFAULT_CODEX_TURN_TIMEOUT,
    _resolve_codex_turn_timeout,
)


def test_no_env_keeps_historical_default(monkeypatch):
    """A non-kanban session (CLI/gateway chat) is unaffected: no env, no behavior change."""
    monkeypatch.delenv("HERMES_KANBAN_DEADLINE_EPOCH", raising=False)
    assert _resolve_codex_turn_timeout() == _DEFAULT_CODEX_TURN_TIMEOUT


def test_long_card_budget_raises_the_turn_timeout_above_600s(monkeypatch):
    """A 2700s card gives codex far more than 600s for its one continuous turn, with the
    dispatcher's shutdown grace reserved so the process can still exit cleanly."""
    monkeypatch.setenv("HERMES_KANBAN_DEADLINE_EPOCH", str(time.time() + 2700))
    timeout = _resolve_codex_turn_timeout()
    assert timeout > _DEFAULT_CODEX_TURN_TIMEOUT
    # Within a couple seconds of (2700 - grace), allowing for test execution time.
    assert abs(timeout - (2700 - _CODEX_TURN_TIMEOUT_GRACE_SECONDS)) < 5


def test_short_remaining_budget_shrinks_the_turn_timeout_below_600s(monkeypatch):
    """A worker that already burned most of its budget on prior turns must not get the full
    600s default — that would run past the dispatcher's SIGTERM with nothing reported."""
    monkeypatch.setenv("HERMES_KANBAN_DEADLINE_EPOCH", str(time.time() + 120))
    timeout = _resolve_codex_turn_timeout()
    assert timeout < _DEFAULT_CODEX_TURN_TIMEOUT
    assert abs(timeout - (120 - _CODEX_TURN_TIMEOUT_GRACE_SECONDS)) < 5


def test_already_expired_deadline_floors_rather_than_reviving_default(monkeypatch):
    """A stale/expired deadline must interrupt fast, not silently fall back to 600s and get
    SIGKILLed mid-turn with no chance for codex to make its native reporting tool call."""
    monkeypatch.setenv("HERMES_KANBAN_DEADLINE_EPOCH", str(time.time() - 500))
    assert _resolve_codex_turn_timeout() == 5.0


@pytest.mark.parametrize("bad_value", ["", "not-a-number", "nan-ish"])
def test_malformed_env_falls_back_to_default(monkeypatch, bad_value):
    monkeypatch.setenv("HERMES_KANBAN_DEADLINE_EPOCH", bad_value)
    assert _resolve_codex_turn_timeout() == _DEFAULT_CODEX_TURN_TIMEOUT


def test_run_codex_app_server_turn_passes_resolved_timeout(monkeypatch):
    """The call site must actually thread the resolved value into run_turn, not just compute it."""
    import agent.codex_runtime as codex_runtime

    monkeypatch.setenv("HERMES_KANBAN_DEADLINE_EPOCH", str(time.time() + 90))
    captured = {}

    class FakeSession:
        def run_turn(self, *, user_input, turn_timeout):
            captured["turn_timeout"] = turn_timeout
            raise RuntimeError("stop after capturing the call")

    agent = type("Agent", (), {})()
    agent._codex_session = FakeSession()
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda *a, **k: None)
    monkeypatch.setattr(codex_runtime, "_start_codex_thread", lambda *a, **k: None)
    monkeypatch.setattr(codex_runtime, "_close_codex_session", lambda *a, **k: None)
    monkeypatch.setattr(codex_runtime, "_consume_user_interrupt", lambda *a, **k: (False, None))

    codex_runtime.run_codex_app_server_turn(
        agent, user_message="hi", original_user_message="hi", messages=[], effective_task_id="t1",
    )

    assert "turn_timeout" in captured
    assert abs(captured["turn_timeout"] - (90 - _CODEX_TURN_TIMEOUT_GRACE_SECONDS)) < 5
