"""FracMan 8.0 + dfnWorks MCP server.

Three execution channels, one control plane:

* live GUI bridge — runs macros in an already-open FracMan session;
* batch runner — starts a hidden FracMan process per macro and judges success
  from the generated ``.fml`` log;
* dfnWorks runner — validates a DFN spec on Windows and executes it with the
  dfnWorks venv inside WSL (DFNGen, LaGriT meshing, connectivity graph);
* file intelligence — parses FAB/FFB/F2D/SAB/FML/FRD files.

The MCP itself is local stdio only: no network listener, no authentication.
"""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from fracman_mcp import __version__, gui
from fracman_mcp.config import settings
from fracman_mcp.dfnworks import (
    DfnBusy,
    DfnError,
    DfnSpecError,
    build_spec,
    compare as compare_dfnworks,
    dfn_runner,
    merge_spec,
)
from fracman_mcp.macro import MacroValidationError
from fracman_mcp.parsers import SUPPORTED_SUFFIXES, inspect_file as inspect_file_impl
from fracman_mcp.requests import dfn_jobs_registry, requests_registry, runs_registry
from fracman_mcp.runner import BatchBusy, BatchError, batch_runner
from fracman_mcp.sessions import SessionNotFound, session_manager

INSTRUCTIONS = """\
FracMan 8.0 has no API. This server controls it through macros, the running GUI,
and file parsing. dfnWorks is exposed through the same server but runs inside
WSL (DFNGen, LaGriT conforming mesh, connectivity graph); its jobs are validated
specs executed by a generated driver.

Start FracMan work with discover_sessions. Use run_macro for the user's live
session and submit_macro for headless batch work. Judge batch success from the
.fml log, never from the exit code. request_id/run_id are idempotency keys:
after a timeout report the unknown outcome and query
get_request_status/monitor_run; never replay. Live macros reject EXIT and
DeleteAll unless explicitly allowed. close_session refuses user-owned sessions
unless force=true.

For dfnWorks call dfnworks_check first, then dfnworks_generate; pass its job_id
to dfnworks_mesh and dfnworks_graph. dfnworks_compare checks a FracMan .fab
against a dfnWorks job. dfnWorks job ids are idempotency keys too: query status
instead of resubmitting, and cancel only jobs started by this server.
"""

mcp = FastMCP("wmw-fracman-mcp", instructions=INSTRUCTIONS)

