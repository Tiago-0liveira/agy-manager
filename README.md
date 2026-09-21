# agym — Antigravity Manager

`agym` is a Python tool for managing multiple isolated Google Antigravity accounts without mutating the host Antigravity login. Each named profile gets its own isolated home directory (`HOME`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA`), and `agym` launches the real host `agy` binary with file-backed credential storage and automated stale lock cleanup.

## Current backend

For profile `personal`, `agym` constructs an environment equivalent to:

```bash
HOME=~/.local/share/agym/profiles/personal/home \
GEMINI_FORCE_FILE_STORAGE=true \
agy
```

On Windows, it redirects `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `LOCALAPPDATA`, and `APPDATA` to the profile folder, pre-creates application data subdirectories, cleans up stale Chromium singleton locks (`SingletonLock`, `lockfile`), and isolates Windows Credential Manager authentication (`gemini:antigravity`) per profile so that each profile authenticates and operates with its own distinct Google account without credential collisions. The real `agy` executable is resolved from the host `PATH` before the profile environment is constructed. The process keeps the caller's current working directory and inherits the terminal directly.

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
agym setup work --reauth               # Re-authenticate an existing profile with a fresh sign-in flow

agym personal
agym work

# Profile launch configuration
agym config personal
agym config personal --model gemini-2.5-flash
agym config personal --model default
agym config personal --dangerously-skip-permissions
agym config personal --no-dangerously-skip-permissions

# Auto-prompt workflow
agym personal --auto-prompt "make me a plan to change feature A to B"

agym personal -- -p "explain this repository"
# The `--` is optional with agym's dispatcher:
agym personal -p "explain this repository"

agym list
agym rotate                           # Sequentially rotates to next profile and launches agy
agym rotate -p "run review"           # Rotates to next profile and executes prompt
agym rotate --simulate 3              # Dry-run simulate next 3 rotations without launching
agym rotate --status                  # View current rotation index, active profile, and history
agym rotate --reset                   # Reset rotation state to initial index
agym rotate --file accounts.txt       # Rotate through custom accounts file (handles CRLF / UTF-8 BOM)
agym usage
agym usage personal
agym usage --json
agym usage --refresh
agym tokens
agym tokens personal
agym tokens --json
agym tokens --refresh
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

### Token Usage Tracking & Terminal Graphs

`agym tokens` (alias: `agym token-usage`) tracks and visualizes token consumption across all accounts:

```bash
# Display summary card and fleet volume comparison chart
agym tokens

# Show detailed tabular token composition breakdown per profile
agym tokens --breakdown

# Filter specific profiles
agym tokens personal work

# Export full metrics and summary statistics as JSON
agym tokens --json

# Bypass local cache and re-scan conversation databases
agym tokens --refresh
```

- **Metrics Captured**: Input, Output, Thinking, Cache Read, and Total tokens with automated cache efficiency percentages.
- **Fleet Comparison Chart**: Proportional horizontal bar chart comparing consumption volume across all profiles.
- **Composition Breakdown Table (`--breakdown` / `-b`)**: Clean, beautifully formatted tabular breakdown displaying Total, Input, Output, Think, Cache, and Hit % per profile with a Fleet Total aggregate row.
- **Fleet Summary Card**: Displays fleet-wide token consumption, top consuming profile, average tokens per account, prompt cache hit ratio, and data cache status.
- **Non-TTY Fallback**: Automatically adapts to piped environments (`agym tokens | cat`) using clean ASCII blocks and no escape codes.

### Multi-Tier Caching

To ensure instantaneous terminal responses and avoid redundant disk or subprocess operations:

- **Usage Cache (`agym usage`)**: 60-second TTL. Queries within 1 minute return instantly from cache and show a subtle age indicator (e.g. `· 24s ago`). Live refresh queries the Google backend via `agy -p /usage`.
- **Token Cache (`agym tokens`)**: 10-minute TTL. Scans local Antigravity conversation databases and transcripts, persisting cumulative snapshots per profile.
- **Cache Bypass (`-f` / `--refresh` / `--no-cache`)**: Bypasses the cache (forces live API query for `usage`, re-scans local conversation databases for `tokens`).
- **Cache Location & Privacy**: Stored securely at `~/.local/share/agym/cache/` (or `AGYM_DATA_HOME/cache`) with private `0o700` directories and atomic `0o600` files. Corrupted files are recovered automatically.

### Profile Launch Settings

Each profile can be configured with default launch settings using `agym config <profile>`:

- **Model selection**: Set a profile default model using `--model <model>` (e.g. `agym config personal --model gemini-2.5-flash`), or clear it back to the Antigravity default with `agym config personal --model default`.
- **Permissions**: Automatic permission skipping (`--dangerously-skip-permissions`) is **OFF by default** for all profiles. It can be configured per profile or supplied on invocation:
  - **Short aliases**: Use `-y`, `--yes`, `--dsp`, or `--skip-perms` as concise shortcuts for `--dangerously-skip-permissions` (both singular `--dangerously-skip-permission` and plural `--dangerously-skip-permissions` are supported).
  - **Disable aliases**: Use `--no-dsp`, `--no-skip-perms`, `--no-dangerously-skip-permissions`, or `--no-dangerously-skip-permission` to disable.
  - **Profile configuration**: `agym config <profile> -y` (or `--dsp`, `--skip-perms`, `--dangerously-skip-permissions`) and `agym config <profile> --no-dsp` (or `--no-dangerously-skip-permissions`).
  - **Invocation shortcut**: `agym personal -y` or `agym personal --dsp` or `agym personal --skip-perms`. `agym` normalizes these aliases and passes the native `--dangerously-skip-permissions` flag to `agy`.
  - **Environment variables**: Set `DSP=1` or `DANGEROUSLY_SKIP_PERMISSIONS=1` for session/headless bypass. Set to `0` or `false` to disable.
  - **Precedence Hierarchy**:
    1. CLI flags (`-y`, `--dsp`, `--skip-perms`, `--no-dsp`, etc.) [Highest priority]
    2. Environment variables (`DANGEROUSLY_SKIP_PERMISSIONS=1` or `DSP=1`)
    3. Profile configuration file (`settings.json`)
    4. Safe default (`false`) [Lowest priority]

### Shell Integration & Aliases

For immediate convenience when invoking Antigravity or `agym` from your shell (`~/.bashrc`, `~/.zshrc`):

```bash
# Direct Antigravity shortcut with bypass
alias agyy="agy --dangerously-skip-permissions"

# Profile manager shortcuts with bypass
alias agymp="agym personal -y"
alias agymw="agym work -y"

# Wrapper function allowing additional arguments
agyy-run() {
  agy --dangerously-skip-permissions "$@"
}
```

### Auto-Prompt

The `--auto-prompt` feature runs a two-stage prompt workflow:

```bash
agym personal --auto-prompt "make me a plan to change feature A to B"
```

1. `agym` runs the selected profile non-interactively with `agy [profile defaults] --prompt "<USER PROMPT>"`.
2. Captures the raw response output.
3. Starts the same profile interactively using `agy [profile defaults] --prompt-interactive "<RAW RESPONSE>"`.

The first stage is non-interactive while the second stage becomes the normal interactive terminal session in the current working directory using the exact same profile, environment, and defaults.

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

