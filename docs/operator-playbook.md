# Operator Playbook

Concrete, runnable starting points for common `crewai-headless-flow` operating modes.

## Prerequisite

Create the sample target repo used throughout these examples:

```bash
uv run python examples/create_sample_target.py /tmp/demo-target
```

## 1. Default Fast Path

Use when:
- You want the repo default behavior
- You want Grok on `do_work`, Codex elsewhere
- You do not need human gates or crew-backed stages

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add a subtract function and a corresponding test using TDD" \
  --target-repo /tmp/demo-target \
  --config-dir config
```

## 2. Claude On Edit Stage

Use when:
- You want to keep planning/review/finalize stable
- You only want to swap the mutating stage to Claude Code

Config dir:
- `examples/configs/claude-do-work`

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add a subtract function and a corresponding test using TDD" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/claude-do-work
```

## 3. Gemini On Edit Stage

Use when:
- You want to validate the Gemini CLI adapter in the mutating stage
- You want to keep planning/review/finalize stable
- You only want to swap the edit worker, not the broader flow topology

Config dir:
- `examples/configs/gemini-do-work`

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add a subtract function and a corresponding test using TDD" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/gemini-do-work
```

## 3.5. Jira Workflow (Ticket → PR)

Use when:
- You want to plan, implement, and audit Jira tickets (`AS-####`) via the dashboard or CLI
- You want `jira-ticket-plan` → `jira-ticket-implement` → `jira-ticket-plan-audit`
- You are targeting `asure.ptm.portal.web.ui.new` (webapi is reference-only and rejected as `--target-repo`)

Config dir:
- `examples/configs/jira-workflow`

Pack behavior (Ticket → PR):
- Conditional HITL + `escalation.channel: file` (dashboard-safe park/resume)
- `max_revisions: 3` in the pack (CLI/`run` honors it when `--max-revisions` is omitted; dashboard also defaults to 3)
- Deliver: push + **draft** PR on `flow/AS-####-<run_id>`
- Verify: `scripts/verify-round.sh` each review; `scripts/verify-pre-delivery.sh` before ship
- Pack scripts use `{config_dir}` so they resolve even when cwd is the target repo

Preferred kick:

```bash
export ASURE_BASE_DIR=/path/to/asure   # parent of portal + webapi checkouts
uv run python -m crewai_headless_flow dashboard \
  --queue-dir ./queue \
  --runs-dir ./runs
```

`--runs-dir` must match where serve writes run dirs — approval APIs read `pending_approval.json` from there. Without `ASURE_BASE_DIR`, paste an absolute portal path; webapi targets are rejected.

CLI escape hatch:

```bash
uv run python -m crewai_headless_flow run \
  --request "AS-5245" \
  --target-repo /path/to/asure.ptm.portal.web.ui.new \
  --config-dir examples/configs/jira-workflow \
  --max-revisions 3 \
  --runs-dir ./runs
```


## 4. Cursor On All Stages

Use when:
- You want Cursor Agent CLI across plan, do_work, review, and finalize
- You already authenticate with `CURSOR_API_KEY` in your shell or via `cursor agent login`
- You want a single-model lane without editing the default `config/` pack

Config dir:
- `examples/configs/cursor-do-work`

Command:

```bash
uv run python -m crewai_headless_flow doctor --config-dir examples/configs/cursor-do-work

uv run python -m crewai_headless_flow run \
  --request "Add a subtract function and a corresponding test using TDD" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/cursor-do-work
```

## 5. Implementation Crew

Use when:
- You want CrewAI to wrap each planned task in an inspect/edit/verify subflow
- You want bounded self-correction plus task-local decomposition
- You want to choose between crew-only execution, conservative parallel crew execution, planner-assisted parallel crew execution, and full replan-recovery crew execution
- You do not need the broader HITL surface

