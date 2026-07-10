# Deny-path enforcement at Flow-owned boundaries; serial edits can run isolated

## Status
Accepted.

Edit-mode workers run their CLIs fully bypassed — that is the product — so nothing constrained what they wrote. `src/crewai_headless_flow/paths_policy.py` enforces an operator-declared deny list at the boundaries the Flow owns, and a new `do_work.isolation` knob gives serial edits the same workspace-copy containment the parallel path always had (closing Gap 6's deeper fix).

Decisions:

- **Deny globs are broad by design.** `fnmatch.fnmatchcase` against posix relpaths, where `*` crosses `/` — `*.env` also denies `sub/dir/x.env`. This is a safety control: over-denying fails a task loudly; under-denying leaks. Pinned by a test so nobody "fixes" it into a bypass. Deny-only (no allowlist — allowlists cause false failures), and **file-only config**: there is no CLI override, because a per-run flag that can weaken a deny list undermines it.
- **Enforced at all four Flow-owned boundaries.** (1) Parallel mergeback: a denied task fails closed and nothing leaves its workspace copy — merging an allowed subset would mark the task complete while silently dropping changes. (2) Structured serial in-place: post-hoc restore plus a task failure that routes into the existing replan/failure machinery. (3) The unstructured (task-less) edit — now snapshot-bracketed like finalize instead of trusting the worker's self-report. (4) The finalize snapshot diff: denied paths are restored and excluded from delivery. `_maybe_deliver` additionally filters denied paths from the staged set, so unrestorable leftovers are never laundered into the commit.
- **Restore semantics are honest about their limits.** Tracked file → `git checkout -- <path>`; untracked file the run created → unlinked; untracked file that *pre-existed* the run → left in place and reported unrestorable, because snapshots store hashes, not content, and deleting it would destroy operator data. Writes entirely outside the target repo are not detectable post-hoc. The clean containment for both is `isolation: copy`. Doctor warns when deny globs are configured but serial enforcement is post-hoc-restore only.
- **`do_work.isolation: in_place | copy`** (default `in_place`, overridable via `--override-stage-extra do_work.isolation=copy`). `copy` runs serial and direct edits in a disposable workspace copy with the same pre-merge deny filter the parallel path has; a failed or denied edit leaves the target repo pristine.
- **Amended git-writes invariant:** git writers are `delivery.py` (branch/commit/push) and `paths_policy.restore_denied_paths` (scoped to `git checkout -- <denied paths>`) — nothing else. The git runner is shared and injectable.
- `create_workspace_copy` now skips sockets/fifos/devices — git's fsmonitor daemon leaves a `.git/fsmonitor--daemon.ipc` socket in real repos that broke `copytree`.

Config: top-level `paths: {deny: [globs]}` in `worker.yaml` (relative globs only).

See `docs/architecture/autonomy-gap-analysis.md` (Gaps 6 and 8, Phase 2) and ADR-0006.
