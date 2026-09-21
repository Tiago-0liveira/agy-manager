"""SQLite persistence layer for AGYM Council.

Provides database connection management with WAL mode, foreign keys,
5000ms busy timeout, 11 core relational tables, atomic transactions,
worker attempt claiming, stage release barriers, event replay, and idempotency.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from agym.council.models import (
    Account,
    AgentTemplate,
    ArtifactRef,
    Attempt,
    CouncilEvent,
    Run,
    RunStatus,
    StageConfig,
    WorkerConfig,
)
from agym.profiles import _default_data_root


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Connection & Database Path Resolution
# ---------------------------------------------------------------------------


def resolve_db_path(db_path: Path | str | None = None) -> Path:
    """Resolve the SQLite database path."""
    if db_path is not None:
        path = Path(db_path).expanduser().resolve()
        if not str(path).strip():
            raise ValueError("Database path cannot be empty")
        return path
    return (_default_data_root() / "council" / "council.db").resolve()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open an SQLite connection with Council pragmas configured.

    Uses isolation_level=None (autocommit mode) so that explicit
    BEGIN IMMEDIATE transactions can be used without Python interference.
    """
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass

    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Execute a block within an explicit BEGIN IMMEDIATE transaction.

    In autocommit mode, this immediately acquires the SQLite RESERVED lock,
    preventing concurrent write deadlocks.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Relational DDL & Initialization
# ---------------------------------------------------------------------------

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    provider_type TEXT NOT NULL,
    profile_ref TEXT NOT NULL UNIQUE,
    display_label TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    auth_status TEXT NOT NULL DEFAULT 'unknown',
    last_auth_check TEXT,
    cli_version TEXT,
    advisory_usage TEXT,
    usage_collected_at TEXT,
    concurrency_limit INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_profile_ref ON accounts(profile_ref);
CREATE INDEX IF NOT EXISTS idx_accounts_enabled ON accounts(enabled);

CREATE TABLE IF NOT EXISTS agent_templates (
    template_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    display_name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    base_instructions TEXT NOT NULL,
    working_style TEXT NOT NULL DEFAULT '',
    output_expectations TEXT NOT NULL DEFAULT '',
    model_preference TEXT,
    suggested_capability TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_templates (
    workflow_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 1,
    definition_json TEXT NOT NULL,
    is_preset INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'supplied_evidence',
    schema_version INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    current_stage_id TEXT,
    final_stage_id TEXT,
    total_model_calls INTEGER NOT NULL DEFAULT 0,
    total_wall_clock_seconds REAL NOT NULL DEFAULT 0.0,
    error_message TEXT,
    dissent_recorded INTEGER NOT NULL DEFAULT 0,
    final_deliverable_artifact_id TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at);

CREATE TABLE IF NOT EXISTS stages (
    stage_instance_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage_id TEXT NOT NULL,
    sequence_order INTEGER NOT NULL,
    kind TEXT NOT NULL,
    context_policy TEXT NOT NULL,
    release_policy TEXT NOT NULL DEFAULT 'after_all_required',
    failure_policy TEXT NOT NULL DEFAULT 'needs_attention',
    status TEXT NOT NULL DEFAULT 'PENDING',
    instruction TEXT NOT NULL,
    required_sections_json TEXT NOT NULL,
    input_stages_json TEXT NOT NULL,
    workers_json TEXT NOT NULL,
    released_at TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, stage_id)
);
CREATE INDEX IF NOT EXISTS idx_stages_run_order ON stages(run_id, sequence_order);
CREATE INDEX IF NOT EXISTS idx_stages_status ON stages(status);

CREATE TABLE IF NOT EXISTS workers (
    worker_instance_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL,
    name TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    instructions TEXT NOT NULL,
    task TEXT NOT NULL,
    conversation_handle TEXT,
    status TEXT NOT NULL DEFAULT 'IDLE',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, worker_id)
);
CREATE INDEX IF NOT EXISTS idx_workers_run_id ON workers(run_id);
CREATE INDEX IF NOT EXISTS idx_workers_account_ref ON workers(account_ref);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    input_digest TEXT,
    rendered_prompt TEXT,
    workspace_dir TEXT,
    pid INTEGER,
    process_start_time REAL,
    exit_code INTEGER,
    error_details TEXT,
    model_used TEXT,
    usage_json TEXT,
    prompt_artifact_id TEXT,
    result_artifact_id TEXT,
    stdout_artifact_id TEXT,
    stderr_artifact_id TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_run_stage_worker ON attempts(run_id, stage_id, worker_id);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);
