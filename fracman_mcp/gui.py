"""Windows GUI automation for FracMan 8.0.

Only standard UIA/Win32 mechanisms are used.  There is no in-process bridge;
every operation is a sequence of observable GUI actions plus file parsing.
"""

from __future__ import annotations

import base64
import ctypes
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil
import win32con
import win32gui
import win32process
import win32ui
from PIL import Image
from pywinauto import Application, Desktop
from pywinauto.keyboard import send_keys

USER32 = ctypes.windll.user32
USER32.SetProcessDPIAware()


class GuiError(RuntimeError):
    pass


class GuiBusyError(GuiError):
    pass


_WINDOW_CACHE: dict[tuple[int, int], Any] = {}


def normalize_label(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").replace("...", "…")


def connect(pid: int, hwnd: int | None = None) -> Any:
    key = (int(pid), int(hwnd or 0))
    cached = _WINDOW_CACHE.get(key)
    if cached is not None:
        try:
            cached.window_text()
            return cached
        except Exception:
            _WINDOW_CACHE.pop(key, None)
    app = Application(backend="uia").connect(process=pid)
    if hwnd:
        window = app.window(handle=hwnd)
    else:
        candidates = app.windows()
        if not candidates:
            raise GuiError(f"FracMan process {pid} has no top-level window")
        window = candidates[0]
    _WINDOW_CACHE[key] = window
    return window


def forget(pid: int, hwnd: int | None = None) -> None:
    for key in list(_WINDOW_CACHE):
        if key[0] == int(pid) and (hwnd is None or key[1] == int(hwnd)):
            _WINDOW_CACHE.pop(key, None)


def find_main_windows() -> list[dict[str, Any]]:
    """Enumerate visible FracMan main windows without UIA."""
    found: list[dict[str, Any]] = []

    def callback(hwnd: int, _lparam: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if "FracMan" not in title and "弗拉克曼" not in title:
            return True
        class_name = win32gui.GetClassName(hwnd)
        if not class_name.startswith("Afx:"):
            return True
        try:
            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = psutil.Process(pid).name().lower()
        except Exception:
            return True
        if name != "fracman.exe":
            return True
        found.append({"hwnd": hwnd, "pid": pid, "title": title, "class_name": class_name})
        return True

    win32gui.EnumWindows(callback, None)
    return found


def find_window_for_pid(pid: int) -> dict[str, Any] | None:
    for item in find_main_windows():
        if item["pid"] == pid:
            return item
    return None


def _top_level_windows_for_pid(pid: int) -> list[dict[str, Any]]:
    """Visible top-level windows (any class) owned by a process."""
    found: list[dict[str, Any]] = []

    def callback(hwnd: int, _lparam: Any) -> bool:
        try:
            _thread_id, owner = win32process.GetWindowThreadProcessId(hwnd)
            if owner != int(pid) or not win32gui.IsWindowVisible(hwnd):
                return True
            found.append(
                {
                    "hwnd": hwnd,
                    "class_name": win32gui.GetClassName(hwnd),
                    "title": win32gui.GetWindowText(hwnd),
                }
            )
        except Exception:
            pass
        return True

    win32gui.EnumWindows(callback, None)
    return found


def dismiss_beta_notice(pid: int) -> list[dict[str, Any]]:
    """Dismiss FracMan's informational beta-build notice for a launched process.

    Only this specific notice is touched; license and save dialogs are left
    for the caller to report.
    """
    dismissed: list[dict[str, Any]] = []
    for item in _top_level_windows_for_pid(pid):
        if item["class_name"] != "#32770":
            continue
        try:
            dialog = Desktop(backend="uia").window(handle=item["hwnd"])
            payload = _dialog_payload(dialog)
        except Exception:
            continue
        text = ((payload.get("text") or "") + " " + (payload.get("title") or "")).lower()
        if "beta test build" not in text:
            continue
        button = _find_button_in_dialog(dialog, ("确定", "OK", "Continue"))
        if button is None:
            continue
        if invoke(button):
            dismissed.append(payload)
            time.sleep(0.5)
    return dismissed


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    return win32gui.GetWindowRect(hwnd)


def rect_list(rect: Any) -> list[int]:
    """Convert a pywinauto RECT (not iterable) to [left, top, right, bottom]."""
    return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]


def _child(w: Any, auto_id: str) -> Any | None:
    try:
        return w.child_window(auto_id=auto_id)
    except Exception:
        return None


def read_status(w: Any) -> str:
    ctrl = _child(w, "59393")
    if ctrl is None:
        return ""
    try:
        return (ctrl.window_text() or "").strip()
    except Exception:
        return ""


def is_ready(status: str | None = None, w: Any | None = None) -> bool:
    if status is None:
        status = read_status(w)
    text = (status or "").strip().lower()
    if not text:
        return False
    return ("准备好" in text) or text.startswith("ready")


def read_log(w: Any) -> str:
    ctrl = _child(w, "3715")
    if ctrl is None:
        return ""
    try:
        return ctrl.window_text() or ""
    except Exception:
        return ""


def _ribbon(w: Any) -> Any | None:
    return _child(w, "59398")


def find_run_button(w: Any) -> Any | None:
    ribbon = _ribbon(w)
    if ribbon is None:
        return None
    try:
        controls = ribbon.descendants()
    except Exception:
        return None
    for ctrl in controls:
        try:
            label = normalize_label(ctrl.window_text())
            if label in {"运行宏。", "运行宏…", "运行宏..."}:
                return ctrl
        except Exception:
            continue
    return None


def invoke(ctrl: Any) -> bool:
    try:
        ctrl.invoke()
        return True
    except Exception:
        pass
    try:
        ctrl.iface_invoke.Invoke()
        return True
    except Exception:
        pass
    try:
        ctrl.click_input()
        return True
    except Exception:
        return False


def _dialog_children(d: Any) -> list[Any]:
    try:
        return d.descendants()
    except Exception:
        return []


def _control_text(ctrl: Any) -> str:
    try:
        return ctrl.window_text() or ""
    except Exception:
        return ""


def _auto_id(ctrl: Any) -> str:
    try:
        return str(ctrl.automation_id())
    except Exception:
        return ""


def _class_name(ctrl: Any) -> str:
    try:
        return ctrl.class_name() or ""
    except Exception:
        return ""


def _dialog_payload(d: Any) -> dict[str, Any]:
    title = _control_text(d)
    content = ""
    kind = "unknown"
    buttons: list[dict[str, Any]] = []
    for ctrl in _dialog_children(d):
        cls = _class_name(ctrl)
        aid = _auto_id(ctrl)
        text = _control_text(ctrl)
        if aid == "ContentText" and text:
            content = text
            kind = "task_dialog"
        elif aid == "65535" and text:
            content = text
            if kind == "unknown":
                kind = "message_box"
        elif cls == "Button" and text:
            buttons.append({"text": text, "auto_id": aid, "class_name": cls})
        elif cls == "CCPushButton" and text:
            buttons.append({"text": text, "auto_id": aid, "class_name": cls})
        elif aid == "1148" and cls == "Edit":
            kind = "file_dialog"
    if kind == "unknown" and any("打开" in b["text"] for b in buttons):
        kind = "file_dialog"
    if kind == "unknown" and any(
        any(token in b["text"] for token in ("是", "否", "取消", "Yes", "No")) for b in buttons
    ):
        kind = "save_prompt"
    try:
        rect = rect_list(d.rectangle())
    except Exception:
        rect = []
    return {
        "title": title,
        "kind": kind,
        "text": content or title,
        "buttons": buttons,
        "rect": rect,
    }


def candidate_dialogs(w: Any, deep: bool = False) -> list[Any]:
    result: list[Any] = []
    try:
        children = w.children()
    except Exception:
        children = []
    for ctrl in children:
        if _class_name(ctrl) == "#32770":
            result.append(ctrl)
    if deep and result:
        # Nested task dialogs (for example an invalid path inside the common
        # file dialog) live under an already discovered dialog.  Do not scan
        # the whole main window here: polling that tree is far too slow.
        for dialog in list(result):
            try:
                nested = dialog.descendants()
            except Exception:
                nested = []
            for ctrl in nested:
                if _class_name(ctrl) != "#32770":
                    continue
                duplicate = False
                for existing in result:
                    try:
                        if existing.rectangle() == ctrl.rectangle() and _control_text(existing) == _control_text(ctrl):
                            duplicate = True
                            break
                    except Exception:
                        pass
                if not duplicate:
                    result.append(ctrl)
    return result


def get_dialogs(w: Any, deep: bool = False) -> list[dict[str, Any]]:
    payloads = []
    for dialog in candidate_dialogs(w, deep=deep):
        payloads.append(_dialog_payload(dialog))
    return payloads


def _find_button_in_dialog(d: Any, names: tuple[str, ...]) -> Any | None:
    for ctrl in _dialog_children(d):
        if _class_name(ctrl) not in ("Button", "CCPushButton"):
            continue
        text = normalize_label(_control_text(ctrl))
        for name in names:
            if name in text:
                return ctrl
    return None


def dismiss_error_dialogs(w: Any, deep: bool = True) -> list[dict[str, Any]]:
    """Dismiss macro/license error dialogs and return what was seen.

    Save prompts are deliberately left alone.
    """
    seen: list[dict[str, Any]] = []
    for dialog in candidate_dialogs(w, deep=deep):
        payload = _dialog_payload(dialog)
        if payload["kind"] == "file_dialog":
            continue
        if payload["kind"] == "save_prompt":
            continue
        text = payload["text"] or ""
        if any(token in text for token in ("保存", "Save changes", "save changes")):
            continue
        button = _find_button_in_dialog(dialog, ("确定", "OK", "关闭", "Close"))
        if button is None and payload["kind"] == "task_dialog":
            for ctrl in _dialog_children(dialog):
                if _auto_id(ctrl) == "CommandButton_1":
                    button = ctrl
                    break
        if button is not None:
            invoke(button)
            seen.append(payload)
            time.sleep(0.2)
    return seen


def wait_for_file_dialog(w: Any, timeout: float = 8.0) -> Any | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        dialogs = candidate_dialogs(w, deep=True)
        for dialog in dialogs:
            payload = _dialog_payload(dialog)
            if payload["kind"] == "file_dialog":
                return dialog
            if payload["kind"] in ("task_dialog", "message_box"):
                button = _find_button_in_dialog(dialog, ("确定", "OK"))
                if button is not None:
                    invoke(button)
        time.sleep(0.2)
    return None


def _get_edit_value(edit: Any) -> str:
    for getter in ("get_value",):
        try:
            value = getattr(edit, getter)()
            if value:
                return str(value)
        except Exception:
            pass
    try:
        return str(edit.iface_value.CurrentValue or "")
    except Exception:
        return ""


def _set_filename(edit: Any, path: str) -> None:
    target = str(Path(path))
    try:
        edit.set_focus()
    except Exception:
        pass
    try:
        edit.set_edit_text(target)
    except Exception:
        pass
    time.sleep(0.15)
    value = _get_edit_value(edit)
    if target.lower() in (value or "").lower():
        return
    # Fallback: clear and type.  Generated macro paths are ASCII and contain no
    # pywinauto key modifiers.
    try:
        edit.type_keys("^a")
        edit.type_keys("{BACKSPACE}")
        edit.type_keys(target, with_spaces=True)
    except Exception as exc:
        raise GuiError(f"could not set the macro file name: {exc}") from exc


def _file_dialog_edit(dialog: Any) -> Any | None:
    for ctrl in _dialog_children(dialog):
        if _auto_id(ctrl) == "1148" and _class_name(ctrl) == "Edit":
            return ctrl
    return None


def _file_dialog_open_button(dialog: Any) -> Any | None:
    for ctrl in _dialog_children(dialog):
        if _auto_id(ctrl) == "1" and _class_name(ctrl) == "Button":
            return ctrl
    return None


def _submit_file_dialog(w: Any, dialog: Any, macro_path: str) -> None:
    edit = _file_dialog_edit(dialog)
    if edit is None:
        raise GuiError("macro file dialog has no file-name edit control")
    _set_filename(edit, macro_path)
    try:
        edit.set_focus()
    except Exception:
        pass
    send_keys("{ENTER}")
    if _wait_dialog_closed(w, dialog, timeout=12.0):
        return
    open_button = _file_dialog_open_button(dialog)
    if open_button is not None and invoke(open_button):
        if _wait_dialog_closed(w, dialog, timeout=8.0):
            return
    raise GuiError("macro file dialog did not close after submitting the path")


def _dialog_alive(dialog: Any) -> bool:
    """A UIA wrapper can outlive its HWND; trust the native handle."""
    try:
        hwnd = int(dialog.handle)
    except Exception:
        hwnd = 0
    if hwnd:
        return bool(win32gui.IsWindow(hwnd)) and bool(win32gui.IsWindowVisible(hwnd))
    return _dialog_exists(dialog)


def _wait_dialog_closed(w: Any, dialog: Any, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _dialog_alive(dialog):
            return True
        # A validation task dialog can be a sibling of the file dialog rather
        # than a descendant, so scan the main window as well.
        for ctrl in candidate_dialogs(w, deep=True):
            if _same_dialog(ctrl, dialog):
                continue
            payload = _dialog_payload(ctrl)
            if payload["kind"] in ("task_dialog", "message_box"):
                text = payload["text"]
                button = _find_button_in_dialog(ctrl, ("确定", "OK"))
                if button is not None:
                    invoke(button)
                raise GuiError(f"FracMan rejected the macro path: {text}")
        time.sleep(0.2)
    return False


def _same_dialog(first: Any, second: Any) -> bool:
    try:
        return first.rectangle() == second.rectangle() and _control_text(first) == _control_text(second)
    except Exception:
        return False


def _dialog_exists(dialog: Any) -> bool:
    try:
        return bool(dialog.exists())
    except Exception:
        try:
            dialog.rectangle()
            return True
        except Exception:
            return False


def _read_text_with_retry(path: Path, attempts: int = 8, delay: float = 0.2) -> str:
    for _ in range(attempts):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            time.sleep(delay)
        except OSError:
            time.sleep(delay)
    return ""


def _log_delta(before: str, after: str) -> str:
    """New log text even when the UIA log pane has scrolled at its cap."""
    if after == before:
        return ""
    if after.startswith(before):
        return after[len(before) :]
    max_overlap = min(len(before), len(after), 4000)
    for size in range(max_overlap, 0, -1):
        if before.endswith(after[:size]):
            return after[size:]
    return after


def _parse_fml_errors(text: str) -> list[str]:
    errors: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("#Macro Error"):
            block = [line]
            for follow in lines[index + 1 : index + 12]:
                if follow.strip().startswith("#") or not follow.strip():
                    block.append(follow)
                else:
                    break
            errors.append("\n".join(part for part in block if part.strip()))
    return errors


def execute_live_macro(
    w: Any,
    *,
    macro_path: str | Path,
    fml_path: str | Path,
    hard_timeout: float = 6 * 3600,
    poll_interval: float = 0.25,
) -> dict[str, Any]:
    """Run a macro in the attached GUI and wait for completion.

    The caller must hold the per-session execution lock.  This function never
    retries a submission: a failed dialog is reported, not replayed.
    """
    macro_path = Path(macro_path)
    fml_path = Path(fml_path)
    if not is_ready(w=w):
        raise GuiBusyError(f"FracMan is not ready: status={read_status(w)!r}")

    dismiss_error_dialogs(w, deep=True)
    before_log = read_log(w)
    button = find_run_button(w)
    if button is None:
        raise GuiError("could not find the FracMan 'Run Macro' ribbon button")
    if not invoke(button):
        raise GuiError("could not activate the FracMan 'Run Macro' button")

    dialog = wait_for_file_dialog(w, timeout=8.0)
    if dialog is None:
        raise GuiError("the FracMan macro file dialog did not appear")

    submitted_at = time.time()
    _submit_file_dialog(w, dialog, str(macro_path))

    seen_dialogs: list[dict[str, Any]] = []
    saw_busy = False
    after_log = before_log
    last_status = ""
    deadline = submitted_at + hard_timeout

    while time.time() < deadline:
        for payload in get_dialogs(w, deep=True):
            if payload["kind"] == "file_dialog":
                continue
            buttons = " ".join(item.get("text", "") for item in payload.get("buttons", []))
            if payload["kind"] == "save_prompt" or any(
                token in buttons for token in ("是", "否", "Yes", "No")
            ):
                # A confirmation dialog would need a human decision; do not
                # guess, and do not release the caller's lock.
                return {
                    "ok": False,
                    "status": "needs_user_action",
                    "elapsed_seconds": round(time.time() - submitted_at, 2),
                    "macro_path": str(macro_path),
                    "fml_path": str(fml_path),
                    "log_delta": "",
                    "errors": [f"blocked by confirmation dialog: {payload.get('text', '')}"],
                    "dialogs": [payload],
                    "fml_tail": "",
                    "note": "answer the dialog in FracMan manually; the session lock is retained",
                }
        for payload in dismiss_error_dialogs(w, deep=True):
            seen_dialogs.append(payload)
        status = read_status(w)
        last_status = status
        if not is_ready(status):
            saw_busy = True
        after_log = read_log(w)
        file_dialogs = [
            payload
            for payload in get_dialogs(w, deep=True)
            if payload["kind"] == "file_dialog"
        ]
        if (
            not file_dialogs
            and is_ready(status)
            and after_log != before_log
            and (saw_busy or time.time() - submitted_at > 1.0)
        ):
            elapsed = time.time() - submitted_at
            fml_text = _read_text_with_retry(fml_path)
            errors = _parse_fml_errors(fml_text)
            return {
                "ok": not errors and not seen_dialogs and bool(fml_text.strip()),
                "status": "completed" if not errors and fml_text.strip() else (
                    "macro_error" if errors else "no_log"
                ),
                "elapsed_seconds": round(elapsed, 2),
                "macro_path": str(macro_path),
                "fml_path": str(fml_path),
                "log_delta": _log_delta(before_log, after_log),
                "errors": errors,
                "dialogs": seen_dialogs,
                "fml_tail": fml_text[-4000:],
            }
        if time.time() - submitted_at > 8.0 and file_dialogs:
            raise GuiError(
                "the macro file dialog is still open; the path was likely rejected"
            )
        time.sleep(poll_interval)

    fml_text = _read_text_with_retry(fml_path, attempts=2)
    return {
        "ok": False,
        "status": "hard_timeout",
        "elapsed_seconds": round(time.time() - submitted_at, 2),
        "macro_path": str(macro_path),
        "fml_path": str(fml_path),
        "log_delta": _log_delta(before_log, after_log) if after_log else "",
        "errors": _parse_fml_errors(fml_text),
        "dialogs": seen_dialogs,
        "fml_tail": fml_text[-4000:],
        "note": "the GUI may still be busy; the session lock is intentionally kept until completion",
    }


def view_rect(w: Any, main_rect_uia: Any | None = None) -> list[int] | None:
    """Return the largest 3D view rectangle in UIA coordinates."""
    try:
        controls = w.descendants()
    except Exception:
        return None
    best: tuple[float, list[int]] | None = None
    for ctrl in controls:
        if _class_name(ctrl) != "AfxFrameOrView140u":
            continue
        try:
            rect = ctrl.rectangle()
            area = float(rect.width()) * float(rect.height())
            if area <= 0:
                continue
            if best is None or area > best[0]:
                best = (area, [rect.left, rect.top, rect.right, rect.bottom])
        except Exception:
            continue
    return best[1] if best else None


def _print_window_image(hwnd: int) -> Image.Image:
    left, top, right, bottom = window_rect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise GuiError("FracMan window has an invalid size")
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    if not hwnd_dc:
        raise GuiError("could not get the FracMan window DC")
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)
    try:
        painted = USER32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        if not painted:
            painted = USER32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        if not painted:
            # Last resort: screen copy.  May capture overlapping windows.
            save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), win32con.SRCCOPY)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        )
        return image.copy()
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def capture_window(
    w: Any,
    hwnd: int,
    *,
    out_path: str | Path,
    mode: str = "full",
) -> dict[str, Any]:
    image = _print_window_image(hwnd)
    uia_rect = w.rectangle()
    try:
        left, top, right, bottom = window_rect(hwnd)
        captured_width = right - left
        scale = captured_width / float(uia_rect.width()) if uia_rect.width() else 1.0
    except Exception:
        scale = 1.0
        left = top = 0
    cropped = False
    view = view_rect(w)
    if mode == "view" and view:
        x1 = int(max(0, (view[0] - uia_rect.left) * scale))
        y1 = int(max(0, (view[1] - uia_rect.top) * scale))
        x2 = int(min(image.width, (view[2] - uia_rect.left) * scale))
        y2 = int(min(image.height, (view[3] - uia_rect.top) * scale))
        if x2 - x1 > 20 and y2 - y1 > 20:
            image = image.crop((x1, y1, x2, y2))
            cropped = True
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, format="PNG")
    return {
        "path": str(out),
        "mode": mode,
        "width": image.width,
        "height": image.height,
        "view_cropped": cropped,
        "view_rect": view,
        "uia_window_rect": [uia_rect.left, uia_rect.top, uia_rect.right, uia_rect.bottom],
        "capture_scale": round(scale, 4),
    }