RESOURCE_DIR = Path(__file__).resolve().parent / "fracman_mcp" / "resources"


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _tool(func: Callable[..., Any]) -> Callable[..., Any]:
    """Return JSON errors instead of FastMCP tracebacks, preserving the schema."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await func(*args, **kwargs)
        except (
            SessionNotFound,
            MacroValidationError,
            BatchBusy,
            BatchError,
            DfnBusy,
            DfnError,
            DfnSpecError,
        ) as exc:
            payload: dict[str, Any] = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            active = getattr(exc, "active", None)
            if active:
                payload["active_runs"] = active
            return _json(payload)
        except KeyError as exc:
            return _json(
                {"ok": False, "error_type": "NotFound", "error": f"unknown id: {exc}"}
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the client
            return _json(
                {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
            )

    return wrapper


def _ensure_workspace_path(path: str) -> Path:
    candidate = Path(path).expanduser().resolve()
    workspace = settings.workspace.resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError(
            f"write target must be inside the MCP workspace ({workspace}); got {candidate}"
        )
    return candidate


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


# ---------------------------------------------------------------------------
# Configuration and sessions
# ---------------------------------------------------------------------------


@mcp.tool()
@_tool
async def get_config() -> str:
    """Show the FracMan executable, workspace, limits, and package version."""
    return _json(
        {
            "ok": True,
            "version": __version__,
            "settings": settings.snapshot(),
            "supported_file_types": SUPPORTED_SUFFIXES,
        }
    )


@mcp.tool()
@_tool
async def set_workspace(path: str) -> str:
    """Change the MCP workspace (macro runs, screenshots, project copies).

    The directory is created if missing. ASCII paths are strongly recommended
    because FracMan file dialogs and macros are ASCII-oriented.
    """
    workspace = await asyncio.to_thread(settings.set_workspace, path)
    return _json(
        {
            "ok": True,
            "workspace": str(workspace),
            "ascii": str(workspace).isascii(),
            "settings": settings.snapshot(),
        }
    )


@mcp.tool()
@_tool
async def discover_sessions() -> str:
    """Enumerate running FracMan windows, read their status and log size."""
    sessions = await asyncio.to_thread(session_manager.discover)
    return _json({"ok": True, "count": len(sessions), "sessions": sessions})


@mcp.tool()
@_tool
async def list_sessions() -> str:
    """List cached sessions (call discover_sessions to refresh)."""
    sessions = await asyncio.to_thread(session_manager.list)
    return _json({"ok": True, "count": len(sessions), "sessions": sessions})


@mcp.tool()
@_tool
async def set_active_session(session_id: str) -> str:
    """Select the default session for subsequent live operations."""
    session = await asyncio.to_thread(session_manager.set_active, session_id)
    return _json({"ok": True, "session": session.to_dict()})


@mcp.tool()
@_tool
async def get_session_info(session_id: str | None = None, log_chars: int = 4000) -> str:
    """Read status, project title, dialogs, window/view rectangles and log tail."""
    info = await asyncio.to_thread(
        session_manager.info, session_id, int(_clamp(log_chars, 200, 20000))
    )
    return _json({"ok": True, **info})


@mcp.tool()
@_tool
async def ping(session_id: str | None = None) -> str:
    """Check that the GUI bridge can read the FracMan status bar."""
    result = await asyncio.to_thread(session_manager.ping, session_id)
    return _json(result)


# ---------------------------------------------------------------------------
# Live execution
# ---------------------------------------------------------------------------


@mcp.tool()
@_tool
async def run_macro(
    code: str | None = None,
    path: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    wait_seconds: float = 30.0,
    allow_exit: bool = False,
    allow_destructive: bool = False,
) -> str:
    """Run a macro inside the live FracMan GUI (single-flight per session).

    Provide exactly one of ``code`` or ``path``. Returns the macro log delta,
    parsed errors and dialogs. If the wait elapses, the status is
    ``timeout_pending`` and the original request must be queried with
    get_request_status; it is never replayed automatically.
    """
    result = await asyncio.to_thread(
        session_manager.start_live_macro,
        code=code,
        path=path,
        session_id=session_id,
        request_id=request_id,
        wait_seconds=_clamp(wait_seconds, 0.0, 6 * 3600),
        allow_exit=allow_exit,
        allow_destructive=allow_destructive,
    )
    return _json(result)


@mcp.tool()
@_tool
async def get_request_status(request_id: str, wait_seconds: float = 0.0) -> str:
    """Query a live macro request. Waits at most wait_seconds for completion."""
    record = requests_registry.get(request_id)
    if record is None:
        return _json(
            {"ok": False, "error_type": "NotFound", "error": f"unknown request_id {request_id!r}"}
        )
    if wait_seconds > 0 and record.status in ("queued", "running"):
        await asyncio.to_thread(requests_registry.wait, request_id, _clamp(wait_seconds, 0.0, 600))
        record = requests_registry.get(request_id)
    data = record.to_dict() if record else {}
    return _json({"ok": bool(record and record.status == "completed"), **data})


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------


@mcp.tool()
@_tool
async def submit_macro(
    code: str | None = None,
    path: str | None = None,
    run_id: str | None = None,
    expected_artifacts: list[str] | None = None,
    timeout: float | None = None,
    wait_seconds: float = 0.0,
    label: str = "",
) -> str:
    """Start a hidden FracMan batch process for a macro.

    The server adds LOGTOFILE, EXITONERROR and EXIT. Success is judged from the
    ``.fml`` log and ``expected_artifacts`` (absolute paths or globs relative to
    the run directory). Returns immediately unless wait_seconds > 0.
    """
    result = await asyncio.to_thread(
        batch_runner.submit,
        code=code,
        macro_path=path,
        run_id=run_id,
        expected_artifacts=expected_artifacts,
        timeout=timeout,
        wait_seconds=_clamp(wait_seconds, 0.0, 6 * 3600),
        label=label,
    )
    return _json(result)


@mcp.tool()
@_tool
async def monitor_run(run_id: str, wait_seconds: float = 0.0) -> str:
    """Show a batch run's state, artifacts and errors; optionally wait."""
    result = await asyncio.to_thread(
        batch_runner.monitor, run_id, _clamp(wait_seconds, 0.0, 6 * 3600)
    )
    return _json(result)