CREATE INDEX IF NOT EXISTS idx_attempts_account_ref ON attempts(account_ref);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage_id TEXT,
    worker_id TEXT,
    attempt_id TEXT REFERENCES attempts(attempt_id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'text/plain',
    storage_path TEXT NOT NULL,
    released INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run_stage ON artifacts(run_id, stage_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run_released ON artifacts(run_id, released);
CREATE INDEX IF NOT EXISTS idx_artifacts_content_hash ON artifacts(content_hash);

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage_id TEXT,
    worker_id TEXT,
    attempt_id TEXT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, sequence);

CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage_id TEXT,
    worker_id TEXT,
    attempt_id TEXT,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolution_note TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_run_unresolved ON issues(run_id, resolved);

CREATE TABLE IF NOT EXISTS idempotency_records (
    key TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    response_status_code INTEGER,
    response_headers_json TEXT,
    response_body TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_idempotency_status ON idempotency_records(status);
"""


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Initialize the SQLite database, creating all tables and indexes.

    Idempotent: safe to call repeatedly.
    """
    conn = get_connection(db_path)
    conn.executescript(SCHEMA_DDL)
    with immediate_transaction(conn):
        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 1"
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, description) VALUES (1, ?, ?)",
                (_utc_now_iso(), "Initial AGYM Council schema v1"),
            )
    return conn


# ---------------------------------------------------------------------------
# Account Persistence
# ---------------------------------------------------------------------------


def create_account(conn: sqlite3.Connection, account: Account | dict[str, Any]) -> str:
    """Insert a new account record into SQLite."""
    if isinstance(account, Account):
        d = account.model_dump(mode="json")
    else:
        d = dict(account)

    account_id = d.get("id") or str(uuid.uuid4())
    now_iso = _utc_now_iso()
    created_at = d.get("created_at") or now_iso
    updated_at = d.get("updated_at") or now_iso

    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT INTO accounts (
                account_id, provider_type, profile_ref, display_label, enabled,
                auth_status, last_auth_check, cli_version, advisory_usage,
                usage_collected_at, concurrency_limit, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                d.get("provider") or d.get("provider_type") or "antigravity",
                d["profile_ref"],
                d.get("label") or d.get("display_label") or d["profile_ref"],
                1 if d.get("enabled", True) else 0,
                str(d.get("auth_status") or "unknown"),
                d.get("last_auth_check"),
                d.get("cli_version"),
                json.dumps(d.get("usage_advisory") or {}) if d.get("usage_advisory") is not None else None,
                d.get("usage_collected_at"),
                int(d.get("concurrency_limit") or 1),
                created_at,
                updated_at,
            ),
        )
    return account_id


