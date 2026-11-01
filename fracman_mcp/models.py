"""Small data models used by the FracMan MCP server."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return _clean(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean(item) for item in value]
    return value


class ModelMixin:
    def to_dict(self) -> dict[str, Any]:
        return _clean(dataclasses.asdict(self))  # type: ignore[arg-type]


@dataclass
class SessionInfo(ModelMixin):
    session_id: str
    pid: int
    hwnd: int
    title: str = ""
    project: str = ""
    status: str = "unknown"
    owned: bool = False
    active: bool = False
    created_at: str = field(default_factory=now_iso)
    last_seen: str = field(default_factory=now_iso)
    window_rect: list[int] | None = None
    view_rect: list[int] | None = None
    dialogs: list[dict[str, Any]] = field(default_factory=list)
    log_chars: int = 0
    note: str = ""


@dataclass
class RequestRecord(ModelMixin):
    request_id: str
    session_id: str
    kind: str
    fingerprint: str
    status: str = "queued"
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    macro_path: str = ""
    fml_path: str = ""
    log_delta: str = ""
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class RunRecord(ModelMixin):
    run_id: str
    status: str = "queued"
    fingerprint: str = ""
    macro_path: str = ""
    fml_path: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    workspace_dir: str = ""
    timeout: float = 3600.0
    expected_artifacts: list[str] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pid: int | None = None
    submitted_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    returncode: int | None = None
    log_tail: str = ""
    cancel_requested: bool = False
    message: str = ""


@dataclass
class DfnJobRecord(ModelMixin):
    """One dfnWorks stage executed inside WSL."""

    job_id: str
    stage: str
    status: str = "queued"
    fingerprint: str = ""
    workspace_dir: str = ""
    spec_path: str = ""
    driver_path: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    result_path: str = ""
    timeout: float = 7200.0
    pid: int | None = None
    submitted_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    returncode: int | None = None
    result: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    log_tail: str = ""
    cancel_requested: bool = False
    message: str = ""
