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

### One-Line Install (Recommended)

**Windows (PowerShell)**:
```powershell
irm https://raw.githubusercontent.com/Tiago-0liveira/agy-manager/main/install.ps1 | iex
```

**Linux / macOS (Bash)**:
```bash
curl -fsSL https://raw.githubusercontent.com/Tiago-0liveira/agy-manager/main/install.sh | bash
```

The installer automatically downloads the pre-built standalone executable (no Python required) or configures an isolated virtual environment, placing `agym` in your user PATH.

### Alternative / Manual Install

Requires Python 3.10+ and an existing `agy` installation:

```bash
pipx install git+https://github.com/Tiago-0liveira/agy-manager.git
```

or locally:

```bash
pip install .
```

## Updates

`agym` automatically checks GitHub Releases on startup in interactive sessions. When a newer version is available, it prompts:
```text
[agym] A new version is available: 0.1.0 -> 0.1.1
Would you like to update now? [y/N]:
```
If you decline (`N`), `agym` remembers your choice and suppresses prompts for **24 hours**.

You can also check or update manually at any time:
```bash
agym update --check      # Check if an update is available without installing
agym update              # Update agym to latest version
agym update --force      # Reinstall / force update to latest release
```

## Usage

```bash
agym setup personal
agym setup work

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

# Multi-pane fleet launch:
agym all                                  # Launch all profiles in evenly arranged terminal panes
agym all -- -p "fleet review"             # Pass arguments to all launched profiles

agym list
agym rename personal main
agym rename jmcar AI1
agym smartrename num                      # Sequentially renames all accounts: 1, 2, 3...
agym smartrename letter                   # Sequentially renames all accounts: A, B, ..., Aa...
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
agym statusline
agym statusline personal --preview
agym statusline --sync
agym doctor
agym doctor personal
agym remove personal
agym remove work --yes
```

`agym <profile>` launches Antigravity directly in the current terminal. On POSIX it replaces the wrapper process with `agy`, which preserves the native TTY, signals, colors, terminal resizing, alternate-screen behavior, current working directory, and `agy` exit semantics as closely as possible.

### Multi-Pane Fleet Launch (`agym all`)

`agym all` launches every configured Antigravity profile simultaneously, with one profile per terminal pane arranged as evenly as practical within the current terminal window.

```bash
# Launch all accounts in evenly subdivided panes
agym all

# Auto-approve permissions for all instances with --dsp (or -y)
agym all --dsp

# Limit how many profiles to launch (e.g. first 4 profiles in a 2x2 grid)
agym all -n 4

# Open all panes in a specific project directory (-C / --cwd)
agym all -C /path/to/my-repo

# Launch a specific subset of profiles by name
agym all --profiles personal,work

# Combine options and forward custom Antigravity flags
agym all -n 4 -C /path/to/project --dsp -- -p "fleet review"
```

