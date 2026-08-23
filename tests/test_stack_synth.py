import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from stacks.github_oidc_stack import GitHubOidcStack
from stacks.pipeline_stack import WeatherPipelineStack


def _synth_pipeline() -> Template:
    app = cdk.App()
    stack = WeatherPipelineStack(app, "TestWeatherPipeline")
    return Template.from_stack(stack)


def test_pipeline_creates_exactly_one_state_machine():
    _synth_pipeline().resource_count_is("AWS::StepFunctions::StateMachine", 1)


def test_pipeline_creates_our_three_lambdas_plus_cdks_auto_delete_helper():
    # fetch, transform, and load are ours -- load replaced a Step
    # Functions native s3:putObject SDK integration that turned out to
    # corrupt multi-line string bodies (see lambdas/load/handler.py).
    # The 4th is CDK's own auto-generated custom-resource Lambda that
    # empties the bucket on stack deletion (from auto_delete_objects=True
    # on WeatherDataBucket) -- not something this app defines directly.
    _synth_pipeline().resource_count_is("AWS::Lambda::Function", 4)


def test_pipeline_creates_hourly_schedule():
    template = _synth_pipeline()
    template.resource_count_is("AWS::Scheduler::Schedule", 1)
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        Match.object_like({"ScheduleExpression": "rate(1 hour)"}),
    )


def test_pipeline_creates_glue_table_with_partition_projection():
    template = _synth_pipeline()
    template.has_resource_properties(
        "AWS::Glue::Table",
        Match.object_like(
            {
                "TableInput": Match.object_like(
                    {"Parameters": Match.object_like({"projection.enabled": "true"})}
                )
            }
        ),
    )


def test_pipeline_bucket_blocks_public_access():
    template = _synth_pipeline()
    template.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like(
            {
                "PublicAccessBlockConfiguration": Match.object_like(
                    {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                    }
                )
            }
        ),
    )


def test_oidc_role_trust_is_scoped_to_repo_and_branch():
    app = cdk.App()
    stack = GitHubOidcStack(
        app,
        "TestOidc",
        github_owner="my-org",
        github_repo="serverless-weather-pipeline",
        github_branch="main",
    )
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::IAM::Role",
        Match.object_like(
            {
                "AssumeRolePolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Action": "sts:AssumeRoleWithWebIdentity",
                                        "Condition": Match.object_like(
                                            {
                                                "StringLike": Match.object_like(
                                                    {
                                                        # GitHub's sub claim appends each side's
                                                        # immutable numeric ID (e.g. "kasukur@1291877"),
                                                        # so only the "@*" suffix is wildcarded here --
                                                        # the owner/repo names themselves stay pinned.
                                                        "token.actions.githubusercontent.com:sub": (
                                                            "repo:my-org@*/serverless-weather-pipeline@*:"
                                                            "ref:refs/heads/main"
                                                        )
                                                    }
                                                )
                                            }
                                        ),
                                    }
                                )
                            ]
                        )
                    }
                )
            }
        ),
    )


def test_oidc_stack_has_no_static_credentials_anywhere_in_template():
    app = cdk.App()
    stack = GitHubOidcStack(
        app,
        "TestOidc2",
        github_owner="my-org",
        github_repo="serverless-weather-pipeline",
        github_branch="main",
    )
    template_json = Template.from_stack(stack).to_json()
    rendered = str(template_json)
    for forbidden in ("AKIA", "aws_secret_access_key", "AWS_SECRET_ACCESS_KEY"):
        assert forbidden not in rendered
