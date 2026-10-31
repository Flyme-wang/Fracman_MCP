"""FracMan file intelligence entry point."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

from .common import ParseError, file_info, read_text, sha256_file
from .f2d import parse_f2d
from .fab import parse_fab, parse_ffb
from .fml import parse_fmf, parse_fml
from .frd import parse_frd
from .sab import parse_sab

PARSERS: dict[str, Callable[..., dict[str, Any]]] = {
    ".fab": parse_fab,
    ".ffb": parse_ffb,
    ".f2d": parse_f2d,
    ".sab": parse_sab,
    ".fml": parse_fml,
    ".fmf": parse_fmf,
    ".frd": parse_frd,
}

SUPPORTED_SUFFIXES = sorted(PARSERS)


def inspect_file(
    path: str | Path,
    *,
    kind: str | None = None,
    preview_limit: int = 20,
    include_hash: bool = False,
) -> dict[str, Any]:
    p = Path(path).expanduser()
    suffix = (kind or p.suffix).lower()
    if not suffix.startswith("."):
        suffix = "." + suffix
    parser = PARSERS.get(suffix)
    if parser is not None:
        try:
            return _call(parser, p, preview_limit=preview_limit, include_hash=include_hash)
        except ParseError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            fallback = _generic(p, include_hash=include_hash)
            fallback["parse_error"] = f"{type(exc).__name__}: {exc}"
            return fallback
    return _generic(p, include_hash=include_hash)


def _call(
    parser: Callable[..., dict[str, Any]],
    path: Path,
    *,
    preview_limit: int,
    include_hash: bool,
) -> dict[str, Any]:
    parameters = inspect.signature(parser).parameters
    kwargs: dict[str, Any] = {}
    if "preview_limit" in parameters:
        kwargs["preview_limit"] = preview_limit
    if "include_hash" in parameters:
        kwargs["include_hash"] = include_hash
    return parser(path, **kwargs)


def _generic(path: Path, *, include_hash: bool = False) -> dict[str, Any]:
    info = file_info(path, with_hash=include_hash)
    result: dict[str, Any] = {**info, "kind": info["suffix"].lstrip(".") or "unknown"}
    if not path.is_file():
        result["error"] = "file does not exist"
        return result
    data = path.read_bytes()[:8192]
    if b"\x00" in data:
        result["format"] = "binary"
        result["note"] = "no dedicated parser for this suffix; only metadata is reported"
        return result
    result["format"] = "text"
    try:
        result["preview"] = read_text(path)[:4000]
    except ParseError as exc:
        result["error"] = str(exc)
    return result


__all__ = [
    "PARSERS",
    "SUPPORTED_SUFFIXES",
    "ParseError",
    "inspect_file",
    "parse_f2d",
    "parse_fab",
    "parse_ffb",
    "parse_fmf",
    "parse_fml",
    "parse_frd",
    "parse_sab",
    "sha256_file",
]
