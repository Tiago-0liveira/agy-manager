# E2E Test Infra: AGYM Council

## Test Philosophy
- Opaque-box, requirement-driven. No dependency on implementation design.
- Zero-cost testability: all test runs use `FakeProviderAdapter` or offline CLI mocks to guarantee 0 billable tokens.
- Methodology: Category-Partition + Boundary Value Analysis + Pairwise Combinatorial Testing + Real-World Workload Testing.

## Feature Inventory & Target Coverage
| # | Feature | Source (requirement) | Tier 1 | Tier 2 | Tier 3 |
|---|---------|---------------------|:------:|:------:|:------:|
| 1 | Zero-Dependency Core | ORIGINAL_REQUEST §R1 | 5 | 5 | ✓ |
| 2 | Profile Collision Immunity (`agym council` vs `agym-council`) | ORIGINAL_REQUEST §R1 | 5 | 5 | ✓ |
| 3 | Cross-Process Profile Locking & Concurrency | ORIGINAL_REQUEST §R1 | 5 | 5 | ✓ |
| 4 | Domain Model Validation & Pydantic Contracts | ORIGINAL_REQUEST §R2 | 5 | 5 | ✓ |
| 5 | SQLite Relational Persistence & WAL Mode | ORIGINAL_REQUEST §R2 | 5 | 5 | ✓ |
| 6 | Content-Addressed Artifacts & Traversal Protection | ORIGINAL_REQUEST §R2 | 5 | 5 | ✓ |
| 7 | Deterministic FakeProviderAdapter & Latency/Dissent | ORIGINAL_REQUEST §R2 | 5 | 5 | ✓ |
| 8 | Headless Antigravity CLI Execution & Flag Syntax | ORIGINAL_REQUEST §R3 | 5 | 5 | ✓ |
| 9 | Model Discovery Stdout Parsing vs Stderr Spinners | ORIGINAL_REQUEST §R3 | 5 | 5 | ✓ |
| 10 | Non-Billable Auth Verification Probe | ORIGINAL_REQUEST §R3 | 5 | 5 | ✓ |
| 11 | Process Tree Cancellation (`os.killpg`/`taskkill`) | ORIGINAL_REQUEST §R3 | 5 | 5 | ✓ |
| 12 | Stage Release Barriers & Draft Isolation | ORIGINAL_REQUEST §R4 | 5 | 5 | ✓ |
| 13 | Strict Dissent Preservation in Synthesis | ORIGINAL_REQUEST §R4 | 5 | 5 | ✓ |
| 14 | Dual-Lease Scheduler & Account Serialization | ORIGINAL_REQUEST §R4 | 5 | 5 | ✓ |
| 15 | Startup Crash Reconciliation & PID Validation | ORIGINAL_REQUEST §R4 | 5 | 5 | ✓ |
| 16 | Loopback FastAPI REST API & Session Security | ORIGINAL_REQUEST §R5 | 5 | 5 | ✓ |
| 17 | Mutation Idempotency via `Idempotency-Key` | ORIGINAL_REQUEST §R5 | 5 | 5 | ✓ |
| 18 | SSE Real-Time Streaming & Reconnection Replay | ORIGINAL_REQUEST §R5 | 5 | 5 | ✓ |
| 19 | Redacted Run Export Endpoint | ORIGINAL_REQUEST §R5 | 5 | 5 | ✓ |
| 20 | Workflow Presets Distribution (`quick_council`, etc.) | ORIGINAL_REQUEST §R5 | 5 | 5 | ✓ |

## Test Architecture
- Test location: `tests/council/` (automatically discovered by `python3 -m unittest discover tests`)
- Test runner: `python3 -m unittest discover tests`
- Acceptance runner: `python3 -m unittest discover tests/council`
- Pass/fail semantics: Exit code 0, 0 failures, 0 errors.

## Real-World Application Scenarios (Tier 4)
| # | Scenario | Features Exercised | Complexity |
|---|----------|--------------------|------------|
| 1 | Quick Council Preset Execution | F4, F5, F6, F7, F12, F13, F20 | Medium |
| 2 | Research and Design Preset Execution | F4, F5, F6, F7, F12, F13, F14, F20 | High |
| 3 | Review and Revise Preset Execution | F4, F5, F6, F7, F12, F13, F20 | High |
| 4 | Simulated Crash Recovery Under Active Load | F5, F14, F15 | High |
| 5 | Concurrent Run Dispatches with Account Contention | F3, F5, F14, F17 | High |
| 6 | SSE Connection Drop & Reconnection Replay | F5, F18 | Medium |
| 7 | Full Run Redacted Export & Integrity Audit | F5, F6, F19 | Medium |

## Coverage Thresholds
- Tier 1: ≥5 test cases per feature (100+ cases total)
- Tier 2: ≥5 boundary & corner test cases per feature (100+ cases total)
- Tier 3: Pairwise coverage of major feature combinations (≥20 cases)
- Tier 4: ≥7 realistic application scenarios