Config dir:
- `examples/configs/implementation-crew`
- `examples/configs/implementation-crew-parallel`
- `examples/configs/implementation-crew-parallel-planner`
- `examples/configs/implementation-crew-parallel-replan`

Crew-only variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/implementation-crew \
  --debug-report-file /tmp/implementation-crew-report.md
```

Parallel crew variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/implementation-crew-parallel \
  --debug-report-file /tmp/implementation-crew-parallel-report.md
```

Planner-assisted parallel crew variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/implementation-crew-parallel-planner \
  --debug-report-file /tmp/implementation-crew-parallel-planner-report.md
```

Full replan-recovery crew variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/implementation-crew-parallel-replan \
  --debug-report-file /tmp/implementation-crew-parallel-replan-report.md
```

## 6. Planning Crew

Use when:
- You want multi-agent repository research before edit work starts
- You want the plan stage to stay read-only while using CrewAI for planning depth
- You want a bounded example of the optional planning-crew surface

Config dir:
- `examples/configs/planning-crew`

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/planning-crew \
  --debug-report-file /tmp/planning-crew-report.md
```

## 7. Plan Gate

Use when:
- You want a human checkpoint before repository-wide planning begins
- You want to optionally inject one-line operator guidance into the planning prompt
- You want a narrow example of the `before_plan` resume path

Config dir:
- `examples/configs/plan-gate`

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/plan-gate \
  --state-file /tmp/plan-gate-state.json
```

Useful follow-up:

```bash
uv run python -m crewai_headless_flow run \
  --resume-state-file /tmp/plan-gate-state.json
```

## 8. Review Crew

Use when:
- You want multi-agent review depth without turning on broader operator gates
- You want automated review to stay read-only while using a CrewAI review coordinator
- You want a bounded example of the optional review-crew surface

Config dir:
- `examples/configs/review-crew`

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-crew \
  --debug-report-file /tmp/review-crew-report.md
```

## 9. Operator Review Gate

Use when:
- You want a human decision after automated review findings are available
- You want to choose between a broader multi-action lane and narrow rerun-review / target / replan / force-pass / force-revise lanes
- You do not want prompts before `do_work` or `finalize`

Config dir:
- `examples/configs/operator-review-gate`
- `examples/configs/review-rerun-review-gate`
- `examples/configs/review-targeting-only-gate`
- `examples/configs/review-replan-only-gate`
- `examples/configs/review-force-revise-gate`
- `examples/configs/review-force-pass-gate`

Broad review-decision variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add a subtract function and a corresponding test using TDD" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/operator-review-gate \
  --state-file /tmp/review-gate-state.json
```

Narrow rerun-review variant:

Use when:
- You want the operator to rerun the read-only review stage with extra guidance after automated findings
- You do not need replanning, targeting, or force-pass / force-revise controls

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-rerun-review-gate \
  --state-file /tmp/review-rerun-review-state.json
```

Narrow targeting variant:

Use when:
- You want the operator to reopen only exact structured tasks after automated findings
- You do not need rerun-review, replanning, or force-pass / force-revise controls

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-targeting-only-gate \
  --state-file /tmp/review-targeting-only-state.json
```

Narrow replanning variant:

Use when:
- You want the operator to force the next revise loop through structured replanning after automated findings
- You do not need rerun-review, targeting, or force-pass / force-revise controls

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-replan-only-gate \
  --state-file /tmp/review-replan-only-state.json
```

Narrow force-revise variant:

Use when:
- You want the review checkpoint to reopen work with explicit issue text from the operator
- You do not need rerun-review, replanning, targeting, or force-pass controls

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-force-revise-gate \
  --state-file /tmp/review-force-revise-state.json
```

Narrow force-pass variant:

Use when:
- You want the operator to accept the run after automated review findings
- You do not need rerun-review, replanning, targeting, or force-revise controls

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-force-pass-gate \
  --state-file /tmp/review-force-pass-state.json
