"""Parser tests: synthetic fixtures plus optional read-only training samples."""

from __future__ import annotations

from pathlib import Path

import pytest

from fracman_mcp.parsers import SUPPORTED_SUFFIXES, ParseError, inspect_file
from fracman_mcp.parsers.fab import parse_fab, parse_ffb
from fracman_mcp.parsers.f2d import parse_f2d
from fracman_mcp.parsers.sab import parse_sab
from fracman_mcp.parsers.fml import parse_fmf, parse_fml
from fracman_mcp.parsers.frd import parse_frd

TRAINING = Path(r"D:\BaiduNetdiskDownload\Fracman\20200525-FracMan WorkShop")

FAB_TEXT = """BEGIN FORMAT
    Format = Ascii
    No_Fractures = 2
END FORMAT

BEGIN PROPERTIES
    Prop1    =    (Real*4)    "Permeability"
    Prop2    =    (Real*4)    "Aperture"
END PROPERTIES

BEGIN SETS
    Set1    =    "SetA"
END SETS

BEGIN FRACTURE
    1    4    1    10.5    0.25
    1    0    0    0
    2    10    0    0
    3    10    10    0
    4    0    10    0
    0    0    0    1
    2    3    0    3.0    0.5
    1    0    0    0
    2    3    0    0
    3    0    4    0
    0    0    0    1
END FRACTURE
"""

F2D_TEXT = """#Trace Data: Tracemap: TestMap
#x1 y1 z1 x2 y2 z2 len dipdir dip segid traceid
0 0 0 10 0 0 10 0 0 1 1
10 0 0 10 10 0 10 90 0 2 1
"""

SAB_TEXT = """BEGIN Coordinate_System
    xAxis = East
END
BEGIN Well
    Name = "WellA"
END
BEGIN Points
    0 0 0 0.1
    0 0 10 0.2
    0 0 25 0.15
END
"""

FML_ERROR_TEXT = """BEGIN SetSeed
\tSeed = 111
END

#Macro Error: Invalid value/unit specified
#11
#END
#Macro file batch_error2.fmf (line: 12)
#
#
#Invalid value/unit specified
#
#   Macro Stack:
#   batch_error2.fmf (line: 14)
"""

