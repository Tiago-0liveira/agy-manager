"""Security, Origin validation, Session authentication, and Idempotency middleware for AGYM Council API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from agym.council.storage import (
    complete_idempotency_record,
    get_connection,
    get_idempotency_record,
    start_idempotency_record,
)
from agym.profiles import _default_data_root

# ---------------------------------------------------------------------------
# Session Token Management
# ---------------------------------------------------------------------------

_SESSION_TOKEN: str | None = None


def get_or_create_session_token() -> str:
    """Return the active session token, generating and caching one if needed."""
    global _SESSION_TOKEN
    env_token = os.environ.get("AGYM_COUNCIL_SESSION_TOKEN")
    if env_token:
        _SESSION_TOKEN = env_token
        return _SESSION_TOKEN

    if _SESSION_TOKEN is None:
        token_path = _default_data_root() / "council" / ".session_token"
        try:
            if token_path.is_file():
                _SESSION_TOKEN = token_path.read_text(encoding="utf-8").strip()
            if not _SESSION_TOKEN:
                _SESSION_TOKEN = secrets.token_urlsafe(32)
                token_path.parent.mkdir(parents=True, exist_ok=True)
                token_path.write_text(_SESSION_TOKEN, encoding="utf-8")
                if os.name != "nt":
                    token_path.chmod(0o600)
        except Exception:
            _SESSION_TOKEN = secrets.token_urlsafe(32)
    return _SESSION_TOKEN


def verify_session_token(token: str | None) -> bool:
    """Verify provided session token against active session token."""
    if os.environ.get("AGYM_COUNCIL_NO_AUTH") == "1":
        return True
    active = get_or_create_session_token()
    if not token or not isinstance(token, str):
        return False
    return hmac.compare_digest(token.strip(), active.strip())


# ---------------------------------------------------------------------------
# Loopback & Origin Validation
# ---------------------------------------------------------------------------

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}


def is_loopback_host(host_header: str | None) -> bool:
    """Check if the Host header points to a loopback interface."""
    if not host_header:
        return True
    host_str = host_header.strip()
    if host_str.startswith("["):
        # IPv6 literal format: [::1] or [::1]:8000
        end_bracket = host_str.find("]")
        if end_bracket != -1:
            host_clean = host_str[1:end_bracket]
        else:
            host_clean = host_str.strip("[]")
    else:
        host_clean = host_str.split(":")[0]
    return host_clean.lower() in LOOPBACK_HOSTS


def is_valid_origin(origin_header: str | None) -> bool:
    """Check if the Origin header belongs to an authorized loopback address."""
    if not origin_header:
        # Direct requests without Origin (e.g. curl, python requests, native GUI) are allowed
        return True
    try:
        parsed = urlparse(origin_header)
        hostname = (parsed.hostname or "").lower().strip("[]")
        return hostname in LOOPBACK_HOSTS
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Middleware: Security & Idempotency
# ---------------------------------------------------------------------------


class CouncilSecurityMiddleware(BaseHTTPMiddleware):
    """Enforces loopback binding, origin verification, and session token checks."""

    def __init__(self, app: Any, db_path: Path | str | None = None) -> None:
        super().__init__(app)
        self.db_path = db_path

    async def dispatch(self, request: Request, call_next: Callable[[Request], Any]) -> Response:
        # 1. Host header validation (DNS rebinding protection)
        host = request.headers.get("host")
        if not is_loopback_host(host):
            return JSONResponse(
                status_code=403,
                content={"detail": f"Forbidden: Host '{host}' is not a permitted loopback interface."},
            )

        # 2. Origin header validation (Anti-CSRF from external websites)
        origin = request.headers.get("origin")
        if not is_valid_origin(origin):
            return JSONResponse(
                status_code=403,
                content={"detail": f"Forbidden: Origin '{origin}' is not an authorized loopback origin."},
            )

        # 3. Session token check on mutating state methods
        path = request.url.path
        method = request.method.upper()

        # Exempt paths from session check
        exempt_paths = {
            "/api/session",
            "/api/health",
            "/api/version",
            "/docs",
            "/redoc",
            "/openapi.json",
        }
        # Static files and frontend UI are also exempt from mutation checks
        if not path.startswith("/api/"):
            return await call_next(request)

        if method in ("POST", "PATCH", "PUT", "DELETE") and path not in exempt_paths:
            # Check X-Council-Session header
            token = request.headers.get("X-Council-Session")
            if not verify_session_token(token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized: Invalid or missing X-Council-Session token header."},
                )

        return await call_next(request)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Enforces request idempotency on mutating operations using Idempotency-Key."""

    def __init__(self, app: Any, db_path: Path | str | None = None) -> None:
        super().__init__(app)
        self.db_path = db_path

    async def dispatch(self, request: Request, call_next: Callable[[Request], Any]) -> Response:
        method = request.method.upper()
        if method not in ("POST", "PATCH", "PUT"):
            return await call_next(request)

        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key or not idempotency_key.strip():
            # If no idempotency key provided, proceed normally
            return await call_next(request)

        idempotency_key = idempotency_key.strip()
        body_bytes = await request.body()
        path = request.url.path
        request_hash = hashlib.sha256(f"{method}:{path}:{body_bytes.decode('utf-8', errors='replace')}".encode()).hexdigest()

        conn = get_connection(self.db_path)
        try:
            # Check existing idempotency record
            record = get_idempotency_record(conn, idempotency_key)
            if record:
                saved_hash = str(record["request_hash"])
                if saved_hash != request_hash:
                    return JSONResponse(
                        status_code=409,
                        content={"detail": f"Idempotency-Key '{idempotency_key}' collision with different request payload."},
                    )
                record_status = str(record["status"] or "")
                if record_status == "IN_PROGRESS":
                    return JSONResponse(
                        status_code=409,
                        content={"detail": f"Request with Idempotency-Key '{idempotency_key}' is currently in progress."},
                    )

                saved_body_raw = str(record["response_body"] or "")
                saved_status = int(record["response_status_code"] or 200)
                saved_headers_json = record["response_headers_json"]

                headers_dict = {"X-Idempotent-Replay": "true"}
                media_type = "application/json"
                is_binary = False

                if saved_headers_json:
                    try:
                        meta = json.loads(saved_headers_json)
                        if isinstance(meta, dict):
                            orig_headers = meta.get("headers", {})
                            for k, v in orig_headers.items():
                                if k.lower() not in ("content-length", "content-encoding", "transfer-encoding"):
                                    headers_dict[k] = v
                            media_type = meta.get("media_type") or orig_headers.get("content-type", "application/json")
                            is_binary = meta.get("is_binary", False)
                    except Exception:
                        pass

                if is_binary:
                    content_bytes = base64.b64decode(saved_body_raw)
                    return Response(
                        content=content_bytes,
                        status_code=saved_status,
                        media_type=media_type,
                        headers=headers_dict,
                    )
                else:
                    return Response(
                        content=saved_body_raw,
                        status_code=saved_status,
                        media_type=media_type,
                        headers=headers_dict,
                    )

            # Record in-flight attempt
            start_idempotency_record(conn, key=idempotency_key, action=path, request_hash=request_hash)

            # Execute downstream handler
            response = await call_next(request)

            # Read response body to cache it
            response_body_bytes = b""
            async for chunk in response.body_iterator:
                response_body_bytes += chunk if isinstance(chunk, bytes) else chunk.encode()

            # Filter and serialize headers
            resp_headers = dict(response.headers)
            content_type = resp_headers.get("content-type", "")

            is_binary = False
            try:
                body_text = response_body_bytes.decode("utf-8")
            except UnicodeDecodeError:
                is_binary = True
                body_text = base64.b64encode(response_body_bytes).decode("ascii")

            headers_meta = {
                "headers": resp_headers,
                "is_binary": is_binary,
                "media_type": response.media_type or content_type,
            }

            # Save completed record
            complete_idempotency_record(
                conn,
                key=idempotency_key,
                status_code=response.status_code,
                body=body_text,
                headers_json=json.dumps(headers_meta),
            )

            # Return reconstructed response
            return Response(
                content=response_body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )
        finally:
            conn.close()
