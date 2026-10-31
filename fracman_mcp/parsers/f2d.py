"""Parser for FracMan trace maps (.f2d)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import bbox_update, file_info, read_text, stats

_NAME_RE = re.compile(r"#\s*Trace Data:\s*Tracemap:\s*(.+)", re.IGNORECASE)


def _column_names(line: str) -> list[str]:
    tokens = line.lstrip("#").split()
    names: list[str] = []
    for token in tokens:
        match = re.match(r"([A-Za-z_][\w]*?)(?:\[.*\])?$", token)
        names.append(match.group(1).lower() if match else token.lower())
    return names


def parse_f2d(
    path: str | Path,
    *,
    preview_limit: int = 20,
    max_segments: int = 2_000_000,
    include_hash: bool = False,
) -> dict[str, Any]:
    text = read_text(path)
    name = ""
    columns: list[str] = []
    lengths: list[float] = []
    segments: list[dict[str, Any]] = []
    trace_lengths: dict[int, float] = {}
    trace_segments: dict[int, int] = {}
    bbox: dict[str, list[float]] | None = None
    warnings: list[str] = []
    total_segments = 0
    truncated = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if not name:
                match = _NAME_RE.match(stripped)
                if match:
                    name = match.group(1).strip()
                    continue
            if not columns and re.search(r"\bx1\b", stripped, re.IGNORECASE):
                columns = _column_names(stripped)
            continue
        if total_segments >= max_segments:
            truncated = True
            break
        tokens = stripped.replace(",", " ").split()
        if len(tokens) < 8:
            continue
        try:
            values = [float(token) for token in tokens]
        except ValueError:
            continue
        if columns and len(columns) == len(values):
            record = dict(zip(columns, values))
            x1, y1, z1 = record.get("x1"), record.get("y1"), record.get("z1")
            x2, y2, z2 = record.get("x2"), record.get("y2"), record.get("z2")
            length = record.get("len")
            seg_id = record.get("segid")
            trace_id = record.get("traceid")
        else:
            x1, y1, z1, x2, y2, z2 = values[:6]
            length = values[6] if len(values) > 6 else None
            seg_id = int(values[8]) if len(values) > 8 else None
            trace_id = int(values[9]) if len(values) > 9 else None
        if None in (x1, y1, z1, x2, y2, z2):
            continue
        if length is None:
            length = ((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2) ** 0.5
        lengths.append(length)
        if trace_id is not None:
            key = int(trace_id)
            trace_lengths[key] = trace_lengths.get(key, 0.0) + length
            trace_segments[key] = trace_segments.get(key, 0) + 1
        bbox = bbox_update(bbox, x1, y1, z1)
        bbox = bbox_update(bbox, x2, y2, z2)
        if len(segments) < preview_limit:
            segments.append(
                {
                    "seg_id": int(seg_id) if seg_id is not None else None,
                    "trace_id": int(trace_id) if trace_id is not None else None,
                    "p1": [x1, y1, z1],
                    "p2": [x2, y2, z2],
                    "length": length,
                }
            )
        total_segments += 1

    if truncated:
        warnings.append(f"stopped after max_segments={max_segments}")
    traces = [
        {"trace_id": trace_id, "segments": trace_segments[trace_id], "length": trace_lengths[trace_id]}
        for trace_id in sorted(trace_lengths)[:max(preview_limit, 20)]
    ]
    info = file_info(path, with_hash=include_hash)
    return {
        **info,
        "kind": "f2d",
        "tracemap": name,
        "columns": columns,
        "segment_count": total_segments,
        "trace_count": len(trace_lengths),
        "total_length": sum(lengths),
        "length": stats(lengths),
        "bbox": bbox,
        "segments_preview": segments,
        "traces_preview": traces,
        "warnings": warnings,
    }
