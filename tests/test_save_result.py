"""Tests for save_result with configurable output directory."""
import json
from pathlib import Path

import pytest


def test_save_result_writes_to_custom_output_dir(tmp_path):
    """save_result should write result.json under output_dir/session_id/."""
    from run import save_result

    result = {
        "session_id": "abc-123",
        "scenario_name": "test_scenario",
        "status": "completed",
    }
    save_result(result, output_dir=tmp_path)

    out_file = tmp_path / "abc-123" / "result.json"
    assert out_file.exists()
    data = json.loads(out_file.read_text())
    assert data["session_id"] == "abc-123"


def test_save_result_defaults_to_test_results(tmp_path, monkeypatch):
    """save_result without output_dir should use default DEFAULT_RESULTS_DIR."""
    from run import save_result

    monkeypatch.setattr("run.DEFAULT_RESULTS_DIR", tmp_path)

    result = {
        "session_id": "def-456",
        "scenario_name": "test_scenario",
        "status": "completed",
    }
    save_result(result)

    out_file = tmp_path / "def-456" / "result.json"
    assert out_file.exists()


def test_save_result_skips_when_no_session_id(tmp_path):
    """save_result should skip when session_id is missing."""
    from run import save_result

    result = {"scenario_name": "test", "status": "completed"}
    save_result(result, output_dir=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_run_one_accepts_output_dir():
    """run_one should accept output_dir parameter."""
    import inspect
    from run import run_one
    sig = inspect.signature(run_one)
    assert "output_dir" in sig.parameters, "run_one must accept output_dir parameter"


def test_run_all_accepts_output_dir():
    """run_all should accept output_dir parameter."""
    import inspect
    from run import run_all
    sig = inspect.signature(run_all)
    assert "output_dir" in sig.parameters


def test_retry_failed_accepts_output_dir():
    """retry_failed should accept output_dir parameter."""
    import inspect
    from run import retry_failed
    sig = inspect.signature(retry_failed)
    assert "output_dir" in sig.parameters


def test_run_with_dashboard_accepts_output_dir():
    """run_with_dashboard should accept output_dir parameter."""
    import inspect
    from run import run_with_dashboard
    sig = inspect.signature(run_with_dashboard)
    assert "output_dir" in sig.parameters
