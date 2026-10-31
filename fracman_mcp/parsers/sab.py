"""Parser for FracMan borehole files (.sab)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import bbox_update, file_info, parse_key_value, read_text, stats


def parse_sab(
    path: str | Path,
    *,
    preview_limit: int = 20,
    include_hash: bool = False,
) -> dict[str, Any]:
    text = read_text(path)
    section = ""
    kind = ""
    name = ""
    xaxis = ""
    points: list[dict[str, float]] = []
    warnings: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        upper = stripped.upper()
        if upper.startswith("BEGIN"):
            parts = stripped.split(None, 1)
            section = parts[1].strip().lower() if len(parts) > 1 else ""
            if section in ("well", "curved_borehole", "borehole"):
                kind = section
            continue
        if upper.startswith("END"):
            section = ""
            continue
        if section == "coordinate_system":
            pair = parse_key_value(stripped)
            if pair and pair[0].lower() == "xaxis":
                xaxis = pair[1]
            continue
        if section in ("well", "curved_borehole", "borehole", "points"):
            pair = parse_key_value(stripped)
            if pair and pair[0].lower() == "name":
                name = pair[1]
                continue
        if section == "points":
            tokens = stripped.replace(",", " ").split()
            if len(tokens) < 3:
                warnings.append(f"skipped point line: {stripped[:60]!r}")
                continue
            try:
                x, y, z = (float(token) for token in tokens[:3])
                radius = float(tokens[3]) if len(tokens) > 3 else 0.0
            except ValueError:
                warnings.append(f"unparseable point line: {stripped[:60]!r}")
                continue
            points.append({"x": x, "y": y, "z": z, "radius": radius})
            continue

    if not points and section == "":
        # Fallback: bare numeric lines (no section headers).
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            tokens = stripped.replace(",", " ").split()
            if len(tokens) < 3:
                continue
            try:
                x, y, z = (float(token) for token in tokens[:3])
            except ValueError:
                continue
            radius = float(tokens[3]) if len(tokens) > 3 and _is_float(tokens[3]) else 0.0
            points.append({"x": x, "y": y, "z": z, "radius": radius})

    lengths: list[float] = []
    measured_depth: list[float] = [0.0]
    bbox: dict[str, list[float]] | None = None
    for index, point in enumerate(points):
        bbox = bbox_update(bbox, point["x"], point["y"], point["z"])
        if index:
            prev = points[index - 1]
            step = (
                (point["x"] - prev["x"]) ** 2
                + (point["y"] - prev["y"]) ** 2
                + (point["z"] - prev["z"]) ** 2
            ) ** 0.5
            lengths.append(step)
            measured_depth.append(measured_depth[-1] + step)

    for index, point in enumerate(points):
        point["md"] = measured_depth[index]

    info = file_info(path, with_hash=include_hash)
    return {
        **info,
        "kind": "sab",
        "borehole_type": kind or "unknown",
        "name": name or Path(path).stem,
        "xaxis": xaxis,
        "point_count": len(points),
        "length": sum(lengths),
        "radius": stats(point["radius"] for point in points),
        "bbox": bbox,
        "points_preview": points[:preview_limit],
        "points_tail": points[-min(preview_limit, len(points)) :] if points else [],
        "warnings": warnings[:50],
    }


def _is_float(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False
