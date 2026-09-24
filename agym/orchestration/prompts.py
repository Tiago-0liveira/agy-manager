"""Coordinator system prompts, assessment prompts, observation formatting, and synthesis prompts.

This module encapsulates all prompt generation logic for the AGYM coordinator:
- System prompt defining the strict WHAT vs HOW boundary.
- Assessment prompt for Round 0 requiring TaskAssessment + CoordinatorAction.
- Observation formatting converting CoordinatorObservation into structured messages (redacted of internal profile names).
- Standardized infrastructure failure formatting.
- Synthesis prompt support for consolidating worker outputs and auditor findings without exposing orchestration internals.
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from agym.orchestration.contracts import (
    AuditResult,
    CoordinatorObservation,
    FleetView,
    TaskAssessment,
    WorkerResult,
)
from agym.orchestration.protocol import (
    COORDINATOR_ACTION_SCHEMA,
    INITIAL_RESPONSE_SCHEMA,
    InfrastructureFailure,
)

__all__ = [
    "COORDINATOR_SYSTEM_PROMPT",
    "get_coordinator_system_prompt",
    "build_assessment_prompt",
    "format_coordinator_observation",
    "build_observation_prompt",
    "format_infrastructure_failure",
    "build_synthesis_prompt",
    "redact_profile_identities",
]


# ============================================================================
# 1. Coordinator System Prompt
# ============================================================================

COORDINATOR_SYSTEM_PROMPT: str = """\
You are the AGYM Orchestration Coordinator.

### RESPONSIBILITY BOUNDARY
- You decide WHAT work is useful and how reasoning, review, synthesis, refinement, and implementation should be decomposed.
- AGYM decides HOW it executes that work: profiles, leases, budgets, retries, processes, persistence, and workspace enforcement.
- You are the main reasoning/control loop. Do not execute host actions yourself.

### QUALITY POLICY
Choose worker roles dynamically from the task. Workers in the same wave MUST have meaningfully different objectives.
Bad: three workers all "analyze architecture".
Good: understand current architecture; identify failure modes; propose minimal implementation; challenge the proposal.
Do not force SECURITY, PERFORMANCE, or other specialties when they are irrelevant.

Auditors inspect EXISTING outputs. They look for incorrect assumptions, missed requirements, regressions,
contradictions, edge cases, test gaps, and architectural weaknesses. An auditor must not simply repeat the task.
For MEDIUM work, auditing is normally worthwhile when `value_of_auditing >= 0.5`, but the deterministic
MEDIUM finalization gate remains perspectives + synthesis + no open critical findings.

When an audit or critique finds a concrete problem, prefer a small targeted follow-up worker for that unresolved
issue instead of rerunning the entire worker wave. Launch another broad wave only when the uncertainty is broad.

Before requesting more work, consider remaining uncertainty, critical findings, disagreements, remaining budget,
and the expected value of another round. Capacity is a maximum, not a target: do not launch agents merely because
profiles are available.

### QUALITY STATE
After every completed round, report the reasoning-derived quality state through `quality_update`:
- `open_questions`: unresolved questions that matter to the answer.
- `disagreements`: unresolved HIGH-PRIORITY disagreements only.
- `open_critical_findings`: unresolved findings that would make finalization unsafe or materially incomplete.
- `confidence`: 0.0 to 1.0 confidence in the current solution.
AGYM independently derives perspective, audit, synthesis, critique, executor, and verification counts. Never claim
that an audit/synthesis/verification happened through quality_update.

### EXPECTED DEEP PLAN SHAPE
For HIGH-complexity PLAN work, normally use:
ASSESS -> distinct parallel investigation -> independent audit -> synthesis v1 -> critique of that synthesis
-> targeted refinement when needed -> synthesis v2 when needed -> FINALIZE.
This is guidance, not a fixed sequence; AGYM's deterministic quality gates decide whether FINALIZE is allowed.

### IMPLEMENT MODE
Strongly separate reasoning from mutation:
INVESTIGATE -> PLAN -> AUDIT PLAN -> SYNTHESIZE APPROVED PLAN -> RUN_EXECUTOR
-> VERIFY IMPLEMENTATION -> IMPLEMENTATION AUDIT -> targeted fix if required -> FINAL VERIFICATION -> FINALIZE.
Only a RUN_EXECUTOR worker with role EXECUTOR may request MUTATING workspace access.
All analysis, audit, synthesis, and verification workers remain READ_ONLY.
Executor success is not proof of correctness. For MEDIUM/HIGH implementation work, use independent
post-implementation verification; HIGH work also requires an implementation audit.

