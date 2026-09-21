"""Preset workflow definitions and discovery for AGYM Council."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agym.council.models import WorkflowConfig


def get_presets_dir() -> Path:
    """Return the absolute path to the bundled presets directory."""
    return Path(__file__).resolve().parent / "presets"


def list_presets() -> list[dict[str, Any]]:
    """List all bundled workflow presets with metadata.

    Returns:
        A list of dictionaries with preset metadata (id, name, goal, workers_count, stages_count).
    """
    presets_dir = get_presets_dir()
    if not presets_dir.is_dir():
        return []

    results: list[dict[str, Any]] = []
    for file in sorted(presets_dir.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            preset_id = file.stem
            results.append({
                "id": preset_id,
                "name": data.get("name", preset_id),
                "goal": data.get("goal", ""),
                "workers_count": len(data.get("workers", [])),
                "stages_count": len(data.get("stages", [])),
                "execution_mode": data.get("execution_mode", "supplied_evidence"),
                "draft": data.get("draft", True),
                "schema_version": data.get("schema_version", 1),
            })
        except Exception:
            continue
    return results


def load_preset_raw(preset_id: str) -> dict[str, Any] | None:
    """Load a raw preset configuration dict by identifier or filename.

    Args:
        preset_id: Name of preset (e.g. 'quick_council' or 'quick_council.json')

    Returns:
        Parsed JSON dictionary or None if not found.
    """
    stem = preset_id[:-5] if preset_id.endswith(".json") else preset_id
    presets_dir = get_presets_dir()
    target_file = presets_dir / f"{stem}.json"
    if not target_file.is_file():
        return None
    try:
        return json.loads(target_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_preset_workflow(preset_id: str) -> WorkflowConfig | None:
    """Load and validate a preset as a WorkflowConfig model.

    Args:
        preset_id: Name of preset (e.g. 'quick_council')

    Returns:
        Validated WorkflowConfig or None if not found or invalid.
    """
    raw = load_preset_raw(preset_id)
    if raw is None:
        return None
    return WorkflowConfig.model_validate(raw)
