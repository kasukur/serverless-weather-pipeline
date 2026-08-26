"""Turn the Step Functions Map's fetch results into a JSON-Lines body and
an S3 key partitioned by dt=YYYY-MM-DD/hour=HH.

Deliberately does NOT call any AWS API -- the actual S3 write happens in
a separate Lambda (see lambdas/load/handler.py and the LoadToS3 task in
stacks/pipeline_stack.py), so this function needs no IAM permissions at
all beyond writing CloudWatch Logs, and it's trivially unit-testable
(see tests/test_transform.py) since `build_records` is a pure function.
"""
import json
import re
from datetime import datetime


def _partition_parts(run_started_at: str):
    """run_started_at is the Step Functions Execution.StartTime context
    field, e.g. '2026-08-23T10:00:05.123Z'."""
    dt = datetime.fromisoformat(run_started_at.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H")


def _sanitize(run_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", run_id)


def build_records(fetch_results, run_id: str, run_started_at: str):
    """Split the Map's per-city results into clean records vs failures,
    and build the JSON-Lines body + S3 key for the successful ones."""
    records = []
    failures = []
    for item in fetch_results:
        if isinstance(item, dict) and "error" in item:
            failures.append({"city": item.get("city", "unknown"), "error": item["error"]})
        else:
            records.append(item)

    date_part, hour_part = _partition_parts(run_started_at)
    key = f"processed/dt={date_part}/hour={hour_part}/{_sanitize(run_id)}.jsonl"
    body = "\n".join(json.dumps(record, sort_keys=True) for record in records)

    return {
        "key": key,
        "body": body,
        "record_count": len(records),
        "failure_count": len(failures),
        "failures": failures,
    }


def handler(event, context):
    fetch_results = event["fetch_results"]
    run_id = event["run_id"]
    run_started_at = event["run_started_at"]

    result = build_records(fetch_results, run_id, run_started_at)

    if result["record_count"] == 0:
        # No cities succeeded this run -- fail the state so the
        # workflow's Catch publishes an SNS alert instead of silently
        # writing an empty file to S3.
        raise RuntimeError(
            f"All {result['failure_count']} weather fetches failed for run {run_id}: {result['failures']}"
        )

    if result["failure_count"]:
        print(
            f"WARNING: {result['failure_count']} of "
            f"{result['failure_count'] + result['record_count']} city fetches "
            f"failed for run {run_id}: {result['failures']}"
        )

    return {
        "key": result["key"],
        "body": result["body"],
        "record_count": result["record_count"],
        "failure_count": result["failure_count"],
    }