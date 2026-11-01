"""Single-flight request registry and batch run registry.

The semantics mirror the Abaqus MCP bridge where it matters:

* a request_id identifies one logical execution;
* reusing an id with a different payload is a conflict, never a retry;
* a timeout is reported as ``timeout_pending`` and the watcher keeps the
  original record up to date;
* nothing is ever replayed automatically.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import OrderedDict
from typing import Any, Callable

from .models import RequestRecord, RunRecord, now_iso


class RegistryError(RuntimeError):
    pass


class RequestConflict(RegistryError):
    pass


def make_request_id() -> str:
    return "fm-" + uuid.uuid4().hex


def fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RequestRegistry:
    """Bounded registry of live-macro requests."""

    def __init__(self, limit: int = 256) -> None:
        self._lock = threading.RLock()
        self._records: OrderedDict[str, RequestRecord] = OrderedDict()
        self._events: dict[str, threading.Event] = {}
        self._limit = limit

    def _event(self, request_id: str) -> threading.Event:
        event = self._events.get(request_id)
        if event is None:
            event = threading.Event()
            self._events[request_id] = event
        return event

    def admit(
        self,
        request_id: str | None,
        session_id: str,
        payload: Any,
        kind: str = "live_macro",
    ) -> tuple[RequestRecord, bool]:
        if request_id is None or not str(request_id).strip():
            request_id = make_request_id()
        request_id = str(request_id).strip()
        if len(request_id) > 200:
            raise ValueError("request_id must be at most 200 characters")
        digest = fingerprint(payload)
        with self._lock:
            existing = self._records.get(request_id)
            if existing is not None:
                if existing.fingerprint != digest:
                    raise RequestConflict(
                        f"request_id {request_id!r} already exists with a different payload"
                    )
                return existing, False
            record = RequestRecord(
                request_id=request_id,
                session_id=session_id,
                kind=kind,
                fingerprint=digest,
            )
            self._records[request_id] = record
            self._event(request_id)
            self._prune_locked()
            return record, True

    def update(self, request_id: str, **fields: Any) -> RequestRecord:
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                raise KeyError(request_id)
            for key, value in fields.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            return record

    def mark_running(self, request_id: str) -> RequestRecord:
        return self.update(request_id, status="running", started_at=now_iso())

    def complete(self, request_id: str, result: dict[str, Any], **fields: Any) -> RequestRecord:
        with self._lock:
            record = self.update(
                request_id,
                status="completed",
                finished_at=now_iso(),
                result=result,
                **fields,
            )
            self._event(request_id).set()
            return record

    def fail(self, request_id: str, error: str, status: str = "failed", **fields: Any) -> RequestRecord:
        with self._lock:
            record = self.update(
                request_id,
                status=status,
                finished_at=now_iso(),
                error=error,
                **fields,
            )
            self._event(request_id).set()
            return record

    def get(self, request_id: str) -> RequestRecord | None:
        with self._lock:
            return self._records.get(request_id)

    def wait(self, request_id: str, timeout: float = 0.0) -> RequestRecord | None:
        record = self.get(request_id)
        if record is None:
            return None
        if timeout > 0 and record.status in ("queued", "running"):
            self._event(request_id).wait(timeout)
        return self.get(request_id)

    def active_for_session(self, session_id: str) -> RequestRecord | None:
        with self._lock:
            for record in reversed(self._records.values()):
                if record.session_id == session_id and record.status in ("queued", "running"):
                    return record
        return None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records.values())[-limit:]
            return [record.to_dict() for record in reversed(records)]

    def _prune_locked(self) -> None:
        if len(self._records) <= self._limit:
            return
        for request_id in list(self._records.keys()):
            if len(self._records) <= self._limit:
                break
            record = self._records[request_id]
            if record.status in ("queued", "running"):
                continue
            self._records.pop(request_id, None)
            self._events.pop(request_id, None)


class RunRegistry:
    """Registry of batch runs with per-run completion events."""

    def __init__(self, limit: int = 256) -> None:
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, RunRecord] = OrderedDict()
        self._events: dict[str, threading.Event] = {}
        self._active_pids: dict[str, int] = {}
        self._limit = limit

    def create(self, record: RunRecord) -> RunRecord:
        with self._lock:
            self._runs[record.run_id] = record
            self._event(record.run_id)
            return record

    def _event(self, run_id: str) -> threading.Event:
        event = self._events.get(run_id)
        if event is None:
            event = threading.Event()
            self._events[run_id] = event
        return event

    def update(self, run_id: str, **fields: Any) -> RunRecord:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            for key, value in fields.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            return record

    def finish(self, run_id: str, **fields: Any) -> RunRecord:
        with self._lock:
            record = self.update(run_id, finished_at=now_iso(), **fields)
            self._event(run_id).set()
            return record

    def register_pid(self, run_id: str, pid: int) -> None:
        with self._lock:
            self._active_pids[run_id] = pid
            self.update(run_id, pid=pid)

    def clear_pid(self, run_id: str) -> None:
        with self._lock:
            self._active_pids.pop(run_id, None)

    def pid_for(self, run_id: str) -> int | None:
        with self._lock:
            return self._active_pids.get(run_id)

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def find(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        if record is None:
            raise KeyError(f"unknown run_id: {run_id}")
        return record

    def forget(self, run_id: str) -> None:
        """Drop a record from memory; on-disk artifacts are left untouched."""
        with self._lock:
            self._runs.pop(run_id, None)
            self._events.pop(run_id, None)
            self._active_pids.pop(run_id, None)

    def wait(self, run_id: str, timeout: float = 0.0) -> RunRecord:
        record = self.find(run_id)
        if timeout > 0 and record.status in ("queued", "running"):
            self._event(run_id).wait(timeout)
        return self.find(run_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._runs.values())[-limit:]
            return [record.to_dict() for record in reversed(records)]

    def count_active(self) -> int:
        with self._lock:
            return sum(
                1
                for record in self._runs.values()
                if record.status in ("queued", "running")
            )


class DfnJobRegistry(RunRegistry):
    """Registry of dfnWorks jobs (same lifecycle as batch runs)."""

    def create(self, record: Any) -> Any:
        with self._lock:
            self._runs[record.job_id] = record
            self._event(record.job_id)
            return record


requests_registry = RequestRegistry()
runs_registry = RunRegistry()
dfn_jobs_registry = DfnJobRegistry()
