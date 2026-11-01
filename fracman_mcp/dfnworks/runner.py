"""dfnWorks job runner: validated specs, WSL subprocesses, result.json verdicts.

Unlike FracMan, dfnWorks does have a Python API, but it only exists inside the
WSL venv. The Windows server therefore writes a self-contained driver and runs
it through ``wsl.exe``; the driver's ``result.json`` is the authoritative
success signal (dfnWorks itself may call ``sys.exit`` in the middle of a run).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..config import settings
from ..models import DfnJobRecord, now_iso
from ..requests import RequestConflict, dfn_jobs_registry, fingerprint
from .driver_template import DRIVER_SOURCE
from .paths import windows_to_wsl


class DfnError(RuntimeError):
    pass


class DfnBusy(DfnError):
    def __init__(self, message: str, active: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.active = active


PROBE_SOURCE = r'''import json, os, sys
info = {"python": sys.executable, "python_version": sys.version.split()[0]}
try:
    import pydfnworks
    info["pydfnworks_version"] = getattr(pydfnworks, "__version__", "unknown")
except Exception as exc:
    info["import_error"] = f"{type(exc).__name__}: {exc}"
try:
    import networkx
    info["networkx_version"] = networkx.__version__
except Exception as exc:
    info["networkx_error"] = f"{type(exc).__name__}: {exc}"
rc_path = os.path.expanduser("~/.dfnworksrc")
info["dfnworksrc"] = rc_path
rc = {}
if os.path.isfile(rc_path):
    try:
        with open(rc_path, "r", encoding="utf-8") as handle:
            rc = json.load(handle)
    except Exception as exc:
        info["dfnworksrc_error"] = f"{type(exc).__name__}: {exc}"
info["settings"] = rc
repo = rc.get("dfnworks_PATH", "")
info["repo_exists"] = bool(repo) and os.path.isdir(repo)
lagrit = rc.get("LAGRIT_EXE", "")
info["lagrit_exists"] = bool(lagrit) and os.path.isfile(lagrit)
dfngen = os.path.join(repo, "DFNGen/DFNGen") if repo else ""
info["dfngen_exists"] = bool(dfngen) and os.path.isfile(dfngen)
info["mnt_c"] = os.path.isdir("/mnt/c")
info["pflotran_configured"] = bool(rc.get("PFLOTRAN_EXE"))
info["fehm_configured"] = bool(rc.get("FEHM_EXE"))
print("DFN_PROBE_JSON:" + json.dumps(info))
'''


def make_job_id() -> str:
    return "dfn-" + uuid.uuid4().hex[:12]


def _tail(path: Path | None, chars: int = 4000) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-chars:]
    except OSError:
        return ""


def _hidden_startupinfo() -> subprocess.STARTUPINFO:
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


def _wsl_base() -> list[str]:
    return [
        settings.wsl_exe,
        "-d",
        settings.wsl_distro,
        "-u",
        settings.wsl_user,
        "--",
    ]


class DfnJobRunner:
    def __init__(
        self,
        command_builder: Callable[[Path], list[str]] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._command_builder = command_builder or self._wsl_command

    # -- submission ---------------------------------------------------------

    def submit(
        self,
        stage: str,
        spec: dict[str, Any],
        *,
        job_id: str | None = None,
        timeout: float | None = None,
        wait_seconds: float = 0.0,
        label: str = "",
    ) -> dict[str, Any]:
        if not isinstance(spec, dict):
            raise DfnError("spec must be an object")
        effective_timeout = float(timeout) if timeout is not None else settings.dfn_timeout
        if effective_timeout <= 0:
            raise DfnError("timeout must be positive")
        digest = fingerprint({"stage": stage, "spec": spec})

        if job_id is None:
            job_id = make_job_id()
        job_id = str(job_id).strip()
        if not job_id:
            raise DfnError("job_id must not be empty")
        if len(job_id) > 200:
            raise DfnError("job_id must be at most 200 characters")

        existing = dfn_jobs_registry.get(job_id)
        if existing is not None:
            if existing.fingerprint and existing.fingerprint != digest:
                raise RequestConflict(
                    f"job_id {job_id!r} already exists with a different payload"
                )
            return self._describe(existing)

        with self._lock:
            active = dfn_jobs_registry.count_active()
            if active >= settings.max_dfn_jobs:
                raise DfnBusy(
                    f"dfnWorks capacity reached ({active}/{settings.max_dfn_jobs}); "
                    "wait for a job to finish or raise FRACMAN_MCP_DFN_MAX_JOBS",
                    dfn_jobs_registry.list(limit=settings.max_dfn_jobs + 5),
                )
            job_dir = settings.dfn_jobs_dir / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            spec_path = job_dir / "spec.json"
            driver_path = job_dir / "driver.py"
            spec_path.write_text(
                json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            driver_path.write_text(DRIVER_SOURCE, encoding="utf-8")
            record = DfnJobRecord(
                job_id=job_id,
                stage=stage,
                fingerprint=digest,
                workspace_dir=str(job_dir),
                spec_path=str(spec_path),
                driver_path=str(driver_path),
                stdout_path=str(job_dir / "stdout.txt"),
                stderr_path=str(job_dir / "stderr.txt"),
                result_path=str(job_dir / "result.json"),
                timeout=effective_timeout,
                message=label or stage,
            )
            dfn_jobs_registry.create(record)
            cancel_event = threading.Event()
            self._cancel[job_id] = cancel_event

        worker = threading.Thread(
            target=self._execute,
            args=(job_id, effective_timeout, cancel_event),
            name=f"dfnworks-{job_id}",
            daemon=True,
        )
        worker.start()

        if wait_seconds and wait_seconds > 0:
            dfn_jobs_registry.wait(
                job_id, min(float(wait_seconds), effective_timeout + 10.0)
            )
        return self._describe(dfn_jobs_registry.find(job_id))

    def _describe(self, record: DfnJobRecord) -> dict[str, Any]:
        result = record.to_dict()
        if record.status in ("queued", "running"):
            result["hint"] = (
                "still running; call dfnworks_job_status(job_id, wait_seconds) "
                "or dfnworks_job_log(job_id); do not resubmit the same job_id"
            )
        job_dir = Path(record.workspace_dir) if record.workspace_dir else None
        if job_dir is not None and job_dir.is_dir():
            result["job_dir"] = str(job_dir)
        return result

    # -- execution ----------------------------------------------------------

    def _wsl_command(self, job_dir: Path) -> list[str]:
        driver_wsl = windows_to_wsl(job_dir / "driver.py")
        job_wsl = windows_to_wsl(job_dir)
        script = (
            f"{shlex.quote(settings.dfnworks_python)} -u "
            f"{shlex.quote(driver_wsl)} {shlex.quote(job_wsl)}"
        )
        shell = (
            "export MPLBACKEND=Agg; "
            "export PETSC_DIR='' PETSC_ARCH='' PFLOTRAN_EXE='' FEHM_EXE=''; "
            f"exec {script}"
        )
        return _wsl_base() + ["bash", "-c", shell]

    def _execute(
        self, job_id: str, timeout: float, cancel_event: threading.Event
    ) -> None:
        record = dfn_jobs_registry.find(job_id)
        job_dir = Path(record.workspace_dir)
        stdout_path = Path(record.stdout_path)
        stderr_path = Path(record.stderr_path)
        result_path = Path(record.result_path)
        proc: subprocess.Popen[bytes] | None = None
        try:
            if cancel_event.is_set():
                dfn_jobs_registry.finish(
                    job_id,
                    status="cancelled",
                    message="cancelled before start",
                    errors=["cancelled by request"],
                    cancel_requested=True,
                )
                return
            command = self._command_builder(job_dir)
            dfn_jobs_registry.update(job_id, status="running", started_at=now_iso())
            with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
                proc = subprocess.Popen(
                    command,
                    cwd=str(job_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    startupinfo=_hidden_startupinfo(),
                )
                with self._lock:
                    self._processes[job_id] = proc
                dfn_jobs_registry.register_pid(job_id, proc.pid)

                deadline = time.monotonic() + timeout
                timed_out = False
                cancelled = False
                while proc.poll() is None:
                    if cancel_event.is_set():
                        cancelled = True
                        self._terminate(proc, job_dir)
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        self._terminate(proc, job_dir)
                        break
                    time.sleep(0.25)
                returncode = proc.poll()
                if returncode is None:
                    self._terminate(proc, job_dir)
                    returncode = proc.poll()
            dfn_jobs_registry.update(job_id, returncode=returncode)

            result = self._read_result(result_path)
            errors: list[str] = []
            if cancelled:
                errors.append("cancelled by request")
            if timed_out:
                errors.append(f"timeout after {timeout:.0f}s; process terminated")
            if not result:
                stderr_tail = _tail(stderr_path, 1200).strip()
                errors.append(
                    "driver did not write result.json"
                    + (f"; stderr: {stderr_tail[-600:]}" if stderr_tail else "")
                )
            elif not result.get("ok"):
                errors.append(
                    str(result.get("error") or "driver reported failure")
                )
                errors.extend(self._traceback_lines(result))
            else:
                errors.extend(self._artifact_errors(job_dir, record.stage))

            if errors:
                status = (
                    "cancelled"
                    if cancelled
                    else ("timeout" if timed_out else "failed")
                )
                message = errors[0]
            else:
                status = "completed"
                message = result.get("message") or "completed"

            dfn_jobs_registry.finish(
                job_id,
                status=status,
                returncode=returncode,
                pid=proc.pid if proc else None,
                result=result,
                artifacts=self._list_artifacts(job_dir),
                errors=errors,
                log_tail=_tail(stdout_path, 1500),
                cancel_requested=cancelled,
                message=message,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            dfn_jobs_registry.finish(
                job_id,
                status="failed",
                errors=[f"{type(exc).__name__}: {exc}"],
                message=f"runner failed: {exc}",
            )
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
                self._cancel.pop(job_id, None)
            dfn_jobs_registry.clear_pid(job_id)

    @staticmethod
    def _read_result(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _traceback_lines(result: dict[str, Any]) -> list[str]:
        trace = str(result.get("traceback") or "").strip()
        if not trace:
            return []
        return [f"traceback: {line}" for line in trace.splitlines()[-12:]]

    @staticmethod
    def _artifact_errors(job_dir: Path, stage: str) -> list[str]:
        required: list[Path] = [job_dir / "dfn_stats.json"]
        if stage == "mesh":
            required.extend([job_dir / "mesh_stats.json", job_dir / "run" / "full_mesh.inp"])
        if stage == "graph":
            required.extend([job_dir / "graph_metrics.json", job_dir / "graph.json"])
        missing = [str(path) for path in required if not path.is_file()]
        return [f"expected artifact missing: {path}" for path in missing]

    @staticmethod
    def _list_artifacts(job_dir: Path) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for path in sorted(job_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.name in {
                "spec.json",
                "driver.py",
                "stdout.txt",
                "stderr.txt",
                "result.json",
            }:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            artifacts.append(
                {
                    "path": str(path),
                    "size_bytes": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
        return artifacts

    def _terminate(self, proc: subprocess.Popen[bytes], job_dir: Path) -> None:
        if proc.poll() is None:
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
        self._kill_remote(job_dir)

    @staticmethod
    def _kill_remote(job_dir: Path) -> None:
        """Kill only this job's driver inside WSL (the job path is unique)."""
        try:
            pattern = windows_to_wsl(job_dir / "driver.py")
        except ValueError:
            return
        try:
            subprocess.run(
                _wsl_base() + ["pkill", "-f", pattern],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # -- query / control ----------------------------------------------------

    def cancel(self, job_id: str, wait_seconds: float = 5.0) -> dict[str, Any]:
        record = dfn_jobs_registry.find(job_id)
        if record.status not in ("queued", "running"):
            return record.to_dict()
        with self._lock:
            event = self._cancel.get(job_id)
            if event is None:
                raise DfnError(f"job {job_id} is active but has no cancel handle")
            event.set()
        dfn_jobs_registry.update(
            job_id, cancel_requested=True, message="cancel requested"
        )
        if wait_seconds > 0:
            dfn_jobs_registry.wait(job_id, wait_seconds)
        result = dfn_jobs_registry.find(job_id).to_dict()
        if result["status"] in ("queued", "running"):
            result["hint"] = (
                "cancellation requested; dfnworks_job_status(job_id) shows the "
                "final state"
            )
        return result

    def monitor(self, job_id: str, wait_seconds: float = 0.0) -> dict[str, Any]:
        record = dfn_jobs_registry.find(job_id)
        if wait_seconds > 0 and record.status in ("queued", "running"):
            dfn_jobs_registry.wait(job_id, wait_seconds)
            record = dfn_jobs_registry.find(job_id)
        return self._describe(record)

    def log(self, job_id: str, tail_chars: int = 8000) -> dict[str, Any]:
        record = dfn_jobs_registry.find(job_id)
        result = record.to_dict()
        stdout = Path(record.stdout_path) if record.stdout_path else None
        stderr = Path(record.stderr_path) if record.stderr_path else None
        result["stdout_tail"] = _tail(stdout, tail_chars)
        result["stderr_tail"] = _tail(stderr, tail_chars)
        job_dir = Path(record.workspace_dir)
        logs: dict[str, str] = {}
        for path in sorted(job_dir.glob("*.log")):
            logs[path.name] = _tail(path, tail_chars)
        if logs:
            result["dfnworks_logs"] = logs
        return result

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        return dfn_jobs_registry.list(limit=limit)

    # -- environment probe --------------------------------------------------

    def check(self, timeout: float | None = None) -> dict[str, Any]:
        probe = settings.dfnworks_dir / "probe.py"
        probe.write_text(PROBE_SOURCE, encoding="utf-8")
        script = (
            f"exec {shlex.quote(settings.dfnworks_python)} -u "
            f"{shlex.quote(windows_to_wsl(probe))}"
        )
        command = _wsl_base() + ["bash", "-c", script]
        effective = float(timeout) if timeout else settings.dfn_check_timeout
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=effective,
            )
        except FileNotFoundError as exc:
            return {
                "ok": False,
                "error": f"WSL executable not found: {settings.wsl_exe}",
                "details": str(exc),
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"dfnWorks probe timed out after {effective:.0f}s",
            }
        except OSError as exc:
            return {"ok": False, "error": f"probe failed to start: {exc}"}

        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        payload: dict[str, Any] | None = None
        for line in stdout.splitlines():
            if line.startswith("DFN_PROBE_JSON:"):
                try:
                    payload = json.loads(line.split(":", 1)[1])
                except ValueError:
                    payload = None
        duration = round(time.monotonic() - started, 2)
        if payload is None:
            return {
                "ok": False,
                "returncode": completed.returncode,
                "duration_s": duration,
                "error": "probe did not return DFN_PROBE_JSON",
                "stdout_tail": stdout[-1500:],
                "stderr_tail": stderr[-1500:],
            }
        ready = bool(
            payload.get("pydfnworks_version")
            and payload.get("repo_exists")
            and payload.get("lagrit_exists")
            and payload.get("dfngen_exists")
        )
        return {
            "ok": ready,
            "returncode": completed.returncode,
            "duration_s": duration,
            "probe": payload,
            "checks": {
                "pydfnworks_import": bool(payload.get("pydfnworks_version")),
                "dfnworks_repo": bool(payload.get("repo_exists")),
                "lagrit": bool(payload.get("lagrit_exists")),
                "dfngen": bool(payload.get("dfngen_exists")),
                "wsl_mnt_c": bool(payload.get("mnt_c")),
            },
            "stderr_tail": stderr[-800:],
            "note": (
                "PFLOTRAN/FEHM are optional and not used by this server"
                if not payload.get("pflotran_configured")
                else "a flow solver is configured but this server exposes no flow tools"
            ),
        }


dfn_runner = DfnJobRunner()
