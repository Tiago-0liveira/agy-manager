# AGYM Council Implementation — Status & Resumption Guide

> **Date**: 2026-09-21  
> **Branch**: `feat/council`  
> **Status**: **IN PROGRESS** (Milestone 5 Completed & Audited; Milestone 6 Up Next)  
> **Test Status**: **401 / 401 tests passing** (0 failures, 100% clean)


---

## 1. Executive Summary

Execution of the **AGYM Council** implementation plan (`agy_council_blueprint/IMPLEMENTATION_PLAN.md`) was launched via the teamwork multi-agent coordinator and subsequently stopped cleanly per user request.

All background subagents, crons, and tasks have been safely terminated. The repository is in a clean, consistent, fully passing state with zero regressions.

---

## 2. Completed Milestones

### Milestone 1: Base Package Compatibility, Profile Locking & Packaging ✅ (COMPLETED & AUDITED)

1. **Re-entrant Cross-Process File Locking** (`agym/profiles.py`):
   - Implemented `_profile_lock(lock_path: Path)` context manager supporting cross-process file locks (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows relative to byte 0 via `a+` seek).
   - Guarded in-process concurrency with `threading.RLock()` and per-thread recursion depth tracking (`_LockState`).
   - Wrapped all mutating methods in `ProfileStore`: `create()`, `update_settings()`, `set_subscription_date()`, and `remove()`.
   - Verified race condition immunity under high-concurrency multiprocessing and multithreading stress tests.

2. **Packaging & Zero-Dependency Core Invariant** (`pyproject.toml`):
   - Retained core `dependencies = []` (100% zero external dependencies for baseline `agym`).
   - Added optional extra: `[project.optional-dependencies] council = ["fastapi", "uvicorn[standard]", "pydantic", "aiofiles"]`.
   - Added CLI entry point: `agym-council = "agym.council.cli:main"`.
   - Configured dynamic setuptools discovery: `include = ["agym*"]` and package data for presets and web dist.

3. **Council CLI & Collision Immunity** (`agym/council/cli.py` & `agym/council/__init__.py`):
   - Created standalone `agym-council` CLI with commands: `version`, `account list`, `account add`, `profile-bridge`.
   - Guaranteed profile collision immunity: running `agym council` unconditionally launches an existing user profile literally named `council`, while `agym-council` launches the council management service/CLI.
   - Built non-raising optional dependency checker `check_council_dependencies()`.

4. **Global Architecture & Testing Strategy Artifacts**:
   - `PROJECT.md`: 43-feature inventory mapped across all 5 milestones, invariant definitions, component tree, and interface contracts.
   - `TEST_INFRA.md`: 4-tier requirement-driven testing strategy (Unit, Component, Integrated Engine, System).

5. **Test Suite Expansion**:
   - Baseline: 104 tests passing.
   - Current: **147 tests passing** (43 new dedicated council tests in `tests/council/` covering locking, adversarial stress, profile collision, and packaging).

### Milestone 2: Domain Models, Content-Addressed Storage & Fake Provider ✅ (COMPLETED & AUDITED)

1. **Pydantic Domain Models** (`agym/council/models.py`):
   - Fully validated models for `Account`, `AgentTemplate`, `WorkerConfig`, `StageConfig`, `LimitsConfig`, `RunConfig`, `TurnRequest`, and `TurnResult`.
   - Validation checks for unique worker IDs, acyclic stage dependencies, input stage references, and required sections.

2. **SQLite Persistence Layer** (`agym/council/storage.py`):
   - Database initialization with WAL mode, foreign keys ON, and 5000ms busy timeout handling.
   - 11 core tables with relational integrity and transactional mutation support.

3. **Content-Addressed Artifact Store** (`agym/council/artifacts.py`):
   - SHA-256 content hashing, relative path resolution, and strict directory traversal prevention (`..` rejection).

4. **Provider Adapters** (`agym/council/providers/base.py` & `fake.py`):
   - Asynchronous `ProviderAdapter` ABC with lifecycle methods (`capabilities`, `check_account`, `discover_models`, `run_turn`, `cancel`, `reconcile`).
   - Deterministic `FakeProviderAdapter` supporting synthetic delay, failure injection, and dissent simulation for zero-cost testing.

