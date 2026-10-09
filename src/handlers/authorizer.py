"""Lambda authorizer for the HTTP API: the X-Read-Passcode header must match the shared
passcode in SSM Parameter Store (SecureString, PASSCODE_PARAM). Simple responses (D90)."""

import hmac
import os
import time

import boto3

CACHE_SECONDS = 300
_cache = {"value": None, "at": 0.0}
_ssm = None


def passcode() -> str:
    global _ssm
    if _cache["value"] is None or time.monotonic() - _cache["at"] > CACHE_SECONDS:
        _ssm = _ssm or boto3.client("ssm")
        res = _ssm.get_parameter(Name=os.environ["PASSCODE_PARAM"], WithDecryption=True)
        _cache.update(value=res["Parameter"]["Value"], at=time.monotonic())
    return _cache["value"]


def check(given: str | None, expected: str) -> bool:
    return bool(given) and bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def handler(event: dict, context) -> dict:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return {"isAuthorized": check(headers.get("x-read-passcode"), passcode())}