def get_account(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    """Retrieve an account by its unique ID."""
    return conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()


def get_account_by_profile_ref(conn: sqlite3.Connection, profile_ref: str) -> sqlite3.Row | None:
    """Retrieve an account by its profile reference."""
    return conn.execute("SELECT * FROM accounts WHERE profile_ref = ?", (profile_ref,)).fetchone()


def list_accounts(conn: sqlite3.Connection, enabled_only: bool = False) -> list[sqlite3.Row]:
    """List accounts in the database."""
    if enabled_only:
        return conn.execute(
            "SELECT * FROM accounts WHERE enabled = 1 ORDER BY display_label ASC"
        ).fetchall()
    return conn.execute("SELECT * FROM accounts ORDER BY display_label ASC").fetchall()


def update_account(conn: sqlite3.Connection, account: Account | dict[str, Any]) -> None:
    """Update an existing account record."""
    if isinstance(account, Account):
        d = account.model_dump(mode="json")
    else:
        d = dict(account)

    account_id = d.get("id") or d.get("account_id")
    if not account_id:
        raise ValueError("account_id is required for update")

    existing = get_account(conn, account_id)
    if not existing:
        raise ValueError(f"Account {account_id} not found")

    provider = d.get("provider") or d.get("provider_type") or existing["provider_type"]
    profile_ref = d.get("profile_ref") or existing["profile_ref"]
    label = d.get("label") or d.get("display_label") or existing["display_label"]
    enabled = d.get("enabled") if "enabled" in d else bool(existing["enabled"])
    auth_status = d.get("auth_status") if "auth_status" in d else existing["auth_status"]
    last_auth_check = d.get("last_auth_check") if "last_auth_check" in d else existing["last_auth_check"]
    cli_version = d.get("cli_version") if "cli_version" in d else existing["cli_version"]

    if "usage_advisory" in d or "advisory_usage" in d:
        advisory_val = d.get("usage_advisory") if "usage_advisory" in d else d.get("advisory_usage")
        advisory_json = json.dumps(advisory_val) if advisory_val is not None else None
    else:
        advisory_json = existing["advisory_usage"]

    usage_collected_at = d.get("usage_collected_at") if "usage_collected_at" in d else existing["usage_collected_at"]
    concurrency_limit = d.get("concurrency_limit") if "concurrency_limit" in d else existing["concurrency_limit"]

    now_iso = _utc_now_iso()
    with immediate_transaction(conn):
        conn.execute(
            """
            UPDATE accounts SET
                provider_type = ?,
                profile_ref = ?,
                display_label = ?,
                enabled = ?,
                auth_status = ?,
                last_auth_check = ?,
                cli_version = ?,
                advisory_usage = ?,
                usage_collected_at = ?,
                concurrency_limit = ?,
                updated_at = ?
            WHERE account_id = ?
            """,
            (
                provider,
                profile_ref,
                label,
                1 if enabled else 0,
                str(auth_status),
                last_auth_check,
                cli_version,
                advisory_json,
                usage_collected_at,
                int(concurrency_limit or 1),
                now_iso,
                account_id,
            ),
        )


def delete_account(conn: sqlite3.Connection, account_id: str) -> bool:
    """Delete an account record."""
    with immediate_transaction(conn):
        cur = conn.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Agent Template Persistence
# ---------------------------------------------------------------------------


def create_agent_template(
    conn: sqlite3.Connection,
    template: AgentTemplate | dict[str, Any],
) -> str:
    """Create or replace an agent template."""
    if isinstance(template, AgentTemplate):
        d = template.model_dump()
    else:
        d = dict(template)

    template_id = str(d.get("id") or d.get("template_id") or uuid.uuid4())
    name = str(d.get("name") or d.get("display_name") or "")
    purpose = str(d.get("purpose") or "")
    instructions = str(d.get("instructions") or d.get("base_instructions") or "")
    working_style = str(d.get("working_style") or "")

    output_exp = d.get("output_expectations") or ["findings", "evidence_or_assumptions", "uncertainties", "next_action"]
    output_exp_str = json.dumps(output_exp) if isinstance(output_exp, list) else str(output_exp)

    preferred_model = d.get("preferred_model") or d.get("model_preference")
    suggested_caps = d.get("suggested_capabilities") or d.get("suggested_capability") or []
    suggested_caps_str = json.dumps(suggested_caps) if isinstance(suggested_caps, list) else str(suggested_caps)

    now_iso = _utc_now_iso()
    created_at = str(d.get("created_at") or now_iso)
    updated_at = str(d.get("updated_at") or now_iso)
    version = int(d.get("version") or 1)

    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT OR REPLACE INTO agent_templates (
                template_id, version, display_name, purpose, base_instructions,
                working_style, output_expectations, model_preference, suggested_capability,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
                version,
                name,
                purpose,
                instructions,
                working_style,
                output_exp_str,
                preferred_model,
                suggested_caps_str,
                created_at,
                updated_at,
            ),
        )
    return template_id


def get_agent_template(conn: sqlite3.Connection, template_id: str) -> sqlite3.Row | None:
    """Retrieve an agent template by ID."""
    return conn.execute(
        "SELECT * FROM agent_templates WHERE template_id = ?", (template_id,)
    ).fetchone()


def list_agent_templates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all agent templates ordered by display_name."""
    return conn.execute("SELECT * FROM agent_templates ORDER BY display_name ASC").fetchall()


def update_agent_template(
    conn: sqlite3.Connection,
    template: AgentTemplate | dict[str, Any],
) -> None:
    """Update existing agent template fields."""
    if isinstance(template, AgentTemplate):
        d = template.model_dump()
    else:
        d = dict(template)

    template_id = str(d.get("id") or d.get("template_id"))
    now_iso = _utc_now_iso()
    output_exp = d.get("output_expectations")
    output_exp_str = json.dumps(output_exp) if isinstance(output_exp, list) else (str(output_exp) if output_exp is not None else None)
    suggested_caps = d.get("suggested_capabilities")
    suggested_caps_str = json.dumps(suggested_caps) if isinstance(suggested_caps, list) else (str(suggested_caps) if suggested_caps is not None else None)

    with immediate_transaction(conn):
        conn.execute(
            """
            UPDATE agent_templates
            SET display_name = COALESCE(?, display_name),
                purpose = COALESCE(?, purpose),
                base_instructions = COALESCE(?, base_instructions),
                working_style = COALESCE(?, working_style),
                output_expectations = COALESCE(?, output_expectations),
                model_preference = COALESCE(?, model_preference),
                suggested_capability = COALESCE(?, suggested_capability),
                updated_at = ?
            WHERE template_id = ?
            """,
            (
                d.get("name") or d.get("display_name"),
                d.get("purpose"),
                d.get("instructions") or d.get("base_instructions"),
                d.get("working_style"),
                output_exp_str,
                d.get("preferred_model") or d.get("model_preference"),
                suggested_caps_str,
                now_iso,
                template_id,
            ),
        )


def delete_agent_template(conn: sqlite3.Connection, template_id: str) -> bool:
    """Delete an agent template by ID."""
    with immediate_transaction(conn):
        cur = conn.execute("DELETE FROM agent_templates WHERE template_id = ?", (template_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Workflow Template Persistence
# ---------------------------------------------------------------------------


def create_workflow_template(
    conn: sqlite3.Connection,
    workflow_id: str,
    name: str,
    definition_json: str,
    description: str = "",
    goal: str = "",
    schema_version: int = 1,
    is_preset: bool = False,
) -> None:
    """Insert or replace a workflow template."""
    now_iso = _utc_now_iso()
    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT OR REPLACE INTO workflow_templates (
                workflow_id, version, name, description, goal,
                schema_version, definition_json, is_preset, created_at, updated_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                name,
                description,
                goal,
                schema_version,
                definition_json,
                1 if is_preset else 0,
                now_iso,
                now_iso,
            ),
        )


def get_workflow_template(conn: sqlite3.Connection, workflow_id: str) -> sqlite3.Row | None:
    """Retrieve a workflow template by ID."""
    return conn.execute(
        "SELECT * FROM workflow_templates WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()


def list_workflow_templates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all workflow templates."""
    return conn.execute("SELECT * FROM workflow_templates ORDER BY name ASC").fetchall()


