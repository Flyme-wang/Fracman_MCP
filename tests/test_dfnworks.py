"""dfnWorks subsystem tests: specs, WSL paths, comparison and job lifecycle.

No WSL process is started here: the runner is exercised with an injected
command builder that runs a plain Python stand-in for the real driver.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from fracman_mcp.dfnworks import (
    CompareError,
    DfnSpecError,
    build_spec,
    compare,
    load_dfnworks_stats,
    merge_spec,
    windows_to_wsl,
)
from fracman_mcp.dfnworks.paths import WslPathError
from fracman_mcp.dfnworks.runner import DfnBusy, DfnError, DfnJobRunner
from fracman_mcp.requests import RequestConflict, dfn_jobs_registry

FAB_TEXT = """BEGIN FORMAT
    Format = Ascii
    No_Fractures = 2
END FORMAT

BEGIN PROPERTIES
    Prop1    =    (Real*4)    "Permeability"
END PROPERTIES

BEGIN SETS
    Set1    =    "SetA"
END SETS

BEGIN FRACTURE
    1    4    1    10.5
    1    0    0    0
    2    10    0    0
    3    10    10    0
    4    0    10    0
    0    0    0    1
    2    3    0    3.0
    1    0    0    0
    2    3    0    0
    3    0    4    0
    0    0    0    1
