"""agym.council - Multi-account, multi-worker orchestration platform for Google Antigravity CLI."""

from __future__ import annotations

from agym import __version__

__all__ = ["__version__", "check_council_dependencies"]


def check_council_dependencies() -> tuple[bool, list[str]]:
    """Check whether optional dependencies for council are installed.

    Returns:
        A tuple of (all_installed: bool, missing_packages: list[str]).
    """
    missing: list[str] = []
    for pkg in ("fastapi", "uvicorn", "pydantic", "aiofiles"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    return len(missing) == 0, missing