def delete_workflow_template(conn: sqlite3.Connection, workflow_id: str) -> bool:
    """Delete a workflow template by ID."""
    with immediate_transaction(conn):
        cur = conn.execute("DELETE FROM workflow_templates WHERE workflow_id = ?", (workflow_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Run, Stage, and Worker Persistence
# ---------------------------------------------------------------------------


def create_run(
    conn: sqlite3.Connection,
    run: Run | WorkflowConfig | dict[str, Any],
    stages: list[StageConfig] | None = None,
    workers: list[WorkerConfig] | None = None,
) -> str:
    """Create a new run along with its frozen stages and workers."""
    from agym.council.models import WorkflowConfig

    if isinstance(run, dict):
        if "config" in run:
            run = Run.model_validate(run)
        else:
            run = Run(config=WorkflowConfig.model_validate(run), status=RunStatus.READY)
    elif isinstance(run, WorkflowConfig):
        run = Run(config=run, status=RunStatus.READY)

    run_d = run.model_dump(mode="json")
    run_id = run_d["id"]
    cfg = run_d["config"]
    now_iso = _utc_now_iso()

    with immediate_transaction(conn):
        # 1. Insert run
        conn.execute(
            """
            INSERT INTO runs (
                run_id, name, goal, status, execution_mode, schema_version,
                config_json, inputs_json, limits_json, current_stage_id,
                final_stage_id, total_model_calls, total_wall_clock_seconds,
                error_message, dissent_recorded, final_deliverable_artifact_id,
                created_at, started_at, finished_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                cfg.get("name", "Unnamed Run"),
                cfg.get("goal", ""),
                str(run_d.get("status") or "READY"),
                cfg.get("execution_mode", "supplied_evidence"),
                cfg.get("schema_version", 1),
                json.dumps(cfg),
                json.dumps(cfg.get("inputs", [])),
                json.dumps(cfg.get("limits", {})),
                run_d.get("current_stage_id"),
                cfg.get("final_stage"),
                run_d.get("model_calls_made", 0),
                0.0,
                None,
                1 if run_d.get("dissent_recorded") else 0,
                run_d.get("final_deliverable_artifact_id"),
                run_d.get("created_at") or now_iso,
                run_d.get("started_at"),
                run_d.get("finished_at"),
                run_d.get("updated_at") or now_iso,
            ),
        )

        # 2. Insert stages
        stage_list = stages or [StageConfig.model_validate(s) for s in cfg.get("stages", [])]
        for order, stage in enumerate(stage_list):
            stage_instance_id = f"{run_id}:{stage.id}"
            conn.execute(
                """
                INSERT INTO stages (
                    stage_instance_id, run_id, stage_id, sequence_order, kind,
                    context_policy, release_policy, failure_policy, status,
                    instruction, required_sections_json, input_stages_json,
                    workers_json, released_at, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    stage_instance_id,
                    run_id,
                    stage.id,
                    order,
                    str(stage.kind),
                    str(stage.context),
                    str(stage.release),
                    str(stage.failure_policy),
                    stage.instruction,
                    json.dumps(stage.required_sections),
                    json.dumps(stage.input_stages),
                    json.dumps(stage.workers),
                    now_iso,
                    now_iso,
                ),
            )

        # 3. Insert workers
        worker_list = workers or [WorkerConfig.model_validate(w) for w in cfg.get("workers", [])]
        for worker in worker_list:
            worker_instance_id = f"{run_id}:{worker.id}"
            conn.execute(
                """
                INSERT INTO workers (
                    worker_instance_id, run_id, worker_id, name, account_ref,
                    model, role, instructions, task, conversation_handle,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'IDLE', ?, ?)
                """,
                (
                    worker_instance_id,
                    run_id,
                    worker.id,
                    worker.name,
                    worker.account_ref,
                    worker.model,
                    worker.role,
                    worker.instructions,
                    worker.task,
                    now_iso,
                    now_iso,
                ),
            )

    return run_id


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    """Retrieve run record by run_id."""
    return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()


def list_runs(
    conn: sqlite3.Connection,
    status: RunStatus | str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """List runs, optionally filtered by status."""
    if status is not None:
        status_str = status.value if isinstance(status, RunStatus) else str(status)
        return conn.execute(
            "SELECT * FROM runs WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (status_str, limit, offset),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM runs ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()


def update_run_status(
    conn: sqlite3.Connection,
    run_id: str,
    status: RunStatus | str | None = None,
    error_message: str | None = None,
    current_stage_id: str | None = None,
    final_deliverable_artifact_id: str | None = None,
    dissent_recorded: bool | None = None,
    total_model_calls: int | None = None,
    total_wall_clock_seconds: float | None = None,
) -> None:
    """Update a run's status and lifecycle attributes."""
    now_iso = _utc_now_iso()

    updates = ["updated_at = ?"]
    params: list[Any] = [now_iso]

    if status is not None:
        status_str = status.value if isinstance(status, RunStatus) else str(status)
        updates.append("status = ?")
        params.append(status_str)

        if status_str == "RUNNING":
            updates.append("started_at = COALESCE(started_at, ?)")
            params.append(now_iso)
        elif status_str in ("COMPLETED", "FAILED", "CANCELLED", "NEEDS_ATTENTION", "BUDGET_EXHAUSTED"):
            updates.append("finished_at = COALESCE(finished_at, ?)")
            params.append(now_iso)


    if error_message is not None:
        updates.append("error_message = ?")
        params.append(error_message)
    if current_stage_id is not None:
        updates.append("current_stage_id = ?")
        params.append(current_stage_id)
    if final_deliverable_artifact_id is not None:
        updates.append("final_deliverable_artifact_id = ?")
        params.append(final_deliverable_artifact_id)
    if dissent_recorded is not None:
        updates.append("dissent_recorded = ?")
        params.append(1 if dissent_recorded else 0)
    if total_model_calls is not None:
        updates.append("total_model_calls = ?")
        params.append(total_model_calls)
    if total_wall_clock_seconds is not None:
        updates.append("total_wall_clock_seconds = ?")
        params.append(total_wall_clock_seconds)

    params.append(run_id)
    sql = f"UPDATE runs SET {', '.join(updates)} WHERE run_id = ?"

    with immediate_transaction(conn):
        conn.execute(sql, params)


def get_stages_for_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Get all stages for a run in execution sequence order."""
    return conn.execute(
        "SELECT * FROM stages WHERE run_id = ? ORDER BY sequence_order ASC", (run_id,)
    ).fetchall()


def get_stage(conn: sqlite3.Connection, run_id: str, stage_id: str) -> sqlite3.Row | None:
    """Get a specific stage within a run."""
    return conn.execute(
        "SELECT * FROM stages WHERE run_id = ? AND stage_id = ?", (run_id, stage_id)
    ).fetchone()


def update_stage_status(
    conn: sqlite3.Connection,
    run_id: str,
    stage_id: str,
    status: Any,
    error_message: str | None = None,
) -> None:
    """Update a stage's status."""
    status_str = status.value if hasattr(status, "value") else str(status)
    now_iso = _utc_now_iso()
    with immediate_transaction(conn):
        conn.execute(
            """
            UPDATE stages SET status = ?, error_message = ?, updated_at = ?
            WHERE run_id = ? AND stage_id = ?
            """,
            (status_str, error_message, now_iso, run_id, stage_id),
        )


def get_workers_for_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Get all participating workers for a run."""
    return conn.execute("SELECT * FROM workers WHERE run_id = ?", (run_id,)).fetchall()


def update_worker_status(
    conn: sqlite3.Connection,
    run_id: str,
    worker_id: str,
    status: Any,
    conversation_handle: str | None = None,
) -> None:
    """Update a worker's status and optionally conversation handle."""
    status_str = status.value if hasattr(status, "value") else str(status)
    now_iso = _utc_now_iso()
    updates = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status_str, now_iso]

    if conversation_handle is not None:
        updates.append("conversation_handle = ?")
        params.append(conversation_handle)

    params.extend([run_id, worker_id])
    sql = f"UPDATE workers SET {', '.join(updates)} WHERE run_id = ? AND worker_id = ?"
    with immediate_transaction(conn):
        conn.execute(sql, params)


# ---------------------------------------------------------------------------
# Attempt Tracking & Concurrency Claiming
# ---------------------------------------------------------------------------


def create_attempt(conn: sqlite3.Connection, attempt: Attempt) -> str:
    """Create a new attempt record."""
    d = attempt.model_dump(mode="json")
    attempt_id = d["id"]
    now_iso = _utc_now_iso()

    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT INTO attempts (
                attempt_id, run_id, stage_id, worker_id, account_ref,
                attempt_number, status, input_digest, rendered_prompt,
                workspace_dir, pid, process_start_time, exit_code,
                error_details, model_used, usage_json, prompt_artifact_id,
                result_artifact_id, stdout_artifact_id, stderr_artifact_id,
                started_at, finished_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                d["run_id"],
                d["stage_id"],
                d["worker_id"],
                d["account_ref"],
                d.get("attempt_number", 1),
                str(d.get("status") or "QUEUED"),
                d.get("working_directory"),
                d.get("pid"),
                d.get("process_start_time"),
                d.get("exit_code"),
                d.get("error_message"),
                d.get("model"),
                d.get("prompt_artifact_id"),
                d.get("result_artifact_id"),
                d.get("stdout_artifact_id"),
                d.get("stderr_artifact_id"),
                d.get("started_at"),
                d.get("finished_at"),
                d.get("created_at") or now_iso,
                now_iso,
            ),
        )
    return attempt_id


def get_attempt(conn: sqlite3.Connection, attempt_id: str) -> sqlite3.Row | None:
    """Retrieve attempt record by attempt_id."""
    return conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()


def claim_attempt_atomic(conn: sqlite3.Connection, attempt_id: str) -> bool:
    """Atomically transition an attempt from QUEUED to CLAIMED.

    Returns True if successfully claimed; False if already claimed or not found.
    """
    with immediate_transaction(conn):
        row = conn.execute(
            "SELECT status FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if not row or row["status"] != "QUEUED":
            return False

        now_iso = _utc_now_iso()
        conn.execute(
            "UPDATE attempts SET status = 'CLAIMED', updated_at = ? WHERE attempt_id = ?",
            (now_iso, attempt_id),
        )
        return True


def update_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    status: str,
    pid: int | None = None,
    process_start_time: float | None = None,
    exit_code: int | None = None,
    error_details: str | None = None,
    model_used: str | None = None,
    usage_json: str | None = None,
    workspace_dir: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    prompt_artifact_id: str | None = None,
    result_artifact_id: str | None = None,
    stdout_artifact_id: str | None = None,
    stderr_artifact_id: str | None = None,
    rendered_prompt: str | None = None,
) -> None:
    """Update attempt execution status and metrics."""
    now_iso = _utc_now_iso()
    updates = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, now_iso]

    if rendered_prompt is not None:
        updates.append("rendered_prompt = ?")
        params.append(rendered_prompt)

    if pid is not None:
        updates.append("pid = ?")
        params.append(pid)
    if process_start_time is not None:
        updates.append("process_start_time = ?")
        params.append(process_start_time)
    if exit_code is not None:
        updates.append("exit_code = ?")
        params.append(exit_code)
    if error_details is not None:
        updates.append("error_details = ?")
        params.append(error_details)
    if model_used is not None:
        updates.append("model_used = ?")
        params.append(model_used)
    if usage_json is not None:
        updates.append("usage_json = ?")
        params.append(usage_json)
    if workspace_dir is not None:
        updates.append("workspace_dir = ?")
        params.append(workspace_dir)
    if started_at is not None:
        updates.append("started_at = ?")
        params.append(started_at)
    if finished_at is not None:
        updates.append("finished_at = ?")
        params.append(finished_at)
    if prompt_artifact_id is not None:
        updates.append("prompt_artifact_id = ?")
        params.append(prompt_artifact_id)
    if result_artifact_id is not None:
        updates.append("result_artifact_id = ?")
        params.append(result_artifact_id)
    if stdout_artifact_id is not None:
        updates.append("stdout_artifact_id = ?")
        params.append(stdout_artifact_id)
    if stderr_artifact_id is not None:
        updates.append("stderr_artifact_id = ?")
        params.append(stderr_artifact_id)

    params.append(attempt_id)
    sql = f"UPDATE attempts SET {', '.join(updates)} WHERE attempt_id = ?"
    with immediate_transaction(conn):
        conn.execute(sql, params)


def get_in_flight_attempts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Retrieve all attempts currently active (CLAIMED, DISPATCHED, RUNNING)."""
    return conn.execute(
        """
        SELECT * FROM attempts
        WHERE status IN ('CLAIMED', 'DISPATCHED', 'RUNNING')
        ORDER BY created_at ASC
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# Artifact DB Records & Stage Release Barrier
# ---------------------------------------------------------------------------


def store_artifact_record(conn: sqlite3.Connection, artifact: ArtifactRef) -> None:
    """Insert or update an artifact record in SQLite."""
    d = artifact.model_dump(mode="json")
    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT OR REPLACE INTO artifacts (
                artifact_id, run_id, stage_id, worker_id, attempt_id,
                name, content_hash, byte_size, media_type, storage_path,
                released, metadata_json, created_at, released_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                d["id"],
                d["run_id"],
                d.get("stage_id"),
                d.get("worker_id"),
                d.get("attempt_id"),
                d["name"],
                d["sha256"],
                d["size_bytes"],
                d.get("mime_type", "text/plain"),
                d["path"],
                1 if d.get("released") else 0,
                d.get("created_at") or _utc_now_iso(),
                _utc_now_iso() if d.get("released") else None,
            ),
        )


