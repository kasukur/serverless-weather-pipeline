"""GitHub Actions OIDC trust: lets `deploy.yml` assume an AWS role for
exactly this repo + branch, with no long-lived AWS access keys stored
anywhere. Deploy this stack once, locally, with your own AWS credentials:

    cdk deploy WeatherPipeline-GitHubOidc \\
        --context github_owner=<your-github-username-or-org> \\
        --context github_repo=<your-repo-name> \\
        --context github_branch=main

Then take the DeployRoleArn output and put it in the GitHub repo's
Settings -> Secrets and variables -> Actions -> Variables (NOT Secrets --
an IAM role ARN isn't sensitive on its own; the security boundary is the
trust policy's `sub` condition below, which only lets this exact
repo+branch assume it).
"""
from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

# The default AWS-managed thumbprint list for GitHub's OIDC issuer.
# CDK's OpenIdConnectProvider will fetch/verify this itself; we don't
# need to hardcode it.
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_AUDIENCE = "sts.amazonaws.com"

# Default CDK bootstrap qualifier. If you bootstrapped with a custom
# --qualifier, change this to match.
CDK_QUALIFIER = "hnb659fds"


class GitHubOidcStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        github_owner: str,
        github_repo: str,
        github_branch: str = "main",
        existing_oidc_provider_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if existing_oidc_provider_arn:
            # Most AWS accounts can only have ONE OIDC provider per
            # issuer URL. If a previous project already registered
            # GitHub's provider, import it instead of re-creating it.
            provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
                self, "GitHubOidcProvider", existing_oidc_provider_arn
            )
            provider_arn = existing_oidc_provider_arn
        else:
            provider = iam.OpenIdConnectProvider(
                self,
                "GitHubOidcProvider",
                url=GITHUB_OIDC_ISSUER,
                client_ids=[GITHUB_OIDC_AUDIENCE],
            )
            provider_arn = provider.open_id_connect_provider_arn

        # Restrict *which* GitHub workflow runs may assume this role:
        # only pushes to github_branch in github_owner/github_repo.
        # See https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect
        sub_condition = f"repo:{github_owner}/{github_repo}:ref:refs/heads/{github_branch}"

        deploy_role = iam.Role(
            self,
            "GitHubActionsDeployRole",
            assumed_by=iam.FederatedPrincipal(
                provider_arn,
                conditions={
                    "StringEquals": {f"{GITHUB_OIDC_ISSUER.removeprefix('https://')}:aud": GITHUB_OIDC_AUDIENCE},
                    "StringLike": {f"{GITHUB_OIDC_ISSUER.removeprefix('https://')}:sub": sub_condition},
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
            max_session_duration=Duration.hours(1),
            description=(
                "Assumed by GitHub Actions via OIDC to deploy the weather pipeline. "
                f"Trust is restricted to {sub_condition}. No static AWS keys involved."
            ),
        )

        # --- Least-privilege CDK deploy permissions -----------------
        # `cdk deploy` doesn't need this role to have S3/Lambda/etc.
        # permissions directly -- the CDK bootstrap roles (created by
        # `cdk bootstrap`) do the actual work. This role only needs to:
        #   1. drive CloudFormation for stacks named WeatherPipeline*
        #   2. assume the bootstrap file-publishing/lookup/deploy roles
        #   3. pass the bootstrap cfn-exec-role to CloudFormation
        #   4. read the bootstrap version SSM parameter (the CLI checks it)
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudFormationStackOperations",
                actions=[
                    "cloudformation:CreateStack",
                    "cloudformation:UpdateStack",
                    "cloudformation:DeleteStack",
                    "cloudformation:DescribeStacks",
                    "cloudformation:DescribeStackEvents",
                    "cloudformation:DescribeStackResource",
                    "cloudformation:DescribeStackResources",
                    "cloudformation:GetTemplate",
                    "cloudformation:GetTemplateSummary",
                    "cloudformation:CreateChangeSet",
                    "cloudformation:DescribeChangeSet",
                    "cloudformation:ExecuteChangeSet",
                    "cloudformation:DeleteChangeSet",
                    "cloudformation:ListStackResources",
                ],
                resources=[f"arn:aws:cloudformation:*:{self.account}:stack/WeatherPipeline*/*"],
            )
        )
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeCdkBootstrapRoles",
                actions=["sts:AssumeRole"],
                resources=[
                    f"arn:aws:iam::{self.account}:role/cdk-{CDK_QUALIFIER}-file-publishing-role-{self.account}-*",
                    f"arn:aws:iam::{self.account}:role/cdk-{CDK_QUALIFIER}-lookup-role-{self.account}-*",
                    f"arn:aws:iam::{self.account}:role/cdk-{CDK_QUALIFIER}-deploy-role-{self.account}-*",
                ],
            )
        )
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="PassCloudFormationExecutionRole",
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{self.account}:role/cdk-{CDK_QUALIFIER}-cfn-exec-role-{self.account}-*"],
            )
        )
        deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadBootstrapVersion",
                actions=["ssm:GetParameter"],
                resources=[f"arn:aws:ssm:*:{self.account}:parameter/cdk-bootstrap/{CDK_QUALIFIER}/version"],
            )
        )

        CfnOutput(
            self,
            "DeployRoleArn",
            value=deploy_role.role_arn,
            description=(
                "Add this as a GitHub repo VARIABLE named AWS_DEPLOY_ROLE_ARN "
                "(Settings -> Secrets and variables -> Actions -> Variables tab). "
                "It is not a secret."
            ),
        )
        CfnOutput(self, "OidcProviderArn", value=provider_arn)