### ACTIONS
You may request workers and specialists, audits, synthesis, another round, an executor, or finalize:

1. RUN_WORKERS for parallel or targeted read-only investigation/verification.
2. RUN_AUDITORS to independently inspect prior outputs.
3. RUN_SYNTHESIS for a read-only SYNTHESIZER that consolidates evidence.
4. RUN_EXECUTOR for exactly one mutating EXECUTOR.
5. FINALIZE with the definitive user-facing response.

### FORBIDDEN
Never:
- Name AGYM profiles.
- Run shell commands or Spawn processes directly.
- Override budgets or Override leases.
- Override workspace restrictions.
- Recursively create agents itself; decomposition must go through AGYM actions.
- Specify command/argv/shell/environment fields or override profile selection/retries.
Any forbidden field ('profile', 'profile_name', 'command', 'argv', 'shell', 'environment') is rejected.

### EXECUTION STRATEGY
The coordinator itself runs at HIGH_EFFORT. This is not Antigravity /boost.
Choose worker strategy based on expected value. Auditors should normally use HIGH_EFFORT when the review matters.
Do not request BOOST simply to imitate /boost semantics.

### ACTION RATIONALE
Use `reason` for a concise safe rationale (500 characters maximum) explaining why the next orchestration action
is useful. Do not reveal private chain-of-thought. A short decision summary is sufficient.

### RESPONSE FORMAT
All responses must be structured JSON.
Round 0: return both `assessment` (TaskAssessment) and `action` (CoordinatorAction).
Later rounds: return one valid CoordinatorAction. After completed work, include all four quality_update fields.
"""


def get_coordinator_system_prompt() -> str:
    """Return the static coordinator system prompt."""
    return COORDINATOR_SYSTEM_PROMPT


# ============================================================================
# 2. Assessment Prompt (Round 0)
# ============================================================================


def build_assessment_prompt(
    task: str,
    fleet_view: FleetView,
    repository_scope: str = "",
) -> str:
    """Build the prompt for the coordinator's initial Round 0 task assessment.

    The coordinator's response MUST contain both:
    1. TaskAssessment
    2. CoordinatorAction
    No arbitrary prose-only decisions are accepted.

    Args:
        task: Primary task description.
        fleet_view: Coordinator-safe aggregate fleet capacity (strictly redacted of profile names).
        repository_scope: Optional repository paths or scope hints.

    Returns:
        Formatted prompt string.
    """
    initial_schema_str = json.dumps(INITIAL_RESPONSE_SCHEMA, indent=2)

    scope_section = ""
    if repository_scope.strip():
        scope_section = f"### Repository Scope\n{repository_scope.strip()}\n\n"

    prompt = f"""\
## Initial Task Assessment (Round 0)

### Primary Task
{task.strip()}

{scope_section}### Current Fleet Capacity (Aggregate)
- Available Profiles: {fleet_view.available_profiles}
- Max Parallel Concurrency: {fleet_view.max_parallel}
- Standard Tier Capacity: {fleet_view.standard_capacity}
- High Effort Tier Capacity: {fleet_view.high_effort_capacity}
- Boost Tier Capacity: {fleet_view.boost_capacity}
- Quota Band Distribution: {json.dumps(fleet_view.quota_band_counts)}

### Instructions
1. Analyze the task and produce a comprehensive `TaskAssessment`:
   - Categorize `task_type` and `complexity`.
   - Rate `confidence` (0.0 to 1.0).
   - Specify whether `mutation_required` is true.
   - Evaluate `value_of_parallel_reasoning` (0.0 to 1.0) and `value_of_auditing` (0.0 to 1.0).
   - Provide an executive `summary` and `proposed_initial_work`.
2. Propose your first `CoordinatorAction`:
   - If parallel exploration/analysis is beneficial, propose `RUN_WORKERS` with one or more workers.
   - If the task is simple and clear, you may propose `RUN_EXECUTOR` directly (if mutation is required) or `FINALIZE` (if answerable directly).
