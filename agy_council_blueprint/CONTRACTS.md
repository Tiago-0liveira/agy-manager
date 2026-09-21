# Build contracts

These are proposed interfaces and semantics. They do not claim existing agym functionality beyond the source review. Implement a small useful slice before adding every entity as a separate service.

## Account and worker records

Council Account: UUID, provider type, stable local profile reference, display label, enabled flag, last auth-verification time/status, observed CLI version, advisory usage plus collection time, per-account concurrency limit. Account credentials stay in the provider-managed profile. Profile name is initially the compatibility reference; display renaming does not rename directories. Missing/deleted profiles become unavailable records without deleting past run provenance.

AgentTemplate: UUID/version, display name, purpose, base instructions, working-style guidance, output expectations, optional model preference and suggested capability level. Templates are reused across accounts. Worker: run ID, worker ID, account reference, frozen template/instructions, per-run task, pinned model, conversation handle, workspace reference, effective policy and status.

Prompt composition order: application execution/data rules; current user goal and constraints; workflow/stage contract; role and per-worker assignment; released evidence and prior outputs with clear delimiters. Lower-priority role/personality text cannot change account, tools or budgets. Repository/document text is context, not permission to rewrite the application contract.

## Provider interface

Conceptual Python interface; types and implementations are to be supplied by the builder:

```python
class ProviderAdapter:
    async def capabilities(self, account_ref): ...
    async def check_account(self, account_ref): ...
    async def discover_models(self, account_ref): ...
    async def run_turn(self, request): ...  # async stream of normalized events
    async def cancel(self, attempt_id): ...
    async def reconcile(self, attempt_id): ...
```

TurnRequest: attempt ID; account reference; worker ID; model identifier; existing conversation handle or null; rendered prompt; input-artifact manifest; working directory; effective permission policy; time/call/token allowance. Configuration validation happens before dispatch. The adapter obtains the profile environment through agym primitives and launches the resolved executable with an argument array, never a string-built shell command.

TurnResult: normalized status; conversation handle; answer/artifact references; provider/process error details; observed model; usage with unknown fields preserved; started/finished timestamps; raw stdout/stderr artifact references. Normalize at least success, needs-auth, unavailable, rate-limited, permission-blocked, timeout, cancelled, malformed-output and unknown-completion. An unknown CLI error is not confidently diagnosed as quota exhaustion.

Do not store or display auth codes, passwords, raw credential files or token-bearing URLs. Provider raw logs may need redaction before release or export. Preserve enough diagnostic information to debug without leaking secrets.

## Example configuration format

The three JSON examples are resolved draft run configurations. The actual UI will expand reusable templates into this form. They contain illustrative profile aliases and model placeholders requiring binding before live execution; they are not launch-ready commands. All have `schema_version: 1`.

Top-level fields:

| Field | Meaning |
|---|---|
| name, goal | Display label and user objective |
| inputs | Named inputs with description and required flag; supplied later by the user |
| limits | Global/per-account concurrency, call/retry/time limits and no automatic account switching |
| workers | Unique worker IDs, friendly names, account aliases, model binding, role, instructions and assignment |
| stages | Ordered independent or collaborative stages |
| final_stage | The stage providing the final deliverable |

Each stage has an ID, kind, worker IDs, prior-stage inputs, instruction, execution context, required sections and failure policy. `kind` is one of independent, critique, revise, synthesize or audit in this initial format. All are model-turn stages; the kinds express intent and default validation, not separate execution engines.

`input_stages` references only earlier completed stages. `context: fresh` starts a new conversation for each participating worker in that stage; `context: continue` resumes that worker's run-local conversation or starts one if it has not yet participated. Continue retains earlier context even when it is not repeated in input_stages. Therefore, any stage needing a genuinely fresh/blinded interpretation must use fresh and record the new conversation segment. The UI must make this distinction visible.

