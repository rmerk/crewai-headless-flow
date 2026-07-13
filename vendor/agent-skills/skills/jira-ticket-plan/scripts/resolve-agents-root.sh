#!/usr/bin/env bash
# Resolve monorepo root containing .agents/jira-plans (walk up from PWD).
# Prints absolute path to .agents/jira-plans directory.
set -euo pipefail

if [[ -n "${PLAN_DIR:-}" ]]; then
  echo "$PLAN_DIR"
  exit 0
fi

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
while [[ "$dir" != "/" ]]; do
  if [[ -d "$dir/.agents/jira-plans" ]]; then
    echo "$dir/.agents/jira-plans"
    exit 0
  fi
  dir="$(dirname "$dir")"
done

# Legacy fallback when not run from the asure workspace
echo "${HOME}/.agents/jira-plans"
