import importlib.util
import json
import pathlib
import sys

import pytest

# Both Lambda handlers are named `handler.py`, so we load this one under a
# unique module name instead of sys.path-inserting its directory -- that
# would let whichever handler.py gets imported first "win" for every test
# module in the whole run.
_HANDLER_PATH = pathlib.Path(__file__).resolve().parents[1] / "lambdas" / "transform" / "handler.py"
_spec = importlib.util.spec_from_file_location("transform_handler", _HANDLER_PATH)
transform_handler = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = transform_handler
_spec.loader.exec_module(transform_handler)

build_records = transform_handler.build_records
handler = transform_handler.handler

SUCCESS_ITEM = {
    "city": "Sydney",
    "latitude": -33.8688,
    "longitude": 151.2093,
    "temperature_c": 18.4,
    "windspeed_kmh": 12.1,
    "winddirection_deg": 200,
    "weathercode": 1,
    "observation_time": "2026-08-23T10:00",
    "fetched_at": "2026-08-23T10:00:03+00:00",
}

FAILURE_ITEM = {
    "city": "Atlantis",
    "latitude": 0,
    "longitude": 0,
    "error": {"Error": "RuntimeError", "Cause": "Failed to fetch weather for Atlantis: timed out"},
}


def test_build_records_splits_success_and_failure():
    result = build_records([SUCCESS_ITEM, FAILURE_ITEM], run_id="test-run", run_started_at="2026-08-23T10:15:00Z")
    assert result["record_count"] == 1
    assert result["failure_count"] == 1
    assert result["failures"][0]["city"] == "Atlantis"


def test_build_records_partition_key_uses_run_started_at():
    result = build_records(
        [SUCCESS_ITEM],
        run_id="2026-08-23T10:00:00Z_abc123",
        run_started_at="2026-08-23T10:00:00.512Z",
    )
    assert result["key"] == "processed/dt=2026-08-23/hour=10/2026-08-23T10-00-00Z_abc123.jsonl"


def test_build_records_body_is_valid_json_lines():
    result = build_records([SUCCESS_ITEM], run_id="run1", run_started_at="2026-08-23T00:00:00Z")
    lines = result["body"].splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["city"] == "Sydney"
    assert parsed["temperature_c"] == 18.4


def test_build_records_with_no_successes_reports_all_failures():
    result = build_records([FAILURE_ITEM], run_id="run-all-fail", run_started_at="2026-08-23T00:00:00Z")
    assert result["record_count"] == 0
    assert result["failure_count"] == 1


def test_handler_raises_when_all_fetches_fail():
    event = {
        "fetch_results": [FAILURE_ITEM],
        "run_id": "run-all-fail",
        "run_started_at": "2026-08-23T00:00:00Z",
    }
    with pytest.raises(RuntimeError):
        handler(event, None)


def test_handler_returns_step_functions_payload_for_mixed_results():
    event = {
        "fetch_results": [SUCCESS_ITEM, FAILURE_ITEM],
        "run_id": "run-mixed",
        "run_started_at": "2026-08-23T05:00:00Z",
    }
    payload = handler(event, None)
    assert payload["record_count"] == 1
    assert payload["failure_count"] == 1
    assert payload["key"].startswith("processed/dt=2026-08-23/hour=05/")
    assert "Sydney" in payload["body"]