#### Core Behavior & Profile Isolation
- **Discovery**: Retrieves all configured profiles from `ProfileStore` (matching `agym list`).
- **Full Isolation**: Each pane executes through the standard `agym <profile>` launch path, preserving isolated home directories (`HOME`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA`), credentials, statuslines, default models, permission flags, and current working directory.
- **Shortcut Handling**: If no profiles exist, displays setup guidance (`agym setup <profile>`). If exactly 1 profile exists, launches it directly without initializing multi-pane backends.

#### Supported Terminal Backends by OS

`agym all` automatically detects the current operating system and hosting terminal/multiplexer using reliable environment signals:

| OS | Supported Backends | Detection & Behavior |
|---|---|---|
| **Windows** | Windows Terminal | Detected via `WT_SESSION`. Targets the existing/recent tab/window (`-w 0`) using documented `wt.exe` pane commands (`split-pane`, `move-focus`) and argument vectors without opening separate GUI windows. |
| **Linux** | tmux, WezTerm | If inside tmux (`$TMUX`), splits active session panes. If outside tmux but `tmux` is installed, automatically creates a managed session, configures panes, and attaches. Inside WezTerm (`$WEZTERM_PANE`), splits via `wezterm cli`. |
| **macOS** | tmux, WezTerm | If inside tmux (`$TMUX`), splits active session panes. If outside tmux but `tmux` is installed, automatically creates a managed session and attaches. Inside WezTerm (`$WEZTERM_PANE`), splits via `wezterm cli`. |

#### Managed tmux Fallback (Linux & macOS)
When running outside tmux on Linux or macOS, `agym all` creates a dedicated collision-safe tmux session (`agym-<pid>-<timestamp>`), calculates the split geometry, spawns each profile, and attaches the user's terminal to that session. If setup fails at any point, the session is cleanly destroyed immediately so no orphaned or broken sessions remain.

#### Recursive Pane Layout Algorithm
The pane layout is generated recursively using pure geometric subdivision rather than hard-coded grids:
1. Begins with the full window area as one rectangular pane.
2. Selects the candidate pane with the largest area to split next.
3. If multiple candidate panes have equal area, breaks ties spatially: **bottom before top**, and **right before left**.
4. Splits the chosen pane into two equal halves along its longer dimension (width $\ge$ height $\to$ left/right side-by-side split; height $>$ width $\to$ top/bottom stacked split).
5. When subdividing four equal quadrants (e.g. going from 4 to 5, 6, 7, and 8 panes), candidates are selected in the exact deterministic order:
   $$\text{bottom-right} \longrightarrow \text{bottom-left} \longrightarrow \text{top-right} \longrightarrow \text{top-left}$$
6. Works deterministically for arbitrary numbers of profiles ($N \ge 1$).

#### Unsupported Terminal Environments
If running in a terminal emulator without programmatic pane creation APIs (and without `tmux` available on Linux/macOS), `agym all` exits cleanly with a descriptive error reporting the detected OS, detected terminal, supported alternatives, and remediation steps (e.g. installing tmux or running inside Windows Terminal). `agym` never attempts brittle GUI or keyboard automation.

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

### Persistent Antigravity Statusline

`agym` provides an integrated statusline for Antigravity that renders the active account name, active Git branch and worktree, active model, live quota, and context window usage at all times at the bottom of the terminal:

```text
👤 personal │ 🌿 feat/statusline [statusline] │ ⚡ Gemini 3.8 Flash │ 5h: [█████░] 90% (1h18m) │ Wk: 95% (6d) │ Ctx: 12%
```

```bash
# Check statusline installation and preview statusline across profiles
agym statusline

# Preview statusline rendering for a specific profile
agym statusline --preview personal

# Manually synchronize and install statusline across all registered profiles
agym statusline --sync

# Enable or disable statusline across all profiles
agym statusline --enable
agym statusline --disable
```

- **Git Branch & Worktree Awareness**: Pure filesystem-based repository inspection (< 1ms, zero subprocess overhead) displays the active Git branch and linked worktree (e.g. `🌿 feat/statusline [statusline]`), adapting to standard repos (`🌿 main`) and detached HEAD states.
- **Automatic Multi-Account Configuration**: Setting up an account via `agym setup <profile>` automatically installs the statusline runner and synchronizes the `statusLine` configuration across all registered accounts in their isolated `settings.json`.
- **Live & Cached Quota**: Seamlessly reads live bucket quota streamed by `agy` on state changes, falling back to `agym`'s usage cache when uninitialized.
- **Adaptive Layout**: Automatically detects terminal width and adjusts between full (≥ 105 cols), standard (≥ 75 cols), compact (≥ 55 cols), and minimal layouts.
- **Zero Overhead**: Written in standard library Python without external dependencies, executing in ~15-30ms with robust error suppression.

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

### Renaming Profiles

Easily rename a profile and seamlessly move its isolated storage and caches without recreating or re-authenticating:

```bash
agym rename <old-profile> <new-name>
# Or using the alias:
agym mv <old-profile> <new-name>
# Or via edit:
agym edit <old-profile> --name <new-name>
```

This updates the configuration, moves the profile's home directory (`~/.local/share/agym/profiles/<profile>/`), preserves all profile settings and subscription tracking, and migrates local usage and token caches to the new name.

### `smartrename`

Renames all available accounts in a clean, sequential order.

**Usage**:
```bash
agym smartrename <num|letter>
```

**Arguments**:
- `num`: Renames accounts to numeric identifiers starting at `1` up to `n` (e.g., `1`, `2`, `3`, ...).
- `letter`: Renames accounts to alphabetical identifiers:
  - Accounts 1–26: `A` to `Z`
  - Accounts 27–52: `Aa` to `Az`
  - Accounts 53–78: `Ba` to `Bz`
  - Accounts 703+: `Aaa`, `Aab`, ...

**Collision Safety**:
Renames execute via a safe two-phase staging strategy (`Phase 1`: rename all accounts to temporary collision-free identifiers; `Phase 2`: commit target names). This prevents unique naming collisions even when existing account names overlap with the target sequence.

**Examples**:
```bash
$ agym smartrename num
Renamed 3 accounts using 'num' sequence:
  [1/3] OldName1 -> 1
  [2/3] OldName2 -> 2
  [3/3] OldName3 -> 3

$ agym smartrename letter
Renamed 28 accounts using 'letter' sequence:
  [1/28] old1 -> A
  ...
  [26/28] old26 -> Z
  [27/28] old27 -> Aa
  [28/28] old28 -> Ab
```

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