def get_artifact_record(conn: sqlite3.Connection, artifact_id: str) -> sqlite3.Row | None:
    """Retrieve an artifact record by artifact_id."""
    return conn.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()


def list_artifacts_for_run(
    conn: sqlite3.Connection,
    run_id: str,
    stage_id: str | None = None,
    released_only: bool = False,
) -> list[sqlite3.Row]:
    """List artifacts for a given run."""
    conditions = ["run_id = ?"]
    params: list[Any] = [run_id]

    if stage_id is not None:
        conditions.append("stage_id = ?")
        params.append(stage_id)
    if released_only:
        conditions.append("released = 1")

    sql = f"SELECT * FROM artifacts WHERE {' AND '.join(conditions)} ORDER BY created_at ASC"
    return conn.execute(sql, params).fetchall()


def list_released_artifacts_for_stages(
    conn: sqlite3.Connection,
    run_id: str,
    stage_ids: list[str],
) -> list[sqlite3.Row]:
    """Query only released artifacts from specified prerequisite stages.

    Guarantees that unreleased draft artifacts (released=0) are never returned.
    """
    if not stage_ids:
        return []
    placeholders = ",".join("?" for _ in stage_ids)
    sql = f"""
        SELECT * FROM artifacts
        WHERE run_id = ? AND released = 1 AND stage_id IN ({placeholders})
        ORDER BY created_at ASC
    """
    return conn.execute(sql, [run_id, *stage_ids]).fetchall()


