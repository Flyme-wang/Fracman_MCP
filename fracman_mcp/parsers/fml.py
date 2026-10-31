"""Parser for FracMan macro logs (.fml) and macro files (.fmf)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import file_info, read_text

_ERROR_RE = re.compile(r"^#\s*Macro Error:\s*(.*)$", re.IGNORECASE)
_STACK_RE = re.compile(r"#\s*(.+?)\s*\(line:\s*(\d+)\)", re.IGNORECASE)
_MACRO_FILE_RE = re.compile(r"#\s*Macro file\s+(.+?)\s*\(line:\s*(\d+)\)", re.IGNORECASE)
_BEGIN_RE = re.compile(r"^\s*BEGIN\s+([A-Za-z_][\w]*)", re.IGNORECASE)


def parse_fml(path: str | Path, *, tail_chars: int = 4000, include_hash: bool = False) -> dict[str, Any]:
    text = read_text(path)
    lines = text.splitlines()
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    executed: list[str] = []

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.upper().startswith("BEGIN"):
            match = _BEGIN_RE.match(stripped)
            if match:
                executed.append(match.group(1))
            index += 1
            continue
        match = _ERROR_RE.match(stripped)
        if match:
            message = match.group(1).strip()
            block: list[str] = []
            cursor = index + 1
            while cursor < len(lines):
                candidate = lines[cursor].strip()
                if not candidate:
                    cursor += 1
                    if cursor < len(lines) and not lines[cursor].strip().startswith("#"):
                        break
                    continue
                if not candidate.startswith("#"):
                    break
                block.append(candidate.lstrip("#").strip())
                cursor += 1
            macro_file = None
            line_number = None
            stack: list[dict[str, Any]] = []
            for item in block:
                file_match = _MACRO_FILE_RE.match("# " + item)
                if file_match:
                    macro_file = file_match.group(1)
                    line_number = int(file_match.group(2))
                    continue
                stack_match = _STACK_RE.match("# " + item)
                if stack_match:
                    stack.append(
                        {"macro": stack_match.group(1), "line": int(stack_match.group(2))}
                    )
                    if line_number is None:
                        line_number = int(stack_match.group(2))
                        macro_file = stack_match.group(1)
            errors.append(
                {
                    "message": message,
                    "macro_file": macro_file,
                    "line": line_number,
                    "stack": stack,
                    "context": block[:12],
                }
            )
            index = cursor
            continue
        index += 1

    if not errors and "error" in text.lower() and "macro error" not in text.lower():
        for line in lines:
            if re.search(r"\berror\b", line, re.IGNORECASE):
                warnings.append(line.strip()[:200])
                if len(warnings) >= 5:
                    break

    info = file_info(path, with_hash=include_hash)
    return {
        **info,
        "kind": "fml",
        "ok": not errors,
        "error_count": len(errors),
        "errors": errors,
        "executed_blocks": executed[:500],
        "block_count": len(executed),
        "log_chars": len(text),
        "log_tail": text[-tail_chars:] if tail_chars > 0 else "",
        "warnings": warnings,
    }


def parse_fmf(path: str | Path, *, include_hash: bool = False) -> dict[str, Any]:
    text = read_text(path)
    commands: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _BEGIN_RE.match(stripped)
        if match:
            commands.append(match.group(1))
    info = file_info(path, with_hash=include_hash)
    return {
        **info,
        "kind": "fmf",
        "line_count": len(text.splitlines()),
        "commands": commands,
        "command_count": len(commands),
        "preview": text[:4000],
    }
