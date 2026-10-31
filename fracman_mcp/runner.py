"""Batch macro runner: hidden FracMan process, log-based verdicts.

Exit codes from FracMan are not reliable (a macro error still exits 0), so the
only authoritative success signal is the generated ``.fml`` log plus the
expected artifacts.
"""

from __future__ import annotations

import glob
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import settings
from .macro import (
    new_run_dir,
    normalize,
    read_macro,
    validate,
    wrap_batch,
    write_macro,
)
from .models import RunRecord, now_iso
from .parsers.fml import parse_fml
from .requests import RequestConflict, fingerprint, runs_registry


class BatchError(RuntimeError):
    pass


class BatchBusy(BatchError):
    def __init__(self, message: str, active: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.active = active


def make_run_id() -> str:
    return "fmr-" + uuid.uuid4().hex[:12]


def _tail(path: Path | None, chars: int = 2000) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-chars:]


def _hidden_startupinfo() -> subprocess.STARTUPINFO:
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


def _visible_modal_dialog(pid: int) -> str | None:
    """Return the title of a visible #32770 owned by *pid* (our own batch)."""
    if not pid:
        return None
    try:
        import win32gui
        import win32process
    except Exception:
        return None
    found: list[str] = []

    def _callback(hwnd: int, _extra: Any) -> None:
        if found or not win32gui.IsWindowVisible(hwnd):
            return
        try:
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return
        if window_pid != pid or win32gui.GetClassName(hwnd) != "#32770":
            return
        found.append(win32gui.GetWindowText(hwnd) or "(untitled dialog)")

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        return None
    return found[0] if found else None