FMF_TEXT = """# comment
BEGIN SetSeed
Seed = 1
END
BEGIN GenerateFractureSet
DefinitionName = "D"
END
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_fab(tmp_path):
    result = parse_fab(_write(tmp_path, "a.fab", FAB_TEXT))
    assert result["kind"] == "fab"
    assert result["fracture_count"] == 2
    assert result["properties"] == ["Permeability", "Aperture"]
    assert result["sets"] == {"Set1": "SetA"}
    assert result["set_counts"] == {"SetA": 1, "Set0": 1}
    assert result["total_area"] == pytest.approx(106.0)
    assert result["area"]["mean"] == pytest.approx(53.0)
    assert result["bbox"]["min"] == [0, 0, 0]
    assert result["bbox"]["max"] == [10, 10, 0]
    assert result["mean_pole"]["trend"] == pytest.approx(0.0)
    assert result["mean_pole"]["plunge"] == pytest.approx(90.0)
    first = result["preview"][0]
    assert first["properties"] == {"Permeability": 10.5, "Aperture": 0.25}
    assert first["area"] == pytest.approx(100.0)
    assert first["trend"] == pytest.approx(0.0)
    assert result["warnings"] == []


def test_parse_fab_header_mismatch_and_truncation(tmp_path):
    broken = FAB_TEXT.replace("No_Fractures = 2", "No_Fractures = 3").replace(
        "    0    0    0    1\nEND FRACTURE", "END FRACTURE"
    )
    result = parse_fab(_write(tmp_path, "b.fab", broken))
    assert result["fracture_count"] == 1
    assert any("No_Fractures=3" in item for item in result["warnings"])
    assert any("truncated fracture 2" in item for item in result["warnings"])


def test_parse_ffb_reuses_fab(tmp_path):
    result = parse_ffb(_write(tmp_path, "a.ffb", FAB_TEXT))
    assert result["kind"] == "ffb"
    assert result["fracture_count"] == 2


def test_parse_f2d(tmp_path):
    result = parse_f2d(_write(tmp_path, "map.f2d", F2D_TEXT))
    assert result["tracemap"] == "TestMap"
    assert result["segment_count"] == 2
    assert result["trace_count"] == 1
    assert result["total_length"] == pytest.approx(20.0)
    assert result["bbox"]["max"] == [10, 10, 0]
    assert result["segments_preview"][1]["seg_id"] == 2
    assert result["traces_preview"][0]["segments"] == 2


def test_parse_sab(tmp_path):
    result = parse_sab(_write(tmp_path, "Golder_1a.sab", SAB_TEXT))
    assert result["borehole_type"] == "well"
    assert result["name"] == "WellA"
    assert result["point_count"] == 3
    assert result["length"] == pytest.approx(25.0)
    assert result["radius"]["mean"] == pytest.approx(0.15)
    assert result["bbox"]["max"] == [0, 0, 25]
    assert [point["md"] for point in result["points_preview"]] == [0, 10, 25]


def test_parse_fml_error_and_clean(tmp_path):
    bad = parse_fml(_write(tmp_path, "bad.fml", FML_ERROR_TEXT))
    assert bad["ok"] is False
    assert bad["error_count"] == 1
    error = bad["errors"][0]
    assert error["message"] == "Invalid value/unit specified"
    assert error["macro_file"] == "batch_error2.fmf"
    assert error["line"] == 12
    assert error["stack"] == [{"macro": "batch_error2.fmf", "line": 14}]
    assert "SetSeed" in bad["executed_blocks"]

    clean = parse_fml(_write(tmp_path, "clean.fml", "BEGIN SetSeed\nSeed = 1\nEND\n"))
    assert clean["ok"] is True
    assert clean["error_count"] == 0


def test_parse_fmf(tmp_path):
    result = parse_fmf(_write(tmp_path, "run.fmf", FMF_TEXT))
    assert result["command_count"] == 2
    assert result["commands"] == ["SetSeed", "GenerateFractureSet"]
    assert "# comment" in result["preview"]


def test_parse_frd_banner_and_objects(tmp_path):
    payload = (
        "FracMan 8.0 test".encode("utf-16-le")
        + b"\x00\x00"
        + b"G3dWell\x00Well_1\x00"
        + b"G3dContainer\x00"
    )
    path = tmp_path / "tiny.frd"
    path.write_bytes(payload)
    result = parse_frd(path)
    assert "FracMan" in result["banner"]
    assert "G3dWell" in result["class_names"]
    assert {"type": "G3dWell", "name": "Well_1"} in result["object_names"]
    assert "numeric fields are not decoded" in result["note"]


def test_inspect_file_dispatch_and_generic(tmp_path):
    fab_path = _write(tmp_path, "a.fab", FAB_TEXT)
    assert inspect_file(fab_path)["fracture_count"] == 2
    assert inspect_file(fab_path, include_hash=True)["sha256"]

    disguised = _write(tmp_path, "a.txt", FAB_TEXT)
    assert inspect_file(disguised, kind="fab")["fracture_count"] == 2

    generic = inspect_file(_write(tmp_path, "notes.txt", "hello"))
    assert generic["kind"] == "txt"
    assert generic["format"] == "text"
    assert generic["preview"] == "hello"

    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02")
    result = inspect_file(binary)
    assert result["format"] == "binary"
    assert "no dedicated parser" in result["note"]

    missing = inspect_file(tmp_path / "nothing.bin")
    assert missing["error"] == "file does not exist"

    with pytest.raises(ParseError):
        inspect_file(tmp_path / "missing.fab")


def test_supported_suffixes():
    assert set(SUPPORTED_SUFFIXES) == {".fab", ".ffb", ".f2d", ".sab", ".fml", ".fmf", ".frd"}


@pytest.mark.skipif(
    not (TRAINING / "Exercise02 - Measuring the Size Distribution/TopGM-2.f2d").is_file(),
    reason="training samples are not available",
)
def test_real_training_files():
    f2d = parse_f2d(TRAINING / "Exercise02 - Measuring the Size Distribution/TopGM-2.f2d")
    assert f2d["segment_count"] == 12
    assert f2d["total_length"] == pytest.approx(16537.854, rel=1e-4)

    sab = parse_sab(
        TRAINING / "Exercise03 - Correlating Fracture Intensity to Seismic Attributes/Golder_1a.sab"
    )
    assert sab["point_count"] == 386
    assert sab["length"] == pytest.approx(8942.8, rel=1e-3)

    ffb = parse_ffb(TRAINING / "Exercise09 - WellTest Simulation/MF_LN70_10.ffb")
    assert ffb["fracture_count"] == 158
    assert len(ffb["properties"]) == 3

    frd = parse_frd(TRAINING / "Exercise09 - WellTest Simulation/Exercise 12e.frd")
    assert "FracMan" in frd["banner"]
    assert frd["size_bytes"] == 88917
