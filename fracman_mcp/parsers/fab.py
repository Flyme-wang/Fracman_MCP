"""Parser for FracMan fracture files (.fab / .ffb)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import (
    ParseError,
    bbox_update,
    file_info,
    numeric_tokens,
    parse_key_value,
    polygon_area,
    read_text,
    stats,
    trend_plunge,
)


def _extract_prop_name(raw: str) -> str:
    """``Prop1 = (Real*4) "Permeability"`` -> ``Permeability``."""
    if '"' in raw:
        first = raw.index('"')
        second = raw.find('"', first + 1)
        if second > first:
            return raw[first + 1 : second]
    parts = raw.split()
    return parts[-1] if parts else raw


def parse_fab(
    path: str | Path,
    *,
    preview_limit: int = 20,
    max_fractures: int = 2_000_000,
    include_hash: bool = False,
) -> dict[str, Any]:
    text = read_text(path)
    lines = text.splitlines()

    format_info: dict[str, str] = {}
    properties: list[str] = []
    sets: dict[str, str] = {}
    warnings: list[str] = []

    count = 0
    set_counts: dict[str, int] = {}
    bbox: dict[str, list[float]] | None = None
    areas: list[float] = []
    radii: list[float] = []
    normals: list[tuple[float, float, float]] = []
    preview: list[dict[str, Any]] = []

    section = ""
    state = ""
    pending: dict[str, Any] | None = None
    vertex_count = 0
    vertices: list[tuple[float, float, float]] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        upper = stripped.upper()

        if upper.startswith("BEGIN"):
            parts = stripped.split(None, 1)
            section = parts[1].strip().lower() if len(parts) > 1 else ""
            if section == "fracture":
                state = "header"
                pending = None
            continue
        if upper.startswith("END"):
            section = ""
            state = ""
            continue

        if section == "format":
            pair = parse_key_value(stripped)
            if pair:
                format_info[pair[0]] = pair[1]
            continue
        if section == "properties":
            pair = parse_key_value(stripped)
            if pair:
                properties.append(_extract_prop_name(pair[1]))
            continue
        if section == "sets":
            pair = parse_key_value(stripped)
            if pair:
                sets[pair[0]] = pair[1]
            continue
        if section != "fracture":
            continue

        if state == "header":
            tokens = stripped.split()
            if len(tokens) < 3:
                warnings.append(f"unexpected fracture header: {stripped[:80]!r}")
                continue
            try:
                frac_id = int(float(tokens[0]))
                nverts = int(float(tokens[1]))
                set_id = int(float(tokens[2]))
            except ValueError:
                warnings.append(f"unparseable fracture header: {stripped[:80]!r}")
                continue
            values = [float(item) for item in tokens[3:] if _is_float(item)]
            if len(values) < len(properties):
                values.extend([float("nan")] * (len(properties) - len(values)))
            props = {name: value for name, value in zip(properties, values)}
            set_name = sets.get(f"Set{set_id}", f"Set{set_id}")
            pending = {
                "id": frac_id,
                "set_id": set_id,
                "set": set_name,
                "nverts": nverts,
                "properties": props,
            }
            vertex_count = 0
            vertices = []
            state = "verts" if nverts > 0 else "normal"
            continue

        if state == "verts":
            tokens = stripped.split()
            data = tokens[1:4] if len(tokens) >= 4 else tokens[:3]
            values = [float(item) for item in data if _is_float(item)]
            if len(values) < 3:
                warnings.append(f"bad vertex line on fracture {pending['id'] if pending else '?'}")
            else:
                vertices.append((values[0], values[1], values[2]))
            vertex_count += 1
            if vertex_count >= pending["nverts"]:
                state = "normal"
            continue

        if state == "normal":
            values = numeric_tokens(stripped)
            normal = (values[1], values[2], values[3]) if len(values) >= 4 else (float("nan"),) * 3
            if pending is None:
                state = "header"
                continue
            area = polygon_area(vertices)
            cx = sum(v[0] for v in vertices) / len(vertices) if vertices else 0.0
            cy = sum(v[1] for v in vertices) / len(vertices) if vertices else 0.0
            cz = sum(v[2] for v in vertices) / len(vertices) if vertices else 0.0
            radius = max((_distance(v, (cx, cy, cz)) for v in vertices), default=0.0)
            areas.append(area)
            radii.append(radius)
            valid_normal = (
                all(_is_float(value) for value in normal)
                and any(abs(value) > 0 for value in normal)
            )
            if valid_normal:
                normals.append(normal)
            for vertex in vertices:
                bbox = bbox_update(bbox, *vertex)
            count += 1
            set_counts[pending["set"]] = set_counts.get(pending["set"], 0) + 1
            if len(preview) < preview_limit:
                trend, plunge = trend_plunge(normal) if valid_normal else (None, None)
                preview.append(
                    {
                        "id": pending["id"],
                        "set": pending["set"],
                        "nverts": pending["nverts"],
                        "properties": pending["properties"],
                        "area": area,
                        "radius": radius,
                        "center": [cx, cy, cz],
                        "normal": list(normal),
                        "trend": trend,
                        "plunge": plunge,
                    }
                )
            pending = None
            vertices = []
            state = "header"
            if count >= max_fractures:
                warnings.append(f"stopped after max_fractures={max_fractures}")
                break
            continue

    if pending is not None:
        warnings.append(f"truncated fracture {pending.get('id')} (missing normal line)")
    declared = _int_or_none(format_info.get("No_Fractures"))
    if declared is not None and declared != count:
        warnings.append(f"header declares No_Fractures={declared} but parsed {count}")

    trend_plunge_mean = None
    if normals:
        tx = ty = tz = 0.0
        for nx, ny, nz in normals:
            if nz < 0:
                nx, ny, nz = -nx, -ny, -nz
            length = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
            tx += nx / length
            ty += ny / length
            tz += nz / length
        trend_plunge_mean = dict(zip(("trend", "plunge"), trend_plunge((tx, ty, tz))))

    info = file_info(path, with_hash=include_hash)
    return {
        **info,
        "kind": info["suffix"].lstrip("."),
        "format": format_info,
        "properties": properties,
        "sets": sets,
        "fracture_count": count,
        "set_counts": set_counts,
        "bbox": bbox,
        "area": stats(areas),
        "total_area": sum(areas),
        "radius": stats(radii),
        "mean_pole": trend_plunge_mean,
        "preview": preview,
        "preview_limit": preview_limit,
        "warnings": warnings[:50],
    }


def _is_float(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_ffb(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    result = parse_fab(path, **kwargs)
    result["kind"] = "ffb"
    return result
