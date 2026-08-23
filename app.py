#!/usr/bin/env python3
"""CDK app entry point for the serverless weather ingestion pipeline.

Two independent stacks:

  * WeatherPipeline-GitHubOidc — the GitHub Actions OIDC provider + a
    deploy role scoped to this repo/branch. Deploy this ONCE, locally,
    with your own AWS credentials. Nothing in here is a secret; its
    only output is a role ARN that is safe to store as a plain GitHub
    repo *variable* (not a *secret*) because the role's trust policy
    only lets requests from this exact repo+branch assume it.

  * WeatherPipeline — the actual data pipeline (EventBridge Scheduler,
    Step Functions, Lambda, S3, Glue, Athena, CloudWatch). Everything
    after the first bootstrap deploys via GitHub Actions using the
    role above — no AWS access keys ever touch the repo.
"""
import os

import aws_cdk as cdk

from stacks.github_oidc_stack import GitHubOidcStack
from stacks.pipeline_stack import WeatherPipelineStack

app = cdk.App()

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION", "ap-southeast-2"),
)

github_owner = app.node.try_get_context("github_owner") or "CHANGE_ME"
github_repo = app.node.try_get_context("github_repo") or "serverless-weather-pipeline"
github_branch = app.node.try_get_context("github_branch") or "main"
existing_oidc_provider_arn = app.node.try_get_context("existing_oidc_provider_arn") or None
alert_email = app.node.try_get_context("alert_email") or None

GitHubOidcStack(
    app,
    "WeatherPipeline-GitHubOidc",
    github_owner=github_owner,
    github_repo=github_repo,
    github_branch=github_branch,
    existing_oidc_provider_arn=existing_oidc_provider_arn,
    env=env,
    description="GitHub Actions OIDC provider + deploy role for the weather pipeline repo.",
)

WeatherPipelineStack(
    app,
    "WeatherPipeline",
    alert_email=alert_email,
    env=env,
    description="Serverless weather ingestion pipeline: EventBridge Scheduler -> Step Functions -> Lambda -> S3 -> Glue/Athena.",
)

app.synth()
