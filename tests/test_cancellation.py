from __future__ import annotations

import threading

import app as app_module


def test_cancel_all_marks_queue_and_signals_active_job() -> None:
    queued = app_module.Job(
        id="queued-job",
        label="Queued",
        source="test",
        density=7,
        threshold=128,
    )
    printing = app_module.Job(
        id="printing-job",
        label="Printing",
        source="test",
        density=7,
        threshold=128,
        status="printing",
    )
    done = app_module.Job(
        id="done-job",
        label="Done",
        source="test",
        density=7,
        threshold=128,
        status="done",
    )
    cancel_event = threading.Event()

    with app_module.jobs_lock:
        app_module.jobs.clear()
        app_module.jobs[queued.id] = queued
        app_module.jobs[printing.id] = printing
        app_module.jobs[done.id] = done
        app_module.active_job_id = printing.id
        app_module.active_cancel_event = cancel_event

    try:
        response = app_module.app.test_client().post("/api/jobs/cancel-all")
        assert response.status_code == 200
        assert response.get_json() == {
            "ok": True,
            "cancelled_queued": 1,
            "active_cancel_requested": True,
            "cancelled_total": 2,
        }
        assert cancel_event.is_set()
        assert queued.status == "cancelled"
        assert printing.status == "cancelling"
        assert done.status == "done"
    finally:
        with app_module.jobs_lock:
            app_module.jobs.clear()
            app_module.active_job_id = None
            app_module.active_cancel_event = None
