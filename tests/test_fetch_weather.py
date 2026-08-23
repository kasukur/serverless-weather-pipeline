import importlib.util
import json
import pathlib
import sys
from io import BytesIO
from unittest.mock import patch

import pytest

# Loaded under a unique module name -- see the comment in test_transform.py
# for why (both Lambdas' handler files are named `handler.py`).
_HANDLER_PATH = pathlib.Path(__file__).resolve().parents[1] / "lambdas" / "fetch_weather" / "handler.py"
_spec = importlib.util.spec_from_file_location("fetch_weather_handler", _HANDLER_PATH)
fetch_weather_handler = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fetch_weather_handler
_spec.loader.exec_module(fetch_weather_handler)

handler = fetch_weather_handler.handler


class _FakeResponse(BytesIO):
    """Minimal stand-in for the context-manager object urlopen() returns."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_handler_returns_normalized_weather():
    fake_payload = json.dumps(
        {
            "current_weather": {
                "temperature": 21.3,
                "windspeed": 9.4,
                "winddirection": 180,
                "weathercode": 2,
                "time": "2026-08-23T10:00",
            }
        }
    ).encode("utf-8")

    with patch("fetch_weather_handler.urllib.request.urlopen", return_value=_FakeResponse(fake_payload)):
        result = handler({"city": "Sydney", "latitude": -33.8688, "longitude": 151.2093}, None)

    assert result["city"] == "Sydney"
    assert result["temperature_c"] == 21.3
    assert result["windspeed_kmh"] == 9.4
    assert result["observation_time"] == "2026-08-23T10:00"
    assert "fetched_at" in result


def test_handler_raises_on_missing_current_weather():
    fake_payload = json.dumps({}).encode("utf-8")

    with patch("fetch_weather_handler.urllib.request.urlopen", return_value=_FakeResponse(fake_payload)):
        with pytest.raises(RuntimeError):
            handler({"city": "Nowhere", "latitude": 0, "longitude": 0}, None)