def release_stage_artifacts_atomic(
    conn: sqlite3.Connection,
    run_id: str,
    stage_id: str,
) -> None:
    """Atomically commit stage completion and release all stage artifacts.

    Enforces Invariant 3 (Physical Release Barriers):
    Marks all draft artifacts for this stage released = 1 and updates the stage
    status to COMPLETED in the same atomic transaction.
    """
    now_iso = _utc_now_iso()
    with immediate_transaction(conn):
        # 1. Verify stage exists
        stage = conn.execute(
            "SELECT * FROM stages WHERE run_id = ? AND stage_id = ?",
            (run_id, stage_id),
        ).fetchone()
        if not stage:
            raise ValueError(f"Stage '{stage_id}' not found in run '{run_id}'")

        # 2. Release all draft artifacts for this stage
        conn.execute(
            """
            UPDATE artifacts
            SET released = 1, released_at = ?
            WHERE run_id = ? AND stage_id = ? AND released = 0
            """,
            (now_iso, run_id, stage_id),
        )

        # 3. Mark stage as COMPLETED
        conn.execute(
            """
            UPDATE stages
            SET status = 'COMPLETED', released_at = ?, updated_at = ?
            WHERE run_id = ? AND stage_id = ?
            """,
            (now_iso, now_iso, run_id, stage_id),
        )

        # 4. Insert durable stage.released event
        event_id = str(uuid.uuid4())
        payload = json.dumps({"stage_id": stage_id, "released_at": now_iso})
        conn.execute(
            """
            INSERT INTO events (event_id, run_id, stage_id, timestamp, event_type, payload_json)
            VALUES (?, ?, ?, ?, 'stage.released', ?)
            """,
            (event_id, run_id, stage_id, now_iso, payload),
        )


