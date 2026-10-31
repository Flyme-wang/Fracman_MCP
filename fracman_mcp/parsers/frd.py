"""Heuristic inspector for FracMan binary result files (.frd).

The FRD container is proprietary and undocumented. This parser extracts the
embedded UTF-16 version banner and the ASCII object/class names that FracMan
writes into the container. It does not decode numeric fields.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import file_info

_ASCII_RUN = re.compile(rb"[\x20-\x7e]{3,}")
_CLASS_PREFIXES = (
    "G3d",
    "GSurface",
    "GWell",
    "GProperty",
    "GInterval",
    "GRegion",
    "GScalar",
    "GVector",
    "GFracture",
    "GContainer",
    "GObject",
)


def _decode_utf16_banner(data: bytes, limit: int = 256) -> str:
    chunk = data[:limit]
    for offset in range(0, min(64, len(chunk))):
        candidate = chunk[offset:]
        if len(candidate) < 16:
            break
        try:
            text = candidate.decode("utf-16-le", errors="ignore")
        except LookupError:
            break
        cleaned = "".join(char if char.isprintable() else " " for char in text)
        if "FracMan" not in cleaned:
            continue
        start = cleaned.index("FracMan")
        banner = cleaned[start : start + 80].strip()
        banner = re.sub(r"\s{2,}", " ", banner)
        return banner
    return ""


def parse_frd(path: str | Path, *, max_strings: int = 400, include_hash: bool = False) -> dict[str, Any]:
    p = Path(path)
    data = p.read_bytes()
    banner = _decode_utf16_banner(data)
    raw_strings = [match.decode("latin-1") for match in _ASCII_RUN.findall(data)]
    unique: list[str] = []
    seen: set[str] = set()
    for value in raw_strings:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)

    classes: list[str] = []
    members: list[str] = []
    for value in unique:
        if any(value.startswith(prefix) for prefix in _CLASS_PREFIXES) and len(value) <= 80:
            classes.append(value)
        elif re.fullmatch(r"[A-Za-z_][\w \-().]{0,60}", value):
            members.append(value)

    objects = _guess_objects(unique)

    info = file_info(path, with_hash=include_hash)
    return {
        **info,
        "kind": "frd",
        "format": "FracMan binary result (heuristic)",
        "banner": banner,
        "class_names": classes[:100],
        "string_count": len(unique),
        "strings_preview": unique[:max_strings],
        "object_names": objects,
        "note": "FRD is proprietary; numeric fields are not decoded. Use FracMan macros or the GUI for values.",
    }


def _guess_objects(strings: list[str]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    pending: str | None = None
    for value in strings:
        if value in ("G3dScene", "G3dContainer", "G3dWell", "Scene"):
            if value in ("G3dWell",) and pending is None:
                pending = value
            continue
        if value.startswith("G3dWell"):
            pending = "G3dWell"
            continue
        if pending and re.fullmatch(r"[A-Za-z_][\w \-().]{0,60}", value):
            if pending == "G3dWell" and not value.lower().startswith("well"):
                continue
            if value in ("Index", "Radius", "Length"):
                continue
            objects.append({"type": pending, "name": value})
            pending = None
        if len(objects) >= 40:
            break
    return objects
