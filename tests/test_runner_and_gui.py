"""Runner guard rails and GUI helper tests (no FracMan process is started)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fracman_mcp import gui
from fracman_mcp.config import settings
from fracman_mcp.macro import MacroValidationError
from fracman_mcp.models import RunRecord
from fracman_mcp.requests import fingerprint, runs_registry
from fracman_mcp.runner import BatchBusy, BatchError, BatchRunner, _tail, make_run_id
from fracman_mcp.sessions import _project_from_title

CODE = "BEGIN SetSeed\nSeed = 1\nEND\n"


def test_run_id_and_tail(tmp_path):
    run_id = make_run_id()
    assert run_id.startswith("fmr-")
    assert len(run_id) == 16
    path = tmp_path / "log.fml"
    path.write_text("0123456789", encoding="utf-8")
    assert _tail(path, chars=4) == "6789"
    assert _tail(None) == ""
    assert _tail(tmp_path / "missing") == ""


def test_submit_requires_exactly_one_source(tmp_path):
    runner = BatchRunner()
    with pytest.raises(BatchError):
        runner.submit()
    with pytest.raises(BatchError):
        runner.submit(code=CODE, macro_path=str(tmp_path / "x.fmf"))
    with pytest.raises(MacroValidationError):
        runner.submit(macro_path=str(tmp_path / "missing.fmf"))


def test_submit_rejects_bad_timeout():
    runner = BatchRunner()
    with pytest.raises(BatchError):
        runner.submit(code=CODE, timeout=0)


def test_submit_replay_returns_existing_record():
    runner = BatchRunner()
    digest = fingerprint(
        {"code": CODE, "expected": [], "timeout": settings.batch_timeout}
    )
    record = RunRecord(
        run_id="fmr-replay",
        status="completed",
        fingerprint=digest,
        message="original",
    )
    runs_registry.create(record)
    try:
        result = runner.submit(code=CODE, run_id="fmr-replay")
        assert result["run_id"] == "fmr-replay"
        assert result["status"] == "completed"
        assert "still running" not in result.get("hint", "")
    finally:
        runs_registry.finish("fmr-replay", status="completed")


def test_submit_respects_capacity(monkeypatch):
    runner = BatchRunner()
    monkeypatch.setattr(settings, "max_batch", 1)
    runs_registry.create(RunRecord(run_id="fmr-holder", status="running"))
    try:
        with pytest.raises(BatchBusy):
            runner.submit(code=CODE)
    finally:
        runs_registry.finish("fmr-holder", status="completed")


def test_project_from_title():
    assert (
        _project_from_title("弗拉克曼 8.00 Beta (2021.02.05) 64bit - proj_123")
        == "proj_123"
    )
    assert _project_from_title("弗拉克曼 8.00 Beta - FracMan1") == ""
    assert _project_from_title("no separator") == ""
    assert _project_from_title("X - FracMan") == ""


def test_rect_list_accepts_rect_object():
    rect = SimpleNamespace(left=1, top=2, right=3, bottom=4)
    assert gui.rect_list(rect) == [1, 2, 3, 4]


def test_log_delta_handles_growth_and_scroll():
    assert gui._log_delta("abc", "abc") == ""
    assert gui._log_delta("abc", "abcdef") == "def"
    # The UIA log pane caps at 4096 chars, so the prefix disappears.
    assert gui._log_delta("aaaaBBBB", "BBBBnew") == "new"
    assert gui._log_delta("old", "unrelated") == "unrelated"


def test_is_ready_variants():
    assert gui.is_ready("准备好")
    assert gui.is_ready("Ready")
    assert not gui.is_ready("")
    assert not gui.is_ready("正在运行")
    assert not gui.is_ready("未知")


def test_dialog_error_text_ignores_progress_boxes():
    info = [{"title": "FracMan", "text": "Fracture generation complete"}]
    assert gui._dialog_error_text(info) == []
    assert gui._dialog_error_text([{"title": "FracMan", "text": ""}]) == []


def test_dialog_error_text_detects_error_boxes():
    assert gui._dialog_error_text(
        [{"title": "FracMan", "text": "Invalid value/unit specified"}]
    )
    assert gui._dialog_error_text([{"title": "FracMan", "text": "宏错误：文件未找到"}])
    assert not gui._dialog_error_text(
        [{"title": "FracMan", "text": "Export finished successfully"}]
    )


def test_image_is_blank_detects_dead_printwindow():
    from PIL import Image

    assert gui._image_is_blank(Image.new("RGB", (40, 40), (0, 0, 0)))
    patterned = Image.new("RGB", (40, 40), (0, 0, 0))
    patterned.putpixel((20, 20), (255, 255, 255))
    assert not gui._image_is_blank(patterned)


def test_fml_finished_detects_runmacro_sentinel():
    sentinel = "C:/tmp/run/sentinel.fmf"
    body = 'BEGIN SetCurrentUnits\nValue = "Length"\nUnits = "m"\nEND\n'
    assert not gui._fml_finished(body, sentinel)
    assert not gui._fml_finished("", sentinel)

    done = (
        body
        + "#Running Macro: C:/tmp/run/sentinel.fmf\n"
        + 'BEGIN RunMacro\n\tPath = "C:/tmp/run/sentinel.fmf"\nEND\n'
    )
    assert gui._fml_finished(done, sentinel)
    assert gui._fml_finished(done.replace("C:/tmp", "C:\\tmp"), sentinel)
    assert not gui._fml_finished(body, sentinel)
    # A different sentinel path in the same run must not satisfy this run.
    other = done.replace("sentinel.fmf", "other.fmf")
    assert not gui._fml_finished(other, sentinel)
