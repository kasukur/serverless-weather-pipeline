"""Write the transformed JSON-Lines body to S3.

This exists because Step Functions' native SDK integration for
s3:putObject (`CallAwsService`) doesn't reliably carry a raw multi-line
string as binary content -- S3's `Body` parameter is a blob type, and
Step Functions' JSON-based state language has no native bytes
representation. In testing, it wrote the JSON-*string-encoded* form of
the body (quotes, escaped `\"`, escaped `\n`) as the literal file
content instead of the raw text -- every "row" then failed to parse in
Athena. A plain boto3 call sidesteps the ambiguity entirely.
"""
import boto3

s3 = boto3.client("s3")


def handler(event, context):
    bucket = event["bucket"]
    key = event["key"]
    body = event["body"]

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )

    return {"bucket": bucket, "key": key}
