"""Regression for OPS-CODEX-TURN-DEADLINE-01: the dispatcher must hand the worker its real
runtime budget so codex_runtime can bound a codex app-server turn's internal deadline by it,
instead of a flat 600s disconnected from the card's max_runtime_seconds."""

from __future__ import annotations

from hermes_cli.kanban_db_dispatch import _worker_deadline_epoch_env


def test_no_runtime_cap_sets_nothing():
    assert _worker_deadline_epoch_env(None) is None


def test_runtime_cap_becomes_an_absolute_epoch():
    assert _worker_deadline_epoch_env(2700, now=1000.0) == "3700.0"


def test_malformed_value_sets_nothing():
    assert _worker_deadline_epoch_env("not-a-number", now=1000.0) is None
