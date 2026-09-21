"""Hierarchical Prompt Assembly & Scratch Workspace Staging for AGYM Council.

Implements Feature 22 (Hierarchical Prompt Assembly) and Feature 23 (Scratch Workspace Staging):
- Strictly ordered prompt assembly:
  1. Application Execution & Data Rules (JSON schema, execution mode)
  2. Current User Goal & Workflow Constraints
  3. Stage Contract (instruction, kind, required sections, context policy)
  4. Worker Role & Per-Worker Assignment
  5. Released Evidence & Prior Outputs (with clear delimiters and dissent preservation)
- Physical attempt workspace staging:
  - Isolated per attempt directory.
  - Strictly stages ONLY released artifacts from completed prerequisite stages.
  - Defense-in-depth against directory traversal and unreleased peer leakage.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Sequence

from agym.council.artifacts import (
    ArtifactNotFoundError,
    ArtifactStore,
    PathTraversalError,
    stage_artifact_to_workspace,
    validate_safe_relative_path,
)
from agym.council.models import (
    ArtifactRef,
    CouncilOutputSections,
    ExecutionMode,
    StageConfig,
    StageKind,
    WorkerConfig,
    WorkflowConfig,
)
from agym.profiles import _default_data_root

# Mandatory Dissent Preservation Mandate for Synthesizer
DISSENT_MANDATE = (
    "Produce the final answer with supporting reasons, limitations, and "
    "unresolved disagreement. Do not invent consensus."
)

SYSTEM_OUTPUT_RULES_TEMPLATE = """# Application Execution & Output Rules
- You are a participating worker in AGYM Council multi-agent orchestration.
- Execution Mode: {execution_mode}
- You must reason strictly over the provided inputs and released evidence.
- Do NOT attempt to execute arbitrary unauthorized shell commands or modify external files outside your assigned workspace.
- You must structure your final answer strictly containing the following required sections:
{required_sections_list}

