"""Compare a FracMan fracture file with a dfnWorks network.

FracMan exports ``.fab`` polygons; dfnWorks keeps the same polygons in memory
and the driver writes them to ``fractures.json`` plus a ``dfn_stats.json``.
Both sides are reduced to count / area / radius / bbox / mean-pole statistics
so the comparison stays meaningful even though the file formats differ.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from ..parsers.fab import parse_fab


class CompareError(ValueError):
    pass


def load_dfnworks_stats(source: str | Path) -> dict[str, Any]:
    """Load dfnWorks statistics from a job directory or a stats/result JSON."""
    path = Path(source).expanduser()
    if path.is_dir():
        result_path = path / "result.json"
        stats_path = path / "dfn_stats.json"
        if stats_path.is_file():
            data = _read_json(stats_path)
        elif result_path.is_file():
            result = _read_json(result_path)
            data = result.get("dfn") or {}
            if not data:
                raise CompareError(
                    f"dfnWorks job {path} has no dfn statistics: "
                    f"{result.get('error', 'unknown error')}"
                )
        else:
            raise CompareError(f"no result.json or dfn_stats.json in {path}")
        if not isinstance(data, dict):
            raise CompareError(f"invalid dfnWorks statistics in {path}")
        data = dict(data)
        data.setdefault("source_dir", str(path))
        return data
    if not path.is_file():
        raise CompareError(f"dfnWorks statistics not found: {path}")
    data = _read_json(path)
    if not isinstance(data, dict):
        raise CompareError(f"dfnWorks statistics must be a JSON object: {path}")
    if "dfn" in data and isinstance(data["dfn"], dict):
        return data["dfn"]
    return data


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise CompareError(f"cannot read {path}: {exc}") from exc


def _ratio(numerator: Any, denominator: Any) -> float | None:
    try:
        denominator = float(denominator)
        if denominator == 0:
            return None
        return float(numerator) / denominator
    except (TypeError, ValueError):
        return None


def _bbox_volume(box: Any) -> float | None:
    if not isinstance(box, dict):
        return None
    low, high = box.get("min"), box.get("max")
    if not (isinstance(low, list) and isinstance(high, list)):
        return None
    if len(low) != 3 or len(high) != 3:
        return None
    try:
        dims = [float(high[i]) - float(low[i]) for i in range(3)]
    except (TypeError, ValueError):
        return None
    if min(dims) < 0:
        return None
    return dims[0] * dims[1] * dims[2]


def _bbox_overlap(first: Any, second: Any) -> dict[str, Any] | None:
    if not isinstance(first, dict) or not isinstance(second, dict):
        return None
    try:
        inter = 1.0
        for axis in range(3):
            low = max(float(first["min"][axis]), float(second["min"][axis]))
            high = min(float(first["max"][axis]), float(second["max"][axis]))
            inter *= max(0.0, high - low)
        vol_a = _bbox_volume(first)
        vol_b = _bbox_volume(second)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if vol_a is None or vol_b is None:
        return None
    union = vol_a + vol_b - inter
    return {
        "intersection_volume": inter,
        "union_volume": union,
        "iou": (inter / union) if union > 0 else None,
    }


def _pole_vector(pole: Any) -> tuple[float, float, float] | None:
    if not isinstance(pole, dict):
        return None
    try:
        trend = math.radians(float(pole["trend"]))
        plunge = math.radians(float(pole["plunge"]))
    except (KeyError, TypeError, ValueError):
        return None
    return (
        math.cos(plunge) * math.sin(trend),
        math.cos(plunge) * math.cos(trend),
        math.sin(plunge),
    )


def _pole_angle(first: Any, second: Any) -> float | None:
    a = _pole_vector(first)
    b = _pole_vector(second)
    if a is None or b is None:
        return None
    dot = sum(x * y for x, y in zip(a, b))
    return math.degrees(math.acos(max(-1.0, min(1.0, abs(dot)))))


def compare(
    fracman_fab: str | Path,
    dfnworks: str | Path,
    *,
    domain: list[float] | None = None,
    preview_limit: int = 0,
) -> dict[str, Any]:
    """Compare FracMan ``.fab``/``.ffb`` statistics with dfnWorks output."""
    fracman = parse_fab(fracman_fab, preview_limit=preview_limit)
    other = load_dfnworks_stats(dfnworks)

    fm_count = int(fracman.get("fracture_count") or 0)
    dw_count = int(other.get("fracture_count") or 0)
    fm_total_area = float(fracman.get("total_area") or 0.0)
    dw_total_area = float(other.get("total_area") or 0.0)
    fm_area = fracman.get("area") or {}
    dw_area = other.get("area") or {}
    fm_radius = fracman.get("radius") or {}
    dw_radius = other.get("radius") or {}

    volume = None
    if domain:
        try:
            values = [float(item) for item in domain]
            if len(values) == 3 and min(values) > 0:
                volume = values[0] * values[1] * values[2]
        except (TypeError, ValueError):
            volume = None
    if volume is None:
        try:
            values = [float(item) for item in (other.get("domain") or [])]
            if len(values) == 3 and min(values) > 0:
                volume = values[0] * values[1] * values[2]
        except (TypeError, ValueError):
            volume = None

    fm_p32 = (fm_total_area / volume) if volume else None
    dw_p32 = other.get("p32")
    bbox = _bbox_overlap(fracman.get("bbox"), other.get("bbox"))
    pole_angle = _pole_angle(fracman.get("mean_pole"), other.get("mean_pole"))

    warnings: list[str] = []
    if fm_count == 0:
        warnings.append("FracMan file contains no fractures")
    if dw_count == 0:
        warnings.append("dfnWorks network contains no fractures")
    count_ratio = _ratio(dw_count, fm_count)
    if count_ratio is not None and not 0.5 <= count_ratio <= 2.0:
        warnings.append(
            f"fracture counts differ by more than 2x (ratio {count_ratio:.2f})"
        )
    area_ratio = _ratio(dw_total_area, fm_total_area)
    if area_ratio is not None and not 0.5 <= area_ratio <= 2.0:
        warnings.append(
            f"total fracture area differs by more than 2x (ratio {area_ratio:.2f})"
        )
    if bbox is not None and bbox.get("iou") is not None and bbox["iou"] < 0.5:
        warnings.append(
            f"fracture bounding boxes overlap only {bbox['iou']:.2f} (IoU)"
        )
    if pole_angle is not None and pole_angle > 30.0:
        warnings.append(
            f"mean poles differ by {pole_angle:.1f} deg; check orientation "
            "conventions (trend/plunge vs strike/dip)"
        )
    warnings.extend(fracman.get("warnings") or [])

    per_set = [
        {
            "name": str(name),
            "fracman_count": int(count),
            "dfnworks_count": None,
        }
        for name, count in (fracman.get("set_counts") or {}).items()
    ]
    per_family = [
        {
            "family": str(name),
            "dfnworks_count": int(count),
            "fracman_count": None,
        }
        for name, count in (other.get("family_counts") or {}).items()
    ]

    return {
        "ok": True,
        "fracman": {
            "path": str(fracman.get("path")),
            "fracture_count": fm_count,
            "set_counts": fracman.get("set_counts"),
            "total_area": fm_total_area,
            "area": fm_area,
            "radius": fm_radius,
            "bbox": fracman.get("bbox"),
            "mean_pole": fracman.get("mean_pole"),
            "p32": fm_p32,
        },
        "dfnworks": {
            "source": str(dfnworks),
            "fracture_count": dw_count,
            "family_counts": other.get("family_counts"),
            "total_area": dw_total_area,
            "area": dw_area,
            "radius": dw_radius,
            "bbox": other.get("bbox"),
            "mean_pole": other.get("mean_pole"),
            "p32": dw_p32,
            "intersections": other.get("intersections"),
            "connection_degree": other.get("connection_degree"),
        },
        "deltas": {
            "count_ratio_dfnworks_over_fracman": count_ratio,
            "count_difference": dw_count - fm_count,
            "total_area_ratio": area_ratio,
            "total_area_difference": dw_total_area - fm_total_area,
            "mean_area_ratio": _ratio(dw_area.get("mean"), fm_area.get("mean")),
            "mean_radius_ratio": _ratio(
                dw_radius.get("mean"), fm_radius.get("mean")
            ),
            "p32_dfnworks_minus_fracman": (
                (float(dw_p32) - fm_p32)
                if dw_p32 is not None and fm_p32 is not None
                else None
            ),
            "mean_pole_angle_deg": pole_angle,
            "bbox_overlap": bbox,
        },
        "volume_m3": volume,
        "per_set_fracman": per_set,
        "per_family_dfnworks": per_family,
        "matching_note": (
            "FracMan sets and dfnWorks families are listed side by side; "
            "names/ids are not assumed to correspond"
        ),
        "warnings": warnings[:50],
        "verdict": _verdict(count_ratio, area_ratio, bbox, pole_angle),
    }


def _verdict(
    count_ratio: float | None,
    area_ratio: float | None,
    bbox: dict[str, Any] | None,
    pole_angle: float | None,
) -> str:
    if count_ratio is None:
        return "insufficient data: FracMan count is zero"
    parts = []
    if 0.8 <= count_ratio <= 1.25:
        parts.append("comparable fracture counts")
    elif 0.5 <= count_ratio <= 2.0:
        parts.append("same order of magnitude in count")
    else:
        parts.append("counts differ substantially")
    if area_ratio is not None:
        if 0.8 <= area_ratio <= 1.25:
            parts.append("comparable total area")
        elif 0.5 <= area_ratio <= 2.0:
            parts.append("total areas within 2x")
        else:
            parts.append("total areas diverge")
    if bbox is not None and bbox.get("iou") is not None:
        parts.append(f"bbox IoU {bbox['iou']:.2f}")
    if pole_angle is not None:
        parts.append(f"mean-pole angle {pole_angle:.1f} deg")
    return "; ".join(parts)
