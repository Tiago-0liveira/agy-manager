from __future__ import annotations


class IntegrationError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 retry_after_seconds: int | None = None, run_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.run_id = run_id


RUN_NOT_FOUND = "RUN_NOT_FOUND"
WORKSPACE_BUSY = "WORKSPACE_BUSY"
PROFILE_UNAVAILABLE = "PROFILE_UNAVAILABLE"
NO_CAPACITY = "NO_CAPACITY"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
UNSUPPORTED_PROTOCOL = "UNSUPPORTED_PROTOCOL"
INVALID_REQUEST = "INVALID_REQUEST"
INTERNAL_ERROR = "INTERNAL_ERROR"
