"""Shared helpers for the FracMan file parsers."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any, Iterable

MAX_TEXT_BYTES = 512 * 1024 * 1024


class ParseError(ValueError):
    pass


def read_text(path: str | Path, max_bytes: int = MAX_TEXT_BYTES) -> str:
    p = Path(path)
    if not p.is_file():
        raise ParseError(f"file does not exist: {p}")
    size = p.stat().st_size
    if size > max_bytes:
        raise ParseError(f"file is too large to parse as text ({size} bytes): {p}")
    raw = p.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def numeric_tokens(line: str) -> list[float]:
    values: list[float] = []
    for token in line.replace(",", " ").split():
        try:
            values.append(float(token))
        except ValueError:
            continue
    return values


def finite(values: Iterable[float]) -> list[float]:
    return [value for value in values if math.isfinite(value)]


def stats(values: Iterable[float]) -> dict[str, float | None]:
    data = sorted(finite(values))
    if not data:
        return {"min": None, "max": None, "mean": None, "median": None}
    count = len(data)
    mean = sum(data) / count
    if count % 2:
        median = data[count // 2]
    else:
        median = 0.5 * (data[count // 2 - 1] + data[count // 2])
    return {"min": data[0], "max": data[-1], "mean": mean, "median": median}


def bbox_update(box: dict[str, list[float]] | None, x: float, y: float, z: float) -> dict[str, list[float]]:
    if box is None:
        return {"min": [x, y, z], "max": [x, y, z]}
    box["min"][0] = min(box["min"][0], x)
    box["min"][1] = min(box["min"][1], y)
    box["min"][2] = min(box["min"][2], z)
    box["max"][0] = max(box["max"][0], x)
    box["max"][1] = max(box["max"][1], y)
    box["max"][2] = max(box["max"][2], z)
    return box


def polygon_area(vertices: list[tuple[float, float, float]]) -> float:
    """Area of a planar-ish 3D polygon via Newell's method."""
    if len(vertices) < 3:
        return 0.0
    sx = sy = sz = 0.0
    for index, (x1, y1, z1) in enumerate(vertices):
        x2, y2, z2 = vertices[(index + 1) % len(vertices)]
        sx += y1 * z2 - z1 * y2
        sy += z1 * x2 - x1 * z2
        sz += x1 * y2 - y1 * x2
    return 0.5 * math.sqrt(sx * sx + sy * sy + sz * sz)


def normalize_vector(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = vector
    norm = math.sqrt(x * x + y * y + z * z)
    if norm <= 1e-15:
        return (0.0, 0.0, 0.0)
    return (x / norm, y / norm, z / norm)


def trend_plunge(normal: tuple[float, float, float]) -> tuple[float, float]:
    """Return pole trend (deg from +Y/North) and plunge (deg, upward positive)."""
    x, y, z = normalize_vector(normal)
    if z < 0:
        x, y, z = -x, -y, -z
    trend = math.degrees(math.atan2(x, y)) % 360.0
    plunge = math.degrees(math.asin(max(-1.0, min(1.0, z))))
    return trend, plunge


_KEY_VALUE = re.compile(r"^\s*([A-Za-z_][\w]*)\s*=\s*(.*?)\s*$")


def parse_key_value(line: str) -> tuple[str, str] | None:
    match = _KEY_VALUE.match(line)
    if not match:
        return None
    raw = match.group(2).strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    return match.group(1), raw


def file_info(path: str | Path, with_hash: bool = False) -> dict[str, Any]:
    p = Path(path)
    info: dict[str, Any] = {
        "path": str(p),
        "name": p.name,
        "suffix": p.suffix.lower(),
        "exists": p.is_file(),
    }
    if p.is_file():
        stat = p.stat()
        info.update({"size_bytes": stat.st_size, "modified": stat.st_mtime})
        if with_hash:
            info["sha256"] = sha256_file(p)
    return info