@mcp.tool()
@_tool
async def cancel_run(run_id: str, wait_seconds: float = 5.0) -> str:
    """Terminate an MCP-owned batch process (never a user GUI session)."""
    result = await asyncio.to_thread(
        batch_runner.cancel, run_id, _clamp(wait_seconds, 0.0, 120)
    )
    return _json(result)


@mcp.tool()
@_tool
async def get_run_log(run_id: str, tail_chars: int = 8000) -> str:
    """Read the macro log, stdout/stderr and parsed errors of a batch run."""
    result = await asyncio.to_thread(
        batch_runner.log, run_id, int(_clamp(tail_chars, 200, 200000))
    )
    return _json(result)


@mcp.tool()
@_tool
async def list_runs(limit: int = 20) -> str:
    """List recent batch runs, newest first."""
    return _json(
        {"ok": True, "runs": runs_registry.list(limit=int(_clamp(limit, 1, 200)))}
    )


# ---------------------------------------------------------------------------
# dfnWorks (WSL) — generation, meshing, connectivity, comparison
# ---------------------------------------------------------------------------


def _job_spec(job_id: str) -> dict[str, Any]:
    record = dfn_jobs_registry.get(job_id)
    if record is None:
        raise KeyError(job_id)
    spec = json.loads(
        Path(record.spec_path).read_text(encoding="utf-8", errors="replace")
    )
    spec.pop("_run_dir", None)
    return spec


@mcp.tool()
@_tool
async def dfnworks_check() -> str:
    """Probe the WSL dfnWorks install: import, DFNGen, LaGriT, repo paths."""
    result = await asyncio.to_thread(dfn_runner.check)
    return _json(result)


@mcp.tool()
@_tool
async def dfnworks_generate(
    domain: list[float] | None = None,
    h: float | None = None,
    n_poly: int | None = None,
    seed: int | None = None,
    families: list[dict[str, Any]] | None = None,
    stop_condition: int = 0,
    orientation_option: int = 1,
    boundary_faces: list[int] | None = None,
    ncpu: int = 4,
    output_report: bool = False,
    job_id: str | None = None,
    timeout: float | None = None,
    wait_seconds: float = 60.0,
) -> str:
    """Generate a DFN with DFNGen and export fracture statistics.

    ``domain`` is [x, y, z] in metres; ``h`` is the LaGriT target length scale
    (default max(domain)/50, i.e. 2% of the longest side). With
    ``stop_condition=0`` fracture counts follow ``n_poly`` and per-family
    ``probability``; with ``stop_condition=1`` use per-family ``p32``.
    ``families`` accepts the dfnWorks add_fracture_family keys (shape,
    distribution, kappa, aspect, trend, plunge, constant, min_radius,
    max_radius, probability/p32, hy_variable, hy_function, hy_params, ...).
    Outputs: dfn_stats.json, fractures.json/csv.
    """
    spec = await asyncio.to_thread(
        build_spec,
        "generate",
        domain=domain,
        h=h,
        n_poly=n_poly,
        seed=seed,
        families=families,
        stop_condition=stop_condition,
        orientation_option=orientation_option,
        boundary_faces=boundary_faces,
        ncpu=ncpu,
        output_report=output_report,
    )
    result = await asyncio.to_thread(
        dfn_runner.submit,
        "generate",
        spec,
        job_id=job_id,
        timeout=timeout,
        wait_seconds=_clamp(wait_seconds, 0.0, 6 * 3600),
        label="generate",
    )
    return _json(result)


