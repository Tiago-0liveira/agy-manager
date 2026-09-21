"""FastAPI router implementation for AGYM Council REST endpoints."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agym import __version__ as core_version
from agym.council import __version__ as council_version
from agym.council.api.auth_broker import (
    check_account_probe,
    discover_account_models,
    launch_setup_terminal,
)
from agym.council.api.events import sse_event_stream
from agym.council.api.exporter import export_run_dossier_bytes, sanitize_workflow_for_export
from agym.council.api.security import get_or_create_session_token
from agym.council.artifacts import ArtifactStore, PathTraversalError, validate_safe_relative_path
from agym.council.engine import CouncilEngine
from agym.council.models import (
    Account,
    AccountAuthStatus,
    AgentTemplate,
    ProviderType,
    Run,
    RunStatus,
    StageConfig,
    WorkerConfig,
    WorkflowConfig,
    load_workflow_from_file,
    redact_secrets,
)
from agym.council.presets import list_presets, load_preset_raw, load_preset_workflow
from agym.council.providers.antigravity import AntigravityProviderAdapter
from agym.council.providers.fake import FakeProviderAdapter
from agym.council.storage import (
    create_account,
    create_agent_template,
    create_run,
    create_workflow_template,
    delete_account,
    delete_agent_template,
    delete_workflow_template,
    get_account,
    get_account_by_profile_ref,
    get_agent_template,
    get_artifact_record,
    get_connection,
    get_run,
    get_stage,
    get_stages_for_run,
    get_workflow_template,
    get_workers_for_run,
    list_accounts,
    list_agent_templates,
    list_artifacts_for_run,
    list_events_for_run,
    list_issues_for_run,
    list_runs,
    list_workflow_templates,
    resolve_db_path,
    update_account,
    update_agent_template,
    update_run_status,
)
from agym.profiles import ProfileStore

router = APIRouter(prefix="/api")

# Active execution registry: run_id -> (engine, asyncio.Task)
_ACTIVE_ENGINES: dict[str, CouncilEngine] = {}
_ACTIVE_TASKS: dict[str, asyncio.Task[Any]] = {}


def get_db(request: Request) -> sqlite3.Connection:
    """Dependency: retrieve SQLite connection configured with Council pragmas."""
    db_path = getattr(request.app.state, "db_path", None)
    conn = get_connection(db_path)
    try:
        return conn
    except Exception:
        conn.close()
        raise


def get_default_provider() -> Any:
    """Resolve active provider adapter based on environment variable."""
    if os.environ.get("AGYM_COUNCIL_PROVIDER") == "fake":
        return FakeProviderAdapter()
    return AntigravityProviderAdapter()


# ---------------------------------------------------------------------------
# 1. System, Handshake & Presets Endpoints
# ---------------------------------------------------------------------------


@router.get("/health")
def api_health() -> dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "ok",
        "council_version": council_version,
        "core_version": core_version,
        "loopback_only": True,
    }


@router.get("/session")
def api_session() -> dict[str, Any]:
    """Session handshake endpoint for local browser UI to obtain session token."""
    token = get_or_create_session_token()
    return {
        "session_token": token,
        "council_version": council_version,
    }


@router.get("/presets")
@router.get("/workflows/presets")
def api_list_presets() -> list[dict[str, Any]]:
    """List bundled workflow presets with metadata."""
    return list_presets()


@router.get("/presets/{preset_id}")
@router.get("/workflows/presets/{preset_id}")
def api_get_preset(preset_id: str) -> dict[str, Any]:
    """Retrieve raw preset definition by ID."""
    preset = load_preset_raw(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"Preset '{preset_id}' not found.")
    return preset


# ---------------------------------------------------------------------------
# 2. Account Management Endpoints
# ---------------------------------------------------------------------------


class AccountCreateRequest(BaseModel):
    profile_ref: str
    label: str
    provider: str = "antigravity"
    enabled: bool = True
    concurrency_limit: int = 1


class AccountUpdateRequest(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    concurrency_limit: int | None = None


@router.get("/accounts")
def api_list_accounts(
    enabled_only: bool = False,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    """List registered council accounts."""
    try:
        rows = list_accounts(conn, enabled_only=enabled_only)
        results = []
        for r in rows:
            d = dict(r)
            if d.get("advisory_usage"):
                try:
                    d["advisory_usage"] = json.loads(d["advisory_usage"])
                except Exception:
                    pass
            results.append(d)
        return results
    finally:
        conn.close()


@router.get("/profiles/available")
def api_get_available_profiles(
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return all profiles configured in agym with linkage status to Council."""
    try:
        from agym.profiles import ProfileStore
        store = ProfileStore()
        profiles = store.list()
        linked_accounts = {str(r["profile_ref"]): dict(r) for r in list_accounts(conn)}

        results = []
        for p in profiles:
            linked = linked_accounts.get(p.name)
            results.append({
                "profile_name": p.name,
                "home": str(p.home),
                "model": p.settings.model or "default",
                "is_linked": linked is not None,
                "account_id": linked["account_id"] if linked else None,
                "auth_status": linked["auth_status"] if linked else "unlinked",
                "display_label": linked["display_label"] if linked else p.name,
                "concurrency_limit": linked["concurrency_limit"] if linked else 1,
            })
        return results
    finally:
        conn.close()


