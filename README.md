# agym — Antigravity Manager

`agym` is a small Python wrapper for explicitly selecting among multiple legitimate Google Antigravity accounts without mutating the normal host Antigravity login. Each named profile gets its own isolated fake home and `agym` launches the real host `agy` binary with file-backed credential storage enabled.

It deliberately does **not** rotate accounts, pool quota, fail over on quota exhaustion, scrape OAuth data, or patch `agy`.

## Current backend

For profile `personal`, `agym` constructs an environment equivalent to:

```bash
HOME=~/.local/share/agym/profiles/personal/home \
GEMINI_FORCE_FILE_STORAGE=true \
agy
```

On Windows it also redirects the conventional user-home variables used by native applications. The real `agy` executable is resolved from the host `PATH` before the profile environment is constructed. The process keeps the caller's current working directory and inherits the terminal directly.

> Important: the target machine must pass the OAuth isolation proof in [`docs/manual-integration.md`](docs/manual-integration.md). `agym` cannot prove a real Google login in unit tests.

## Install

Requires Python 3.10+ and an existing `agy` installation.

```bash
pipx install .
```

or:

```bash
pip install .
```

## Usage

```bash
agym setup personal
agym setup work

agym personal
agym work

agym personal -- -p "explain this repository"
# The `--` is optional with agym's dispatcher:
agym personal -p "explain this repository"

agym list
agym usage
agym usage personal
agym usage --json
agym doctor
agym doctor personal
agym remove personal
agym remove work --yes
```

`agym <profile>` launches Antigravity directly in the current terminal. On POSIX it replaces the wrapper process with `agy`, which preserves the native TTY, signals, colors, terminal resizing, alternate-screen behavior, current working directory, and `agy` exit semantics as closely as possible.

### Quota and Usage Retrieval

`agym usage` queries quota and usage across all configured profiles concurrently using Antigravity's `/usage` command:

```bash
# Query all profiles with a live progressive terminal UI
agym usage

# Filter specific profiles
agym usage personal work

# Machine-readable JSON output for scripting or automation
agym usage --json

# Custom per-profile timeout
agym usage --timeout 45
```

- **Concurrent & Isolated**: Queries profiles in parallel (up to 8 concurrent processes) while maintaining strict environment isolation for each profile's credentials.
- **Progressive Table UI**: In interactive terminals, renders an animated, compact table sized to fit in standard 80-column terminals. Displays 5-hour and weekly quota with colored progress bars (6+ ranks based on remaining capacity) and abbreviated reset durations (`6d+`, `3h+`, `45m`, `now`), separating only after the weekly row.
- **JSON Output**: Returns structured quota groups, bucket IDs, windows (e.g. 5h, weekly), remaining percentages, fractions, and ISO reset timestamps.

```text
┌────────────┬─────────────────────────────┬─────────────────────────────┐
│ Account    │           Gemini            │        Claude & GPT         │
├────────────┼─────────────────────────────┼─────────────────────────────┤
│ personal   │ 5h: [████████░░]  84%   3h+ │ 5h: [██████████] 100%   4h+ │
│            │ Wk: [██████████]  99%   6d+ │ Wk: [██████████] 100%   6d+ │
├────────────┼─────────────────────────────┼─────────────────────────────┤
│ work       │ ✗ Failed: agy exited with status 1 (session expired)      │
│            │                                                           │
└────────────┴─────────────────────────────┴─────────────────────────────┘
```

## Storage

Linux defaults:

```text
~/.config/agym/config.json
~/.local/share/agym/profiles/<profile>/home/
```

macOS uses `~/Library/Application Support/agym/`. Windows uses `%LOCALAPPDATA%\agym\`.

The metadata file stores only profile metadata such as creation time, isolated home path, and the `agy` version observed during setup. OAuth tokens, cookies, authorization codes, passwords, browser sessions, and refresh tokens are never copied into `config.json`.

On POSIX, directories created by `agym` are mode `0700` and the metadata file is mode `0600`. `agym doctor` warns if profile directories become group/world accessible. It can report that a known credential-state file exists, but never reads or prints its contents.

For test isolation, `AGYM_CONFIG_HOME` and `AGYM_DATA_HOME` may override `agym`'s own roots. These variables do not alter Antigravity's credential format or host `~/.gemini`.

## Host configuration behavior

Changing `HOME` can affect tools launched by Antigravity. `agym` preserves the existing environment (including `PATH`, `SSH_AUTH_SOCK`, shell, terminal, and locale variables) and, when the host `~/.gitconfig` exists and `GIT_CONFIG_GLOBAL` is otherwise unset, points Git back to that host global config.

`agym` intentionally does **not** copy or symlink `~/.ssh` private keys/configuration into a profile. Validate your own Git/SSH workflow with the manual integration test. If HOME redirection is too disruptive, the next architecture should isolate only `~/.gemini` with a filesystem namespace rather than copying the user's home.

## Safety guarantees

- Host `~/.gemini` is never migrated, overwritten, or deleted by setup/remove.
- `agym remove <profile>` deletes only that profile's directory and metadata; it never calls global `agy /logout`.
- The installed `agy` binary is not copied or patched, so profiles naturally use the current host version.
- Credential file contents are never emitted by `list` or `doctor`.

## Tests

```bash
python -m unittest discover -s tests -v
```

The unit tests cover profile validation, creation/duplicates/list/removal, environment construction, working-directory preservation, argument passthrough, host `agy` resolution before HOME changes, non-disclosure of credential contents, distinct profile auth/data paths, host `.gemini` safety, quota response parsing, reset-time calculations, bounded concurrency, and progressive UI rendering.

Real OAuth persistence and concurrency are covered by the manual integration procedure because they require the installed Antigravity build and interactive Google sign-in.

