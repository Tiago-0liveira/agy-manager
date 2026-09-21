"""Run dossier exporter with secret redaction and traversal protection."""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from agym.council.artifacts import ArtifactStore, validate_safe_relative_path
from agym.council.models import redact_secrets, scrub_sensitive_strings
from agym.council.storage import (
    get_connection,
    get_run,
    get_stages_for_run,
    get_workers_for_run,
    list_artifacts_for_run,
    list_events_for_run,
    list_issues_for_run,
)


def sanitize_workflow_for_export(config_data: dict[str, Any]) -> dict[str, Any]:
    """Produce a portable workflow configuration suitable for export to fresh installations.

    Role and stage definitions survive intact, but account bindings and credentials
    require local assignment on the destination machine.
    """
    exported = json.loads(json.dumps(config_data))
    exported["draft"] = True

    # Strip local account references
    workers = exported.get("workers", [])
    for idx, w in enumerate(workers):
        if isinstance(w, dict):
            w["account_ref"] = f"<assign-local-profile-{idx+1}>"
            if not w.get("model") or not w.get("model").startswith("<"):
                w["model"] = "<choose-discovered-model>"

    return exported


def export_run_dossier_bytes(
    conn: sqlite3.Connection,
    run_id: str,
    redact: bool = True,
    artifact_store_root: Path | str | None = None,
) -> bytes:
    """Generate a ZIP dossier containing run records, summary, portable template, and released artifacts.

    Args:
        conn: SQLite connection.
        run_id: Run identifier.
        redact: Whether to strip sensitive tokens and local account references.
        artifact_store_root: Optional root directory of artifact store.

    Returns:
        ZIP archive bytes.
    """
    run_row = get_run(conn, run_id)
    if not run_row:
        raise ValueError(f"Run '{run_id}' not found.")

    config_raw = run_row["config_json"]
    try:
        config_data = json.loads(config_raw) if config_raw else {}
    except Exception:
        config_data = {}

    stages = [dict(s) for s in get_stages_for_run(conn, run_id)]
    workers = [dict(w) for w in get_workers_for_run(conn, run_id)]
    issues = [dict(i) for i in list_issues_for_run(conn, run_id)]
    events = [dict(e) for e in list_events_for_run(conn, run_id, limit=2000)]
    artifacts = [dict(a) for a in list_artifacts_for_run(conn, run_id, released_only=True)]

    # 1. Summary Record
    summary = {
        "run_id": str(run_row["run_id"]),
        "name": str(run_row["name"]),
        "goal": str(run_row["goal"]),
        "status": str(run_row["status"]),
        "current_stage_id": run_row["current_stage_id"],
        "total_model_calls": int(run_row["total_model_calls"] or 0),
        "dissent_recorded": bool(run_row["dissent_recorded"]),
        "final_deliverable_artifact_id": run_row["final_deliverable_artifact_id"],
        "started_at": run_row["started_at"],
        "finished_at": run_row["finished_at"],
        "created_at": run_row["created_at"],
        "stages": stages,
        "workers": workers,
        "issues_count": len(issues),
        "artifacts_count": len(artifacts),
    }

    if redact:
        summary = redact_secrets(summary)
        for w in summary.get("workers", []):
            if isinstance(w, dict) and "account_ref" in w:
                w["account_ref"] = "[LOCAL_PROFILE]"

    # 2. Portable Workflow Template
    portable_workflow = sanitize_workflow_for_export(config_data)

    # 3. Create ZIP archive
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Write metadata JSON files
        zf.writestr("run_summary.json", json.dumps(summary, indent=2))
        zf.writestr("workflow_template.json", json.dumps(portable_workflow, indent=2))
        zf.writestr("issues.json", json.dumps(redact_secrets(issues) if redact else issues, indent=2))
        zf.writestr("events.json", json.dumps(redact_secrets(events) if redact else events, indent=2))

        # Write released artifacts
        store = ArtifactStore(artifact_store_root)
        for art in artifacts:
            art_id = str(art["artifact_id"])
            rel_name = Path(str(art["name"])).name  # Defense against directory traversal
            storage_path = str(art["storage_path"])

            # Verify storage path does not escape store root
            try:
                content_hash = str(art.get("content_hash") or "")
                if content_hash and len(content_hash) == 64:
                    resolved = store.get_artifact_path(content_hash)
                else:
                    resolved = validate_safe_relative_path(store.root_dir, storage_path)
                if resolved.is_file():
                    content_bytes = resolved.read_bytes()
                    if redact and art.get("media_type", "text/plain").startswith("text/"):
                        try:
                            text = content_bytes.decode("utf-8")
                            content_bytes = scrub_sensitive_strings(text).encode("utf-8")
                        except Exception:
                            pass
                    zip_entry_name = f"artifacts/{art_id}_{rel_name}"
                    zf.writestr(zip_entry_name, content_bytes)
            except Exception:
                continue

    return zip_buffer.getvalue()


def export_run_to_file(
    conn: sqlite3.Connection,
    run_id: str,
    output_path: Path | str,
    redact: bool = True,
    artifact_store_root: Path | str | None = None,
) -> Path:
    """Export run dossier directly to a filesystem zip file."""
    data = export_run_dossier_bytes(
        conn=conn,
        run_id=run_id,
        redact=redact,
        artifact_store_root=artifact_store_root,
    )
    dest = Path(output_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest
