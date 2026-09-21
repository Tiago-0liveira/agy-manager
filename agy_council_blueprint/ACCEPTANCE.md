# Acceptance scenarios and implementation milestones

These are proposed tests for the future application. They have not been executed against a built council. The packet's example validator checks only its illustrative configurations.

## Milestone 1 contracts and compatibility

- Install the existing CLI with no council extra; existing profile setup/list/config/usage/edit/remove behavior and tests remain valid.
- Install a built wheel with the council extra; all subpackages, presets and built web assets are actually present.
- Preserve a profile literally named `council`; `agym council` still launches it while `agym-council` launches the application.
- Import existing version-1 profile metadata without modifying credential contents or losing renewal/model settings.
- Friendly account renaming does not move credential paths or change historical labels in frozen run records.
- Concurrent profile updates from CLI and web do not overwrite one another. Use a lock covering the entire read/modify/write cycle, while retaining atomic writes.

## Milestone 2 fake-provider working council

- Run a one-worker workflow and a four-worker workflow with the same engine. Fake-provider tests also cover a larger configurable team; queued workers obey concurrency limits.
- Two workers use the same provider and model but have distinct roles, outputs and conversation handles. No dictionary is keyed only by model/provider name.
- Multiple workers assigned to one account serialize by default; workers on different accounts may run concurrently. Two runs share the same account capacity constraints.
- One initial worker completes early; no peer receives its output before the barrier. The shared filesystem/tool surface must not provide an alternate path around that rule when independent stages are advertised as isolated.
- A worker sees only its released input manifest and its permitted own context. A fresh-context stage does not inherit a previous stage's conversation. A new project/run does not inherit another project's discussions by default.
- A required worker fails; the run becomes needs-attention instead of producing an apparently complete council from the remaining workers.
- A synthesizer receives a dissenting view; the final deliverable preserves its disposition rather than hiding it behind a consensus label.

## Milestone 3 recovery and control

- Kill the scheduler after dispatch but before storing a result. Restart and reconcile the attempt; do not blindly repeat an action whose completion is unknown.
- Double-click Start or retry the same HTTP mutation: one run starts, using the idempotency record.
- Browser refresh/SSE reconnect restores prior events without re-dispatching workers or duplicating logical events.
- Pause drains in-flight work and stops new dispatch. Resume continues the same immutable specification. Stop terminates parent and child processes on Windows and leaves no untracked local job running.
- Model-call/time/retry/job limits terminate or pause precisely as documented. Record in-flight overrun and unknown usage rather than claiming exact bounds unsupported by the provider.
- An unavailable account does not automatically switch to another account. Explicit reassignment is recorded and handles conversation portability honestly.
- New template edits do not change an active run. User steering produces a visible amendment/child-run boundary.

## Milestone 4 real Antigravity adapter

- Check the installed CLI version, supported headless output, model IDs and stdin/resume capabilities before a live run.
- Run an explicitly authorized harmless two-turn task; verify resumption uses that worker's exact conversation ID.
- A denied required tool with a successful process exit is detected as incomplete work, not a verified result.
- Malformed/truncated JSON, unknown event types, timeouts, waiting-for-input and missing terminal events are handled without silently marking success.
- Cancel a real harmless call; verify process-tree cleanup on Windows. Do not use the actual research study as the lifecycle test.
- Follow agy-manager's manual real-login procedure: two accounts survive restart, stay distinct concurrently and do not alter the normal host login. No tokens are printed or copied to reports. Repeat compatibility checks after a material CLI upgrade.

## Milestone 5 UI and data boundary

- A user can add/link accounts, rename labels, create a role, choose the number of workers, assign profiles/models, edit the workflow, launch, pause/resume and export without editing source code.
- Authentication status is not inferred from a stored-file name alone. Expired/unavailable/unknown states are visible.
- Raw credentials, auth codes, token-bearing URLs and account-local secrets do not appear in API responses, frontend bundles, exports or normal logs.
- Imported workflow text cannot execute shell commands; paths cannot escape allowed artifact roots via traversal, symlinks or Windows junctions.
- The local server rejects unexpected origins and unauthenticated state changes; it does not listen publicly by default. Model output HTML is safely rendered.
- 'Restricted workspace' is shown only after an enforcement test denies an intentionally forbidden read. If no enforcement exists, the capability remains unavailable for protected workflows.
- Export a workflow and import it into a fresh local installation: role/stage definitions survive, but account bindings and credentials require local assignment.

## Later tool-execution milestone

- A model may request a named job with validated parameters; it cannot add arbitrary commands or escape declared cwd/data scope through parameters.
- Code, inputs and outputs are versioned before claims about test results are accepted.
- A side-effecting job with unknown completion stops for reconciliation; it is not retried solely because a timeout occurred.
- Parallel implementers do not overwrite the same source files; reviewer access is constrained as declared.
- A local editing workflow does not automatically publish, deploy, send messages or change external services.

Operational completion means the promised work was performed with traceable results. It does not certify that a strategic recommendation or generated architecture is correct. Include a small deliberately flawed evidence package to verify that the critic can identify a seeded contradiction, while recording that success as a limited functional exercise rather than an intelligence benchmark.