5. **Test Suite Expansion**:
   - Test count increased to **266 tests passing** (0 failures).

### Milestone 3: Antigravity Headless Adapter ✅ (COMPLETED & AUDITED)

1. **Native CLI Headless Invocation** (`agym/council/providers/antigravity.py`):
   - Headless subprocess execution respecting Go flag rules (`["--print", prompt]`).
   - Isolated tab-separated stdout parser for `agy models` (filtering braille spinner frames).
   - Three-tier non-billable account verification probe (`ready`, `needs_login`, `unavailable`).
   - Process tree termination via `os.killpg` on POSIX session leader and `taskkill` on Windows.
   - NDJSON streaming parser for live tokens, usage metrics, and conversation ID.

2. **Test Suite Expansion**:
   - Test count increased to **302 tests passing** (36 dedicated adapter tests).

### Milestone 4: Durable Staged Engine, Concurrency Scheduler & Crash Recovery ✅ (COMPLETED & AUDITED)

1. **Hierarchical Prompt Assembly & Scratch Staging** (`agym/council/context.py`):
   - Strict 5-tier composition order: Rules -> Goal -> Stage Contract -> Worker Assignment -> Released Inputs.
   - Physical attempt scratch workspace isolation; rejects unreleased draft leakage and directory traversal.

2. **Dual-Lease Capacity Scheduler & Transactional Claiming** (`agym/council/scheduler.py`):
   - Strict per-account worker serialization; concurrent execution across distinct accounts bounded by global limit.
   - Transactional attempt claiming under SQLite `BEGIN IMMEDIATE`.
   - Pause (`pause_run`) and Cancel (`cancel_run`) lifecycles.

3. **Durable Staged Engine & Barriers** (`agym/council/engine.py`):
   - Sequential stage loop with concurrent worker execution.
   - Atomic release barrier (`release_stage_artifacts_atomic`) releasing all stage artifacts in one transaction upon stage completion.
   - Output section validation (`findings`, `evidence_or_assumptions`, `uncertainties`, `next_action`).
   - Zero-tolerance failure policy: halts synthesis and marks run `NEEDS_ATTENTION` on required worker failure.
   - Strict dissent preservation: mandates synthesizer non-invention of consensus when critic objects.

4. **Startup Crash Reconciliation** (`agym/council/recovery.py`):
   - Scans SQLite on startup for in-flight attempts.
   - OS PID and start time checks to prevent PID reuse traps.
   - Marks crashed attempts `UNKNOWN`, logs critical audit issues/events, and transitions runs to `NEEDS_ATTENTION`.

5. **Test Suite Expansion**:
   - Test count increased to **355 tests passing** (0 failures, 100% clean).

### Milestone 5: FastAPI Service, Server-Sent Events, Web UI & Packaging ✅ (COMPLETED & AUDITED)

1. **Loopback-Only REST API & Security** (`agym/council/api/`):
   - 21 REST endpoints bound strictly to loopback (`127.0.0.1`).
   - Host and Origin header validation to prevent DNS rebinding; token authorization via `X-Council-Session`.
   - Mutating routes guarded by `IdempotencyMiddleware` (`Idempotency-Key` header).
   - Server-Sent Events (`/api/runs/{id}/events`) with monotonic sequence numbers and `Last-Event-ID` replay.
   - Redacted run export dossier (`/api/runs/{id}/export`) stripping credential paths, secrets, and auth tokens.
   - Desktop auth broker launching `agym setup <profile>` in local terminal emulator or providing headless shell command fallback.

2. **React + TypeScript SPA Web Interface** (`web/` & `agym/council/web_dist/`):
   - Single-page application built with React 18, TypeScript, and Vite.
   - Dedicated views: Accounts (management, live auth probe, model discovery), Agents (library), Workflows (DAG visualizer & JSON editor), Launch (preset runner), and Dashboard (live SSE run tracking, artifact inspector, pause/resume/cancel controls, and ZIP export).
   - Bundled production build compiled into `agym/council/web_dist/` and served directly by FastAPI.