# ---------------------------------------------------------------------------
# Durable Events Log & SSE Replay
# ---------------------------------------------------------------------------


def record_event(
    conn: sqlite3.Connection,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
    stage_id: str | None = None,
    worker_id: str | None = None,
    attempt_id: str | None = None,
) -> CouncilEvent:
    """Record a durable, monotonically sequenced event."""
    event_id = str(uuid.uuid4())
    now_iso = _utc_now_iso()
    payload_json = json.dumps(payload)

    with immediate_transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO events (
                event_id, run_id, stage_id, worker_id, attempt_id,
                timestamp, event_type, payload_json, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (event_id, run_id, stage_id, worker_id, attempt_id, now_iso, event_type, payload_json),
        )
        seq = cur.lastrowid or 1

    ev = CouncilEvent(
        schema_version=1,
        sequence=seq,
        event_id=event_id,
        run_id=run_id,
        stage_id=stage_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
        timestamp=now_iso,
        type=event_type,
        payload=payload,
    )

    for listener in list(_EVENT_LISTENERS):
        try:
            listener(ev)
        except Exception:
            pass

    return ev


_EVENT_LISTENERS: list[Any] = []


def register_event_listener(listener: Any) -> None:
    """Register a callback to receive newly recorded CouncilEvents."""
    if listener not in _EVENT_LISTENERS:
        _EVENT_LISTENERS.append(listener)


