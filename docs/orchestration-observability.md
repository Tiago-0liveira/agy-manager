# Orchestration observability foundation

## Implemented storage

New runs retain the existing files (`run.json`, `events.jsonl`, invocation
snapshots, assessment, coordinator checkpoint, final result) and add:

```text
<run>/
  trace.jsonl
  attempts/
    attempt-<uuid>/
      record.json
      prompt.txt
      stdout.log       # present when bytes were captured
      stderr.log       # present when bytes were captured
```

The default parent is `~/.local/share/agym/orchestrator/runs` on Linux;
`AGYM_DATA_HOME` overrides the data root. No migration or alteration of old runs
is required. Old runs cannot recover information that was never recorded.

### Attempts and identity

An attempt is one worker/auditor execution or one coordinator model exchange.
Worker retries share their logical `invocation_id` but have distinct random
`attempt_id` values and `attempt_number` values. UUIDs avoid collisions on resume.
Coordinator exchanges include initial assessment, observations, corrections, and
responses that fail protocol validation. Session opening is separately recorded
as `kind=coordinator_session`, including startup errors.

`record.json` has `schema_version=1`, run/attempt identity, kind, start time,
status, and prompt/output filenames. Worker metadata includes invocation and
worker IDs, role, round, profile, strategy, workspace mode, timeout, and attempt
number. Coordinator metadata includes round, profile, conversation, expected
schema, and correction attempt. Completion adds a timestamp and the complete
`ModelResult`: response, structured data, conversation ID, usage when supplied,
exit code, and error. Exceptions and cancellation retain an explicit outcome.

The record is written before execution and atomically replaced at completion.
`RUNNING` without completion is evidence of an unfinished attempt, not proof that
the process is still alive. Inspection must reconcile it with run status and
process ownership; readers must not rewrite historical records.

### Trace timeline

`trace.jsonl` is append-only except for recovery of an incomplete final write.
Every entry contains `schema_version`, `event_id`, UTC `timestamp`, `run_id`,
nullable `attempt_id`, `type`, and `payload`. File order is authoritative within
a run; timestamp sorting alone is not a stable cursor.

Types currently written:

- `orchestration`: the original orchestration event, retaining its event ID and
  timestamp. Includes decisions, profile leases, rounds, and run outcomes.
- `attempt_started`, `attempt_finished`: attempt lifecycle and outcome.
- `process_started`, `process_attached`: PID, argv, working directory, and output
  format. A persistent process can serve several coordinator attempts.
- `output`: stream name, byte offset, and byte length in that attempt's stream file.
- `protocol_accepted`, `protocol_rejected`: response validation and error details.
  Model execution can succeed while protocol validation fails; show both.
- `trace_recovered`: a torn final entry was preserved in a named
  `trace.partial-*.txt` file before further appends.

Writes are serialized across threads sharing the run store. The engine's
existing per-run lock provides exclusive orchestration ownership. There is no
claim of independent concurrent writers being supported for the same run.
`read_trace()` ignores an incomplete final line while a writer is active, but
raises on a malformed complete line so corruption is not silently hidden.

### What is captured live

During recorded worker runs, the runner requests `agy --output-format
stream-json`. Both pipes are drained concurrently in chunks and saved before
process completion. Raw stdout retains provider tool calls, tool results,
deltas, and unknown events when the provider emits them. Only the final result
is used as the worker response; a missing terminal result is a failure. The
stream decoder accepts the terminal shapes already supported by the session
adapter and retains raw evidence when parsing fails.

Persistent coordinator stdout is saved in chunks before NDJSON parsing, including
partial lines. Its
stderr pipe is drained continuously, including startup and between turns.
Startup stderr belongs to the session-opening capture; subsequent stderr is
attributed to the most recently attached turn. The provider does not label stderr
with turn IDs, so late stderr attribution is best effort. Session startup and
idle time are therefore not independent model turns.

Timeouts, nonzero exits, and cancellation retain bytes already received. Disk
files use private permissions. Environment variables and credential stores are
not copied. Prompts and raw provider output can themselves contain sensitive
project data; an eventual export command needs a separate redaction policy.
Future terminal views must escape terminal control characters in captured text.

### Boundaries

- This records externally emitted execution activity, not hidden model reasoning.
- Tool and file activity is available only to the extent the provider emits it.
  A raw tool event is not an independently verified filesystem diff.
- Model usage is recorded when provided; missing usage must display as unknown.
- Fake/custom runners can supply attempt results without subprocess streams.
  Missing stream files must not be presented as proof that no activity occurred.
