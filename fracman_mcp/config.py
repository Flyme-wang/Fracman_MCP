"""Runtime configuration for the FracMan MCP server."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class Settings:
    """Mutable settings shared by all tools in this MCP process."""

    def __init__(self) -> None:
        local_appdata = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
        self._lock = threading.RLock()
        self.fracman_exe = _env_path(
            "FRACMAN_EXE", Path(r"D:\software\fracman\FracMan.exe")
        )
        self.workspace = _env_path(
            "FRACMAN_MCP_WORKSPACE", local_appdata / "FracManMCP"
        )
        self.max_batch = max(1, _env_int("FRACMAN_MCP_MAX_BATCH", 2))
        self.live_timeout = max(5.0, _env_float("FRACMAN_MCP_LIVE_TIMEOUT", 120.0))
        self.batch_timeout = max(10.0, _env_float("FRACMAN_MCP_BATCH_TIMEOUT", 3600.0))
        self.dpi_scale = _env_float("FRACMAN_MCP_DPI_SCALE", 0.0)
        self.log_tail_chars = max(500, _env_int("FRACMAN_MCP_LOG_TAIL", 4000))

        # dfnWorks runs inside WSL; the Windows server only builds job specs and
        # launches the venv interpreter, so all Linux paths stay configurable.
        self.wsl_exe = os.environ.get("FRACMAN_WSL_EXE", "wsl.exe")
        self.wsl_distro = os.environ.get("FRACMAN_WSL_DISTRO", "Ubuntu")
        self.wsl_user = os.environ.get("FRACMAN_WSL_USER", "wmw")
        self.dfnworks_venv = os.environ.get(
            "FRACMAN_DFNWORKS_VENV", "/opt/dfnworks-venv"
        )
        self.dfnworks_repo = os.environ.get(
            "FRACMAN_DFNWORKS_REPO", "/mnt/d/software/dfnWorks"
        )
        self.max_dfn_jobs = max(1, _env_int("FRACMAN_MCP_DFN_MAX_JOBS", 1))
        self.dfn_timeout = max(60.0, _env_float("FRACMAN_MCP_DFN_TIMEOUT", 7200.0))
        self.dfn_check_timeout = max(
            30.0, _env_float("FRACMAN_MCP_DFN_CHECK_TIMEOUT", 300.0)
        )
        self.ensure_dirs()

    @property
    def runs_dir(self) -> Path:
        return self.workspace / "runs"

    @property
    def projects_dir(self) -> Path:
        return self.workspace / "projects"

    @property
    def screenshots_dir(self) -> Path:
        return self.workspace / "screenshots"

    @property
    def exports_dir(self) -> Path:
        return self.workspace / "exports"

    @property
    def dfnworks_dir(self) -> Path:
        return self.workspace / "dfnworks"

    @property
    def dfn_jobs_dir(self) -> Path:
        return self.dfnworks_dir / "jobs"

    @property
    def dfnworks_python(self) -> str:
        return f"{self.dfnworks_venv.rstrip('/')}/bin/python"

    def ensure_dirs(self) -> None:
        for path in (
            self.workspace,
            self.runs_dir,
            self.projects_dir,
            self.screenshots_dir,
            self.exports_dir,
            self.dfnworks_dir,
            self.dfn_jobs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def set_workspace(self, path: str | Path) -> Path:
        with self._lock:
            new_path = Path(path).expanduser().resolve()
            new_path.mkdir(parents=True, exist_ok=True)
            self.workspace = new_path
            self.ensure_dirs()
            return new_path

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "fracman_exe": str(self.fracman_exe),
                "fracman_exe_exists": self.fracman_exe.exists(),
                "workspace": str(self.workspace),
                "workspace_ascii": str(self.workspace).isascii(),
                "runs_dir": str(self.runs_dir),
                "projects_dir": str(self.projects_dir),
                "screenshots_dir": str(self.screenshots_dir),
                "max_batch": self.max_batch,
                "live_timeout": self.live_timeout,
                "batch_timeout": self.batch_timeout,
                "dpi_scale": self.dpi_scale,
                "log_tail_chars": self.log_tail_chars,
                "dfnworks": {
                    "wsl_exe": self.wsl_exe,
                    "wsl_distro": self.wsl_distro,
                    "wsl_user": self.wsl_user,
                    "venv": self.dfnworks_venv,
                    "python": self.dfnworks_python,
                    "repo": self.dfnworks_repo,
                    "jobs_dir": str(self.dfn_jobs_dir),
                    "max_jobs": self.max_dfn_jobs,
                    "timeout": self.dfn_timeout,
                    "check_timeout": self.dfn_check_timeout,
                },
            }


settings = Settings()
