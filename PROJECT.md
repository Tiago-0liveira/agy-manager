# Project: AGYM Council Multi-Account Multi-Worker Orchestration

## Architecture
AGYM Council is a multi-account, multi-worker orchestration platform built as an extension on top of the zero-dependency `agy-manager` core. It enables local councils of specialized agents (coordinators, analysts, critics, synthesizers) running against official Antigravity CLI profiles with strict stage barriers, per-account serialization, dissent preservation, crash recovery, and a local web interface.

### Component Structure
```
agym/
├── cli.py                     # Existing zero-dep CLI (agym <profile> continues to launch profile)
├── profiles.py                # ProfileStore with cross-process file locking (config.lock)
├── launcher.py                # Process & environment construction
└── council/                   # Council extension package (optional dependencies)
    ├── __init__.py
    ├── cli.py                 # agym-council CLI entry point
    ├── models.py              # Pydantic v2 domain models
    ├── storage.py             # SQLite persistence (WAL mode, busy_timeout=5000)
    ├── artifacts.py           # Content-addressed storage (SHA-256)
    ├── context.py             # Hierarchical prompt assembly & workspace staging
    ├── scheduler.py           # Dual-lease capacity scheduler (global + per-account)
    ├── engine.py              # Durable stage runner & atomic release barriers
    ├── recovery.py            # Startup crash reconciler (PID + start time check)
    ├── providers/
    │   ├── base.py            # ProviderAdapter ABC
    │   ├── fake.py            # Deterministic FakeProviderAdapter (zero-cost testing)
    │   └── antigravity.py     # Headless official agy CLI adapter
    ├── api/
    │   ├── app.py             # Loopback FastAPI application
    │   ├── routes.py          # REST endpoints (accounts, workflows, runs, artifacts)
    │   ├── events.py          # SSE streaming with Last-Event-ID replay
    │   └── auth_broker.py     # Terminal launch / headless fallback broker
    └── presets/               # Bundled workflow presets
        ├── quick_council.json
        ├── research_and_design.json
        └── review_and_revise.json

web/                           # React + TypeScript + Vite SPA
├── src/                       # Views: Accounts, Agent Library, Designer, Launcher, Dashboard
└── dist/ -> agym/council/web_dist/  # Built static assets bundled in wheel
```

### Invariants
1. **Zero-Dependency Core**: `agym` core has `dependencies = []`. All council dependencies (`fastapi`, `uvicorn[standard]`, `pydantic`, `aiofiles`) live in `[project.optional-dependencies] council`.
2. **Profile Collision Immunity**: `agym council` launches profile `council`. `agym-council` launches the council app.
3. **Physical Release Barriers**: Peer outputs within a stage are hidden in isolated attempt workspaces until stage completion.
4. **Strict Dissent Preservation**: Critic objections are never filtered; synthesis prompt mandates: "Do not invent consensus."
5. **Strict Account Serialization**: Multiple workers on the same profile account run strictly sequentially.
6. **Durable Crash Recovery**: In-flight attempts checked on startup using OS PID and process start time; orphaned attempts marked `UNKNOWN`, run enters `NEEDS_ATTENTION`.
7. **Mutation Idempotency**: Mutating requests require `Idempotency-Key` and return cached results on replay.