END FRACTURE
"""

DFN_STATS = {
    "fracture_count": 2,
    "total_area": 140.0,
    "area": {"min": 40.0, "max": 100.0, "mean": 70.0, "median": 70.0},
    "radius": {"min": 3.0, "max": 5.0, "mean": 4.0, "median": 4.0},
    "p32": 0.14,
    "domain": [10.0, 10.0, 10.0],
    "bbox": {"min": [0.0, 0.0, 0.0], "max": [10.0, 10.0, 0.0]},
    "mean_pole": {"trend": 0.0, "plunge": 90.0},
    "family_counts": {"1": 2},
    "intersections": {"count": 0, "total_length": 0.0},
    "connection_degree": {"mean": 1.0, "max": 1},
}


# -- WSL path mapping -------------------------------------------------------


def test_windows_to_wsl_drive_path():
    assert windows_to_wsl(r"D:\a b\c.py") == "/mnt/d/a b/c.py"
    assert windows_to_wsl(Path("C:/x/y")) == "/mnt/c/x/y"


@pytest.mark.parametrize("bad", [r"\\server\share\x", "relative/path", ""])
def test_windows_to_wsl_rejects_unsupported(bad):
    with pytest.raises(WslPathError):
        windows_to_wsl(bad)


# -- specification ----------------------------------------------------------


def test_build_spec_defaults_are_complete():
    spec = build_spec("generate")
    assert spec["stage"] == "generate"
    assert spec["domain"] == [100.0, 100.0, 100.0]
    # h = 2% of the longest side keeps h/radius at 0.1, the ratio that was
    # verified end to end on 20 m, 100 m and 1 km domains.
    assert spec["h"] == pytest.approx(2.0)
    assert spec["stop_condition"] == 0
    assert spec["boundary_faces"] == [1, 1, 0, 0, 0, 0]
    assert len(spec["families"]) == 3
    assert sum(item["probability"] for item in spec["families"]) == pytest.approx(1.0)
    for family in spec["families"]:
        # A 4-point outline needs radius >= 3h/sqrt(2) to clear dfnWorks' 3h check.
        assert family["constant"] >= 3.0 * spec["h"] / 2**0.5
        assert family["constant"] == pytest.approx(20.0)
        # Hydraulic properties stay unset: if any family ends up empty,
        # dfnWorks exits while assigning transmissivity.
        assert "hy_variable" not in family
    assert len({(f["trend"], f["plunge"]) for f in spec["families"]}) == 3
    assert "mesh" not in spec and "graph" not in spec


def test_build_spec_mesh_defaults_follow_h():
    spec = build_spec("mesh", domain=[100, 100, 100])
    assert spec["mesh"]["min_dist"] == pytest.approx(spec["h"] / 4.0)
    assert spec["mesh"]["max_dist"] == pytest.approx(5.0 * spec["h"])
    explicit = build_spec("mesh", domain=[100, 100, 100], min_dist=1.0, max_dist=8.0)
    assert explicit["mesh"]["min_dist"] == pytest.approx(1.0)
    assert explicit["mesh"]["max_dist"] == pytest.approx(8.0)


def test_build_spec_keeps_requested_hydraulic_properties():
    spec = build_spec(
        "generate",
        families=[
            {
                "probability": 1.0,
                "constant": 20.0,
                "hy_variable": "transmissivity",
                "hy_function": "correlated",
                "hy_params": {"alpha": 1e-13, "beta": 1},
            }
        ],
    )
    assert spec["families"][0]["hy_variable"] == "transmissivity"
    assert spec["families"][0]["hy_params"] == {"alpha": 1e-13, "beta": 1}


def test_build_spec_radius_satisfies_meshing_rule():
    spec = build_spec("generate", domain=[20, 20, 20], h=4.0)
    assert all(
        family["constant"] >= 3.0 * 4.0 / 2**0.5 for family in spec["families"]
    )


def test_build_spec_rejects_too_small_radius():
    with pytest.raises(DfnSpecError, match="3h meshing limit"):
        build_spec(
            "generate",
            domain=[20, 20, 20],
            h=4.0,
            families=[{"shape": "rect", "distribution": "constant", "constant": 4.0}],
        )


def test_build_spec_rejects_bad_domain():
    with pytest.raises(DfnSpecError):
        build_spec("generate", domain=[10, 10])
    with pytest.raises(DfnSpecError):
        build_spec("generate", domain=[10, 0, 10])


def test_build_spec_rejects_unknown_family_key():
    with pytest.raises(DfnSpecError, match="unsupported keys"):
        build_spec(
            "generate",
            families=[{"shape": "rect", "distribution": "constant", "radius": 4}],
        )


def test_build_spec_stop_condition_one_requires_p32():
    with pytest.raises(DfnSpecError, match="p32"):
        build_spec("generate", stop_condition=1)
    spec = build_spec(
        "generate",
        domain=[20, 20, 20],
        h=4.0,
        stop_condition=1,
        families=[
            {"shape": "rect", "distribution": "constant", "p32": 0.2, "constant": 9.0}
        ],
    )
    assert spec["families"][0]["p32"] == pytest.approx(0.2)


def test_build_spec_graph_needs_distinct_boundaries():
    base = build_spec("generate")
    with pytest.raises(DfnSpecError):
        merge_spec(base, "graph", inflow="left", outflow="left")
    graph = merge_spec(base, "graph", graph_type="intersection", k=5)
    assert graph["stage"] == "graph"
    assert graph["graph"]["k"] == 5
    assert graph["mesh"]["uniform_mesh"] is True


def test_build_spec_rejects_unknown_stage():
    with pytest.raises(DfnSpecError):
        build_spec("simulate")


# -- comparison -------------------------------------------------------------


def test_load_stats_from_job_dir(tmp_path):
    (tmp_path / "dfn_stats.json").write_text(json.dumps(DFN_STATS), encoding="utf-8")
    stats = load_dfnworks_stats(tmp_path)
    assert stats["fracture_count"] == 2
    assert stats["source_dir"] == str(tmp_path)


def test_load_stats_from_result_json(tmp_path):
    payload = {"ok": True, "dfn": DFN_STATS}
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    assert load_dfnworks_stats(tmp_path)["fracture_count"] == 2


def test_load_stats_reports_missing(tmp_path):
    with pytest.raises(CompareError):
        load_dfnworks_stats(tmp_path)


def test_compare_fracman_and_dfnworks(tmp_path):
    fab = tmp_path / "net.fab"
    fab.write_text(FAB_TEXT, encoding="utf-8")
    stats = dict(DFN_STATS)
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(json.dumps(stats), encoding="utf-8")
    result = compare(fab, stats_path, domain=[10, 10, 10])
    assert result["ok"] is True
    assert result["fracman"]["fracture_count"] == 2
    assert result["dfnworks"]["fracture_count"] == 2
    assert result["volume_m3"] == pytest.approx(1000.0)
    assert result["deltas"]["count_ratio_dfnworks_over_fracman"] == pytest.approx(1.0)
    assert result["verdict"]


def test_compare_warns_on_large_orientation_mismatch(tmp_path):
    fab = tmp_path / "net.fab"
    fab.write_text(FAB_TEXT, encoding="utf-8")
    stats = dict(DFN_STATS)
    stats["mean_pole"] = {"trend": 0.0, "plunge": 0.0}  # horizontal pole
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(json.dumps(stats), encoding="utf-8")
    result = compare(fab, stats_path, domain=[10, 10, 10])
    assert result["deltas"]["mean_pole_angle_deg"] > 30.0
    assert any("mean poles differ" in item for item in result["warnings"])


# -- runner lifecycle (injected command) ------------------------------------

STAND_IN = """import json, sys, time
from pathlib import Path

job_dir = Path(sys.argv[1])
spec = json.loads((job_dir / "spec.json").read_text(encoding="utf-8"))
stage = spec["stage"]
if spec.get("_sleep"):
    time.sleep(spec["_sleep"])
