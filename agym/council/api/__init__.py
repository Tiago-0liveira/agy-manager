"""AGYM Council Local API package."""

from __future__ import annotations

__all__ = ["app"]

try:
    from agym.council.api.app import app
except ImportError:
    app = None  # type: ignore[assignment]
