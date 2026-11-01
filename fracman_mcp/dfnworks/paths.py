"""Path translation between the Windows server and its WSL dfnWorks host."""

from __future__ import annotations

import os
from pathlib import Path


class WslPathError(ValueError):
    pass


def windows_to_wsl(path: str | Path) -> str:
    """Map a local drive path to the ``/mnt/<drive>`` view used inside WSL.

    UNC paths cannot be translated implicitly; point the workspace at a drive
    letter instead (the default ``%LOCALAPPDATA%\\FracManMCP`` already does).
    """
    raw = Path(path).expanduser()
    if not str(raw) or not os.path.isabs(str(raw)):
        raise WslPathError(f"path must be an absolute Windows drive path: {str(raw)!r}")
    try:
        resolved = raw.resolve()
    except OSError:  # e.g. a path that does not exist yet
        resolved = Path(os.path.abspath(str(raw)))
    text = str(resolved)
    if text.startswith("\\\\"):
        raise WslPathError(
            f"UNC path cannot be mapped into WSL automatically: {text}; "
            "use a drive-letter workspace"
        )
    drive, rest = os.path.splitdrive(text)
    if len(drive) != 2 or drive[1] != ":":
        raise WslPathError(f"not an absolute drive path: {text}")
    rest = rest.replace("\\", "/")
    if not rest.startswith("/"):
        rest = "/" + rest
    return f"/mnt/{drive[0].lower()}{rest}"