def unregister_event_listener(listener: Any) -> None:
    """Unregister an event listener callback."""
    if listener in _EVENT_LISTENERS:
        _EVENT_LISTENERS.remove(listener)


def list_events_for_run(
    conn: sqlite3.Connection,
    run_id: str,
    after_sequence: int | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Fetch events for SSE streaming and replay via Last-Event-ID."""
    if after_sequence is not None:
        return conn.execute(
            """
            SELECT * FROM events
            WHERE run_id = ? AND sequence > ?
            ORDER BY sequence ASC
            LIMIT ?
            """,
            (run_id, after_sequence, limit),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM events
        WHERE run_id = ?
        ORDER BY sequence ASC
        LIMIT ?
        """,
        (run_id, limit),
    ).fetchall()


# ---------------------------------------------------------------------------
# Mutation Idempotency Records
# ---------------------------------------------------------------------------


def get_idempotency_record(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    """Retrieve an idempotency record by key."""
    return conn.execute(
        "SELECT * FROM idempotency_records WHERE key = ?", (key,)
    ).fetchone()


def start_idempotency_record(
    conn: sqlite3.Connection,
    key: str,
    action: str,
    request_hash: str,
) -> bool:
    """Atomically record start of an idempotent operation.

    Returns True if key is new and registered; False if already exists.
    """
    now_iso = _utc_now_iso()
    with immediate_transaction(conn):
        row = conn.execute(
            "SELECT status FROM idempotency_records WHERE key = ?", (key,)
        ).fetchone()
        if row:
            return False

        conn.execute(
            """
            INSERT INTO idempotency_records (
                key, action, request_hash, status, created_at
            ) VALUES (?, ?, ?, 'IN_PROGRESS', ?)
            """,
            (key, action, request_hash, now_iso),
        )
        return True


def complete_idempotency_record(
    conn: sqlite3.Connection,
    key: str,
    status_code: int,
    body: str,
    headers_json: str | None = None,
) -> None:
    """Mark an idempotency record as COMPLETED with cached response."""
    with immediate_transaction(conn):
        conn.execute(
            """
            UPDATE idempotency_records SET
                status = 'COMPLETED',
                response_status_code = ?,
                response_body = ?,
                response_headers_json = ?
            WHERE key = ?
            """,
            (status_code, body, headers_json, key),
        )


# ---------------------------------------------------------------------------
# Issue & Diagnostic Records
# ---------------------------------------------------------------------------


def record_issue(
    conn: sqlite3.Connection,
    run_id: str,
    code: str = "warning",
    message: str = "",
    severity: str = "warning",
    stage_id: str | None = None,
    worker_id: str | None = None,
    attempt_id: str | None = None,
    details: dict[str, Any] | None = None,
    category: str | None = None,
) -> str:
    """Record an issue requiring diagnostic attention."""
    issue_code = category or code
    issue_id = str(uuid.uuid4())
    now_iso = _utc_now_iso()
    details_json = json.dumps(details or {})


    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT INTO issues (
                issue_id, run_id, stage_id, worker_id, attempt_id,
                severity, code, message, details_json, resolved, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (issue_id, run_id, stage_id, worker_id, attempt_id, severity, issue_code, message, details_json, now_iso),
        )
    return issue_id


def list_issues_for_run(
    conn: sqlite3.Connection,
    run_id: str,
    unresolved_only: bool = False,
) -> list[sqlite3.Row]:
    """List diagnostic issues recorded for a run."""
    if unresolved_only:
        return conn.execute(
            "SELECT * FROM issues WHERE run_id = ? AND resolved = 0 ORDER BY created_at ASC",
            (run_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM issues WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
    ).fetchall()


def resolve_issue(
    conn: sqlite3.Connection,
    issue_id: str,
    resolution_note: str = "",
) -> None:
    """Mark an issue as resolved."""
    now_iso = _utc_now_iso()
    with immediate_transaction(conn):
        conn.execute(
            """
            UPDATE issues SET resolved = 1, resolution_note = ?, resolved_at = ?
            WHERE issue_id = ?
            """,
            (resolution_note, now_iso, issue_id),
        )
