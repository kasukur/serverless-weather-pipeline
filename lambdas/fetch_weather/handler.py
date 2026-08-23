"""Fetch current weather for one city from the Open-Meteo public API.

Open-Meteo (https://open-meteo.com) requires no API key, so this Lambda
has zero external dependencies -- just the Python standard library. That
keeps the deployment package tiny and means there's no Lambda layer to
build or keep in sync.

Invoked as one iteration of a Step Functions Map state, so the event IS
a single city record: {"city": ..., "latitude": ..., "longitude": ...}.
Any failure raises, so the state machine's Retry/Catch handles it -- see
stacks/pipeline_stack.py.
"""
import datetime
import json
import urllib.error
import urllib.request

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS = 10


def handler(event, context):
    city = event["city"]
    latitude = event["latitude"]
    longitude = event["longitude"]

    query = (
        f"?latitude={latitude}&longitude={longitude}"
        "&current_weather=true&timezone=UTC"
    )
    url = OPEN_METEO_URL + query

    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Failed to fetch weather for {city}: {exc}") from exc

    current = payload.get("current_weather")
    if not current:
        raise RuntimeError(f"No current_weather field in Open-Meteo response for {city}: {payload}")

    return {
        "city": city,
        "latitude": latitude,
        "longitude": longitude,
        "temperature_c": current["temperature"],
        "windspeed_kmh": current["windspeed"],
        "winddirection_deg": current["winddirection"],
        "weathercode": current["weathercode"],
        "observation_time": current["time"],
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