@mcp.tool()
@_tool
async def dfnworks_mesh(
    job_id: str,
    uniform_mesh: bool = True,
    min_dist: float = 0.5,
    max_dist: float = 10.0,
    max_resolution_factor: float = 10.0,
    new_job_id: str | None = None,
    timeout: float | None = None,
    wait_seconds: float = 0.0,
) -> str:
    """Build a LaGriT conforming mesh from a generated dfnWorks job.

    The source job's specification is reused, so the same seed reproduces the
    same DFN; the mesh and its statistics are written into the new job
    directory. Meshing is CPU-heavy and typically the slowest stage.
    """
    base = await asyncio.to_thread(_job_spec, job_id)
    spec = await asyncio.to_thread(
        merge_spec,
        base,
        "mesh",
        uniform_mesh=uniform_mesh,
        min_dist=min_dist,
        max_dist=max_dist,
        max_resolution_factor=max_resolution_factor,
    )
    result = await asyncio.to_thread(
        dfn_runner.submit,
        "mesh",
        spec,
        job_id=new_job_id,
        timeout=timeout,
        wait_seconds=_clamp(wait_seconds, 0.0, 6 * 3600),
        label=f"mesh from {job_id}",
    )
    return _json(result)


@mcp.tool()
@_tool
async def dfnworks_graph(
    job_id: str,
    graph_type: str = "fracture",
    inflow: str = "left",
    outflow: str = "right",
    k: int = 10,
    new_job_id: str | None = None,
    timeout: float | None = None,
    wait_seconds: float = 0.0,
) -> str:
    """Build the connectivity graph and k-shortest-path backbone.

    ``graph_type`` is fracture, intersection or bipartite. The graph stage
    regenerates the same DFN (seed) but does not mesh, so it is much faster
    than dfnworks_mesh. Outputs: graph.json, graph_metrics.json and, when
    source and target are connected, backbone.json/backbone_fractures.txt.
    """
    base = await asyncio.to_thread(_job_spec, job_id)
    spec = await asyncio.to_thread(
        merge_spec,
        base,
        "graph",
        graph_type=graph_type,
        inflow=inflow,
        outflow=outflow,
        k=k,
    )
    result = await asyncio.to_thread(
        dfn_runner.submit,
        "graph",
        spec,
        job_id=new_job_id,
        timeout=timeout,
        wait_seconds=_clamp(wait_seconds, 0.0, 6 * 3600),
        label=f"graph from {job_id}",
    )
    return _json(result)


@mcp.tool()
@_tool
async def dfnworks_compare(
    fracman_fab: str,
    job_id: str | None = None,
    dfnworks_stats: str | None = None,
    domain: list[float] | None = None,
) -> str:
    """Compare a FracMan .fab/.ffb with a dfnWorks job (or a stats JSON).

    Reports count, total/mean area and radius, P32, bbox IoU, mean-pole angle
    and 2x-threshold warnings. Provide exactly one of job_id or dfnworks_stats.
    ``domain`` overrides the dfnWorks domain when computing FracMan P32.
    """
    if (job_id is None) == (dfnworks_stats is None):
        raise ValueError("provide exactly one of job_id or dfnworks_stats")
    source: str
    if job_id is not None:
        record = dfn_jobs_registry.get(job_id)
        if record is None:
            raise KeyError(job_id)
        source = record.workspace_dir
    else:
        source = str(dfnworks_stats)
    result = await asyncio.to_thread(
        compare_dfnworks, fracman_fab, source, domain=domain
    )
    return _json(result)