---

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Zero-Dependency Core | Keep `dependencies = []` in pyproject.toml | M1 | R1, BLUEPRINT §1 |
| 2 | Council Optional Extra | Declare `[project.optional-dependencies] council` | M1 | R1, IMPLEMENTATION_PLAN §1 |
| 3 | Dedicated Script Entry Point | Add `agym-council = "agym.council.cli:main"` | M1 | R1, IMPLEMENTATION_PLAN §1 |
| 4 | Package Discovery Configuration | Setuptools discovery for `agym*` and package data | M1 | IMPLEMENTATION_PLAN §1 |
| 5 | Profile Collision Immunity | Preserve unreserved `council` profile in `agym/profiles.py` & `cli.py` | M1 | R1, ACCEPTANCE §M1 |
| 6 | Cross-Process Profile Locking | File lock `config.lock` with `fcntl.flock`/`msvcrt.locking` on mutations | M1 | R1, IMPLEMENTATION_PLAN §3 |
| 7 | In-Process Profile Re-entrant Lock | `threading.RLock()` guarding ProfileStore mutations in-process | M1 | IMPLEMENTATION_PLAN §3 |
| 8 | Pydantic Domain Models | Validated schemas for Account, AgentTemplate, Worker, Stage, Limits, Run | M2 | R2, CONTRACTS §1 |
| 9 | Turn Request & Result Models | Validated TurnRequest and TurnResult contracts | M2 | R2, CONTRACTS §2 |
| 10 | Workflow Configuration Validation | Acyclic graph, unique workers, positive limits validation | M2 | R2, validate_examples.py |
| 11 | SQLite Persistence Layer | `council.db` with WAL mode, foreign keys ON, 5000ms busy timeout | M2 | R2, CONTRACTS §4 |
| 12 | Relational Schema Migrations | 11 core tables (`accounts`, `runs`, `stages`, `attempts`, `artifacts`, etc.) | M2 | R2, CONTRACTS §4 |
| 13 | Content-Addressed Storage | SHA-256 hashed files with path traversal security (`..` rejection) | M2 | R2, CONTRACTS §6 |
| 14 | Provider Adapter Interface | Asynchronous `ProviderAdapter` ABC with 6 lifecycle methods | M2 | R2, CONTRACTS §2 |
| 15 | Deterministic Fake Provider | Mock provider with synthetic latency, error injection, dissent simulation | M2 | R2, CONTRACTS §2 |
| 16 | Antigravity Resolution & Profile Env | Resolve `agy` and build profile isolated HOME/storage env | M3 | R3, launcher.py |
| 17 | Headless CLI Invocation | Subprocess execution with Go flag compatibility (`["--print", prompt]`) | M3 | R3, IMPLEMENTATION_PLAN §2 |
| 18 | Model Discovery Parser | Parse tab-separated stdout while ignoring stderr spinner frames | M3 | R3, IMPLEMENTATION_PLAN §2 |
| 19 | Non-Billable Auth Verification | Account readiness probe via `agy models` exit code | M3 | R3, CONTRACTS §2 |
| 20 | Process Tree Termination | `os.killpg` on POSIX session leader, `taskkill /F /T` on Windows | M3 | R3, IMPLEMENTATION_PLAN §2 |
| 21 | NDJSON Streaming Parser | Extract incremental deltas, usage, and conversation handle from stdout | M3 | R3, CONTRACTS §2 |
| 22 | Hierarchical Prompt Assembly | Stack rules, user goal, stage contract, worker assignment, released inputs | M4 | R4, CONTRACTS §1 |
| 23 | Scratch Workspace Staging | Isolated directory per attempt; only released prerequisite artifacts staged | M4 | R4, BLUEPRINT §5 |
| 24 | Ordered Stage Loop Execution | Sequential stages with concurrent worker execution | M4 | R4, CONTRACTS §3 |
| 25 | Atomic Stage Release Barrier | Transactional commit releasing artifacts (`released=1`) only when stage completes | M4 | R4, BLUEPRINT §5 |
| 26 | Output Section Enforcement | Validate required JSON sections (findings, evidence, uncertainties, action) | M4 | R4, CONTRACTS §1 |
| 27 | Strict Dissent Preservation | Pass critique objections to synthesizer; mandate "Do not invent consensus" | M4 | R4, BLUEPRINT §3 |
| 28 | Zero-Tolerance Failure Policy | Mark run `NEEDS_ATTENTION` on required worker failure | M4 | R4, CONTRACTS §3 |
| 29 | Dual-Lease Capacity Scheduler | Enforce global worker concurrency and per-account serialization semaphores | M4 | R4, BLUEPRINT §6 |
| 30 | Transactional Attempt Claiming | Atomic claiming using SQLite `BEGIN IMMEDIATE` transactions | M4 | R4, CONTRACTS §3 |
| 31 | Run & Attempt Lifecycle State Machine | Full transitions (`READY`, `RUNNING`, `PAUSED`, `NEEDS_ATTENTION`, `COMPLETED`) | M4 | R4, CONTRACTS §3 |
| 32 | Startup Crash Reconciliation | Inspect SQLite on startup, check OS PID & start time, mark `UNKNOWN` | M4 | R4, CONTRACTS §5 |
| 33 | Loopback-Only FastAPI REST API | 21 REST endpoints bound strictly to 127.0.0.1 | M5 | R5, CONTRACTS §6 |
| 34 | Session & Origin Security | Reject external origins; validate `X-Council-Session` header | M5 | R5, CONTRACTS §6 |
| 35 | Mutation Idempotency Middleware | Cache responses by `Idempotency-Key` and request hash | M5 | R5, CONTRACTS §6 |
| 36 | Server-Sent Events (SSE) Stream | Real-time event streaming with sequence IDs and `Last-Event-ID` replay | M5 | R5, CONTRACTS §6 |
| 37 | Redacted Run Export Endpoint | Export run dossier and artifacts while stripping credential paths and secrets | M5 | R5, CONTRACTS §6 |
| 38 | Desktop Auth Broker | Launch `agym setup` in local terminal emulator or return shell fallback | M5 | R5, IMPLEMENTATION_PLAN §2 |
| 39 | React + TypeScript Web UI | Vite SPA with Accounts, Library, Designer, Launcher, and Dashboard views | M5 | R5, BLUEPRINT §4 |
| 40 | Reusable Preset Workflows | Bundle `quick_council`, `research_and_design`, `review_and_revise` | M5 | R5, examples/ |
| 41 | Clean Wheel Build & Packaging | Wheel bundling backend, presets, and built frontend (`web_dist`) | M5 | R5, BUILD_HANDOFF §1 |
| 42 | E2E Test Suite (Tiers 1-4) | 4-tier requirement-driven opaque-box test suite passing 100% | M6 | ACCEPTANCE §M1-M5 |
| 43 | Adversarial Hardening (Tier 5) | White-box stress-testing, edge-case generation, and coverage auditing | M6 | Project Pattern Phase 2 |

