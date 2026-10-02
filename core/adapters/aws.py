"""AWS adapter — boto3 when available, ``aws`` CLI as fallback.

Capabilities exposed as tools:

- ``aws_s3_list``         list bucket objects (prefix, max).
- ``aws_s3_get``          download an object as text (utf-8).
- ``aws_s3_put``          upload text content to an object.
- ``aws_s3_delete``       delete an object.
- ``aws_ec2_list``        describe EC2 instances (filterable).
- ``aws_lambda_invoke``   sync invoke (RequestResponse).
- ``aws_logs_tail``       tail recent CloudWatch log events.
- ``aws_status``          identity + region snapshot.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

from .base import AdapterError, BaseAdapter

try:
    import boto3 as _boto3  # type: ignore
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
except Exception:  # noqa: BLE001
    _boto3 = None  # type: ignore
    BotoCoreError = Exception  # type: ignore
    ClientError = Exception  # type: ignore


class AWSAdapter(BaseAdapter):
    NAME = "aws"
    KEYSTORE_NAMES = ["aws", "AWS_ACCESS_KEY_ID"]
    ENV_KEYS = [
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_REGION", "AWS_DEFAULT_REGION",
    ]
    REQUIRED: List[str] = []
    CLI_FALLBACK = True
    CLI_BIN = "aws"
    RATE_PER_SEC = 8.0
    RATE_BURST = 20

    # ------------------------------------------------------------------
    def is_ready(self) -> bool:
        if self.cred("AWS_ACCESS_KEY_ID"):
            return True
        if self._cli_ok:
            return True
        return False

    @property
    def region(self) -> str:
        return (
            self.cred("AWS_REGION") or self.cred("AWS_DEFAULT_REGION")
            or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        )

    def _client(self, service: str) -> Any:
        if _boto3 is None:
            raise AdapterError("aws: boto3 غير مثبّت")
        try:
            return _boto3.client(
                service,
                region_name=self.region,
                aws_access_key_id=self.cred("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=self.cred("AWS_SECRET_ACCESS_KEY"),
                aws_session_token=self.cred("AWS_SESSION_TOKEN"),
            )
        except (BotoCoreError, ClientError) as exc:  # noqa: BLE001
            raise AdapterError(f"aws: client init failed: {exc}") from exc

    # ------------------------------------------------------------------
    # CLI fallback
    # ------------------------------------------------------------------
    def _aws_cli(self, *args: str) -> Any:
        if not self._cli_ok:
            raise AdapterError("aws: aws CLI غير متاح")
        cp = subprocess.run(
            ["aws", *args, "--output", "json"],
            capture_output=True, text=True, timeout=120,
        )
        if cp.returncode != 0:
            raise AdapterError(f"aws cli failed: {cp.stderr.strip()[:300]}")
        text = cp.stdout.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text[:1000]}

    # ------------------------------------------------------------------
    # S3
    # ------------------------------------------------------------------
    def s3_list(self, bucket: str, prefix: str = "",
                max_keys: int = 100) -> List[Dict[str, Any]]:
        if _boto3 is not None and self.cred("AWS_ACCESS_KEY_ID"):
            def _do() -> List[Dict[str, Any]]:
                cli = self._client("s3")
                resp = cli.list_objects_v2(
                    Bucket=bucket, Prefix=prefix, MaxKeys=min(max_keys, 1000),
                )
                return [
                    {
                        "key": o.get("Key"),
                        "size": o.get("Size"),
                        "last_modified": str(o.get("LastModified", "")),
                    }
                    for o in resp.get("Contents", [])
                ]
            return self._call("s3_list", _do)
        # CLI fallback
        out = self._call("s3_list_cli", self._aws_cli,
                         "s3api", "list-objects-v2",
                         "--bucket", bucket,
                         "--prefix", prefix,
                         "--max-items", str(max_keys))
        return out.get("Contents", []) if isinstance(out, dict) else []

    def s3_get(self, bucket: str, key: str) -> Dict[str, Any]:
        if _boto3 is not None and self.cred("AWS_ACCESS_KEY_ID"):
            def _do() -> Dict[str, Any]:
                cli = self._client("s3")
                resp = cli.get_object(Bucket=bucket, Key=key)
                body = resp["Body"].read()
                try:
                    text = body.decode("utf-8")
                except Exception:
                    text = body[:4096].hex()
                return {
                    "key": key, "size": len(body),
                    "content": text[:200_000],
                    "content_type": resp.get("ContentType", ""),
                }
            return self._call("s3_get", _do)
        raise AdapterError("aws s3_get: يحتاج boto3 + مفاتيح API")

    def s3_put(self, bucket: str, key: str, content: str,
               content_type: str = "text/plain") -> Dict[str, Any]:
        if _boto3 is not None and self.cred("AWS_ACCESS_KEY_ID"):
            def _do() -> Dict[str, Any]:
                cli = self._client("s3")
                cli.put_object(
                    Bucket=bucket, Key=key,
                    Body=content.encode("utf-8"), ContentType=content_type,
                )
                return {"uploaded": True, "key": key}
            return self._call("s3_put", _do)
        raise AdapterError("aws s3_put: يحتاج boto3 + مفاتيح API")

    def s3_delete(self, bucket: str, key: str) -> Dict[str, Any]:
        if _boto3 is not None and self.cred("AWS_ACCESS_KEY_ID"):
            def _do() -> Dict[str, Any]:
                cli = self._client("s3")
                cli.delete_object(Bucket=bucket, Key=key)
                return {"deleted": True, "key": key}
            return self._call("s3_delete", _do)
        raise AdapterError("aws s3_delete: يحتاج boto3 + مفاتيح API")

    # ------------------------------------------------------------------
    # EC2
    # ------------------------------------------------------------------
    def ec2_list(self, *, name_filter: str = "",
                 state: str = "") -> List[Dict[str, Any]]:
        if _boto3 is not None and self.cred("AWS_ACCESS_KEY_ID"):
            def _do() -> List[Dict[str, Any]]:
                cli = self._client("ec2")
                filters = []
                if state:
                    filters.append({"Name": "instance-state-name", "Values": [state]})
                if name_filter:
                    filters.append({"Name": "tag:Name", "Values": [f"*{name_filter}*"]})
                resp = cli.describe_instances(
                    Filters=filters if filters else None,
                )
                out: List[Dict[str, Any]] = []
                for r in resp.get("Reservations", []):
                    for inst in r.get("Instances", []):
                        out.append({
                            "id": inst.get("InstanceId"),
                            "type": inst.get("InstanceType"),
                            "state": (inst.get("State") or {}).get("Name"),
                            "public_ip": inst.get("PublicIpAddress"),
                            "private_ip": inst.get("PrivateIpAddress"),
                        })
                return out
            return self._call("ec2_list", _do)
        raise AdapterError("aws ec2_list: يحتاج boto3 + مفاتيح API")

    # ------------------------------------------------------------------
    # Lambda
    # ------------------------------------------------------------------
    def lambda_invoke(self, function_name: str,
                      payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if _boto3 is not None and self.cred("AWS_ACCESS_KEY_ID"):
            def _do() -> Dict[str, Any]:
                cli = self._client("lambda")
                resp = cli.invoke(
                    FunctionName=function_name,
                    InvocationType="RequestResponse",
                    Payload=json.dumps(payload or {}).encode("utf-8"),
                )
                body = resp["Payload"].read().decode("utf-8", errors="replace")
                return {
                    "status_code": resp.get("StatusCode"),
                    "body": body[:200_000],
                    "function_error": resp.get("FunctionError"),
                }
            return self._call("lambda_invoke", _do)
        raise AdapterError("aws lambda_invoke: يحتاج boto3 + مفاتيح API")

    # ------------------------------------------------------------------
    # CloudWatch logs
    # ------------------------------------------------------------------
    def logs_tail(self, log_group: str, *, minutes: int = 15,
                  limit: int = 200, filter_pattern: str = "") -> List[Dict[str, Any]]:
        if _boto3 is not None and self.cred("AWS_ACCESS_KEY_ID"):
            def _do() -> List[Dict[str, Any]]:
                cli = self._client("logs")
                start_ms = int((time.time() - minutes * 60) * 1000)
                resp = cli.filter_log_events(
                    logGroupName=log_group,
                    startTime=start_ms,
                    limit=min(limit, 1000),
                    filterPattern=filter_pattern or "",
                )
                return [
                    {
                        "ts": e.get("timestamp"),
                        "stream": e.get("logStreamName"),
                        "message": (e.get("message") or "")[:1500],
                    }
                    for e in resp.get("events", [])
                ]
            return self._call("logs_tail", _do)
        raise AdapterError("aws logs_tail: يحتاج boto3 + مفاتيح API")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status_call(self) -> Dict[str, Any]:
        if _boto3 is not None and self.cred("AWS_ACCESS_KEY_ID"):
            try:
                cli = self._client("sts")
                ident = cli.get_caller_identity()
                return {
                    "authenticated": True,
                    "account": ident.get("Account"),
                    "arn": ident.get("Arn"),
                    "region": self.region,
                    "transport": "boto3",
                }
            except Exception as exc:  # noqa: BLE001
                return {"authenticated": False, "error": str(exc)}
        if self._cli_ok:
            try:
                ident = self._aws_cli("sts", "get-caller-identity")
                return {
                    "authenticated": True,
                    "account": ident.get("Account"),
                    "arn": ident.get("Arn"),
                    "region": self.region,
                    "transport": "cli",
                }
            except Exception as exc:  # noqa: BLE001
                return {"authenticated": False, "error": str(exc)}
        return {"authenticated": False, "reason": "no boto3, no cli"}

    # ------------------------------------------------------------------
    def tools_for(self) -> Dict[str, Callable[..., Any]]:
        return {
            "aws_s3_list": self.s3_list,
            "aws_s3_get": self.s3_get,
            "aws_s3_put": self.s3_put,
            "aws_s3_delete": self.s3_delete,
            "aws_ec2_list": self.ec2_list,
            "aws_lambda_invoke": self.lambda_invoke,
            "aws_logs_tail": self.logs_tail,
            "aws_status": self.status_call,
        }


__all__ = ["AWSAdapter"]