@router.post("/accounts/link-all")
async def api_link_all_profiles(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Link all unlinked local agym profiles into Council accounts in one click."""
    try:
        from agym.profiles import ProfileStore
        store = ProfileStore()
        profiles = store.list()
        existing_refs = {str(r["profile_ref"]) for r in list_accounts(conn)}

        newly_linked: list[str] = []
        for p in profiles:
            if p.name in existing_refs:
                continue
            acc = Account(
                profile_ref=p.name,
                label=p.name,
                provider=ProviderType.ANTIGRAVITY if os.environ.get("AGYM_COUNCIL_PROVIDER") != "fake" else ProviderType.FAKE,
                enabled=True,
                auth_status=AccountAuthStatus.UNVERIFIED,
                concurrency_limit=1,
            )
            create_account(conn, acc)
            newly_linked.append(p.name)

        return {
            "linked_count": len(newly_linked),
            "linked_profiles": newly_linked,
            "message": f"Successfully linked {len(newly_linked)} profile(s) to Council.",
        }
    finally:
        conn.close()


_MODEL_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


@router.get("/models")
async def api_get_cached_models(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return cached available models discovered across accounts or default models."""
    now = time.time()
    cached = _MODEL_CACHE.get("global")
    if cached and (now - cached[0]) < 600:  # 10 minute cache TTL
        return cached[1]

    models: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Query connected accounts
    rows = list_accounts(conn, enabled_only=True)
    adapter = getattr(request.app.state, "provider_adapter", None) or get_default_provider()

    for acc in rows:
        acc_id = acc["account_id"]
        try:
            acc_models = await discover_account_models(conn, acc_id, provider_adapter=adapter)
            for m in acc_models:
                m_dict = m.model_dump(mode="json")
                if m_dict["id"] not in seen_ids:
                    seen_ids.add(m_dict["id"])
                    models.append(m_dict)
        except Exception:
            pass

    # Provide fallback models if discovery returned none
    default_list = [
        {"id": "gemini-2.5-pro", "display_name": "Gemini 2.5 Pro (High Reasoning)", "context_window": 1000000, "capabilities": ["thinking", "tools"]},
        {"id": "gemini-2.5-flash", "display_name": "Gemini 2.5 Flash (Fast & Efficient)", "context_window": 1000000, "capabilities": ["thinking", "tools"]},
        {"id": "claude-3-5-sonnet", "display_name": "Claude 3.5 Sonnet", "context_window": 200000, "capabilities": ["tools"]},
        {"id": "fake-model-1", "display_name": "Fake Test Model 1", "context_window": 128000, "capabilities": ["dissent"]},
    ]
    for dm in default_list:
        if dm["id"] not in seen_ids:
            seen_ids.add(dm["id"])
            models.append(dm)

    _MODEL_CACHE["global"] = (now, models)
    return models


@router.post("/accounts", status_code=201)
def api_create_account(
    payload: AccountCreateRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Register or link an Antigravity profile as a Council account."""
    try:
        # Check if already linked
        existing = get_account_by_profile_ref(conn, payload.profile_ref)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Profile '{payload.profile_ref}' is already linked to account '{existing['account_id']}'.",
            )

        # Check if profile exists in profile store
        store = ProfileStore()
        status = AccountAuthStatus.UNVERIFIED
        try:
            store.get(payload.profile_ref)
        except Exception:
            status = AccountAuthStatus.UNAVAILABLE

        acc = Account(
            profile_ref=payload.profile_ref,
            label=payload.label,
            provider=ProviderType(payload.provider),
            enabled=payload.enabled,
            auth_status=status,
            concurrency_limit=payload.concurrency_limit,
        )
        account_id = create_account(conn, acc)
        row = get_account(conn, account_id)
        return dict(row) if row else acc.model_dump(mode="json")
    finally:
        conn.close()


@router.get("/accounts/{account_id}")
def api_get_account(
    account_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Get details for a specific council account."""
    try:
        row = get_account(conn, account_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
        d = dict(row)
        if d.get("advisory_usage"):
            try:
                d["advisory_usage"] = json.loads(d["advisory_usage"])
            except Exception:
                pass
        return d
    finally:
        conn.close()


@router.patch("/accounts/{account_id}")
def api_update_account(
    account_id: str,
    payload: AccountUpdateRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Update label, enabled status, or concurrency limit for an account."""
    try:
        row = get_account(conn, account_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")

        update_data: dict[str, Any] = {"account_id": account_id}
        if payload.label is not None:
            update_data["display_label"] = payload.label
        if payload.enabled is not None:
            update_data["enabled"] = 1 if payload.enabled else 0
        if payload.concurrency_limit is not None:
            if payload.concurrency_limit <= 0:
                raise HTTPException(status_code=400, detail="concurrency_limit must be > 0.")
            update_data["concurrency_limit"] = payload.concurrency_limit

        update_account(conn, update_data)
        updated = get_account(conn, account_id)
        return dict(updated) if updated else update_data
    finally:
        conn.close()


@router.delete("/accounts/{account_id}")
def api_delete_account(
    account_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Unlink an account from council. Underlying profile credentials remain intact."""
    try:
        row = get_account(conn, account_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
        delete_account(conn, account_id)
        return {"deleted": True, "account_id": account_id}
    finally:
        conn.close()


@router.post("/accounts/{account_id}/connect")
def api_connect_account(
    account_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Trigger desktop terminal auth broker to configure or re-authenticate profile."""
    try:
        row = get_account(conn, account_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
        profile_ref = str(row["profile_ref"])
        return launch_setup_terminal(profile_ref)
    finally:
        conn.close()


@router.post("/accounts/{account_id}/check")
async def api_check_account(
    account_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Probe live authentication status for an account via non-billable CLI probe."""
    try:
        row = get_account(conn, account_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
        adapter = getattr(request.app.state, "provider_adapter", None)
        status = await check_account_probe(conn, account_id, provider_adapter=adapter)
        return status.model_dump(mode="json")
    finally:
        conn.close()


@router.get("/accounts/{account_id}/models")
async def api_get_account_models(
    account_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    """Discover available models for an account."""
    try:
        row = get_account(conn, account_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
        adapter = getattr(request.app.state, "provider_adapter", None)
        models = await discover_account_models(conn, account_id, provider_adapter=adapter)
        return [m.model_dump(mode="json") for m in models]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Agent Library Endpoints
# ---------------------------------------------------------------------------


class AgentTemplateCreateRequest(BaseModel):
    name: str
    purpose: str
    instructions: str
    working_style: str | None = None
    output_expectations: list[str] = Field(
        default_factory=lambda: ["findings", "evidence_or_assumptions", "uncertainties", "next_action"]
    )
    preferred_model: str | None = None
    suggested_capabilities: list[str] = Field(default_factory=list)


class AgentTemplateUpdateRequest(BaseModel):
    name: str | None = None
    purpose: str | None = None
    instructions: str | None = None
    working_style: str | None = None
    output_expectations: list[str] | None = None
    preferred_model: str | None = None
    suggested_capabilities: list[str] | None = None


@router.get("/agents")
def api_list_agents(conn: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    """List reusable agent templates."""
    try:
        rows = list_agent_templates(conn)
        results = []
        for r in rows:
            d = dict(r)
            # Deserialize JSON fields
            for k in ("output_expectations", "suggested_capability"):
                if d.get(k):
                    try:
                        d[k] = json.loads(d[k])
                    except Exception:
                        pass
            # Normalize field names to match AgentTemplate model
            d["id"] = d.get("template_id")
            d["name"] = d.get("display_name")
            d["instructions"] = d.get("base_instructions")
            d["preferred_model"] = d.get("model_preference")
            d["suggested_capabilities"] = d.get("suggested_capability") or []
            results.append(d)
        return results
    finally:
        conn.close()


@router.post("/agents", status_code=201)
def api_create_agent(
    payload: AgentTemplateCreateRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Create a new reusable agent template."""
    try:
        template = AgentTemplate(
            name=payload.name,
            purpose=payload.purpose,
            instructions=payload.instructions,
            working_style=payload.working_style,
            output_expectations=payload.output_expectations,
            preferred_model=payload.preferred_model,
            suggested_capabilities=payload.suggested_capabilities,
        )
        template_id = create_agent_template(conn, template)
        row = get_agent_template(conn, template_id)
        return dict(row) if row else template.model_dump(mode="json")
    finally:
        conn.close()


@router.get("/agents/{agent_id}")
def api_get_agent(
    agent_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Get details of an agent template."""
    try:
        row = get_agent_template(conn, agent_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Agent template '{agent_id}' not found.")
        d = dict(row)
        for k in ("output_expectations", "suggested_capability"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        d["id"] = d.get("template_id")
        d["name"] = d.get("display_name")
        d["instructions"] = d.get("base_instructions")
        d["preferred_model"] = d.get("model_preference")
        d["suggested_capabilities"] = d.get("suggested_capability") or []
        return d
    finally:
        conn.close()


@router.patch("/agents/{agent_id}")
def api_update_agent(
    agent_id: str,
    payload: AgentTemplateUpdateRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Update an existing agent template."""
    try:
        row = get_agent_template(conn, agent_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Agent template '{agent_id}' not found.")
        update_agent_template(conn, {
            "template_id": agent_id,
            "name": payload.name,
            "purpose": payload.purpose,
            "instructions": payload.instructions,
            "working_style": payload.working_style,
            "output_expectations": payload.output_expectations,
            "preferred_model": payload.preferred_model,
            "suggested_capabilities": payload.suggested_capabilities,
        })
        updated = get_agent_template(conn, agent_id)
        return dict(updated) if updated else {"id": agent_id}
    finally:
        conn.close()


@router.delete("/agents/{agent_id}")
def api_delete_agent(
    agent_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Delete an agent template."""
    try:
        row = get_agent_template(conn, agent_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Agent template '{agent_id}' not found.")
        delete_agent_template(conn, agent_id)
        return {"deleted": True, "template_id": agent_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. Workflow Template & Validation Endpoints
# ---------------------------------------------------------------------------


class WorkflowCreateRequest(BaseModel):
    name: str
    goal: str
    definition: dict[str, Any]
    description: str = ""
    is_preset: bool = False


@router.get("/workflows")
def api_list_workflows(conn: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    """List saved workflow templates."""
    try:
        rows = list_workflow_templates(conn)
        results = []
        for r in rows:
            d = dict(r)
            if d.get("definition_json"):
                try:
                    d["definition"] = json.loads(d["definition_json"])
                except Exception:
                    d["definition"] = {}
            results.append(d)
        return results
    finally:
        conn.close()


@router.post("/workflows", status_code=201)
def api_create_workflow(
    payload: WorkflowCreateRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Save a workflow template."""
    try:
        # Validate definition against WorkflowConfig
        try:
            wf = WorkflowConfig.model_validate(payload.definition)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid workflow definition: {exc}")

        import uuid
        wf_id = str(uuid.uuid4())
        create_workflow_template(
            conn=conn,
            workflow_id=wf_id,
            name=payload.name or wf.name,
            definition_json=json.dumps(payload.definition),
            description=payload.description,
            goal=payload.goal or wf.goal,
            schema_version=wf.schema_version,
            is_preset=payload.is_preset,
        )
        row = get_workflow_template(conn, wf_id)
        return dict(row) if row else {"workflow_id": wf_id}
    finally:
        conn.close()


@router.get("/workflows/{workflow_id}")
def api_get_workflow(
    workflow_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Retrieve a specific workflow template."""
    try:
        row = get_workflow_template(conn, workflow_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found.")
        d = dict(row)
        if d.get("definition_json"):
            try:
                d["definition"] = json.loads(d["definition_json"])
            except Exception:
                pass
        return d
    finally:
        conn.close()


@router.delete("/workflows/{workflow_id}")
def api_delete_workflow(
    workflow_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Delete a workflow template."""
    try:
        row = get_workflow_template(conn, workflow_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found.")
        delete_workflow_template(conn, workflow_id)
        return {"deleted": True, "workflow_id": workflow_id}
    finally:
        conn.close()


@router.get("/workflows/{workflow_id}/export")
def api_export_workflow(
    workflow_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Export a portable workflow template with account bindings scrubbed."""
    try:
        row = get_workflow_template(conn, workflow_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found.")
        definition = json.loads(row["definition_json"])
        return sanitize_workflow_for_export(definition)
    finally:
        conn.close()


class WorkflowImportRequest(BaseModel):
    definition: dict[str, Any]
    name: str | None = None
    goal: str | None = None


@router.post("/workflows/import", status_code=201)
def api_import_workflow(
    payload: WorkflowImportRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Import and validate an external workflow template."""
    try:
        raw_def = dict(payload.definition)
        raw_def["draft"] = True
        try:
            wf = WorkflowConfig.model_validate(raw_def)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid imported workflow: {exc}")

        import uuid
        wf_id = str(uuid.uuid4())
        name = payload.name or wf.name
        goal = payload.goal or wf.goal
        create_workflow_template(
            conn=conn,
            workflow_id=wf_id,
            name=name,
            definition_json=json.dumps(wf.model_dump(mode="json")),
            description=f"Imported workflow: {name}",
            goal=goal,
            schema_version=wf.schema_version,
            is_preset=0,
        )
        row = get_workflow_template(conn, wf_id)
        return dict(row) if row else {"workflow_id": wf_id}
    finally:
        conn.close()


@router.post("/workflows/validate")
def api_validate_workflow(workflow_data: dict[str, Any]) -> dict[str, Any]:
    """Validate a workflow definition against domain schemas and acyclic graph contracts."""
    try:
        wf = WorkflowConfig.model_validate(workflow_data)
        return {
            "valid": True,
            "name": wf.name,
            "workers_count": len(wf.workers),
            "stages_count": len(wf.stages),
            "draft": wf.draft,
            "errors": [],
        }
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"valid": False, "errors": [str(exc)]},
        )


# ---------------------------------------------------------------------------
# 5. Run Execution & Lifecycle Endpoints
# ---------------------------------------------------------------------------


class RunCreateRequest(BaseModel):
    workflow: dict[str, Any] | None = None
    preset_id: str | None = None
    workflow_id: str | None = None
    name: str | None = None
    goal: str | None = None
    inputs: dict[str, str] | None = None  # input_id -> bound_value
    account_bindings: dict[str, str] | None = None  # worker_id -> profile_ref/account_id
    model_bindings: dict[str, str] | None = None  # worker_id -> model_id


@router.get("/runs")
def api_list_runs(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    """List runs with optional status filtering."""
    try:
        status_enum = RunStatus(status) if status else None
        rows = list_runs(conn, status=status_enum, limit=limit, offset=offset)
        results = []
        for r in rows:
            d = dict(r)
            if d.get("config_json"):
                try:
                    d["config"] = json.loads(d["config_json"])
                except Exception:
                    pass
            results.append(d)
        return results
    finally:
        conn.close()


@router.post("/runs", status_code=201)
def api_create_run(
    payload: RunCreateRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Create a resolved run configuration in SQLite ready for execution."""
    try:
        raw_config: dict[str, Any]
        if payload.preset_id:
            preset = load_preset_raw(payload.preset_id)
            if not preset:
                raise HTTPException(status_code=404, detail=f"Preset '{payload.preset_id}' not found.")
            raw_config = preset
        elif payload.workflow_id:
            wf_row = get_workflow_template(conn, payload.workflow_id)
            if not wf_row:
                raise HTTPException(status_code=404, detail=f"Workflow template '{payload.workflow_id}' not found.")
            raw_config = json.loads(wf_row["definition_json"])
        elif payload.workflow:
            raw_config = payload.workflow
        else:
            raise HTTPException(status_code=400, detail="Must supply 'workflow', 'preset_id', or 'workflow_id'.")

        # Apply overrides
        if payload.name:
            raw_config["name"] = payload.name
        if payload.goal:
            raw_config["goal"] = payload.goal

        # Bind inputs
        if payload.inputs and "inputs" in raw_config:
            for inp in raw_config["inputs"]:
                if inp.get("id") in payload.inputs:
                    inp["value"] = payload.inputs[inp["id"]]

        # Bind worker accounts & models
        if payload.account_bindings and "workers" in raw_config:
            for w in raw_config["workers"]:
                if w.get("id") in payload.account_bindings:
                    w["account_ref"] = payload.account_bindings[w["id"]]

        if payload.model_bindings and "workers" in raw_config:
            for w in raw_config["workers"]:
                if w.get("id") in payload.model_bindings:
                    w["model"] = payload.model_bindings[w["id"]]

        # Auto-resolve remaining placeholders if not explicitly bound
        avail_accs = list_accounts(conn, enabled_only=True)
        default_acc = avail_accs[0]["profile_ref"] if avail_accs else "default"
        for w in raw_config.get("workers", []):
            if not w.get("account_ref") or str(w.get("account_ref")).startswith("<"):
                w["account_ref"] = default_acc
            if not w.get("model") or str(w.get("model")).startswith("<"):
                w["model"] = "gemini-2.5-pro" if os.environ.get("AGYM_COUNCIL_PROVIDER") != "fake" else "fake-model-1"

        # Ensure required inputs like 'brief' have a value bound
        goal_text = raw_config.get("goal") or "Execute council workflow"
        for inp in raw_config.get("inputs", []):
            if inp.get("required") and not inp.get("value"):
                inp["value"] = (payload.inputs.get(inp.get("id")) if payload.inputs else None) or goal_text

        # Set draft = False for execution validation
        raw_config["draft"] = False

        try:
            wf = WorkflowConfig.model_validate(raw_config)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid execution workflow: {exc}")

        # Persist to database
        run_id = create_run(conn, wf, stages=wf.stages, workers=wf.workers)
        row = get_run(conn, run_id)
        result = dict(row) if row else {"run_id": run_id, "status": "READY"}
        result["config"] = wf.model_dump(mode="json")
        return result
    finally:
        conn.close()


@router.get("/runs/{run_id}")
def api_get_run(
    run_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Retrieve full details of a run including stages, workers, and issues."""
    try:
        row = get_run(conn, run_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        d = dict(row)
        if d.get("config_json"):
            try:
                d["config"] = json.loads(d["config_json"])
            except Exception:
                pass
        d["stages"] = [dict(s) for s in get_stages_for_run(conn, run_id)]
        d["workers"] = [dict(w) for w in get_workers_for_run(conn, run_id)]
        d["issues"] = [dict(i) for i in list_issues_for_run(conn, run_id)]
        return d
    finally:
        conn.close()


@router.post("/runs/{run_id}/start")
async def api_start_run(
    run_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Start execution of a READY run in the background with safety wrapper."""
    try:
        row = get_run(conn, run_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

        current_status = str(row["status"])
        if current_status in ("RUNNING", "COMPLETED"):
            return {
                "run_id": run_id,
                "status": current_status,
                "message": f"Run is already {current_status}.",
            }

        # Verify DB connection path
        db_path = getattr(request.app.state, "db_path", None)
        provider = getattr(request.app.state, "provider_adapter", None) or get_default_provider()

        engine = CouncilEngine(
            db_path=db_path,
            provider=provider,
        )
        _ACTIVE_ENGINES[run_id] = engine

        async def _run_safely() -> None:
            try:
                await engine.execute_run(run_id)
            except Exception as exc:
                c = get_connection(db_path)
                try:
                    update_run_status(c, run_id, status=RunStatus.NEEDS_ATTENTION, error_message=f"Engine runtime error: {exc}")
                    record_event(
                        c,
                        run_id=run_id,
                        event_type="run.log",
                        payload={"level": "ERROR", "message": f"Engine execution crashed: {exc}", "details": str(exc)},
                    )
                    record_issue(c, run_id=run_id, severity="error", category="engine_crashed", message=str(exc))
                finally:
                    c.close()

        # Start execution loop in background task
        task = asyncio.create_task(_run_safely())
        _ACTIVE_TASKS[run_id] = task

        return {
            "run_id": run_id,
            "status": "RUNNING",
            "message": "Execution started.",
        }
    finally:
        conn.close()


@router.post("/runs/{run_id}/pause")
def api_pause_run(
    run_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Signal run to pause after current in-flight turns."""
    try:
        row = get_run(conn, run_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

        engine = _ACTIVE_ENGINES.get(run_id)
        if engine is None:
            db_path = getattr(request.app.state, "db_path", None)
            engine = CouncilEngine(db_path=db_path)

        engine.pause_run(run_id)
        return {"run_id": run_id, "status": "PAUSING", "message": "Pause signal sent."}
    finally:
        conn.close()


@router.post("/runs/{run_id}/resume")
async def api_resume_run(
    run_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Resume a paused workflow run."""
    try:
        row = get_run(conn, run_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

        current_status = str(row["status"])
        if current_status != "PAUSED":
            raise HTTPException(status_code=400, detail=f"Run '{run_id}' is not in PAUSED state (current: {current_status}).")

        db_path = getattr(request.app.state, "db_path", None)
        provider = getattr(request.app.state, "provider_adapter", None) or get_default_provider()

        engine = _ACTIVE_ENGINES.get(run_id) or CouncilEngine(db_path=db_path, provider=provider)
        _ACTIVE_ENGINES[run_id] = engine

        task = asyncio.create_task(engine.resume_run(run_id))
        _ACTIVE_TASKS[run_id] = task

        return {"run_id": run_id, "status": "RUNNING", "message": "Run resumed."}
    finally:
        conn.close()


@router.post("/runs/{run_id}/stop")
def api_stop_run(
    run_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Stop/cancel an in-flight run and terminate child process trees."""
    try:
        row = get_run(conn, run_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

        engine = _ACTIVE_ENGINES.get(run_id)
        if engine is None:
            db_path = getattr(request.app.state, "db_path", None)
            engine = CouncilEngine(db_path=db_path)

        engine.cancel_run(run_id)

        task = _ACTIVE_TASKS.get(run_id)
        if task and not task.done():
            task.cancel()

        return {"run_id": run_id, "status": "CANCELLED", "message": "Run stopped and cancelled."}
    finally:
        conn.close()


class ResolveRunRequest(BaseModel):
    action: str = "retry"
    resolution_note: str | None = None


@router.post("/runs/{run_id}/resolve")
def api_resolve_run(
    run_id: str,
    payload: ResolveRunRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Resolve a run in NEEDS_ATTENTION state back to READY."""
    try:
        row = get_run(conn, run_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

        engine = _ACTIVE_ENGINES.get(run_id)
        if engine is None:
            db_path = getattr(request.app.state, "db_path", None)
            engine = CouncilEngine(db_path=db_path)

        try:
            engine.resolve_run(run_id, action=payload.action, resolution_note=payload.resolution_note)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        return {"run_id": run_id, "status": "READY", "message": "Run resolved to READY."}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. Server-Sent Events (SSE) Endpoint
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/events")
async def api_run_events(
    run_id: str,
    request: Request,
    last_event_id: int | None = Header(None, alias="Last-Event-ID"),
    last_seq_query: int | None = Query(None, alias="last_sequence"),
    live: bool = Query(True, description="Keep stream open for live events"),
    conn: sqlite3.Connection = Depends(get_db),
) -> StreamingResponse:
    """Stream real-time Council events via Server-Sent Events (SSE) with sequence replay."""
    try:
        row = get_run(conn, run_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    finally:
        conn.close()

    db_path = getattr(request.app.state, "db_path", None)
    after_seq = last_event_id or last_seq_query or 0

    return StreamingResponse(
        sse_event_stream(run_id=run_id, db_path=db_path, last_sequence=after_seq, live=live),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 7. Artifacts & Export Endpoints
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/artifacts")
def api_list_run_artifacts(
    run_id: str,
    stage_id: str | None = None,
    released_only: bool = False,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    """List artifacts associated with a run."""
    try:
        row = get_run(conn, run_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        artifacts = list_artifacts_for_run(conn, run_id=run_id, stage_id=stage_id, released_only=released_only)
        return [dict(a) for a in artifacts]
    finally:
        conn.close()


@router.get("/artifacts/{artifact_id}")
def api_download_artifact(
    artifact_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Controlled download of an artifact by ID with traversal protection."""
    try:
        row = get_artifact_record(conn, artifact_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' not found.")

        store = ArtifactStore()
        content_hash = str(row["content_hash"])
        file_path = store.get_artifact_path(content_hash)

        if not file_path.is_file():
            # Fallback path check
            storage_path = str(row["storage_path"])
            try:
                file_path = validate_safe_relative_path(store.root_dir, storage_path)
            except PathTraversalError:
                raise HTTPException(status_code=403, detail="Forbidden: Path traversal detected.")

        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="Artifact content file missing from store.")

        media_type = str(row["media_type"] or "application/octet-stream")
        safe_name = Path(str(row["name"])).name
        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            filename=safe_name,
        )
    finally:
        conn.close()


@router.post("/runs/{run_id}/export")
def api_export_run(
    run_id: str,
    redact: bool = Query(True, description="Scrub secrets and local profile paths"),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Export complete run dossier and released artifacts as a ZIP archive with secret redaction."""
    try:
        row = get_run(conn, run_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

        try:
            zip_bytes = export_run_dossier_bytes(conn, run_id=run_id, redact=redact)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Export failed: {exc}")

        filename = f"council_run_{run_id}.zip"
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        conn.close()
