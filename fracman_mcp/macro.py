"""Macro validation, wrapping and path helpers."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

from .config import settings


class MacroValidationError(ValueError):
    pass


MAX_MACRO_BYTES = 4 * 1024 * 1024


def read_macro(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        raise MacroValidationError(f"macro file does not exist: {p}")
    if p.stat().st_size > MAX_MACRO_BYTES:
        raise MacroValidationError(f"macro file is larger than {MAX_MACRO_BYTES} bytes: {p}")
    raw = p.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def normalize(code: str) -> str:
    text = code.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.endswith("\n"):
        text += "\n"
    return text


def strip_comments(code: str) -> str:
    cleaned: list[str] = []
    for line in code.splitlines():
        in_quote = False
        cut = len(line)
        for index, char in enumerate(line):
            if char == '"':
                in_quote = not in_quote
            elif char == "#" and not in_quote:
                cut = index
                break
        cleaned.append(line[:cut])
    return "\n".join(cleaned)


def _has_line_command(code: str, command: str) -> bool:
    pattern = re.compile(rf"^\s*{re.escape(command)}\s*$", re.IGNORECASE | re.MULTILINE)
    return bool(pattern.search(strip_comments(code)))


def _has_begin_command(code: str, command: str) -> bool:
    pattern = re.compile(
        rf"^\s*BEGIN\s+{re.escape(command)}\b", re.IGNORECASE | re.MULTILINE
    )
    return bool(pattern.search(strip_comments(code)))


def validate(
    code: str,
    *,
    live: bool,
    allow_exit: bool = False,
    allow_destructive: bool = False,
) -> list[str]:
    """Validate a user macro and return non-fatal warnings."""
    text = normalize(code)
    if not text.strip():
        raise MacroValidationError("macro is empty")
    if len(text.encode("utf-8")) > MAX_MACRO_BYTES:
        raise MacroValidationError("macro is too large")

    bare = strip_comments(text)
    warnings: list[str] = []

    if _has_line_command(text, "LOGTOFILE") or _has_begin_command(text, "LOGTOFILE"):
        raise MacroValidationError(
            "do not include LOGTOFILE in the macro body; the MCP server adds its own log file"
        )
    if live and (
        _has_line_command(text, "EXITONERROR") or _has_begin_command(text, "EXITONERROR")
    ):
        raise MacroValidationError(
            "EXITONERROR is not allowed for live macros because it closes FracMan on error"
        )
    if _has_line_command(text, "EXIT"):
        if live and not allow_exit:
            raise MacroValidationError(
                "EXIT would close the live FracMan session; pass allow_exit=true only if that is intended"
            )
        if not live:
            warnings.append("macro contains EXIT; batch runs are unaffected")
    if live and _has_line_command(text, "STOP"):
        raise MacroValidationError(
            "STOP ends the macro before the completion sentinel and cannot be resumed; "
            "remove it or run the macro in batch mode"
        )
    if re.search(r"\bDeleteAll\b", bare, re.IGNORECASE) and not allow_destructive:
        raise MacroValidationError(
            "DeleteAll would remove all project objects; pass allow_destructive=true to permit it"
        )
    if re.search(r"\bRunMacro\b", bare, re.IGNORECASE):
        warnings.append("macro calls RunMacro; nested macros are allowed but harder to audit")

    if len(re.findall(r"^\s*BEGIN_LOOP\b", bare, re.IGNORECASE | re.MULTILINE)) != len(
        re.findall(r"^\s*END_LOOP\b", bare, re.IGNORECASE | re.MULTILINE)
    ):
        warnings.append("BEGIN_LOOP/END_LOOP counts differ")
    if len(re.findall(r"^\s*BEGIN\b", bare, re.IGNORECASE | re.MULTILINE)) != len(
        re.findall(r"^\s*END\b", bare, re.IGNORECASE | re.MULTILINE)
    ):
        warnings.append("BEGIN/END counts differ")
    return warnings


def to_posix_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve()).replace("\\", "/")


def to_windows_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


LIVE_SENTINEL_NAME = "sentinel.fmf"


def live_sentinel_text() -> str:
    """Body of the trailing sentinel macro; it carries no commands on purpose."""
    return "# fracman-mcp completion sentinel\n"


def wrap_live(
    code: str,
    log_path: str | Path,
    sentinel_path: str | Path,
) -> str:
    """Wrap a live macro and finish with a RunMacro of a no-op sentinel file.

    DrawingOn/DrawingOff are playback-only and are not written to the .fml
    log, so they cannot signal completion.  RunMacro records both the
    ``#Running Macro:`` progress line and the command itself, which gives a
    reliable end marker even for macros that print nothing.
    """
    body = normalize(code)
    return (
        "# Generated by fracman-mcp (live session)\n"
        "BEGIN LOGTOFILE\n"
        f'FILE = "{to_posix_path(log_path)}"\n'
        "END\n\n"
        f"{body}\n"
        "BEGIN RunMacro\n"
        f'Path = "{to_posix_path(sentinel_path)}"\n'
        "END\n"
    )


def wrap_batch(code: str, log_path: str | Path) -> str:
    body = normalize(code)
    if not _has_line_command(body, "EXIT"):
        body += "\nEXIT\n"
    return (
        "# Generated by fracman-mcp (batch)\n"
        "BEGIN LOGTOFILE\n"
        f'FILE = "{to_posix_path(log_path)}"\n'
        "END\n"
        "BEGIN EXITONERROR\n"
        "END\n\n"
        f"{body}"
    )


def list_commands(code: str) -> list[str]:
    commands: list[str] = []
    for line in strip_comments(code).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"BEGIN\s+([A-Za-z_][\w]*)", stripped, re.IGNORECASE)
        if match:
            commands.append(match.group(1))
            continue
        match = re.match(r"BEGIN_LOOP\b", stripped, re.IGNORECASE)
        if match:
            commands.append("BEGIN_LOOP")
            continue
        match = re.match(r"([A-Za-z_][\w]*)\b", stripped)
        if match and match.group(1).upper() in {"EXIT", "STOP"}:
            commands.append(match.group(1).upper())
    return commands


def new_run_dir(kind: str = "live") -> Path:
    settings.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    token = uuid.uuid4().hex[:8]
    path = settings.runs_dir / f"{kind}_{stamp}_{token}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_macro(path: str | Path, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(normalize(text), encoding="utf-8", newline="\n")
    return p


DFN_TEMPLATE = """BEGIN SetSeed
Seed = __SEED__
END
BEGIN SetCurrentUnits
Value = "Length"
Units = "m"
END
BEGIN CREATEREGION
ObjectName = "MCP_Box"
Type = "Box"
Center = 0, 0, 0
Size = __SX__, __SY__, __SZ__
END
BEGIN DefineFractureSetDefinition
DefinitionName = "MCP_Def"
FractureModel = "REGION"
RegionName = "MCP_Box"
ClipToRegion = 1
UseSurfacePoints = 0
Count = __COUNT__
AdjustedByTruncation = 0
UsePole = 1
BEGIN OrientationDistribution
Type = "FISHER"
Parameter "Mean Trend" = __TREND__
Parameter "Mean Plunge" = __PLUNGE__
Parameter "Concentration" = __KAPPA__
END
BEGIN SizeDistribution
ValueType "Length" Units "m"
Type = "UNIFORM"
Parameter "Mean" = __SIZE__
Parameter "Deviation" = __DEV__
END
NumSides = 5
Termination = 0
END
BEGIN GenerateFractureSet
DefinitionName = "MCP_Def"
FractureSetName = "MCP_Set"
END
BEGIN ExportFile
Path = "__EXPORT__"
TypeName = "FAB"
Object = "MCP_Set"
END
"""


def dfn_template(
    *,
    seed: int = 12345,
    count: int = 200,
    size: float = 10.0,
    deviation: float = 3.0,
    trend: float = 15.0,
    plunge: float = 85.0,
    kappa: float = 15.0,
    box: tuple[float, float, float] = (100.0, 100.0, 100.0),
    export_path: str | Path | None = None,
) -> str:
    export = export_path or (settings.exports_dir / "mcp_dfn.fab")
    return (
        DFN_TEMPLATE.replace("__SEED__", str(int(seed)))
        .replace("__COUNT__", str(int(count)))
        .replace("__SX__", str(float(box[0])))
        .replace("__SY__", str(float(box[1])))
        .replace("__SZ__", str(float(box[2])))
        .replace("__TREND__", str(float(trend)))
        .replace("__PLUNGE__", str(float(plunge)))
        .replace("__KAPPA__", str(float(kappa)))
        .replace("__SIZE__", str(float(size)))
        .replace("__DEV__", str(float(deviation)))
        .replace("__EXPORT__", to_posix_path(export))
    )