Every stage has `release: after_all_required` and `failure_policy: needs_attention` in the initial examples. Required contributors cannot be dropped automatically. There are no same-stage artifact inputs. Critique recipients get the prior released outputs; workers do not see peers' draft outputs from the current stage. The application initially accepts only these safe release/failure policies; optional-participant policies are a later versioned extension.

`execution_mode: supplied_evidence` means reasoning over released inputs, without independently running shell jobs or editing the user's workspace. Native file/tool access must be effectively controlled to claim that restriction. Local analysis jobs and workspace writing require a later policy-capable extension; do not execute arbitrary commands stored inside an imported workflow.

JSON examples are shape/relationship checked by `validate_examples.py`. That validator is a lightweight reference for this design packet, not a production JSON Schema implementation or a proof of provider/account availability. Production should use typed validation, version migrations and semantic checks.

## State transitions

Run states: DRAFT -> READY -> RUNNING -> COMPLETED. Side states: PAUSING -> PAUSED -> RUNNING; NEEDS_ATTENTION -> READY/RUNNING after a recorded resolution; BUDGET_EXHAUSTED; CANCELLED; FAILED. Terminal states are not silently reset. Explicit budget extension becomes a recorded amendment; a fresh candidate or changed goal can fork a child run.

Attempts: QUEUED -> CLAIMED -> DISPATCHED -> RUNNING -> SUCCEEDED/FAILED/CANCELLED/UNKNOWN. A crash after dispatch is UNKNOWN until reconciliation. Stage completion requires all specified worker outputs to have accepted structure and required artifact evidence. Content review is a separate process and can disagree with a structurally valid output.

A barrier releases immutable artifact IDs for a completed stage in one transaction. The next stage's input manifest is fixed before its workers start. Immutable submissions remain available alongside any later corrections.

## Local API surface

Proposed routes, all local and subject to session/origin checks:

```text
GET/POST  /api/accounts
POST      /api/accounts/{id}/connect
POST      /api/accounts/{id}/check
PATCH     /api/accounts/{id}                 label/enable metadata
GET       /api/accounts/{id}/models
GET/POST  /api/agents
GET/POST  /api/workflows
POST      /api/workflows/validate
POST      /api/runs                         save resolved run
POST      /api/runs/{id}/start
POST      /api/runs/{id}/pause
POST      /api/runs/{id}/resume
POST      /api/runs/{id}/stop
POST      /api/runs/{id}/resolve             bounded attention resolution
GET       /api/runs/{id}
GET       /api/runs/{id}/events             server-sent events with sequence IDs
GET       /api/runs/{id}/artifacts
GET       /api/artifacts/{id}               controlled download, never raw path
POST      /api/runs/{id}/export
```

Mutations carry idempotency keys so a double-click or network retry does not start two runs. SSE reconnect supplies the last sequence ID; replay plus live tail must not duplicate semantic events. UI progress polling is a fallback, not an execution trigger. Do not add public remote access until it has a separately designed authentication and authorization model.

## Event envelope

Every durable event: schema version, monotonically increasing sequence, event ID, run ID, optional worker/attempt/stage IDs, timestamp, type, structured payload. Example types: run.started, stage.started, worker.started, worker.progress, worker.completed, artifact.created, stage.released, issue.opened, account.unavailable, budget.exhausted, run.paused and run.completed. Large content is referenced by artifact ID rather than repeated in events.

The UI may stream text deltas from normalized provider output, but durable completion depends on the final parsed result and recorded artifact. Partial text is visibly partial. No hidden chain-of-thought collection or display is required for observability.

## Recovery contract

Per-account capacity and task leases survive scheduler restart in SQLite. Never infer active work solely from an in-memory Python task. Store OS process identity with sufficient safeguards against PID reuse. On Windows use a tested process-tree lifecycle strategy such as Job Objects; demonstrate child cleanup before claiming Stop works.

Changing the user's CLI version or account setup can invalidate capabilities. Record what the run used; rerun necessary compatibility checks without silently changing model or permissions. A context checkpoint preserves decisions, source references, unresolved objections and action history. It is not a claim of perfect semantic memory. Keep original artifacts accessible when summaries are used.