- Output is streamed to disk, but response collection still buffers a worker's
  stream for final decoding. Large-output memory limits and retention policies
  are follow-up work; there is no automatic deletion or silent truncation.
- The installed CLI advertises stream-json support. Provider-specific event
  shapes beyond the supported terminal envelopes remain available in raw logs;
  add decoder fixtures when a new shape is encountered.

## Implementation plan: inspect first, logs immediately after

Both commands should share a read-only run reader. Implement `inspect` first:
most current pain is understanding failed or apparently stalled runs, and a
snapshot is simpler to verify than a live follower. `logs` then adds incremental
reading and filtering to the same data model.

### 1. Shared reader and normalized view

Add `agym/orchestration/inspection.py` with immutable views for run, invocation,
attempt, and timeline entries. Load snapshots and attempt records independently;
report missing/corrupt artifacts with their paths without losing healthy data.
Keep model outcome separate from protocol outcome. Link retries by invocation ID,
coordinator corrections by round and schema, and provider events by attempt ID.

Prefer the new trace; for legacy runs read `events.jsonl` and invocation snapshots.
Use original event IDs to avoid duplicates. Label legacy gaps explicitly:
`attempt history unavailable`, `raw streams unavailable`, or `usage unknown`.
Do not infer a worker crash from a coordinator failure or a stale `RUNNING` field.

Provide a byte cursor for timeline reads, plus incremental UTF-8/NDJSON decoding
for stream ranges. Only advance the cursor past complete journal records.
Preserve unknown schema versions/event types in JSON views and mark unsupported
versions instead of misinterpreting them. Validate artifact paths against the run
root before opening them. A reader must never acquire an execution lease or
launch a coordinator/model.

Acceptance: fixtures for legacy/current runs, retries, partial files, corrupt
snapshots, missing output, unsupported versions, cancellation, and resume all
produce useful views without changing the run.

### 2. `orchestrate inspect <run-id>`

Default output:

1. Outcome, task/mode, elapsed time, budget use, and last recorded activity.
2. Worker table: role, final status, attempt count, profiles, durations, error.
3. Coordinator decisions by round, including action rejections and corrections.
4. Failed/unfinished attempts, with reason and paths to prompts/raw evidence.
5. Stored final response and data-availability notes.

Options: `--worker <id>`, `--attempt <id>`, `--timeline`, `--json`.
Detailed attempt output includes prompt, model response, validation status,
process metadata, and available stream paths. Avoid dumping full prompts/raw
output in the default view. JSON is versioned and stable enough for a future UI.
Exit 0 means inspection succeeded even if the run failed; missing/invalid input
returns a nonzero exit. Escape ANSI/control sequences in text views.

Acceptance: the reported all-workers-failed scenario is understandable from one
command; errors appear without opening JSON; successful failover shows both the
failed attempt and the successful retry; inspect works while a run is active.

### 3. `orchestrate logs <run-id> [--follow]`

Reuse the timeline reader and normalized event formatter. Default to readable
milestones, decisions, failures, and tool activity. Options: `--worker`,
`--attempt`, `--stream stdout|stderr|all`, and `--json` for full event envelopes.
Unknown provider events get a generic description and remain available as JSON.
Do not call captured output “raw” after escaping it for terminal display.

Follow mode polls from its byte cursor, holds partial UTF-8/JSON records until
complete, and never reprints entries merely because a snapshot was replaced.
Detect trace replacement/truncation and announce it. Follow attempts created by
retries and resume. After a terminal run event, drain already written output and
exit; a later resume requires invoking follow again. Ctrl+C stops the reader
without cancelling the orchestration. Missing output is reported, not fatal to
other streams. Avoid wall-clock ordering assumptions across processes.

Acceptance: chunked output is visible before worker completion; filters work
across retry attempts; partial lines and restarts neither drop nor duplicate
complete events; Ctrl+C leaves the run running.

### 4. CLI integration and bounded operation

Show the run storage path and an `inspect` command on completion/failure. Share
terminal formatting between inspect and logs, while keeping the active compact
orchestrator display focused on progress. Add documented retention and output
limits before enabling large fleet workloads: any truncation must produce a
visible trace entry and preserve the error/final response. Provide opt-in export
with explicit redaction rather than silently changing the local evidence.

No database or web server is required for these phases. The versioned reader can
later back a UI without changing the execution engine or storage layout.
