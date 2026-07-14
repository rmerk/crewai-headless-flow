---
description: crewai-headless-flow orchestration memory — delivery, verify, dashboard, and HITL patterns for this control plane.
applyTo: "src/crewai_headless_flow/**/*.py,examples/configs/**/*,config/*.yaml"
---

# CrewAI Headless Flow Memory

Patterns for shipping platform seams that stay offline-testable and queue/dashboard-safe.

## Queue and dashboard jobs use conditional HITL + file escalation

Serve/dashboard runs close stdin. For unattended Ticket → PR packs, set `human_feedback.mode: conditional` with Phase 0 triggers and `escalation.channel: file` so a fired gate parks on `pending_approval.json` and can resume from the dashboard (continue/abort), not a TTY prompt.

## Pack verify scripts: target-repo cwd + `{config_dir}` paths

`run_verification` always uses the target repo as `cwd`. Put pack scripts under `examples/configs/<pack>/scripts/` and reference them as `["bash", "{config_dir}/scripts/..."]`. The Flow expands `{config_dir}` from `state.config_dir` at verify time (including `pre_delivery_commands`).

## Draft PRs and ticket-prefixed delivery branches

Enable `deliver.draft: true` so `_ship` passes `--draft` to `gh pr create`. When the request parses as a ticket key (`PROJECT-####`), allocate `flow/<TICKET>-<run_id>` via `parse_jira_ticket_key` rather than forcing per-run `branch_prefix` overrides.

## Pre-delivery verify is a second bar before push/PR

Review-round `verify.commands` and finalize-time `verify.pre_delivery_commands` are distinct. Run pre-delivery in `_maybe_deliver` before `deliver()`, append to `verification_runs`, and let `_delivery_verification_ok` gate push/PR on the latest round.
