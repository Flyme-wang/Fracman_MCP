"""Registry semantics: single-flight ids, conflicts, and run lifecycle."""

from __future__ import annotations

import threading

import pytest

from fracman_mcp.models import RequestRecord, RunRecord
from fracman_mcp.requests import (
    RequestConflict,
    RequestRegistry,
    RunRegistry,
    fingerprint,
)


def test_fingerprint_is_order_independent():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


def test_request_registry_single_flight():
    registry = RequestRegistry()
    record, created = registry.admit("fm-1", "fracman_1", {"code": "A"})
    assert created is True
    assert record.status == "queued"

    again, created_again = registry.admit("fm-1", "fracman_1", {"code": "A"})
    assert created_again is False
    assert again is record

    with pytest.raises(RequestConflict):
        registry.admit("fm-1", "fracman_1", {"code": "B"})


def test_request_registry_completion_and_events():
    registry = RequestRegistry()
    registry.admit("fm-2", "fracman_1", {"code": "A"})
    registry.mark_running("fm-2")
    assert registry.active_for_session("fracman_1").request_id == "fm-2"
    assert registry.wait("fm-2", timeout=0.05).status == "running"
    registry.complete("fm-2", {"ok": True}, fml_path="run.fml")
    final = registry.wait("fm-2", timeout=1.0)
    assert final.status == "completed"
    assert final.result == {"ok": True}
    assert final.fml_path == "run.fml"
    assert registry.active_for_session("fracman_1") is None


def test_request_registry_failure_and_list_order():
    registry = RequestRegistry()
    registry.admit("fm-a", "s", {"n": 1})
    registry.admit("fm-b", "s", {"n": 2})
    registry.fail("fm-b", "boom", status="macro_error")
    listed = registry.list()
    assert [item["request_id"] for item in listed] == ["fm-b", "fm-a"]
    assert listed[0]["status"] == "macro_error"
    assert listed[0]["error"] == "boom"


def test_request_registry_prunes_finished_records():
    registry = RequestRegistry(limit=2)
    registry.admit("old", "s", {"n": 0})
    registry.complete("old", {"ok": True})
    registry.admit("mid", "s", {"n": 1})
    registry.admit("new", "s", {"n": 2})
    assert registry.get("old") is None
    assert registry.get("mid") is not None
    assert registry.get("new") is not None


def test_request_registry_validation():
    registry = RequestRegistry()
    with pytest.raises(ValueError):
        registry.admit("x" * 201, "s", {"n": 1})
    with pytest.raises(KeyError):
        registry.update("missing", status="running")


def test_run_registry_lifecycle():
    registry = RunRegistry()
    record = RunRecord(run_id="fmr-1", workspace_dir="run")
    registry.create(record)
    assert registry.count_active() == 1
    registry.update("fmr-1", status="running")
    registry.register_pid("fmr-1", 1234)
    assert registry.pid_for("fmr-1") == 1234
    registry.clear_pid("fmr-1")
    assert registry.pid_for("fmr-1") is None
    registry.finish("fmr-1", status="completed", returncode=0)
    assert registry.count_active() == 0
    assert registry.find("fmr-1").finished_at
    assert registry.wait("fmr-1", timeout=0.1).status == "completed"
    with pytest.raises(KeyError):
        registry.find("unknown")


def test_registry_is_thread_safe_for_admission():
    registry = RequestRegistry(limit=10_000)
    created: list[bool] = []

    def worker(index: int) -> None:
        _record, is_new = registry.admit(None, "s", {"n": index})
        created.append(is_new)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(created) == 50
    assert all(created)
    assert len(registry.list(limit=100)) == 50
