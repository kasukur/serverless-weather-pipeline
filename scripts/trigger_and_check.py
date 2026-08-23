#!/usr/bin/env python3
"""Manually start the weather pipeline's Step Functions execution and
poll it to completion. Handy right after `cdk deploy` so you don't have
to wait for the next EventBridge Scheduler tick.

Usage:
    python3 scripts/trigger_and_check.py [--region ap-southeast-2]

Requires AWS credentials in your environment (e.g. an AWS SSO profile
or `aws configure` -- this is a local convenience script, it does not
run in CI).
"""
import argparse
import json
import time

import boto3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=None)
    parser.add_argument("--state-machine-name", default="weather-ingestion-pipeline")
    parser.add_argument("--timeout", type=int, default=120, help="seconds to wait for completion")
    args = parser.parse_args()

    sfn = boto3.client("stepfunctions", region_name=args.region)

    state_machines = sfn.list_state_machines()["stateMachines"]
    matches = [sm for sm in state_machines if sm["name"] == args.state_machine_name]
    if not matches:
        raise SystemExit(
            f"No state machine named {args.state_machine_name!r} found. "
            "Has `cdk deploy WeatherPipeline` finished successfully?"
        )
    state_machine_arn = matches[0]["stateMachineArn"]

    start = sfn.start_execution(stateMachineArn=state_machine_arn, input="{}")
    execution_arn = start["executionArn"]
    print(f"Started execution: {execution_arn}")

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        description = sfn.describe_execution(executionArn=execution_arn)
        status = description["status"]
        if status != "RUNNING":
            print(f"Execution finished with status: {status}")
            if description.get("output"):
                print(json.dumps(json.loads(description["output"]), indent=2))
            if status != "SUCCEEDED":
                raise SystemExit(1)
            return
        time.sleep(3)

    raise SystemExit("Timed out waiting for the execution to finish.")


if __name__ == "__main__":
    main()
