Agent W1-D — Coordinator protocol and prompts

Branch

orch/04-protocol

Single responsibility

Define how the coordinator communicates with AGYM.

Not how it executes.

Files owned
agym/orchestration/protocol.py
agym/orchestration/prompts.py
tests/test_orchestration_protocol.py
Exact work
1. Coordinator system prompt

Explicitly tell the coordinator:

you decide WHAT work is useful
AGYM decides HOW it executes

Coordinator may request:

workers
specialists
audits
synthesis
another round
executor
finalize

Coordinator may not:

name AGYM profiles
run shell commands
spawn processes
override budgets
override leases
override workspace restrictions
recursively create agents itself

This responsibility boundary comes directly from the orchestration specification.

2. Assessment prompt

Coordinator's first response must contain:

TaskAssessment
+
CoordinatorAction

No arbitrary prose-only decision.

3. Action JSON schema

Generate or maintain the schema expected from the coordinator.

It must map directly into frozen contracts.

Reject fields not allowed by the contract.

Especially reject:

profile
profile_name
command
argv
shell
environment

where they do not belong.

4. Parser

Input:

raw model structured response

Output:

CoordinatorAction

or an explicit protocol error.

Do not silently repair dangerous malformed actions.

5. Action validation

Structural/semantic checks including:

RUN_WORKERS has workers
RUN_AUDITORS has auditors
FINALIZE has no workers
RUN_EXECUTOR contains exactly one executor
MUTATING mode only valid for executor
duplicate WorkerIds rejected
unknown result references rejected where context supplied

Budget validation belongs to the engine.

6. Observation prompt

Turn CoordinatorObservation into the next coordinator message.

Include:

worker results
failures
rejected requests
budget state
redacted fleet state
round number

Do not include internal profile names.

7. Failure messages

Standardize how infrastructure problems are returned:

worker failed
profile unavailable
quota exhausted
action rejected
budget rejected
timeout

Coordinator can then decide semantically whether to:

retry
replace worker
reduce scope
continue
finalize
8. Synthesis prompt support

Ensure synthesis workers receive:

original task
assessment
relevant worker outputs
auditor findings
repository facts

but not unrelated orchestration internals.

Tests
valid first response
valid worker action
valid audit action
valid executor
valid finalize
invalid JSON
unknown action
profile name injection
command injection field
multiple mutating executors
mutation by normal worker
duplicate worker IDs
empty worker wave
invalid referenced worker
observation serialization