```

Useful follow-up:

```bash
uv run python -m crewai_headless_flow run \
  --resume-state-file /tmp/review-gate-state.json
```

The saved state already remembers `examples/configs/operator-review-gate`. Pass
`--config-dir` on resume only when you want to switch to a different config
pack.
If the saved stop point was the `after_review` checkpoint, resume reopens that
same automated-review decision instead of rerunning review first.
The saved state also records a structured aborted-checkpoint snapshot with the
exact gate, saved checkpoint message, and any saved `before_review` guidance
needed for `rerun-review`, so resume does not have to guess which human
checkpoint to reopen.

## 10. Focused do_work Targeting Gate

Use when:
- You trust the current structured task graph
- You want the operator to run only specific tasks before edit mode begins
- You want the flow to auto-include unmet dependencies but leave the rest of the task graph pending

Notes:
- `target-tasks` accepts comma-separated task IDs, ranges like `2-4`, `all`, `hinted`, or exact file selectors like `file:docs/readme.md`
- Any unmet dependencies for the selected tasks are automatically included in the focused execution set
- Untargeted tasks stay pending for the next loop instead of being forced through the current `do_work` round

Config dir:
- `examples/configs/do-work-targeting-gate`

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/do-work-targeting-gate \
  --state-file /tmp/do-work-targeting-state.json
```

## 11. Focused do_work Replan Gate

Use when:
- You trust the repo context but not the current structured task graph
- You want the operator to force a fresh structured replan before edit mode begins
- You want a narrow config-gated shortcut instead of broader `advanced_actions`

Notes:
- `replan` reruns the planning stage before edit mode with operator guidance
- Because this happens before any task execution starts, the flow can replace the full structured task graph instead of reopening only selected tasks

Config dir:
- `examples/configs/do-work-replan-gate`

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/do-work-replan-gate \
  --state-file /tmp/do-work-replan-state.json
```

## 12. Focused do_work Skip-to-Review Gate

Use when:
- You want the operator to defer edit work entirely and inspect the current state first
- You want the flow to jump straight from planning into the read-only review stage
- You want a narrow config-gated shortcut instead of broader `advanced_actions`

Notes:
- `skip-to-review` bypasses the `do_work` worker entirely
- This is useful when the structured plan already needs review-level scrutiny before another edit round

Config dir:
- `examples/configs/do-work-skip-to-review-gate`

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/do-work-skip-to-review-gate \
  --state-file /tmp/do-work-skip-review-state.json
```

## 13. Review Targeting Gate

Use when:
- You trust the current task graph
- You want the operator to say "only reopen tasks 2 and 4"
- You want a narrower review-time shortcut than a full replan

Notes:
- `target-tasks` accepts comma-separated task IDs, ranges like `2-4`, `all`, `hinted`, or exact file selectors like `file:docs/readme.md`
- The prompt now shows the current task catalog with ids, status, and files, plus suggested review targets when they are available
- You can expose this at `before_review` when the operator already knows what to reopen, or at `after_review` when the automated findings should inform the selection

Config dir:
- `examples/configs/review-targeting-only-gate`
- `examples/configs/review-targeting-gate`
- `examples/configs/review-targeting-before-gate`

Narrow post-review variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-targeting-only-gate \
  --state-file /tmp/review-targeting-only-state.json
```

Broader post-review variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-targeting-gate \
  --state-file /tmp/review-targeting-state.json
```

Pre-review variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-targeting-before-gate \
  --state-file /tmp/review-targeting-before-state.json
```

## 14. Review Replan Gates

Use when:
- You want a narrow config-gated review shortcut instead of broad `advanced_actions`
- You want the operator to be able to say "the task graph is wrong, replan it" either before or after automated review
- You want to choose whether automated review findings inform that replanning decision

Config dir:
- `examples/configs/review-replan-only-gate`
- `examples/configs/review-replan-before-gate`
- `examples/configs/review-replan-gate`

Narrow post-review variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-replan-only-gate \
  --state-file /tmp/review-replan-only-state.json
```