@mcp.tool()
@_tool
async def dfnworks_job_status(job_id: str, wait_seconds: float = 0.0) -> str:
    """Show a dfnWorks job's state, statistics and errors; optionally wait."""
    result = await asyncio.to_thread(
        dfn_runner.monitor, job_id, _clamp(wait_seconds, 0.0, 6 * 3600)
    )
    return _json(result)


@mcp.tool()
@_tool
async def dfnworks_job_cancel(job_id: str, wait_seconds: float = 5.0) -> str:
    """Cancel an MCP-owned dfnWorks job (never an unrelated WSL process)."""
    result = await asyncio.to_thread(
        dfn_runner.cancel, job_id, _clamp(wait_seconds, 0.0, 120)
    )
    return _json(result)


@mcp.tool()
@_tool
async def dfnworks_job_log(job_id: str, tail_chars: int = 8000) -> str:
    """Read stdout/stderr and dfnWorks log tails of a job."""
    result = await asyncio.to_thread(
        dfn_runner.log, job_id, int(_clamp(tail_chars, 200, 200000))
    )
    return _json(result)


@mcp.tool()
@_tool
async def dfnworks_list_jobs(limit: int = 20) -> str:
    """List recent dfnWorks jobs, newest first."""
    return _json({"ok": True, "jobs": dfn_runner.list(limit=int(_clamp(limit, 1, 200)))})


# ---------------------------------------------------------------------------
# Files and views
# ---------------------------------------------------------------------------


@mcp.tool()
@_tool
async def inspect_file(
    path: str, preview_limit: int = 20, include_hash: bool = False
) -> str:
    """Parse a FracMan file: .fab/.ffb/.f2d/.sab/.fml/.fmf/.frd.

    FAB/FFB return fracture counts, sets, properties, area and bbox; F2D
    returns trace/segment statistics; SAB returns borehole geometry; FML
    returns macro errors; FRD is heuristic only.
    """
    result = await asyncio.to_thread(
        inspect_file_impl,
        path,
        preview_limit=int(_clamp(preview_limit, 1, 200)),
        include_hash=include_hash,
    )
    return _json(result)


@mcp.tool()
@_tool
async def list_files(
    directory: str,
    pattern: str = "*",
    recursive: bool = False,
    limit: int = 200,
) -> str:
    """List files and folders for discovery (read-only)."""
    root = Path(directory).expanduser()
    if not root.is_dir():
        return _json(
            {"ok": False, "error_type": "NotFound", "error": f"not a directory: {root}"}
        )
    cap = int(_clamp(limit, 1, 5000))
    iterator = root.rglob(pattern) if recursive else root.glob(pattern)
    entries: list[dict[str, Any]] = []
    truncated = False
    for path in iterator:
        try:
            stat = path.stat()
            is_dir = path.is_dir()
        except OSError:
            continue
        entries.append(
            {
                "name": path.name,
                "path": str(path),
                "is_dir": is_dir,
                "size_bytes": None if is_dir else stat.st_size,
                "modified": stat.st_mtime,
            }
        )
        if len(entries) >= cap:
            truncated = True
            break
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    return _json(
        {
            "ok": True,
            "directory": str(root.resolve()),
            "pattern": pattern,
            "count": len(entries),
            "truncated": truncated,
            "entries": entries,
        }
    )


@mcp.tool()
@_tool
async def capture_window(
    session_id: str | None = None,
    mode: str = "full",
    out_path: str | None = None,
) -> str:
    """Capture the FracMan window (mode='full') or its 3D view (mode='view').

    Returns the PNG path, dimensions and crop metadata. Use get_viewport_image
    when the image itself is wanted. A custom out_path must be inside the
    workspace.
    """
    if mode not in ("full", "view"):
        raise ValueError("mode must be 'full' or 'view'")
    if out_path is not None:
        out_path = str(_ensure_workspace_path(out_path))
    result = await asyncio.to_thread(
        session_manager.capture, session_id, mode=mode, out_path=out_path
    )
    return _json({"ok": bool(result.get("path")), **result})


