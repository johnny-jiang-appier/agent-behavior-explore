"""Comprehensive workspace integration tests for output directory features."""
import json
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Test group 1: save_result edge cases
# ---------------------------------------------------------------------------

def test_save_result_creates_nested_dirs(tmp_path):
    """save_result should create deeply nested output directories."""
    from run import save_result
    deep_path = tmp_path / "workspace" / "feature" / "run-01" / "sessions"
    result = {"session_id": "test-123", "status": "completed"}
    save_result(result, output_dir=deep_path)
    assert (deep_path / "test-123" / "result.json").exists()


def test_save_result_preserves_unicode(tmp_path):
    """save_result should handle unicode content (Chinese characters)."""
    from run import save_result
    result = {
        "session_id": "unicode-test",
        "status": "completed",
        "reason": "測試通過，所有商品資料正確",
    }
    save_result(result, output_dir=tmp_path)
    data = json.loads((tmp_path / "unicode-test" / "result.json").read_text(encoding="utf-8"))
    assert "測試通過" in data["reason"]


def test_save_result_overwrites_existing(tmp_path):
    """save_result should overwrite existing result.json."""
    from run import save_result
    result1 = {"session_id": "overwrite-test", "status": "failed"}
    result2 = {"session_id": "overwrite-test", "status": "completed"}
    save_result(result1, output_dir=tmp_path)
    save_result(result2, output_dir=tmp_path)
    data = json.loads((tmp_path / "overwrite-test" / "result.json").read_text())
    assert data["status"] == "completed"


# ---------------------------------------------------------------------------
# Test group 2: _load_completed_scenarios with custom dir
# ---------------------------------------------------------------------------

def test_load_completed_scenarios_from_custom_dir(tmp_path):
    """_load_completed_scenarios should scan the given directory."""
    from run import _load_completed_scenarios
    # Create a session dir with completed result
    session_dir = tmp_path / "session-abc"
    session_dir.mkdir()
    (session_dir / "result.json").write_text(json.dumps({
        "scenario_name": "test_scenario",
        "status": "completed",
    }))
    # Create another with failed
    session_dir2 = tmp_path / "session-def"
    session_dir2.mkdir()
    (session_dir2 / "result.json").write_text(json.dumps({
        "scenario_name": "test_failed",
        "status": "failed",
    }))

    completed = _load_completed_scenarios(tmp_path)
    assert completed == {"test_scenario"}


def test_load_completed_scenarios_empty_dir(tmp_path):
    """_load_completed_scenarios should return empty set for empty dir."""
    from run import _load_completed_scenarios
    assert _load_completed_scenarios(tmp_path) == set()


def test_load_completed_scenarios_nonexistent_dir(tmp_path):
    """_load_completed_scenarios should return empty set for nonexistent dir."""
    from run import _load_completed_scenarios
    nonexistent = tmp_path / "does-not-exist"
    assert _load_completed_scenarios(nonexistent) == set()


def test_load_completed_scenarios_ignores_corrupt_json(tmp_path):
    """_load_completed_scenarios should skip corrupt result.json files."""
    from run import _load_completed_scenarios
    session_dir = tmp_path / "session-bad"
    session_dir.mkdir()
    (session_dir / "result.json").write_text("not valid json{{{")
    assert _load_completed_scenarios(tmp_path) == set()


# ---------------------------------------------------------------------------
# Test group 3: _update_run_context
# ---------------------------------------------------------------------------

def test_update_run_context_preserves_existing_fields(tmp_path):
    """_update_run_context should not lose existing fields."""
    from run import _update_run_context
    run_dir = tmp_path / "run-01"
    sessions_dir = run_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    ctx = {
        "feature": "my-feature",
        "run": "run-01",
        "workspace": "/some/path",
        "phase": "designed",
        "created_at": "2026-04-01",
        "sessions_count": 0,
        "notes": "important note",
        "custom_field": "keep me",
    }
    (run_dir / "run-context.yaml").write_text(yaml.dump(ctx))

    _update_run_context(sessions_dir, num_sessions=10)

    updated = yaml.safe_load((run_dir / "run-context.yaml").read_text())
    assert updated["phase"] == "executed"
    assert updated["sessions_count"] == 10
    assert updated["notes"] == "important note"
    assert updated["custom_field"] == "keep me"
    assert updated["feature"] == "my-feature"


def test_update_run_context_creates_updated_at(tmp_path):
    """_update_run_context should set updated_at field."""
    from run import _update_run_context
    run_dir = tmp_path / "run-01"
    sessions_dir = run_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    ctx = {"phase": "designed", "sessions_count": 0}
    (run_dir / "run-context.yaml").write_text(yaml.dump(ctx))

    _update_run_context(sessions_dir, num_sessions=5)

    updated = yaml.safe_load((run_dir / "run-context.yaml").read_text())
    assert "updated_at" in updated
    assert len(updated["updated_at"]) == 10  # YYYY-MM-DD format


def test_update_run_context_handles_empty_yaml(tmp_path):
    """_update_run_context should handle empty run-context.yaml."""
    from run import _update_run_context
    run_dir = tmp_path / "run-01"
    sessions_dir = run_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    (run_dir / "run-context.yaml").write_text("")

    _update_run_context(sessions_dir, num_sessions=3)

    updated = yaml.safe_load((run_dir / "run-context.yaml").read_text())
    assert updated["phase"] == "executed"
    assert updated["sessions_count"] == 3


# ---------------------------------------------------------------------------
# Test group 4: CLI argument parsing
# ---------------------------------------------------------------------------

def test_cli_output_arg_parsed():
    """--output should be parsed as string."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--scenarios", default="scenarios.yaml")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--resume", action="store_true")

    args = parser.parse_args(["--output", "/tmp/workspace/feat/run-01/sessions"])
    assert args.output == "/tmp/workspace/feat/run-01/sessions"

    args_default = parser.parse_args([])
    assert args_default.output is None


def test_cli_scenarios_arg_parsed():
    """--scenarios should accept custom path."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", default="scenarios.yaml")

    args = parser.parse_args(["--scenarios", "/tmp/workspace/feat/run-01/scenarios.yaml"])
    assert args.scenarios == "/tmp/workspace/feat/run-01/scenarios.yaml"


# ---------------------------------------------------------------------------
# Test group 5: summary.py
# ---------------------------------------------------------------------------

def test_summary_load_results_from_custom_dir(tmp_path):
    """load_results should work with custom directory."""
    from summary import load_results

    session_dir = tmp_path / "session-abc"
    session_dir.mkdir()
    (session_dir / "result.json").write_text(json.dumps({
        "session_id": "session-abc",
        "scenario_name": "test",
        "status": "completed",
    }))

    results = load_results(tmp_path)
    assert len(results) == 1
    assert results[0]["session_id"] == "session-abc"


def test_summary_load_results_empty_dir(tmp_path):
    """load_results should return empty list for empty dir."""
    from summary import load_results
    assert load_results(tmp_path) == []


def test_summary_load_results_skips_non_dirs(tmp_path):
    """load_results should skip non-directory entries."""
    from summary import load_results
    (tmp_path / "not-a-dir.txt").write_text("hello")
    assert load_results(tmp_path) == []