Pre-review variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-replan-before-gate \
  --state-file /tmp/review-replan-before-state.json
```

Post-review variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-replan-gate \
  --state-file /tmp/review-replan-state.json
```

## 15. Review Force Gates

Use when:
- You already know the current state well enough to make a forced review decision before automated review runs
- You want a narrow config-gated force shortcut instead of broader `advanced_actions`
- You want to choose between forcing a revise outcome or forcing a pass outcome

Config dir:
- `examples/configs/review-force-revise-before-gate`
- `examples/configs/review-force-pass-before-gate`

Force-revise variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-force-revise-before-gate \
  --state-file /tmp/review-force-revise-before-state.json
```

Force-pass variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-force-pass-before-gate \
  --state-file /tmp/review-force-pass-before-state.json
```

## 16. Finalize Review Gates

Use when:
- You want a final human checkpoint before docs/ADR
- You want to choose between a narrow rerun-review lane, a narrow replanning lane, a narrow force-revise lane, a narrow skip lane, a narrow targeting lane, and the broader finalize decision surface

Config dir:
- `examples/configs/finalize-rerun-review-gate`
- `examples/configs/finalize-replan-gate`
- `examples/configs/finalize-force-revise-gate`
- `examples/configs/finalize-skip-gate`
- `examples/configs/finalize-targeting-gate`
- `examples/configs/finalize-review-gate`

Narrow rerun-review variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/finalize-rerun-review-gate \
  --state-file /tmp/finalize-rerun-review-state.json
```

Narrow replanning variant:

Use when:
- You want to reopen work through the structured revision replanner before finalize
- You want the operator to resplit or replace the remaining task graph rather than reopen exact task IDs

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/finalize-replan-gate \
  --state-file /tmp/finalize-replan-state.json
```

Narrow force-revise variant:

Use when:
- You want to reopen work from the final checkpoint with explicit issue text
- You do not need rerun-review, structured replanning, or targeted reopen controls

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/finalize-force-revise-gate \
  --state-file /tmp/finalize-force-revise-state.json
```

Narrow skip variant:

Use when:
- You want the operator to finish the flow without running the finalize worker
- You want a final explicit docs/ADR skip checkpoint and nothing broader

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/finalize-skip-gate \
  --state-file /tmp/finalize-skip-state.json
```

Narrow targeting variant:

Use when:
- You want to reopen only exact structured tasks before finalize
- You want the same task-id/range/file selector targeting model used at review time

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/finalize-targeting-gate \
  --state-file /tmp/finalize-targeting-state.json
```

Broad finalize review variant:

Use when:
- You want the operator to be able to rerun read-only review against the saved latest work summary
- You may need to skip finalize or reopen only specific tasks without enabling earlier HITL gates

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/finalize-review-gate \
  --state-file /tmp/finalize-review-state.json
```

Useful follow-up:

```bash
uv run python -m crewai_headless_flow run \
  --resume-state-file /tmp/finalize-review-state.json
```

If the saved stop point was the `before_finalize` checkpoint, resume reopens that
same final checkpoint. When the saved state still has the latest work summary,
`rerun-review` stays available there without rerunning `do_work`.

## 17. Guided Operator Loop

Use when:
- You want operator guidance captured before edit work
- You may want to run only a targeted subset of structured tasks before review
- You want review-time rerun-review/target-tasks/replan/force-pass/force-revise decisions after automated findings
- You want a final human checkpoint before finalize, including the option to rerun review or reopen work

Config dir:
- `examples/configs/guided-operator-loop`

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/guided-operator-loop \
  --state-file /tmp/guided-operator-state.json
