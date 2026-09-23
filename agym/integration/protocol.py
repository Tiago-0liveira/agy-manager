from __future__ import annotations

import json
import sys
from typing import Any

from .errors import IntegrationError, UNSUPPORTED_PROTOCOL

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
MAX_EVENT_BYTES = 256 * 1024
MAX_PAGE_EVENTS = 200


def version() -> dict[str, int]:
    return {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR}


def success(data: Any) -> dict[str, Any]:
    return {"protocol": version(), "ok": True, "data": data}


def failure(code: str, message: str, *, retryable: bool = False,
            retry_after_seconds: int | None = None, run_id: str | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    if retry_after_seconds is not None:
        error["retry_after_seconds"] = retry_after_seconds
    if run_id is not None:
        error["run_id"] = run_id
    return {"protocol": version(), "ok": False, "error": error}


def validate_protocol(value: int | None) -> None:
    if value != PROTOCOL_MAJOR:
        raise IntegrationError(UNSUPPORTED_PROTOCOL, "Unsupported integration protocol")


def write_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def write_ndjson(value: Any) -> None:
    write_json(value)
