import importlib.util
import pathlib
import sys
from unittest.mock import MagicMock, patch

# Loaded under a unique module name -- see the comment in test_transform.py
# for why (multiple Lambdas' handler files are all named `handler.py`).
_HANDLER_PATH = pathlib.Path(__file__).resolve().parents[1] / "lambdas" / "load" / "handler.py"


def _load_handler_module():
    spec = importlib.util.spec_from_file_location("load_handler", _HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch("boto3.client") as mock_boto_client:
        mock_boto_client.return_value = MagicMock()
        spec.loader.exec_module(module)
    return module


def test_handler_calls_put_object_with_utf8_encoded_body():
    module = _load_handler_module()
    module.s3 = MagicMock()

    event = {
        "bucket": "my-bucket",
        "key": "processed/dt=2026-08-23/hour=11/run-1.jsonl",
        "body": '{"city": "Sydney"}\n{"city": "Melbourne"}',
    }
    result = module.handler(event, None)

    module.s3.put_object.assert_called_once_with(
        Bucket="my-bucket",
        Key="processed/dt=2026-08-23/hour=11/run-1.jsonl",
        Body=event["body"].encode("utf-8"),
        ContentType="application/json",
    )
    assert result == {"bucket": "my-bucket", "key": event["key"]}


def test_handler_preserves_real_newlines_in_body_bytes():
    """Regression test: this Lambda exists specifically because a prior
    approach (Step Functions' native s3:putObject SDK integration)
    ended up writing the JSON-string-escaped form of the body -- quotes
    and "\\n" as two literal characters -- instead of raw bytes with
    real newline characters. Confirm the bytes we hand to boto3 contain
    actual newline bytes, not escaped text.
    """
    module = _load_handler_module()
    module.s3 = MagicMock()

    body = '{"city": "Sydney"}\n{"city": "Melbourne"}'
    module.handler({"bucket": "b", "key": "k", "body": body}, None)

    sent_body = module.s3.put_object.call_args.kwargs["Body"]
    assert sent_body.count(b"\n") == 1  # one real newline byte between the two records
    assert b"\\n" not in sent_body  # not the two-character escape sequence
    assert not sent_body.startswith(b'"')  # not JSON-string-encoded