def image_data_uri(path: str | Path) -> str:
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def launch_project(exe: str | Path, project: str | Path, timeout: float = 45.0) -> dict[str, Any]:
    exe_path = Path(exe)
    project_path = Path(project)
    if not exe_path.is_file():
        raise GuiError(f"FracMan executable not found: {exe_path}")
    if not project_path.is_file():
        raise GuiError(f"project file not found: {project_path}")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 1
    process = subprocess.Popen(
        [str(exe_path), str(project_path)],
        cwd=str(project_path.parent),
        startupinfo=startup,
    )
    deadline = time.time() + timeout
    window = None
    startup_dialogs: list[dict[str, Any]] = []
    while time.time() < deadline:
        if process.poll() is not None:
            raise GuiError(
                f"FracMan exited while opening the project (exit code {process.returncode})"
            )
        # The beta-build notice is modal and appears before the main window.
        startup_dialogs.extend(dismiss_beta_notice(process.pid))
        info = find_window_for_pid(process.pid)
        if info:
            window = info
            break
        time.sleep(0.5)
    if window is None:
        raise GuiError(f"FracMan window for PID {process.pid} did not appear within {timeout}s")

    w = connect(process.pid, window["hwnd"])
    deadline = time.time() + 30.0
    while time.time() < deadline:
        dialogs = get_dialogs(w, deep=True)
        if not dialogs:
            if is_ready(w=w):
                break
            time.sleep(0.4)
            continue
        for dialog in candidate_dialogs(w, deep=True):
            payload = _dialog_payload(dialog)
            text = (payload["text"] or "") + " " + (payload["title"] or "")
            if payload["kind"] == "file_dialog":
                continue
            if any(token in text.lower() for token in ("license", "许可", "flexlm")):
                return {
                    "ok": False,
                    "status": "needs_user_action",
                    "pid": process.pid,
                    "hwnd": window["hwnd"],
                    "dialog": payload,
                    "message": "FracMan is waiting for a license action; handle the dialog manually",
                }
            if payload["kind"] == "save_prompt":
                return {
                    "ok": False,
                    "status": "needs_user_action",
                    "pid": process.pid,
                    "hwnd": window["hwnd"],
                    "dialog": payload,
                    "message": "FracMan is waiting for a save decision",
                }
            startup_dialogs.append(payload)
            button = _find_button_in_dialog(dialog, ("确定", "OK", "继续", "Continue"))
            if button is not None:
                invoke(button)
                time.sleep(0.4)
    status = read_status(w)
    return {
        "ok": True,
        "status": "ready" if is_ready(status) else "opened_not_ready",
        "pid": process.pid,
        "hwnd": window["hwnd"],
        "title": window["title"],
        "fracman_status": status,
        "dialogs": startup_dialogs,
    }