Produce your response as a valid JSON object with matching keys, or with clear markdown sections formatted as:
```json
{{
{json_keys_template}
}}
```
"""


def format_required_sections_list(required_sections: Sequence[str]) -> str:
    """Format required sections as a bulleted markdown list."""
    return "\n".join(f"  - `{section}`" for section in required_sections)


def format_json_template(required_sections: Sequence[str]) -> str:
    """Generate sample JSON template keys for prompt instructions."""
    lines = []
    for s in required_sections:
        lines.append(f'  "{s}": "<content for {s}>"')
    return ",\n".join(lines)


def assemble_prompt(
    goal: str,
    stage: StageConfig,
    worker: WorkerConfig,
    workflow: WorkflowConfig | None = None,
    inputs: list[dict[str, Any]] | None = None,
    execution_mode: str | ExecutionMode = ExecutionMode.SUPPLIED_EVIDENCE,
    released_artifacts: Sequence[ArtifactRef | dict[str, Any]] | None = None,
    prior_stage_outputs: Sequence[dict[str, Any]] | None = None,
    has_dissent: bool = False,
) -> str:
    """Assemble a strictly hierarchical prompt for a worker turn.

    Composition Order (Invariant):
    1. Application execution/data rules (system & output rules)
    2. Current user goal and constraints
    3. Workflow/stage contract
    4. Role and per-worker assignment
    5. Released evidence and prior outputs with clear delimiters

    Args:
        goal: The overarching user objective.
        stage: The StageConfig for the current stage.
        worker: The WorkerConfig for the assigned worker.
        workflow: Optional full WorkflowConfig for metadata.
        inputs: Optional declared workflow inputs (id, description, value).
        execution_mode: Mode of execution (supplied_evidence or workspace).
        released_artifacts: Released artifact references from completed prerequisite stages.
        prior_stage_outputs: Optional rendered outputs/sections from earlier stages.
        has_dissent: If True or stage is SYNTHESIZE, injects strict dissent preservation.

    Returns:
        The complete, rendered prompt string.
    """
    mode_str = execution_mode.value if isinstance(execution_mode, ExecutionMode) else str(execution_mode)
    sections = stage.required_sections or ["findings", "evidence_or_assumptions", "uncertainties", "next_action"]

    # 1. Application Execution & Data Rules
    prompt_parts: list[str] = [
        SYSTEM_OUTPUT_RULES_TEMPLATE.format(
            execution_mode=mode_str,
            required_sections_list=format_required_sections_list(sections),
            json_keys_template=format_json_template(sections),
        ).strip()
    ]

    # 2. Current User Goal & Constraints
    workflow_name = workflow.name if workflow else "Council Workflow"
    goal_section = [
        "\n# User Goal & Workflow Constraints",
        f"Workflow: {workflow_name}",
        f"Goal: {goal.strip()}",
    ]

    bound_inputs: list[dict[str, Any]] = []
    if inputs:
        for inp in inputs:
            if isinstance(inp, dict):
                bound_inputs.append(inp)
            elif hasattr(inp, "model_dump"):
                bound_inputs.append(inp.model_dump())
            elif hasattr(inp, "id"):
                bound_inputs.append({
                    "id": getattr(inp, "id", "input"),
                    "description": getattr(inp, "description", ""),
                    "value": getattr(inp, "value", None),
                })
    elif workflow and workflow.inputs:
        for inp in workflow.inputs:
            bound_inputs.append({"id": inp.id, "description": inp.description, "value": inp.value})

    if bound_inputs:
        goal_section.append("Inputs:")
        for inp in bound_inputs:
            val = inp.get("value")
            val_str = f" = {val}" if val is not None else " (unbound)"
            goal_section.append(f"- {inp.get('id', 'input')}: {inp.get('description', '')}{val_str}")

    prompt_parts.append("\n".join(goal_section))

    # 3. Stage Contract
    stage_kind_val = stage.kind.value if isinstance(stage.kind, StageKind) else str(stage.kind)
    stage_section = [
        f"\n# Stage Contract: {stage.id} (Kind: {stage_kind_val.upper()})",
        f"Stage Instruction: {stage.instruction.strip()}",
        f"Context Policy: {stage.context.value if hasattr(stage.context, 'value') else stage.context}",
        f"Required Sections: {', '.join(sections)}",
    ]

    # Dissent Preservation Invariant:
    # If stage kind is synthesize, or has_dissent is True, explicitly mandate non-invention of consensus
    if stage.kind == StageKind.SYNTHESIZE or stage_kind_val.lower() == "synthesize" or has_dissent:
        stage_section.append(f"\nIMPORTANT DISSENT MANDATE:\n{DISSENT_MANDATE}")

    prompt_parts.append("\n".join(stage_section))

    # 4. Worker Role & Assignment
    worker_section = [
        f"\n# Worker Assignment: {worker.name} (ID: {worker.id})",
        f"Role: {worker.role}",
        f"Task: {worker.task.strip()}",
    ]
    if worker.instructions and worker.instructions.strip():
        worker_section.append(f"Worker Instructions:\n{worker.instructions.strip()}")

    prompt_parts.append("\n".join(worker_section))

    # 5. Released Evidence & Prior Outputs
    evidence_section = ["\n# Released Evidence & Prior-Stage Inputs"]
    has_evidence = False

    if released_artifacts:
        for art in released_artifacts:
            art_id = art.id if hasattr(art, "id") else art.get("id", "")
            art_name = art.name if hasattr(art, "name") else art.get("name", "artifact")
            art_stage = art.stage_id if hasattr(art, "stage_id") else art.get("stage_id", "prior")
            art_worker = art.worker_id if hasattr(art, "worker_id") else art.get("worker_id", "worker")

            content = ""
            if hasattr(art, "metadata") and isinstance(getattr(art, "metadata"), dict):
                content = getattr(art, "metadata").get("content", "")
            elif isinstance(art, dict):
                if isinstance(art.get("metadata"), dict):
                    content = art["metadata"].get("content", "")
                elif "content" in art:
                    content = art["content"]

            evidence_section.append(
                f"\n=== Released Artifact: {art_name} (Stage: {art_stage}, Worker: {art_worker}, ID: {art_id}) ===\n"
                f"{content.strip() if content else '[Content available in workspace files]'}\n"
                f"=== End of Artifact: {art_name} ==="
            )
            has_evidence = True

    if prior_stage_outputs:
        for out in prior_stage_outputs:
            stage_name = out.get("stage_id", "prior_stage")
            worker_ref = out.get("worker_id", "worker")
            out_text = out.get("text") or out.get("content") or ""
            parsed = out.get("parsed_sections")
            if parsed:
                if isinstance(parsed, CouncilOutputSections):
                    out_text = parsed.to_markdown()
                elif isinstance(parsed, dict):
                    try:
                        out_text = CouncilOutputSections.model_validate(parsed).to_markdown()
                    except Exception:
                        out_text = json.dumps(parsed, indent=2)

            evidence_section.append(
                f"\n=== Prior Output: Stage {stage_name} | Worker {worker_ref} ===\n"
                f"{str(out_text).strip()}\n"
                f"=== End of Prior Output ==="
            )
            has_evidence = True

    if not has_evidence:
        evidence_section.append("No prior-stage evidence provided for this stage.")

    prompt_parts.append("\n".join(evidence_section))

    return "\n\n".join(prompt_parts) + "\n"


def resolve_attempt_workspace_path(
    run_id: str,
    worker_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Resolve standard scratch workspace directory for an attempt.

    Layout: {base_dir}/council/runs/{run_id}/workers/{worker_id}/attempts/{attempt_id}/workspace
    """
    root = (base_dir or _default_data_root()).resolve()
    return root / "council" / "runs" / run_id / "workers" / worker_id / "attempts" / attempt_id / "workspace"


