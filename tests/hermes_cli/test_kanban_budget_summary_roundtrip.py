"""The budget-exhaustion summary must survive the trip to the DB and back.

The unit tests in ``tests/agent/test_turn_finalizer_iteration_limit_exit.py``
mock ``_record_task_failure``, so they prove the finalizer *passes* a summary
but not that one is ever *stored*. That gap is exactly where the real bug lived:
13 of 13 budget-exhausted runs fleet-wide carried ``summary IS NULL`` while the
finalizer looked correct in isolation.

These tests close the seam end-to-end against a real board DB — no agent, no
provider call — asserting the summary lands on the closed run and is rendered
back into the next attempt's prompt by ``build_worker_context``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

SUMMARY = "Migrated 7 of 10 tables. Remaining: orders, shipments, invoices."


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claimed_task(conn, title: str = "mirror the tables") -> str:
    """A task with an open run, i.e. the state a dying worker is in."""
    task_id = kb.create_task(conn, title=title, assignee="worker-ic")
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    assert kb.claim_task(conn, task_id) is not None, "fixture failed to open a run"
    return task_id


def _run_rows(conn, task_id: str):
    return conn.execute(
        "SELECT outcome, summary FROM task_runs WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()


def test_budget_exhaustion_persists_the_summary_on_the_closed_run(kanban_home):
    """AC: a budget-exhausted run stores a non-empty ``task_runs.summary``."""
    conn = kb.connect()
    task_id = _claimed_task(conn)

    kb._record_task_failure(
        conn,
        task_id,
        error="Iteration budget exhausted (150/150)",
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        summary=SUMMARY,
    )

    rows = _run_rows(conn, task_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "timed_out"
    assert rows[0]["summary"] == SUMMARY


def test_the_retry_prompt_carries_the_dead_attempt_s_summary(kanban_home):
    """AC: ``build_worker_context`` for the retry contains the prior summary.

    This is the whole point of storing it — a retry that cannot read the
    summary back is still a cold restart no matter what the DB holds.
    """
    conn = kb.connect()
    task_id = _claimed_task(conn)

    kb._record_task_failure(
        conn,
        task_id,
        error="Iteration budget exhausted (150/150)",
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        summary=SUMMARY,
    )

    assert SUMMARY in kb.build_worker_context(conn, task_id)


def test_the_breaker_trip_keeps_the_summary_too(kanban_home):
    """The tripped branch closes the run as ``gave_up`` via a different
    ``_end_run`` call than the below-threshold branch. Both must carry the
    summary, or the last attempt before a block is the one that loses it —
    the attempt whose findings a human is about to read.
    """
    conn = kb.connect()
    task_id = _claimed_task(conn)

    kb._record_task_failure(
        conn,
        task_id,
        error="Iteration budget exhausted (150/150)",
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        summary=SUMMARY,
        failure_limit=1,  # trip on this first failure
    )

    rows = _run_rows(conn, task_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "gave_up"
    assert rows[0]["summary"] == SUMMARY
