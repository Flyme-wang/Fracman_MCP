"""Session discovery and high-level live-session operations."""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from . import gui
from .config import settings
from .macro import (
    LIVE_SENTINEL_NAME,
    MacroValidationError,
    live_sentinel_text,
    new_run_dir,
    read_macro,
    validate,
    wrap_live,
    write_macro,
)
from .models import SessionInfo, now_iso
from .requests import requests_registry


class SessionNotFound(RuntimeError):
    pass


def _project_from_title(title: str) -> str:
    if " - " not in title:
        return ""
    suffix = title.rsplit(" - ", 1)[-1].strip()
    if not suffix or suffix.lower().startswith("fracman"):
        return ""
    return suffix


class SessionManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionInfo] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._owned: set[int] = set()
        self._active_id: str | None = None

    # -- registry ---------------------------------------------------------
    def discover(self) -> list[dict[str, Any]]:
        windows = gui.find_main_windows()
        present_pids = {item["pid"] for item in windows}
        with self._lock:
            for item in windows:
                pid = item["pid"]
                session_id = f"fracman_{pid}"
                session = self._sessions.get(session_id)
                if session is None:
                    session = SessionInfo(
                        session_id=session_id,
                        pid=pid,
                        hwnd=item["hwnd"],
                        title=item["title"],
                        project=_project_from_title(item["title"]),
                        owned=pid in self._owned,
                        active=False,
                    )
                    self._sessions[session_id] = session
                else:
                    session.hwnd = item["hwnd"]
                    session.title = item["title"]
                    session.project = _project_from_title(item["title"])
                    session.owned = pid in self._owned
                session.last_seen = now_iso()
                try:
                    w = gui.connect(pid, item["hwnd"])
                    session.status = gui.read_status(w) or "unknown"
                    session.log_chars = len(gui.read_log(w))
                    session.window_rect = gui.rect_list(w.rectangle())
                except Exception as exc:
                    session.status = "unresponsive"
                    session.note = f"{type(exc).__name__}: {exc}"
            for session_id, session in list(self._sessions.items()):
                if session.pid not in present_pids:
                    self._sessions.pop(session_id, None)
                    self._locks.pop(session_id, None)
            if self._active_id not in self._sessions:
                ready = [
                    session
                    for session in self._sessions.values()
                    if gui.is_ready(session.status)
                ]
                self._active_id = (ready or list(self._sessions.values()))[0].session_id if self._sessions else None
            for session in self._sessions.values():
                session.active = session.session_id == self._active_id
            return [session.to_dict() for session in self._sessions.values()]

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [session.to_dict() for session in self._sessions.values()]

    def get(self, identifier: str | int | None = None) -> SessionInfo:
        if identifier is None or str(identifier).strip() in ("", "active"):
            with self._lock:
                if self._active_id and self._active_id in self._sessions:
                    return self._sessions[self._active_id]
            self.discover()
            with self._lock:
                if self._active_id and self._active_id in self._sessions:
                    return self._sessions[self._active_id]
            raise SessionNotFound("no FracMan session is available; start FracMan or call discover_sessions")

        text = str(identifier).strip()
        with self._lock:
            if text in self._sessions:
                return self._sessions[text]
            if text.isdigit():
                for session in self._sessions.values():
                    if session.pid == int(text):
                        return session
        self.discover()
        with self._lock:
            if text in self._sessions:
                return self._sessions[text]
            if text.isdigit():
                for session in self._sessions.values():
                    if session.pid == int(text):
                        return session
        raise SessionNotFound(f"unknown FracMan session: {identifier}")

    def set_active(self, identifier: str | int) -> SessionInfo:
        session = self.get(identifier)
        with self._lock:
            self._active_id = session.session_id
            for item in self._sessions.values():
                item.active = item.session_id == session.session_id
        return session

    def mark_owned(self, pid: int) -> None:
        with self._lock:
            self._owned.add(pid)

    def is_owned(self, session: SessionInfo) -> bool:
        with self._lock:
            return session.pid in self._owned

    def register_launched(self, pid: int, hwnd: int, title: str) -> SessionInfo:
        self.mark_owned(pid)
        session_id = f"fracman_{pid}"
        with self._lock:
            session = SessionInfo(
                session_id=session_id,
                pid=pid,
                hwnd=hwnd,
                title=title,
                project=_project_from_title(title),
                owned=True,
                active=False,
            )
            self._sessions[session_id] = session
            return session

    def remove(self, session: SessionInfo) -> None:
        with self._lock:
            self._sessions.pop(session.session_id, None)
            self._locks.pop(session.session_id, None)
            if self._active_id == session.session_id:
                self._active_id = None

    # -- execution lock ---------------------------------------------------
    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock

    def try_acquire(self, session: SessionInfo) -> threading.Lock | None:
        lock = self._session_lock(session.session_id)
        return lock if lock.acquire(blocking=False) else None

    def release(self, session: SessionInfo, lock: threading.Lock) -> None:
        try:
            lock.release()
        except Exception:
            pass

    # -- info -------------------------------------------------------------
    def refresh(self, session: SessionInfo, include_view: bool = False) -> SessionInfo:
        w = gui.connect(session.pid, session.hwnd)
        session.title = w.window_text() or session.title
        session.project = _project_from_title(session.title)
        session.status = gui.read_status(w) or "unknown"
        session.log_chars = len(gui.read_log(w))
        session.dialogs = gui.get_dialogs(w, deep=True)
        try:
            session.window_rect = gui.rect_list(w.rectangle())
        except Exception:
            pass
        if include_view:
            session.view_rect = gui.view_rect(w)
        session.last_seen = now_iso()
        return session

    def info(self, identifier: str | int | None = None, log_chars: int | None = None) -> dict[str, Any]:
        session = self.get(identifier)
        w = gui.connect(session.pid, session.hwnd)
        self.refresh(session, include_view=True)
        log = gui.read_log(w)
        tail = log[-(log_chars or settings.log_tail_chars) :]
        data = session.to_dict()
        data["log_tail"] = tail
        data["ready"] = gui.is_ready(session.status)
        data["fracman_exe"] = str(settings.fracman_exe)
        data["owned"] = self.is_owned(session)
        return data

    def ping(self, identifier: str | int | None = None) -> dict[str, Any]:
        session = self.get(identifier)
        started = time.perf_counter()
        try:
            w = gui.connect(session.pid, session.hwnd)
            status = gui.read_status(w)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            return {
                "ok": True,
                "session_id": session.session_id,
                "pid": session.pid,
                "status": status,
                "ready": gui.is_ready(status),
                "round_trip_ms": elapsed_ms,
            }
        except Exception as exc:
            return {
                "ok": False,
                "session_id": session.session_id,
                "pid": session.pid,
                "error": f"{type(exc).__name__}: {exc}",
                "round_trip_ms": round((time.perf_counter() - started) * 1000, 1),
            }

    # -- live macro -------------------------------------------------------
    def start_live_macro(
        self,
        *,
        code: str | None = None,
        path: str | None = None,
        session_id: str | int | None = None,
        request_id: str | None = None,
        wait_seconds: float = 30.0,
        allow_exit: bool = False,
        allow_destructive: bool = False,
    ) -> dict[str, Any]:
        session = self.get(session_id)
        if code is None and path is None:
            raise MacroValidationError("provide either code or path")
        if code is None:
            code = read_macro(path)  # type: ignore[arg-type]
        warnings = validate(
            code,
            live=True,
            allow_exit=allow_exit,
            allow_destructive=allow_destructive,
        )

        existing = requests_registry.get(request_id) if request_id else None
        if existing is not None:
            if existing.fingerprint != requests_registry_fingerprint(code, allow_exit, allow_destructive):
                return {
                    "ok": False,
                    "status": "conflict",
                    "request_id": request_id,
                    "message": "request_id already exists with a different payload",
                }
            if existing.status in ("queued", "running"):
                return {
                    "ok": False,
                    "status": "busy",
                    "request_id": existing.request_id,
                    "active_request_id": existing.request_id,
                    "message": "the original request is still running; query get_request_status instead of replaying",
                }
            return {
                "ok": existing.status == "completed",
                "status": existing.status,
                "request_id": existing.request_id,
                "result": existing.result,
                "error": existing.error,
                "note": "returned the existing request record; nothing was re-executed",
            }

        lock = self.try_acquire(session)
        if lock is None:
            active = requests_registry.active_for_session(session.session_id)
            return {
                "ok": False,
                "status": "busy",
                "session_id": session.session_id,
                "active_request_id": active.request_id if active else None,
                "message": "another live macro is running in this FracMan session",
            }

        run_dir = new_run_dir("live")
        macro_path = run_dir / "macro.fmf"
        fml_path = run_dir / "run.fml"
        sentinel_path = run_dir / LIVE_SENTINEL_NAME
        write_macro(sentinel_path, live_sentinel_text())
        wrapped = wrap_live(code, fml_path, sentinel_path)
        write_macro(macro_path, wrapped)
        record, _created = requests_registry.admit(
            request_id,
            session.session_id,
            {"code": code, "allow_exit": allow_exit, "allow_destructive": allow_destructive},
            kind="live_macro",
        )
        # Record the resolved request id even when the caller supplied none.
        requests_registry.update(
            record.request_id,
            macro_path=str(macro_path),
            fml_path=str(fml_path),
            notes=record.notes + warnings,
        )
        requests_registry.mark_running(record.request_id)

        def worker() -> None:
            released = False
            try:
                w = gui.connect(session.pid, session.hwnd)
                result = gui.execute_live_macro(
                    w,
                    macro_path=macro_path,
                    fml_path=fml_path,
                    sentinel_path=sentinel_path,
                )
                if result.get("ok"):
                    requests_registry.complete(
                        record.request_id,
                        result,
                        macro_path=str(macro_path),
                        fml_path=str(fml_path),
                        log_delta=result.get("log_delta", ""),
                    )
                else:
                    requests_registry.fail(
                        record.request_id,
                        error="\n\n".join(result.get("errors") or [result.get("status", "macro failed")]),
                        status=result.get("status", "failed"),
                        result=result,
                        macro_path=str(macro_path),
                        fml_path=str(fml_path),
                        log_delta=result.get("log_delta", ""),
                    )
                released = True
            except Exception as exc:
                requests_registry.fail(
                    record.request_id,
                    error=f"{type(exc).__name__}: {exc}",
                    status="failed",
                    macro_path=str(macro_path),
                    fml_path=str(fml_path),
                )
                try:
                    w = gui.connect(session.pid, session.hwnd)
                    released = gui.is_ready(w=w)
                except Exception:
                    released = False
            finally:
                if released:
                    self.release(session, lock)
                else:
                    # The GUI may still be executing.  Keep the lock and let
                    # get_session_info expose the busy state; never replay.
                    record.notes.append("session lock retained because GUI readiness was not confirmed")

        thread = threading.Thread(target=worker, name=f"fracman-live-{record.request_id}", daemon=True)
        thread.start()
        final = requests_registry.wait(record.request_id, timeout=max(0.0, float(wait_seconds)))
        if final is None:
            final = record
        data = final.to_dict()
        data["ok"] = final.status == "completed"
        # Lift the worker's payload (errors, warnings, elapsed_seconds,
        # fml_tail, dialogs) so callers do not have to dig into result.
        if isinstance(final.result, dict):
            for key, value in final.result.items():
                data.setdefault(key, value)
        try:
            w = gui.connect(session.pid, session.hwnd)
            data["ready"] = gui.is_ready(w=w)
            data["status_text"] = gui.read_status(w)
        except Exception:
            data["ready"] = False
        if final.status in ("queued", "running"):
            data["status"] = "timeout_pending"
            data["message"] = (
                "the live macro is still running; query get_request_status(request_id) later; do not replay"
            )
        data["warnings"] = warnings
        return data

    # -- project lifecycle ------------------------------------------------
    def open_project(
        self,
        project_path: str,
        *,
        copy: bool = True,
        timeout: float = 45.0,
        activate: bool = True,
    ) -> dict[str, Any]:
        source = Path(project_path).expanduser().resolve()
        if not source.is_file():
            return {"ok": False, "status": "not_found", "message": f"project file not found: {source}"}
        target = source
        if copy:
            settings.ensure_dirs()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            token = uuid.uuid4().hex[:6]
            target = settings.projects_dir / f"proj_{stamp}_{token}{source.suffix.lower()}"
            shutil.copy2(source, target)
        result = gui.launch_project(settings.fracman_exe, target, timeout=timeout)
        if not result.get("ok"):
            if result.get("pid") and result.get("hwnd"):
                self.register_launched(
                    int(result["pid"]), int(result["hwnd"]), result.get("title", "FracMan")
                )
            return {**result, "source": str(source), "project": str(target)}
        session = self.register_launched(
            int(result["pid"]), int(result["hwnd"]), result.get("title", "FracMan")
        )
        try:
            self.refresh(session, include_view=True)
        except Exception:
            pass
        if activate:
            self.set_active(session.session_id)
        data = session.to_dict()
        return {
            "ok": True,
            "status": session.status,
            "session": data,
            "session_id": session.session_id,
            "pid": session.pid,
            "source": str(source),
            "project": str(target),
            "dialogs": result.get("dialogs", []),
        }

    def close_session(
        self,
        session_id: str | int | None = None,
        *,
        save: bool = False,
        force: bool = False,
        terminate: bool = True,
    ) -> dict[str, Any]:
        session = self.get(session_id)
        if not self.is_owned(session) and not force:
            return {
                "ok": False,
                "status": "refused",
                "session_id": session.session_id,
                "message": "refusing to close a user-owned FracMan session; pass force=true to override",
            }
        active = requests_registry.active_for_session(session.session_id)
        if active is not None and not force:
            return {
                "ok": False,
                "status": "busy",
                "active_request_id": active.request_id,
                "message": "a live macro is running; wait for it or pass force=true",
            }
        w = gui.connect(session.pid, session.hwnd)
        result = gui.close_window(w, session.hwnd, session.pid, save=save)
        if not result.get("closed") and terminate:
            gui.terminate_process(session.pid)
            time.sleep(0.5)
            result = {
                **result,
                "terminated": True,
                "closed": not bool(psutil_pid_alive(session.pid)),
            }
        gui.forget(session.pid, session.hwnd)
        if result.get("closed"):
            self.remove(session)
        return {
            "ok": bool(result.get("closed")),
            "session_id": session.session_id,
            "pid": session.pid,
            **result,
        }

    # -- screenshots ------------------------------------------------------
    def capture(
        self,
        session_id: str | int | None = None,
        *,
        mode: str = "full",
        out_path: str | None = None,
    ) -> dict[str, Any]:
        session = self.get(session_id)
        w = gui.connect(session.pid, session.hwnd)
        if out_path is None:
            settings.ensure_dirs()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = str(settings.screenshots_dir / f"{stamp}_{session.pid}_{mode}.png")
        result = gui.capture_window(w, session.hwnd, out_path=out_path, mode=mode)
        result["session_id"] = session.session_id
        result["pid"] = session.pid
        return result


def requests_registry_fingerprint(code: str, allow_exit: bool, allow_destructive: bool) -> str:
    from .requests import fingerprint

    return fingerprint(
        {"code": code, "allow_exit": allow_exit, "allow_destructive": allow_destructive}
    )


def psutil_pid_alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:
        return False


session_manager = SessionManager()