def close_window(
    w: Any,
    hwnd: int,
    pid: int,
    *,
    save: bool = False,
    timeout: float = 15.0,
) -> dict[str, Any]:
    dialogs_seen: list[dict[str, Any]] = []
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as exc:
        raise GuiError(f"could not send WM_CLOSE: {exc}") from exc
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not psutil.pid_exists(pid):
            return {"ok": True, "closed": True, "dialogs": dialogs_seen}
        try:
            if not psutil.Process(pid).is_running():
                return {"ok": True, "closed": True, "dialogs": dialogs_seen}
        except Exception:
            return {"ok": True, "closed": True, "dialogs": dialogs_seen}
        for dialog in candidate_dialogs(w, deep=True):
            payload = _dialog_payload(dialog)
            if payload["kind"] == "save_prompt" or any(
                any(token in b["text"] for token in ("是", "否", "Yes", "No"))
                for b in payload["buttons"]
            ):
                dialogs_seen.append(payload)
                target = ("是", "Yes") if save else ("否", "No")
                button = _find_button_in_dialog(dialog, target)
                if button is None:
                    button = _find_button_in_dialog(dialog, ("取消", "Cancel"))
                    if button is not None:
                        invoke(button)
                        return {
                            "ok": False,
                            "closed": False,
                            "status": "save_cancelled",
                            "dialogs": dialogs_seen,
                        }
                if button is not None:
                    invoke(button)
                    time.sleep(0.5)
                    continue
            if payload["kind"] in ("task_dialog", "message_box"):
                dialogs_seen.append(payload)
                button = _find_button_in_dialog(dialog, ("确定", "OK"))
                if button is not None:
                    invoke(button)
        time.sleep(0.3)
    return {"ok": False, "closed": False, "status": "still_running", "dialogs": dialogs_seen}


def terminate_process(pid: int, timeout: float = 8.0) -> bool:
    try:
        process = psutil.Process(pid)
    except Exception:
        return True
    try:
        process.terminate()
        process.wait(timeout=timeout)
        return True
    except Exception:
        try:
            process.kill()
            return True
        except Exception:
            return False