---

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Base Package Compatibility, Profile Locking & Packaging | Features 1–7: pyproject.toml extras/entrypoints, ProfileStore lock, 104 baseline tests green | none | DONE (135 tests pass) |
| M2 | Domain Models, Content-Addressed Storage & Fake Provider | Features 8–15: Pydantic models, SQLite persistence, artifacts store, FakeProvider | M1 | DONE (266 tests pass) |
| M3 | Antigravity Headless Adapter | Features 16–21: Native agy CLI integration, model discovery, process tree kill | M2 | DONE (302 tests pass) |
| M4 | Durable Staged Engine, Concurrency Scheduler & Crash Recovery | Features 22–32: Dual-lease scheduler, barriers, dissent, lifecycle, crash reconciliation | M2, M3 | DONE (355 tests pass) |

| M5 | FastAPI Service, Browser Web UI & Preset Distribution | Features 33–41: Local API, SSE, Web UI in web/, presets, wheel packaging | M4 | DONE (401 tests pass) |
| M6 | Final Milestone: E2E Verification & Adversarial Hardening | Features 42–43: Phase 1 (100% pass of Tiers 1-4), Phase 2 (Adversarial Tier 5) | M5, TEST_READY.md | PLANNED |

---

## Interface Contracts

### ProfileStore (`agym/profiles.py`)
- `_profile_lock(lock_path: Path) -> ContextManager`: Cross-process re-entrant file lock on `config.lock`.
- `create(name: str, settings: ProfileSettings | None = None, subscription_date: str | None = None) -> Profile`: Thread & process safe.
- `update_settings(name: str, settings: ProfileSettings) -> Profile`: Thread & process safe.
- `set_subscription_date(name: str, subscription_date: str | None) -> Profile`: Thread & process safe.
- `remove(name: str) -> None`: Thread & process safe.

### ProviderAdapter (`agym/council/providers/base.py`)
- `async capabilities() -> ProviderCapabilities`
- `async check_account(account_ref: str) -> AccountStatus`
- `async discover_models(account_ref: str) -> list[ModelDescriptor]`
- `async run_turn(request: TurnRequest) -> AsyncIterator[TurnEvent | TurnResult]`
- `async cancel(attempt_id: str) -> None`
- `async reconcile(in_flight_attempts: list[AttemptSnapshot]) -> list[AttemptReconciliation]`

### Scheduler & Engine (`agym/council/engine.py`, `scheduler.py`)
- `run_stage(run_id: str, stage_id: str) -> StageResult`: Enforces atomic release barrier.
- `claim_attempt(run_id: str, worker_id: str) -> Attempt`: Atomic claim under SQLite `BEGIN IMMEDIATE`.
- `acquire_lease(account_ref: str) -> AsyncContextManager`: Enforces per-account serialization and global concurrency limits.

### Storage & Artifacts (`agym/council/storage.py`, `artifacts.py`)
- `init_db(db_path: Path) -> sqlite3.Connection`: Initializes WAL, foreign keys, 5000ms timeout.
- `store_artifact(run_id: str, name: str, data: bytes, stage_id: str | None, worker_id: str | None, released: bool) -> ArtifactRef`: SHA-256 hashed.

---

## Code Layout
```
/home/tiagoliv/agy-manager/.worktrees/council/
├── pyproject.toml
├── README.md
├── agym/
│   ├── __init__.py
│   ├── cli.py
│   ├── profiles.py
│   ├── launcher.py
│   ├── diagnostics.py
│   ├── subscription.py
│   ├── usage.py
│   └── council/
│       ├── __init__.py
│       ├── cli.py
│       ├── models.py
│       ├── storage.py
│       ├── artifacts.py
│       ├── context.py
│       ├── scheduler.py
│       ├── engine.py
│       ├── recovery.py
│       ├── providers/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── fake.py
│       │   └── antigravity.py
│       ├── api/
│       │   ├── __init__.py
│       │   ├── app.py
│       │   ├── routes.py
│       │   ├── events.py
│       │   └── auth_broker.py
│       └── presets/
│           ├── quick_council.json
│           ├── research_and_design.json
│           └── review_and_revise.json
├── web/
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
└── tests/
    ├── test_cli.py
    ├── test_diagnostics.py
    ├── test_host_safety.py
    ├── test_launcher.py
    ├── test_profiles.py
    ├── test_subscription.py
    ├── test_usage.py
    └── council/
        ├── __init__.py
        ├── test_locking.py
        ├── test_packaging.py
        ├── test_models.py
        ├── test_storage.py
        ├── test_artifacts.py
        ├── test_fake_provider.py
        ├── test_antigravity_adapter.py
        ├── test_engine_barriers.py
        ├── test_scheduler.py
        ├── test_recovery.py
        ├── test_api.py
        ├── test_sse.py
        └── test_e2e_presets.py
```