```

## 18. Conditional HITL (autonomous unless a trigger fires)

Use when:
- You want the flow mostly autonomous, prompting only when it likely needs guidance
- You want a prompt before `do_work` if a task keeps failing (a stuck-task signal)
- You want a prompt after review when the revise loop is nearing its `max_revisions` ceiling

Under `mode: conditional` the five static gate booleans are ignored; prompting is
driven solely by the enabled triggers. Gates without a trigger stay silent.

Config dir:
- `examples/configs/conditional-hitl`

Command:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/conditional-hitl \
  --state-file /tmp/conditional-hitl-state.json
```

Tune a trigger for a single run without editing YAML:

```bash
  --override-human-feedback conditional.triggers.repeated_task_failure.min_attempts=3
```

## 19. Parallel Structured Work

Use when:
- The request likely produces multiple independent tasks
- You want to choose between conservative task batching, planner-assisted parallel selection, and replan recovery

Config dir:
- `examples/configs/parallel-do-work`
- `examples/configs/parallel-planner`
- `examples/configs/parallel-replan`

Conservative parallel variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/parallel-do-work \
  --debug-report-file /tmp/parallel-do-work-report.md
```

Planner-assisted variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/parallel-planner \
  --debug-report-file /tmp/parallel-planner-report.md
```

Full replan-recovery variant:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/parallel-replan \
  --debug-report-file /tmp/parallel-replan-report.md
```

## Inspect Before Running

For any config pack above:

```bash
uv run python -m crewai_headless_flow doctor --config-dir <CONFIG_DIR>
uv run python -m crewai_headless_flow preflight --target-repo /tmp/demo-target
```

`doctor` is an env/config check, not only a YAML parse. It fails closed if a
configured worker CLI is missing or does not expose the required flags for this
repo.

It also accepts the same runtime override flags as `run`, so you can validate
an ad hoc worker/HITL/crew change before executing the flow. Text mode now
prints the resolved per-stage runtime plus resolved HITL settings, so you can
inspect override results without switching to `--format json`.

## One-Run Overrides

You can still layer CLI overrides on top of any example config:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/operator-review-gate \
  --override-human-feedback-action do_work=replan,skip-to-review,target-tasks \
  --override-human-feedback-action after_review=replan,rerun-review,target-tasks,force-revise,force-pass \
  --override-human-feedback-action finalize=skip-finalize,rerun-review \
  --override-default-worker claude \
  --override-skill do_work=test-driven-development \
  --override-worker do_work=claude
```

Starting from `config/`, the default `do_work` stage already matches the local
worker and single-stage crew defaults used by the simpler example packs, so you
can recreate those lanes with a few focused overrides:

Claude `do_work` lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-worker do_work=claude \
  --override-model do_work=sonnet
```

Gemini `do_work` lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-worker do_work=gemini \
  --override-model do_work=gemini-2.5-pro
```

Planning Crew lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-stage-extra plan.crew.enabled=true
```

Review Crew lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-stage-extra review.crew.enabled=true
```

Starting from `config/`, the default `do_work` stage already matches the local
direct-worker and parallel defaults used by the plain parallel example packs,
so you can recreate those lanes with only stage-extra toggles:

Conservative parallel lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4
```

Planner-assisted parallel lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4 \
  --override-stage-extra do_work.parallel.planner.enabled=true
```

Full replan-recovery parallel lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4 \
  --override-stage-extra do_work.parallel.planner.enabled=true \
  --override-stage-extra do_work.replan.enabled=true \
  --override-stage-extra do_work.replan.on_execution_failure=true \
  --override-stage-extra do_work.replan.on_cross_task_change=true \
  --override-stage-extra do_work.replan.on_ambiguous_success=true \
  --override-stage-extra do_work.replan.max_execution_replans=2
```

Starting from `config/`, the default `do_work` stage already matches the local
crew defaults used by the implementation-crew example packs, so you can
recreate those lanes with only stage-extra toggles:

Crew-only implementation lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-stage-extra do_work.crew.enabled=true \
  --override-stage-extra do_work.crew.decomposition.enabled=true \
  --override-stage-extra do_work.crew.decomposition.max_subtasks=3
```