def setup_attempt_workspace(
    run_id: str,
    stage_id: str,
    worker_id: str,
    attempt_id: str,
    released_artifacts: Sequence[ArtifactRef | dict[str, Any]],
    artifact_store: ArtifactStore,
    base_dir: Path | None = None,
    allowed_input_stages: Sequence[str] | None = None,
) -> Path:
    """Safely project released prerequisite artifacts into an isolated scratch workspace.

    Enforces Invariant 3 (Physical Release Barriers):
    - Workspace is physically isolated per attempt.
    - Only released prerequisite artifacts from allowed prior stages are staged.
    - Any unreleased artifact or artifact from current/future stages is strictly rejected.

    Args:
        run_id: Unique run ID.
        stage_id: ID of the current stage.
        worker_id: ID of the worker.
        attempt_id: Unique attempt ID.
        released_artifacts: List of candidate artifacts to project.
        artifact_store: Backing Content-Addressed Storage engine.
        base_dir: Optional root data directory.
        allowed_input_stages: Optional whitelist of stage IDs (from stage.input_stages).

    Returns:
        The canonical Path of the prepared scratch workspace.

    Raises:
        ValueError: If an artifact is unreleased or attempts stage leakage.
        PathTraversalError: If an artifact name attempts path traversal escape.
        ArtifactNotFoundError: If an artifact hash is missing from CAS.
    """
    workspace_dir = resolve_attempt_workspace_path(run_id, worker_id, attempt_id, base_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            workspace_dir.chmod(0o700)
        except OSError:
            pass

    allowed_stages_set = set(allowed_input_stages) if allowed_input_stages is not None else None

    for art in released_artifacts:
        # Extract attributes whether Pydantic model or sqlite3.Row/dict
        if isinstance(art, ArtifactRef):
            art_sha = art.sha256
            art_name = art.name
            art_stage = art.stage_id
            art_released = art.released
        elif hasattr(art, "keys"):
            art_sha = art["content_hash"] if "content_hash" in art.keys() else art.get("sha256")
            art_name = art["name"]
            art_stage = art.get("stage_id")
            art_released = bool(art.get("released"))
        else:
            raise TypeError(f"Unsupported artifact representation: {type(art)}")

        # Enforce Release Barrier Invariants
        if not art_released:
            raise ValueError(
                f"Attempted to stage unreleased artifact '{art_name}' (SHA {art_sha[:8]}) into workspace! "
                "Unreleased artifacts must remain physically sealed until stage completion."
            )

        # Same-stage leakage check
        if art_stage == stage_id:
            raise ValueError(
                f"Attempted to stage artifact '{art_name}' from current stage '{stage_id}'! "
                "Workers in the same stage cannot access peer draft artifacts."
            )

        # Allowed input stages check
        if allowed_stages_set is not None and art_stage not in allowed_stages_set:
            raise ValueError(
                f"Artifact '{art_name}' from stage '{art_stage}' is not in allowed input_stages: {sorted(allowed_stages_set)}"
            )

        # Stage artifact file into workspace
        validate_safe_relative_path(workspace_dir, art_name)
        target_path = art_name
        dest_candidate = workspace_dir / target_path
        if dest_candidate.exists() and dest_candidate.is_file():
            import hashlib
            try:
                with open(dest_candidate, "rb") as f:
                    dest_hash = hashlib.sha256(f.read()).hexdigest()
                if dest_hash != art_sha:
                    target_path = f"{art_stage}_{target_path}"
            except OSError:
                pass

        stage_artifact_to_workspace(
            artifact_store=artifact_store,
            content_hash=art_sha,
            workspace_dir=workspace_dir,
            target_relative_path=target_path,
        )

    return workspace_dir


def clean_attempt_workspace(workspace_dir: Path) -> None:
    """Remove an attempt scratch workspace directory."""
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir, ignore_errors=True)


