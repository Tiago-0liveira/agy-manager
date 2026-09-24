"""Human-readable Markdown artifacts for orchestration runs.

Artifacts mirror successful reasoning outputs for inspection. They never replace
RunState, invocation records, or event logs as authoritative execution state.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agym.orchestration.contracts import (
    AuditRequest,
    AuditResult,
    ExecutionStrategy,
    InvocationStatus,
    RunId,
    WorkerRequest,
    WorkerResult,
    WorkerRole,
)
from agym.orchestration.persistence import atomic_write_text

__all__ = ["ArtifactWriter", "safe_artifact_filename"]


_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_artifact_filename(value: str, *, default: str = "artifact") -> str:
    """Return a traversal-safe Markdown filename stem."""
    raw = str(value or "").strip().replace("\\", "-").replace("/", "-")
    raw = raw.replace("..", "-")
    raw = _SAFE_FILENAME_RE.sub("-", raw).strip(" .-_")
    if not raw:
        raw = default
    return raw[:80]


class ArtifactWriter:
    """Writes readable worker/audit/synthesis artifacts below one run directory."""

    def __init__(self, run_store: Any) -> None:
        self.run_store = run_store

    def _run_dir(self, run_id: RunId | str) -> Path:
        if not hasattr(self.run_store, "run_dir"):
            raise RuntimeError("Run store does not expose a filesystem run directory")
        return Path(self.run_store.run_dir(run_id)).resolve()

    def _write(self, run_id: RunId | str, relative_path: Path, text: str) -> Path:
        root = self._run_dir(run_id)
        destination = (root / relative_path).resolve()
        if not destination.is_relative_to(root):
            raise ValueError("Artifact path escaped the run directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(destination, text)
        return destination

    @staticmethod
    def _metadata(
        *,
        worker_id: str,
        role: str,
        round_number: int,
        strategy: str,
        status: str,
        response: str,
    ) -> str:
        return "\n".join([
            "---",
            f"Worker: {worker_id}",
            f"Role: {role}",
            f"Round: {round_number}",
            f"Strategy: {strategy}",
            f"Status: {status}",
            "---",
            "",
            response.rstrip(),
            "",
        ])

    def write_result(
        self,
        run_id: RunId | str,
        result: WorkerResult | AuditResult,
        request: WorkerRequest | AuditRequest | None,
        *,
        round_number: int,
        synthesis_version: int | None = None,
        critique_version: int | None = None,
    ) -> Path:
        """Persist a successful result to its deterministic Markdown location."""
        worker_id = safe_artifact_filename(str(result.worker_id), default="worker")
        if isinstance(result, AuditResult):
            role = WorkerRole.AUDITOR.value
        else:
            role = result.role.value
        strategy_obj = getattr(request, "strategy", ExecutionStrategy.STANDARD)
        strategy = strategy_obj.value if hasattr(strategy_obj, "value") else str(strategy_obj)
        status_obj = getattr(result, "status", InvocationStatus.SUCCEEDED)
        status = status_obj.value if hasattr(status_obj, "value") else str(status_obj)
        body = self._metadata(
            worker_id=str(result.worker_id),
            role=role,
            round_number=round_number,
            strategy=strategy,
            status=status,
            response=getattr(result, "response", "") or "",
        )

        if synthesis_version is not None:
            relative = Path("artifacts") / "synthesis" / f"v{synthesis_version}.md"
        elif critique_version is not None:
            relative = Path("artifacts") / "synthesis" / f"critique-v{critique_version}.md"
        else:
            role_slug = safe_artifact_filename(role.lower().replace("_", "-"), default="worker")
            relative = (
                Path("artifacts")
                / f"wave-{round_number:02d}"
                / f"{role_slug}-{worker_id}.md"
            )
        return self._write(run_id, relative, body)
