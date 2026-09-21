# Manual integration test for real Antigravity OAuth isolation

This test is intentionally manual because it uses Google's normal OAuth flow and must verify the exact `agy` version installed on the target machine. Do not inspect or print token contents.

## 1. Record the installed CLI

```bash
which agy
agy --version
agy --help
```

Confirm `--help` does not expose a supported profile/data/auth-store selector that should replace this workaround.

## 2. Prove forced file storage with fake home A

```bash
rm -rf /tmp/agym-test-a /tmp/agym-test-b
mkdir -m 700 /tmp/agym-test-a /tmp/agym-test-b
HOME=/tmp/agym-test-a GEMINI_FORCE_FILE_STORAGE=true agy
```

Sign in as account A using the normal Google OAuth page. Exit `agy` fully. Verify only filenames/paths, not contents:

```bash
find /tmp/agym-test-a/.gemini -maxdepth 3 -type f -print
```

Start a fresh process with the exact same environment:

```bash
HOME=/tmp/agym-test-a GEMINI_FORCE_FILE_STORAGE=true agy
```

It must start signed in as A without another login prompt.

## 3. Prove independent fake home B

```bash
HOME=/tmp/agym-test-b GEMINI_FORCE_FILE_STORAGE=true agy
```

Sign in as account B, exit, and start the same command again. It must reuse B.

## 4. Concurrency

In terminal 1:

```bash
HOME=/tmp/agym-test-a GEMINI_FORCE_FILE_STORAGE=true agy
```

In terminal 2:

```bash
HOME=/tmp/agym-test-b GEMINI_FORCE_FILE_STORAGE=true agy
```

Confirm each session shows/uses its own account. Trigger ordinary activity in both, wait long enough that refresh activity can occur, then confirm neither session changes account and files under A and B remain separate. Never compare token contents.

## 5. Development-environment side effects

From inside each Antigravity session, ask it to run or inspect:

```bash
pwd
git config --global --list
git status
ssh-add -l
```

Expected:

- `pwd` remains the project directory from which `agy` was launched.
- `SSH_AUTH_SOCK`, `PATH`, `SHELL`, `TERM`, locale variables, etc. are inherited.
- `agym` points Git to the host `~/.gitconfig` when that file exists and `GIT_CONFIG_GLOBAL` was not already set.
- Files under the host `~/.ssh` are **not** copied or linked into the fake home; agent-backed SSH may work through `SSH_AUTH_SOCK`, while direct key/config lookup may differ.

If the HOME swap causes unacceptable tool behavior, do not copy the whole home. Stop and switch the backend to a filesystem-isolation approach that remaps only `~/.gemini`.

## 6. Timezone workaround

Do **not** set `TZ=UTC` by default. If repeat-login behavior fails and your installed version is affected by a known file-storage timestamp issue, repeat the proof with:

```bash
HOME=/tmp/agym-test-a GEMINI_FORCE_FILE_STORAGE=true TZ=UTC agy
```

Only add a timezone override locally if this materially fixes the installed build, because it can affect commands run inside Antigravity.

## 7. Gate for `agym`

Only rely on the fake-home backend after all of these are true:

- A survives a completely fresh process.
- B survives a completely fresh process.
- A and B remain different accounts concurrently.
- Activity/refresh in one profile does not alter the other profile.
- HOME side effects are acceptable for your development workflow.

## 8. Manual test for profile permissions

Verify safe permissions default:

```bash
agym config personal --no-dsp
agym personal
```

Confirm the `--dangerously-skip-permissions` flag is not implicitly enabled. Tool permission prompts should behave normally.

Then enable dangerous permissions explicitly with short alias:

```bash
agym config personal -y
agym personal
```

Confirm the profile-specific behavior applies (tool permissions are auto-approved). Also verify `--dsp`:

```bash
agym config personal --dsp
agym personal
```

Verify on-the-fly invocation alias overrides default without saving:

```bash
agym config personal --no-dsp
agym personal -y
```

Verify environment variable bypass:

```bash
DSP=1 agym personal
```

Reset it afterward to maintain safe defaults:

```bash
agym config personal --no-dsp
```

## 9. Manual test for model configuration

Configure a known valid model for the profile:

```bash
agym config personal --model <known-valid-model>
agym personal
```

Confirm Antigravity starts and uses that model.

Then reset to normal default model:

```bash
agym config personal --model default
```

Confirm the override is removed and Antigravity uses its normal default model.

## 10. Real OAuth auto-prompt test

Test the two-stage `--auto-prompt` workflow:

```bash
agym personal --auto-prompt "Make me a concise implementation plan for changing feature A to feature B."
```

Confirm:

1. The first non-interactive `agy --prompt` call authenticates using the `personal` profile.
2. Its returned response becomes the initial prompt input to `--prompt-interactive`.
3. The resulting interactive session also uses the `personal` profile.
4. Terminal behavior (TTY, resizing, colors, alternate-screen) remains normal.
5. The current repository working directory is unchanged.
6. Host Antigravity credentials remain untouched.

Repeat with another profile:

```bash
agym work --auto-prompt "Make me a concise implementation plan for changing feature A to feature B."
```

Confirm that the two accounts stay isolated and each stage uses its respective profile authentication.