def parse_council_sections(text: str) -> tuple[CouncilOutputSections | None, dict[str, Any] | None]:
    """Extract CouncilOutputSections using multi-strategy parsing.

    Strategies attempted in order:
    1. Direct JSON parsing of the entire text.
    2. Markdown fenced code blocks (```json ... ``` or ``` ... ```).
    3. Markdown headings (e.g. ## Findings, ## Evidence or Assumptions, etc.).
    4. Fallback: treat entire text as the 'findings' section.
    """
    if not text or not text.strip():
        return None, None

    cleaned = text.strip()

    # Strategy 1: Direct JSON parsing
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            try:
                sections = CouncilOutputSections.model_validate(data)
                return sections, data
            except Exception:
                pass
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: Markdown fenced code blocks
    code_block_pattern = re.compile(
        r"```(?:json)?\s*([\s\S]*?)\s*```",
        re.IGNORECASE,
    )
    for match in code_block_pattern.finditer(cleaned):
        block_content = match.group(1).strip()
        try:
            data = json.loads(block_content)
            if isinstance(data, dict):
                try:
                    sections = CouncilOutputSections.model_validate(data)
                    return sections, data
                except Exception:
                    pass
        except (json.JSONDecodeError, ValueError):
            continue

    # Strategy 3: Markdown Headings or Bold Section Labels
    heading_pattern = re.compile(
        r"^(?:(#{1,4})\s+|\*\*)([^\n\*:]+)(?:\*\*|:)?",
        re.MULTILINE,
    )
    matches = list(heading_pattern.finditer(cleaned))
    if matches:
        sections_dict: dict[str, Any] = {}
        for i, m in enumerate(matches):
            raw_title = m.group(2).strip()
            title_clean = re.sub(r"^(?:section\s+)?\d+[\.\):\-]\s*", "", raw_title, flags=re.IGNORECASE).strip()
            key = re.sub(r"[^a-zA-Z0-9]+", "_", title_clean.lower()).strip("_")
            if key in ("evidence", "assumptions", "evidence_assumptions", "evidence_and_assumptions"):
                key = "evidence_or_assumptions"
            elif key in ("uncertainty", "risks", "dissent", "objections", "limitations", "risks_and_uncertainties", "uncertainties_and_risks"):
                key = "uncertainties"
            elif key in ("next_steps", "next_actions", "action", "recommendation", "recommendations", "next_step"):
                key = "next_action"
            elif key in ("finding", "findings_and_analysis", "analysis"):
                key = "findings"

            start_pos = m.end()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
            section_content = cleaned[start_pos:end_pos].strip()
            section_content = re.sub(r"^[:\*]+\s*", "", section_content)
            sections_dict[key] = section_content

        std_keys = {"findings", "evidence_or_assumptions", "uncertainties", "next_action"}
        if any(k in sections_dict for k in std_keys):
            try:
                sections = CouncilOutputSections.model_validate(sections_dict)
                return sections, sections_dict
            except Exception:
                pass

    # Strategy 4: Fallback - treat entire text as findings
    fallback_dict = {
        "findings": cleaned,
        "evidence_or_assumptions": "",
        "uncertainties": "",
        "next_action": "",
    }
    try:
        sections = CouncilOutputSections.model_validate(fallback_dict)
        return sections, fallback_dict
    except Exception:
        return None, None