3. Output Requirement:
   Your response MUST be a single valid JSON object matching the schema below.
   Do NOT provide an arbitrary prose-only answer without the JSON object.

### Required JSON Schema
```json
{initial_schema_str}
```
"""
    return prompt


# ============================================================================
# 3. Observation Prompt (Rounds 1+)
# ============================================================================


def format_coordinator_observation(observation: CoordinatorObservation) -> str:
    """Format a CoordinatorObservation into the next message for the coordinator.

    Includes:
    - Round number
    - Budget state
    - Redacted fleet state (NO internal profile names)
    - Completed worker and audit results
    - Failed results and failure classifications
    - Rejected requests

    Args:
        observation: The observation state returned from executing the previous action.

    Returns:
        Formatted observation markdown prompt.
    """
    sections: list[str] = [
        f"## Round {observation.round_number} Observation",
        "",
        "### Orchestration Budget State",
        f"- Invocations Used: {observation.budget_usage.invocations}",
        f"- Rounds Completed: {observation.budget_usage.rounds}",
        f"- Boost Invocations Used: {observation.budget_usage.boost_invocations}",
        f"- Retries Attempted: {observation.budget_usage.retries}",
        f"- Runtime Elapsed: {observation.budget_usage.runtime_seconds:.1f}s",
        "",
        "### Fleet Capacity (Aggregate)",
        f"- Available Profiles: {observation.fleet_view.available_profiles}",
        f"- Max Parallel Concurrency: {observation.fleet_view.max_parallel}",
        f"- Standard Tier Capacity: {observation.fleet_view.standard_capacity}",
        f"- High Effort Tier Capacity: {observation.fleet_view.high_effort_capacity}",
        f"- Boost Tier Capacity: {observation.fleet_view.boost_capacity}",
        f"- Quota Band Distribution: {json.dumps(observation.fleet_view.quota_band_counts)}",
        "",
        "### Quality State (AGYM-authoritative mechanical evidence)",
        f"- Independent Perspectives: {observation.quality_state.independent_perspectives}",
        f"- Audits Completed: {observation.quality_state.audits_completed}",
        f"- Syntheses Completed: {observation.quality_state.synthesis_completed}",
        f"- Synthesis Critiques Completed: {observation.quality_state.final_critique_completed}",
        f"- Post-Implementation Verifications: {observation.quality_state.post_implementation_verifications}",
        f"- Implementation Audits: {observation.quality_state.implementation_audits_completed}",
        f"- Open Critical Findings: {len(observation.quality_state.open_critical_findings)}",
        f"- Open Questions: {len(observation.quality_state.open_questions)}",
        f"- High-Priority Disagreements: {len(observation.quality_state.disagreements)}",
        f"- Confidence: {observation.quality_state.confidence:.2f}",
    ]

    if observation.finalization_rejection:
        sections.extend([
            "",
            "### FINALIZE Rejected by AGYM",
            "Do not repeat FINALIZE until these deterministic requirements are satisfied:",
        ])
        sections.extend(f"- {item}" for item in observation.finalization_rejection)

    # Completed Results
    sections.append("")
    sections.append("### Completed Results")
    if not observation.completed_results:
        sections.append("(None)")
    else:
        for res in observation.completed_results:
            if isinstance(res, AuditResult) or hasattr(res, "findings"):
                sections.append(f"#### Auditor Result: {res.worker_id}")
                sections.append(f"- Status: {res.status.value}")
                findings = getattr(res, "findings", [])
                if findings:
                    sections.append("- Key Findings:")
                    for f in findings:
                        sections.append(f"  * {f}")
                if res.response:
                    sections.append(f"- Audit Report:\n{res.response.strip()}")
            else:
                sections.append(f"#### Worker Result: {res.worker_id} (Role: {res.role.value})")
                sections.append(f"- Status: {res.status.value}")
                if res.response:
                    sections.append(f"- Response:\n{res.response.strip()}")
                if res.structured_data:
                    sections.append(f"- Structured Output:\n```json\n{json.dumps(res.structured_data, indent=2)}\n```")
            sections.append("")

    # Failed Results
    sections.append("### Failed Results")
    if not observation.failed_results:
        sections.append("(None)")
    else:
        for res in observation.failed_results:
            fail_class = res.failure.value if res.failure else "UNKNOWN"
            role_str = res.role.value if hasattr(res, "role") else "AUDITOR"
            sections.append(f"#### Failed: {res.worker_id} (Role: {role_str})")
            sections.append(f"- Status: {res.status.value}")
            sections.append(f"- Failure Classification: {fail_class}")
            if getattr(res, "error", None):
                sections.append(f"- Error: {res.error.strip()}")
                if getattr(res, "response", None):
                    sections.append(f"- Output / Response:\n{res.response.strip()}")
            elif res.response:
                sections.append(f"- Error / Message: {res.response.strip()}")
            if hasattr(res, "structured_data") and res.structured_data:
                sections.append(f"- Failure Details: {json.dumps(res.structured_data)}")
            sections.append("")

    # Rejected Requests
    sections.append("### Rejected Requests")
    if not observation.rejected_requests:
        sections.append("(None)")
    else:
        for req in observation.rejected_requests:
            if hasattr(req, "target_worker_ids"):
                sections.append(
                    f"- Auditor Request '{req.worker_id}' targeting {req.target_worker_ids} rejected (Focus: {getattr(req, 'focus', '')})"
                )
            else:
                role_val = req.role.value if hasattr(req, "role") else "UNKNOWN"
                sections.append(
                    f"- Worker Request '{req.worker_id}' (Role: {role_val}) rejected (Objective: {getattr(req, 'objective', '')})"
                )
        sections.append("")

    # Next Action Instruction
    action_schema_str = json.dumps(COORDINATOR_ACTION_SCHEMA, indent=2)
    sections.extend([
        "### Next Decision",
        "Based on the results, failures, and remaining budget, decide your next CoordinatorAction:",
        "- RUN_WORKERS: Launch new or follow-up parallel analysis workers.",
        "- RUN_AUDITORS: Launch auditors to critically inspect completed worker outputs.",
        "- RUN_SYNTHESIS: Synthesize prior worker outputs into a resolved plan.",
        "- RUN_EXECUTOR: Execute file modifications via a single mutating EXECUTOR worker.",
        "- FINALIZE: Conclude only when AGYM quality requirements are satisfied.",
        "- Include a short `reason` (<=500 chars) and, after completed work, all four `quality_update` fields.",
        "",
        "Respond with a single valid JSON object adhering to CoordinatorAction schema:",
        f"```json\n{action_schema_str}\n```",
    ])

    return redact_profile_identities("\n".join(sections))


def redact_profile_identities(
    text: str | None,
    profile_names: Sequence[str] | None = None,
) -> str:
    """Redact physical profile names, profile filesystem paths, and identity leaks.

    Ensures coordinator observations never receive physical profile paths or names.
    """
    if not text:
        return "" if text is not None else ""

    result = str(text)

    # 1. Redact explicit known profile names
    if profile_names:
        for name in profile_names:
            if name and len(name) > 1:
                pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
                result = pattern.sub("[REDACTED_PROFILE]", result)

    # 2. Redact profile paths: /.../profiles/<name>/... or profiles/<name>
    result = re.sub(
        r"(?i)([/\\].*?[/\\]profiles[/\\\\])[^/\\ \t\n\r\"'`]+",
        r"\1[REDACTED]",
        result,
    )
    result = re.sub(
        r"(?i)(profiles[/\\])[^/\\ \t\n\r\"'`]+",
        r"\1[REDACTED]",
        result,
    )

    # 3. Redact Profile '...' or Profile "..." references
    result = re.sub(
        r"(?i)\bprofile\s+['\"][^'\"]+['\"]",
        "profile '[REDACTED]'",
        result,
    )

    return result


def build_observation_prompt(observation: CoordinatorObservation) -> str:
    """Alias for format_coordinator_observation for consistency with prompt builders."""
    return format_coordinator_observation(observation)


# ============================================================================
# 4. Standardized Failure Message Formatting
# ============================================================================


def format_infrastructure_failure(failure: InfrastructureFailure) -> str:
    """Format a standardized InfrastructureFailure into readable markdown for the coordinator."""
    lines = [
        f"### Infrastructure Failure: {failure.reason.value.upper()}",
        f"- Description: {failure.message}",
    ]
    if failure.worker_id:
        lines.append(f"- Affected Worker: {failure.worker_id}")
    if failure.action_id:
        lines.append(f"- Affected Action: {failure.action_id}")
    lines.append(f"- Failure Class: {failure.failure_class.value}")
    lines.append(
        f"- Recommended Action: {failure.suggested_action.upper()} "
        f"(Semantic options: retry, replace worker, reduce scope, continue, or finalize)"
    )
    if failure.details:
        lines.append(f"- Details: {json.dumps(failure.details)}")
    return "\n".join(lines)


# ============================================================================
# 5. Synthesis Prompt Support
# ============================================================================


def build_synthesis_prompt(
    task: str,
    assessment: TaskAssessment | None = None,
    worker_results: list[WorkerResult | AuditResult] | None = None,
    audit_results: list[AuditResult] | None = None,
    repository_facts: str | None = None,
    objective: str | None = None,
) -> str:
    """Construct a clean, focused prompt for a synthesis worker.

    Ensures the synthesis worker receives:
    - Original task
    - Task assessment summary
    - Relevant worker outputs
    - Auditor findings
    - Repository facts
    - Specific synthesis objective

    Strictly excludes unrelated orchestration internals:
    - NO internal profile names
    - NO lease IDs
    - NO invocation IDs
    - NO process IDs or timestamps

    Args:
        task: The original task string.
        assessment: Initial task assessment.
        worker_results: List of completed worker results.
        audit_results: Optional separate list of audit results.
        repository_facts: Optional context or facts about the repository.
        objective: Specific objective for this synthesis step.

    Returns:
        Formatted prompt string for the synthesis worker.
    """
    sections: list[str] = [
        "# Synthesis Task",
        "",
        "You are an expert synthesizer. Your role is to analyze, integrate, and synthesize "
        "the parallel worker analyses and auditor findings into a coherent, high-quality solution.",
        "",
        "## Original Task",
        task.strip(),
        "",
    ]

    if objective and objective.strip():
        sections.extend([
            "## Specific Synthesis Objective",
            objective.strip(),
            "",
        ])

    if assessment is not None:
        sections.extend([
            "## Task Assessment Context",
            f"- Task Type: {assessment.task_type.value}",
            f"- Complexity: {assessment.complexity.value}",
            f"- Mutation Required: {assessment.mutation_required}",
        ])
        if assessment.repository_scope:
            sections.append(f"- Repository Scope: {assessment.repository_scope}")
        if assessment.summary:
            sections.append(f"- Assessment Summary: {assessment.summary}")
        sections.append("")

    if repository_facts and repository_facts.strip():
        sections.extend([
            "## Repository Facts",
            repository_facts.strip(),
            "",
        ])

    # Separate workers and auditors
    workers: list[WorkerResult] = []
    auditors: list[AuditResult] = []

    if worker_results:
        for r in worker_results:
            if isinstance(r, AuditResult) or hasattr(r, "findings"):
                auditors.append(r)  # type: ignore[arg-type]
            elif isinstance(r, WorkerResult):
                workers.append(r)

    if audit_results:
        for a in audit_results:
            if a not in auditors:
                auditors.append(a)

    # Worker outputs
    sections.append("## Worker Outputs to Synthesize")
    if not workers:
        sections.append("(No worker outputs provided)")
    else:
        for w in workers:
            sections.append(f"### Worker: {w.worker_id} (Role: {w.role.value})")
            if w.response:
                sections.append(w.response.strip())
            if w.structured_data:
                sections.append(f"```json\n{json.dumps(w.structured_data, indent=2)}\n```")
            sections.append("")

    # Auditor findings
    sections.append("## Auditor Findings to Address")
    if not auditors:
        sections.append("(No auditor findings recorded)")
    else:
        for a in auditors:
            sections.append(f"### Auditor: {a.worker_id}")
            findings = getattr(a, "findings", [])
            if findings:
                sections.append("Critiques and findings:")
                for f in findings:
                    sections.append(f"- {f}")
            if a.response:
                sections.append(f"Report:\n{a.response.strip()}")
            sections.append("")

    sections.extend([
        "## Synthesis Instructions",
        "1. Identify areas of consensus and resolve contradictions among worker findings.",
        "2. Directly address each critique and finding raised by the auditors.",
        "3. Synthesize the findings into an authoritative, actionable, and unified output.",
        "4. Focus entirely on the technical deliverables and solution design.",
    ])

    return "\n".join(sections)
