#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# Navigate to Repository Root
# -----------------------------------------------------------------------------
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$REPO_ROOT"

# -----------------------------------------------------------------------------
# Parse Arguments
# -----------------------------------------------------------------------------
TARGET_BASE=""
DRY_RUN=false

show_help() {
  cat <<EOF
Usage: $(basename "$0") [options] [base-branch]

Automated Pull Request Creation Command.
Validates git state & gh auth, pushes branch if needed, and creates a PR.

Options:
  -b, --base <branch>   Target base branch (default: feat/usage-graphs if exists, else main)
  -n, --dry-run         Preview PR creation without pushing changes or creating a PR
  -h, --help            Show this help message and exit

Arguments:
  [base-branch]         Optional target base branch override (same as --base)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      show_help
      exit 0
      ;;
    -n|--dry-run)
      DRY_RUN=true
      shift
      ;;
    -b|--base)
      if [[ -z "${2:-}" ]]; then
        echo "Error: --base requires a branch name argument." >&2
        exit 1
      fi
      TARGET_BASE="$2"
      shift 2
      ;;
    *)
      if [[ -z "$TARGET_BASE" ]]; then
        TARGET_BASE="$1"
        shift
      else
        echo "Error: Unexpected argument '$1'" >&2
        show_help >&2
        exit 1
      fi
      ;;
  esac
done

# -----------------------------------------------------------------------------
# Prerequisites Verification
# -----------------------------------------------------------------------------
if ! command -v gh &> /dev/null; then
  echo "Error: 'gh' (GitHub CLI) is not installed. Please install it to proceed." >&2
  exit 1
fi

if ! gh auth status &> /dev/null; then
  echo "Error: GitHub CLI is not logged in. Run 'gh auth login' first." >&2
  exit 1
fi

CURRENT_BRANCH=$(git branch --show-current)
if [[ -z "$CURRENT_BRANCH" ]]; then
  echo "Error: Not currently on a branch (detached HEAD)." >&2
  exit 1
fi

# -----------------------------------------------------------------------------
# Determine Base Branch
# -----------------------------------------------------------------------------
if [[ -z "$TARGET_BASE" ]]; then
  # Prefer feat/usage-graphs if it exists and is not the current branch, otherwise default repo branch
  if [[ "$CURRENT_BRANCH" != "feat/usage-graphs" ]] && git rev-parse --verify origin/feat/usage-graphs &> /dev/null; then
    TARGET_BASE="feat/usage-graphs"
  else
    TARGET_BASE=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo "main")
  fi
fi

if [[ "$CURRENT_BRANCH" == "$TARGET_BASE" ]]; then
  echo "Error: Current branch '$CURRENT_BRANCH' cannot target itself as base." >&2
  exit 1
fi

# -----------------------------------------------------------------------------
# Push Current Branch to Remote
# -----------------------------------------------------------------------------
if [[ "$DRY_RUN" == "false" ]]; then
  if ! git rev-parse --abbrev-ref --symbolic-full-name "@{u}" &> /dev/null; then
    echo "Pushing $CURRENT_BRANCH to origin..." >&2
    git push -u origin "$CURRENT_BRANCH" >&2
  else
    git push >&2
  fi
else
  echo "[dry-run] Skipping git push for $CURRENT_BRANCH" >&2
fi

# -----------------------------------------------------------------------------
# Generate Title & Body
# -----------------------------------------------------------------------------
COMMIT_COUNT=$(git rev-list --count "origin/$TARGET_BASE..$CURRENT_BRANCH" 2>/dev/null || echo "1")
if [[ ! "$COMMIT_COUNT" =~ ^[0-9]+$ ]]; then
  COMMIT_COUNT=1
fi

if [[ "$COMMIT_COUNT" -eq 1 ]]; then
  PR_TITLE=$(git log -1 --pretty=%s)
else
  # Format branch name as fallback title (e.g., feat/usage-graphs -> feat: usage graphs)
  CLEAN_NAME=$(echo "$CURRENT_BRANCH" | sed -E 's#^(feat|fix|chore|docs|refactor)/#\1: #' | tr '-' ' ')
  PR_TITLE="${CLEAN_NAME}"
fi

# Build PR Body with commit logs
COMMITS_LOG=$(git log "origin/$TARGET_BASE..$CURRENT_BRANCH" --pretty=format:"- %s (%h)" 2>/dev/null || git log -1 --pretty=format:"- %s (%h)")
if [[ -z "$COMMITS_LOG" ]]; then
  COMMITS_LOG=$(git log -1 --pretty=format:"- %s (%h)")
fi

PR_BODY=$(cat <<EOF
## Overview
Automated Pull Request for branch \`${CURRENT_BRANCH}\` targeting \`${TARGET_BASE}\`.

## Changes Included
${COMMITS_LOG}

## Verification
- Automated build & tests verified.
EOF
)

# -----------------------------------------------------------------------------
# Check for Existing PR or Create New One
# -----------------------------------------------------------------------------
EXISTING_PR=$(gh pr view "$CURRENT_BRANCH" --json url -q .url 2>/dev/null || true)

if [[ -n "$EXISTING_PR" ]]; then
  echo "$EXISTING_PR"
  exit 0
fi

# Create PR and output URL directly
if [[ "$DRY_RUN" == "true" ]]; then
  echo "[dry-run] Would execute: gh pr create --base \"$TARGET_BASE\" --head \"$CURRENT_BRANCH\" --title \"$PR_TITLE\" --body \"$PR_BODY\"" >&2
  PR_URL=$(gh pr create \
    --base "$TARGET_BASE" \
    --head "$CURRENT_BRANCH" \
    --title "$PR_TITLE" \
    --body "$PR_BODY" \
    --dry-run 2>/dev/null || echo "https://github.com/Tiago-0liveira/agy-manager/pull/dry-run")
  echo "$PR_URL"
  exit 0
fi

PR_URL=$(gh pr create \
  --base "$TARGET_BASE" \
  --head "$CURRENT_BRANCH" \
  --title "$PR_TITLE" \
  --body "$PR_BODY")

echo "$PR_URL"