Conservative parallel crew lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-stage-extra do_work.crew.enabled=true \
  --override-stage-extra do_work.crew.decomposition.enabled=true \
  --override-stage-extra do_work.crew.decomposition.max_subtasks=3 \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4
```

Planner-assisted parallel crew lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-stage-extra do_work.crew.enabled=true \
  --override-stage-extra do_work.crew.decomposition.enabled=true \
  --override-stage-extra do_work.crew.decomposition.max_subtasks=3 \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4 \
  --override-stage-extra do_work.parallel.planner.enabled=true
```

Full replan-recovery crew lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add subtract and divide helpers plus tests and update README usage notes" \
  --target-repo /tmp/demo-target \
  --config-dir config \
  --override-stage-extra do_work.crew.enabled=true \
  --override-stage-extra do_work.crew.decomposition.enabled=true \
  --override-stage-extra do_work.crew.decomposition.max_subtasks=3 \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4 \
  --override-stage-extra do_work.parallel.planner.enabled=true \
  --override-stage-extra do_work.replan.enabled=true \
  --override-stage-extra do_work.replan.on_execution_failure=true \
  --override-stage-extra do_work.replan.on_cross_task_change=true \
  --override-stage-extra do_work.replan.on_ambiguous_success=true \
  --override-stage-extra do_work.replan.max_execution_replans=2
```

To clear inherited HITL shortcuts from a config pack for one run, use
`TARGET=none`:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-targeting-gate \
  --override-human-feedback-action after_review=none
```

If you want only the focused pre-edit targeting shortcut without also exposing
`replan` or `skip-to-review`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_do_work=target-tasks
```

If you want only the focused pre-edit replanning shortcut without also exposing
`skip-to-review` or `target-tasks`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_do_work=replan
```

If you want only the pre-edit skip-to-review shortcut without also exposing
`replan` or `target-tasks`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_do_work=skip-to-review
```

If you want only the focused pre-review replanning shortcut without also exposing
`force-pass`, `force-revise`, or targeted reopen controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_review=replan
```

If you want only the focused pre-review force-revise shortcut without also
exposing `replan`, `force-pass`, or targeted reopen controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_review=force-revise
```

If you want only the focused pre-review force-pass shortcut without also
exposing `replan`, `force-revise`, or targeted reopen controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_review=force-pass
```

If you want only the review-time rerun-review shortcut without also exposing
`replan`, `target-tasks`, `force-revise`, or `force-pass`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action after_review=rerun-review
```

If you want only the review-time targeting shortcut without also exposing
`rerun-review`, `replan`, `force-revise`, or `force-pass`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action after_review=target-tasks
```

If you want only the review-time replanning shortcut without also exposing
`rerun-review`, `target-tasks`, `force-revise`, or `force-pass`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action after_review=replan
```

If you want only the review-time force-revise shortcut without also exposing
`rerun-review`, `replan`, `target-tasks`, or `force-pass`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action after_review=force-revise
```

If you want only the review-time force-pass shortcut without also exposing
`rerun-review`, `replan`, `target-tasks`, or `force-revise`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action after_review=force-pass
```

If you want only the final rerun-review shortcut without also exposing reopen or
skip-finalize controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_finalize=rerun-review
```

If you want only the final replanning shortcut without also exposing rerun-review,
targeted reopen controls, or skip-finalize, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_finalize=replan
```

If you want only the final force-revise shortcut without also exposing rerun-review,
replanning, or skip-finalize, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_finalize=force-revise
```

If you want only the final skip shortcut without also exposing rerun-review or
reopen-work controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_finalize=skip-finalize
```

If you want only the final targeting shortcut without also exposing rerun-review,
replan, or skip-finalize controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_finalize=target-tasks
```