stats = {
    "fracture_count": 1,
    "total_area": 4.0,
    "area": {"mean": 4.0},
    "radius": {"mean": 1.0},
}
(job_dir / "dfn_stats.json").write_text(json.dumps(stats), encoding="utf-8")
if stage == "mesh":
    (job_dir / "run").mkdir(exist_ok=True)
    (job_dir / "run" / "full_mesh.inp").write_text("nodes 1\\n", encoding="utf-8")
    (job_dir / "mesh_stats.json").write_text("{}", encoding="utf-8")
if stage == "graph":
    (job_dir / "graph_metrics.json").write_text("{}", encoding="utf-8")
    (job_dir / "graph.json").write_text("{}", encoding="utf-8")
result = {"ok": True, "message": "stand-in " + stage, "timing": {"total_s": 0.0}}
(job_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
"""


def _python_exe() -> str:
    """A real interpreter even when pytest runs on a wrapper (e.g. LibreOffice)."""
    candidates = [
        sys.executable,
        shutil.which("python") or "",
        shutil.which("py") or "",
        shutil.which("python3") or "",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    pytest.skip("no usable python interpreter for the stand-in driver")


def _stand_in_builder(tmp_path: Path):
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN, encoding="utf-8")
    python = _python_exe()

    def build(job_dir: Path) -> list[str]:
        return [python, str(script), str(job_dir)]

    return build


def test_runner_completes_and_lists_artifacts(tmp_path):
    runner = DfnJobRunner(command_builder=_stand_in_builder(tmp_path))
    spec = build_spec("generate")
    record = runner.submit("generate", spec, wait_seconds=60)
    assert record["status"] == "completed", record
    assert record["result"]["ok"] is True
    names = {Path(item["path"]).name for item in record["artifacts"]}
    assert "dfn_stats.json" in names


def test_runner_mesh_stage_requires_mesh_inp(tmp_path):
    runner = DfnJobRunner(command_builder=_stand_in_builder(tmp_path))
    spec = merge_spec(build_spec("generate"), "mesh")
    record = runner.submit("mesh", spec, wait_seconds=60)
    assert record["status"] == "completed", record
    names = {Path(item["path"]).name for item in record["artifacts"]}
    assert {"mesh_stats.json", "full_mesh.inp"} <= names


def test_runner_is_idempotent_and_rejects_conflicts(tmp_path):
    runner = DfnJobRunner(command_builder=_stand_in_builder(tmp_path))
    spec = build_spec("generate", seed=7)
    first = runner.submit("generate", spec, job_id="dfn-idem-test", wait_seconds=60)
    again = runner.submit("generate", spec, job_id="dfn-idem-test", wait_seconds=5)
    assert again["status"] == first["status"] == "completed"
    with pytest.raises(RequestConflict):
        runner.submit("generate", build_spec("generate", seed=8),
                      job_id="dfn-idem-test", wait_seconds=5)


def test_runner_reports_failure_when_driver_missing_result(tmp_path):
    script = tmp_path / "broken.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    python = _python_exe()
    runner = DfnJobRunner(
        command_builder=lambda job_dir: [python, str(script)]
    )
    record = runner.submit("generate", build_spec("generate"), wait_seconds=60)
    assert record["status"] == "failed"
    assert record["errors"]


def test_runner_capacity_guard(tmp_path, monkeypatch):
    from fracman_mcp.config import settings

    monkeypatch.setattr(settings, "max_dfn_jobs", 1)
    runner = DfnJobRunner(command_builder=_stand_in_builder(tmp_path))
    spec = dict(build_spec("generate"))
    spec["_sleep"] = 3.0  # spec is only written/read here; the driver ignores extras
    first = runner.submit("generate", spec)
    try:
        with pytest.raises(DfnBusy):
            runner.submit("generate", build_spec("generate", seed=99))
    finally:
        runner.cancel(first["job_id"], wait_seconds=30)


def test_runner_cancel_stops_job(tmp_path):
    runner = DfnJobRunner(command_builder=_stand_in_builder(tmp_path))
    spec = dict(build_spec("generate"))
    spec["_sleep"] = 30.0
    record = runner.submit("generate", spec)
    time.sleep(0.5)
    result = runner.cancel(record["job_id"], wait_seconds=30)
    assert result["status"] in ("cancelled", "failed", "timeout")
    assert result["cancel_requested"] is True


def test_runner_rejects_empty_spec():
    runner = DfnJobRunner()
    with pytest.raises(DfnError):
        runner.submit("generate", None)  # type: ignore[arg-type]
    with pytest.raises(DfnError):
        runner.submit("generate", {}, job_id="  ")