class BatchRunner:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._cancel: dict[str, threading.Event] = {}

    def submit(
        self,
        *,
        code: str | None = None,
        macro_path: str | None = None,
        run_id: str | None = None,
        expected_artifacts: list[str] | None = None,
        timeout: float | None = None,
        wait_seconds: float = 0.0,
        label: str = "",
    ) -> dict[str, Any]:
        if code is None and macro_path is None:
            raise BatchError("provide either code or macro_path")
        if code is not None and macro_path is not None:
            raise BatchError("provide only one of code and macro_path")
        if macro_path is not None:
            code = read_macro(macro_path)
        assert code is not None
        code = normalize(code)
        validate(code, live=False)

        effective_timeout = float(timeout) if timeout is not None else settings.batch_timeout
        if effective_timeout <= 0:
            raise BatchError("timeout must be positive")
        expected = [str(item) for item in (expected_artifacts or [])]
        digest = fingerprint(
            {"code": code, "expected": expected, "timeout": effective_timeout}
        )

        if run_id is None:
            run_id = make_run_id()
        run_id = str(run_id).strip()
        if not run_id:
            raise BatchError("run_id must not be empty")
        if len(run_id) > 200:
            raise BatchError("run_id must be at most 200 characters")

        existing = runs_registry.get(run_id)
        if existing is not None:
            if existing.fingerprint and existing.fingerprint != digest:
                raise RequestConflict(
                    f"run_id {run_id!r} already exists with a different payload"
                )
            return self._describe(existing)

        with self._lock:
            active = runs_registry.count_active()
            if active >= settings.max_batch:
                raise BatchBusy(
                    f"batch capacity reached ({active}/{settings.max_batch}); "
                    "wait for a run to finish or raise FRACMAN_MCP_MAX_BATCH",
                    runs_registry.list(limit=settings.max_batch + 5),
                )
            run_dir = new_run_dir("batch")
            macro_file = run_dir / "run.fmf"
            fml_file = run_dir / "run.fml"
            write_macro(macro_file, wrap_batch(code, fml_file))
            record = RunRecord(
                run_id=run_id,
                status="queued",
                fingerprint=digest,
                macro_path=str(macro_file),
                fml_path=str(fml_file),
                stdout_path=str(run_dir / "stdout.txt"),
                stderr_path=str(run_dir / "stderr.txt"),
                workspace_dir=str(run_dir),
                timeout=effective_timeout,
                expected_artifacts=expected,
                message=label,
            )
            runs_registry.create(record)
            cancel_event = threading.Event()
            self._cancel[run_id] = cancel_event

        worker = threading.Thread(
            target=self._execute,
            args=(run_id, effective_timeout, cancel_event),
            name=f"fracman-batch-{run_id}",
            daemon=True,
        )
        worker.start()

        if wait_seconds and wait_seconds > 0:
            runs_registry.wait(run_id, min(float(wait_seconds), effective_timeout + 10.0))
        return self._describe(runs_registry.find(run_id))

    def _describe(self, record: RunRecord) -> dict[str, Any]:
        result = record.to_dict()
        if record.status in ("queued", "running"):
            result["hint"] = (
                "still running; call monitor_run(run_id) or get_run_log(run_id); "
                "do not resubmit the same run_id"
            )
        fml = Path(record.fml_path) if record.fml_path else None
        if fml and fml.is_file():
            result["log_size"] = fml.stat().st_size
        return result

    def _execute(
        self,
        run_id: str,
        timeout: float,
        cancel_event: threading.Event,
    ) -> None:
        record = runs_registry.find(run_id)
        run_dir = Path(record.workspace_dir)
        macro_file = Path(record.macro_path)
        fml_file = Path(record.fml_path)
        stdout_path = Path(record.stdout_path)
        stderr_path = Path(record.stderr_path)
        proc: subprocess.Popen[bytes] | None = None
        try:
            if cancel_event.is_set():
                runs_registry.finish(
                    run_id, status="cancelled", message="cancelled before start",
                    errors=["cancelled by request"], cancel_requested=True,
                )
                return
            exe = Path(settings.fracman_exe)
            if not exe.is_file():
                raise BatchError(f"FracMan executable not found: {exe}")
            runs_registry.update(run_id, status="running", started_at=now_iso())
            with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
                proc = subprocess.Popen(
                    [str(exe), str(macro_file)],
                    cwd=str(run_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    startupinfo=_hidden_startupinfo(),
                )
                with self._lock:
                    self._processes[run_id] = proc
                runs_registry.register_pid(run_id, proc.pid)

                deadline = time.monotonic() + timeout
                dialog_title: str | None = None
                timed_out = False
                cancelled = False
                last_dialog_check = 0.0
                while proc.poll() is None:
                    if cancel_event.is_set():
                        cancelled = True
                        self._terminate(proc)
                        break
                    now = time.monotonic()
                    if now >= deadline:
                        timed_out = True
                        self._terminate(proc)
                        break
                    if now - last_dialog_check >= 2.0:
                        last_dialog_check = now
                        dialog_title = _visible_modal_dialog(proc.pid)
                        if dialog_title:
                            self._terminate(proc)
                            break
                    time.sleep(0.25)
                returncode = proc.poll()
                if returncode is None:
                    self._terminate(proc)
                    returncode = proc.poll()
            runs_registry.update(run_id, returncode=returncode)

            log_result: dict[str, Any] = {}
            log_text = ""
            if fml_file.is_file():
                log_result = parse_fml(fml_file, tail_chars=settings.log_tail_chars)
                log_text = str(log_result.get("log_tail") or "")
            errors: list[str] = []
            if dialog_title:
                errors.append(f"batch blocked by modal dialog: {dialog_title}")
            if timed_out:
                errors.append(f"timeout after {timeout:.0f}s; process terminated")
            if cancelled:
                errors.append("cancelled by request")
            if not fml_file.is_file():
                if not (cancelled or timed_out or dialog_title):
                    errors.append("macro log was not created")
            elif log_result.get("error_count"):
                for item in log_result.get("errors", []):
                    location = ""
                    if item.get("macro_file"):
                        location = f" [{item['macro_file']}:{item.get('line')}]"
                    errors.append(f"Macro Error: {item.get('message')}{location}")
            elif not log_text.strip() and not (timed_out or cancelled or dialog_title):
                errors.append("macro log is empty")

            artifacts, missing = self._collect_artifacts(run_dir, record.expected_artifacts)
            for path in missing:
                errors.append(f"expected artifact missing: {path}")

            if errors:
                status = "cancelled" if cancelled else ("timeout" if timed_out else "failed")
                message = errors[0]
            else:
                status = "completed"
                message = f"completed with {len(artifacts)} artifact(s)"
                if log_result.get("block_count"):
                    message += f", {log_result['block_count']} macro block(s)"

            runs_registry.finish(
                run_id,
                status=status,
                returncode=returncode,
                pid=proc.pid if proc else None,
                artifacts=artifacts,
                errors=errors,
                log_tail=log_text[-1500:],
                cancel_requested=cancelled,
                message=message,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            runs_registry.finish(
                run_id,
                status="failed",
                errors=[f"{type(exc).__name__}: {exc}"],
                message=f"runner failed: {exc}",
            )
        finally:
            with self._lock:
                self._processes.pop(run_id, None)
                self._cancel.pop(run_id, None)
            runs_registry.clear_pid(run_id)

    def _terminate(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    @staticmethod
    def _collect_artifacts(
        run_dir: Path, expected: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        artifacts: list[dict[str, Any]] = []
        missing: list[str] = []
        seen: set[str] = set()
        for pattern in expected:
            candidate = Path(pattern)
            if not candidate.is_absolute():
                candidate = run_dir / candidate
            matches = sorted(glob.glob(str(candidate)))
            if not matches:
                missing.append(str(candidate))
                continue
            for match in matches:
                path = Path(match)
                key = str(path).lower()
                if key in seen:
                    continue
                seen.add(key)
                if not path.is_file():
                    missing.append(str(path))
                    continue
                stat = path.stat()
                artifacts.append(
                    {"path": str(path), "size_bytes": stat.st_size, "modified": stat.st_mtime}
                )
        generated = {"run.fmf", "run.fml", "stdout.txt", "stderr.txt"}
        for path in sorted(run_dir.glob("*")):
            if not path.is_file() or path.name in generated:
                continue
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            stat = path.stat()
            artifacts.append(
                {"path": str(path), "size_bytes": stat.st_size, "modified": stat.st_mtime}
            )
        return artifacts, missing

    def cancel(self, run_id: str, wait_seconds: float = 5.0) -> dict[str, Any]:
        record = runs_registry.find(run_id)
        if record.status not in ("queued", "running"):
            return record.to_dict()
        with self._lock:
            event = self._cancel.get(run_id)
            if event is None:
                raise BatchError(f"run {run_id} is active but has no cancel handle")
            event.set()
        runs_registry.update(run_id, cancel_requested=True, message="cancel requested")
        if wait_seconds > 0:
            runs_registry.wait(run_id, wait_seconds)
        result = runs_registry.find(run_id).to_dict()
        if result["status"] in ("queued", "running"):
            result["hint"] = "cancellation requested; monitor_run(run_id) for the final state"
        return result

    def monitor(self, run_id: str, wait_seconds: float = 0.0) -> dict[str, Any]:
        record = runs_registry.find(run_id)
        if wait_seconds > 0 and record.status in ("queued", "running"):
            runs_registry.wait(run_id, wait_seconds)
            record = runs_registry.find(run_id)
        return self._describe(record)

    def log(self, run_id: str, tail_chars: int = 8000) -> dict[str, Any]:
        record = runs_registry.find(run_id)
        result = record.to_dict()
        fml = Path(record.fml_path) if record.fml_path else None
        stdout = Path(record.stdout_path) if record.stdout_path else None
        stderr = Path(record.stderr_path) if record.stderr_path else None
        result["fml_tail"] = _tail(fml, tail_chars)
        result["stdout_tail"] = _tail(stdout, tail_chars)
        result["stderr_tail"] = _tail(stderr, tail_chars)
        if fml and fml.is_file():
            try:
                result["parsed_log"] = parse_fml(fml, tail_chars=tail_chars)
            except Exception as exc:  # pragma: no cover - defensive
                result["parsed_log"] = {"error": f"{type(exc).__name__}: {exc}"}
        return result


batch_runner = BatchRunner()