@mcp.tool()
@_tool
async def get_viewport_image(
    session_id: str | None = None,
    mode: str = "view",
    out_path: str | None = None,
) -> str:
    """Capture the 3D view and return a PNG data URI for direct display."""
    if mode not in ("full", "view"):
        raise ValueError("mode must be 'full' or 'view'")
    if out_path is not None:
        out_path = str(_ensure_workspace_path(out_path))
    result = await asyncio.to_thread(
        session_manager.capture, session_id, mode=mode, out_path=out_path
    )
    return gui.image_data_uri(result["path"])


# ---------------------------------------------------------------------------
# Project lifecycle
# ---------------------------------------------------------------------------


@mcp.tool()
@_tool
async def open_project(
    project_path: str,
    copy: bool = True,
    timeout: float = 45.0,
    activate: bool = True,
) -> str:
    """Open a FracMan project in a new, MCP-owned window.

    copy=True (default) copies the project into the workspace first, so the
    source file is never modified. Startup/license dialogs are reported, not
    auto-answered beyond OK/Continue.
    """
    result = await asyncio.to_thread(
        session_manager.open_project,
        project_path,
        copy=copy,
        timeout=_clamp(timeout, 5.0, 600.0),
        activate=activate,
    )
    return _json(result)


@mcp.tool()
@_tool
async def close_session(
    session_id: str | None = None,
    save: bool = False,
    force: bool = False,
    terminate: bool = True,
) -> str:
    """Close an MCP-owned FracMan window (refuses user sessions unless forced).

    save=False answers "No" to save prompts. The process is terminated only if
    a graceful WM_CLOSE fails and terminate=True.
    """
    result = await asyncio.to_thread(
        session_manager.close_session,
        session_id,
        save=save,
        force=force,
        terminate=terminate,
    )
    return _json(result)


@mcp.tool()
@_tool
async def get_macro_reference(topic: str | None = None) -> str:
    """Read the bundled FracMan macro reference (verified command subset)."""
    text = (RESOURCE_DIR / "macro_reference.md").read_text(encoding="utf-8")
    if topic:
        needle = topic.strip().lower()
        sections = text.split("\n## ")
        hits = [sections[0]]
        for section in sections[1:]:
            if needle in section.lower():
                hits.append("## " + section)
        if len(hits) == 1:
            hits.append(text)
        text = "\n".join(hits)
    return text


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("fracman://sessions")
def sessions_resource() -> str:
    """Current FracMan sessions as JSON."""
    try:
        session_manager.discover()
    except Exception:
        pass
    return _json({"sessions": session_manager.list()})


@mcp.resource("fracman://runs")
def runs_resource() -> str:
    """Recent batch runs as JSON."""
    return _json({"runs": runs_registry.list(limit=50)})


@mcp.resource("fracman://dfnworks-jobs")
def dfnworks_jobs_resource() -> str:
    """Recent dfnWorks jobs as JSON."""
    return _json({"jobs": dfn_jobs_registry.list(limit=50)})


@mcp.resource("fracman://dfnworks-guide")
def dfnworks_guide_resource() -> str:
    """Operating guide for the dfnWorks channel."""
    return (RESOURCE_DIR / "dfnworks_guide.md").read_text(encoding="utf-8")


@mcp.resource("fracman://macro-reference")
def macro_reference_resource() -> str:
    """Bundled FracMan macro reference."""
    return (RESOURCE_DIR / "macro_reference.md").read_text(encoding="utf-8")


@mcp.resource("fracman://agent-instructions")
def agent_instructions_resource() -> str:
    """Operating rules for an agent controlling FracMan."""
    return (RESOURCE_DIR / "agent_instructions.md").read_text(encoding="utf-8")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