3. **Bundled Presets & Zero-Dependency Core Invariant**:
   - Packaged workflow presets: `quick_council.json`, `research_and_design.json`, `review_and_revise.json`.
   - Zero external dependencies for core `agym`; all FastAPI/Pydantic/Uvicorn/aiofiles dependencies isolated in `[council]` extra.
   - Wheel bundling tested and verified (`agym-0.1.0-py3-none-any.whl`).

4. **Test Suite Expansion**:
   - Test count increased to **401 tests passing** (104 baseline core tests + 297 dedicated council tests, 0 failures, 100% clean).


---

## 3. Current Git Working Tree

To verify the test suite at any time:
```bash
python3 -m unittest discover tests
```

---

## 4. Implementation Roadmap

```
┌─────────────────────────────────────────────────────────────┐
│ [M1: Packaging & Locking]  ──> COMPLETED (147 tests pass)   │
└─────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ [M2: Models, Storage & Fake] ──> COMPLETED (266 tests pass) │
└─────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ [M3: Headless Antigravity Adapter] ──> COMPLETED (302 tests)│
└─────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ [M4: Durable Staged Engine & Scheduler] ──> COMPLETED (355) │
└─────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ [M5: FastAPI Service & Browser UI] ──> COMPLETED (401 tests)│
│  - Loopback REST API (127.0.0.1) & SSE event stream         │
│  - Idempotency middleware (Idempotency-Key)                 │
│  - React + TypeScript + Vite frontend in web/ & web_dist/   │
│  - Bundled presets & wheel packaging                        │
└─────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ [M6: E2E Verification & Adversarial Hardening] ──> UP NEXT  │
│  - Final live Antigravity account smoke verification        │
│  - Full adversarial stress verification                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 5. How to Resume / Keep Going

You can resume the implementation using any of the following approaches:

### Option A: Resume with Antigravity Directly (Recommended for Immediate, Focused Execution)
Give the prompt:
```
Continue implementing AGYM Council from Milestone 2 (Domain Models, Content-Addressed Storage & Fake Provider) as documented in COUNCIL_STATUS.md and PROJECT.md. Implement agym/council/models.py, agym/council/storage.py, agym/council/artifacts.py, and agym/council/providers/fake.py, and write tests in tests/council/test_models.py, test_storage.py, and test_fake_provider.py.
```

### Option B: Resume Multi-Agent Teamwork
Give the prompt:
```
/teamwork-preview @COUNCIL_STATUS.md @PROJECT.md Continue implementing AGYM Council from Milestone 2 through Milestone 6.
```

### Option C: Slice-by-Slice Implementation Order

1. **Milestone 2** (`agym/council/models.py`, `storage.py`, `artifacts.py`, `providers/base.py`, `providers/fake.py`):
   - Reference specifications: `agy_council_blueprint/CONTRACTS.md` §1, §2, §4, §6.
   - Validation: Run `python3 agy_council_blueprint/validate_examples.py` and new unit tests.

2. **Milestone 3** (`agym/council/providers/antigravity.py`):
   - Reference specifications: `agy_council_blueprint/CONTRACTS.md` §2 and `IMPLEMENTATION_PLAN.md` §2.
   - Focus: Handle `--print` argument requirement, isolate stdout from stderr braille spinner frames, process tree cancellation.

3. **Milestone 4** (`agym/council/engine.py`, `scheduler.py`, `context.py`, `recovery.py`):
   - Reference specifications: `agy_council_blueprint/BLUEPRINT.md` §3, §5, §6 and `CONTRACTS.md` §3, §5.
   - Focus: Stage barrier release atomicity, account concurrency serialization, crash reconciliation.

4. **Milestone 5** (`agym/council/api/`, `agym/council/presets/`, `web/`):
   - Reference specifications: `agy_council_blueprint/BLUEPRINT.md` §4 and `CONTRACTS.md` §6.
   - Focus: FastAPI loopback REST & SSE streaming, Vite frontend build.
