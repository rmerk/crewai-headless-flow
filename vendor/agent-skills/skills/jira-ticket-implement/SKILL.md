---
name: jira-ticket-implement
description: >-
  Implement a Jira ticket from a saved personal plan at .agents/jira-plans/{KEY}.md.
  Follows TDD vertical slices, Vue portal rules, and systematic-debugging for bugs. Use after
  jira-ticket-plan or when user says implement from plan, execute plan AS-12345, or
  plan-and-ship handoff.
---

# Jira Ticket Implement

Execute an existing **`.agents/jira-plans/{ISSUE_KEY}.md`** plan produced by [`jira-ticket-plan`](../jira-ticket-plan/SKILL.md). Reads the plan as the single source of truth; implements in the PTM Portal with TDD.

**Plan path:** `.agents/jira-plans/{ISSUE_KEY}.md`

## Workspace layout

```text
WORKSPACE_ROOT   = asure/
GIT_ROOT         = asure.ptm.portal.web.ui.new/
PORTAL_UI        = asure.ptm.portal.web.ui.new/assureptmdashboard/
WEBAPI           = asure.ptm.webapi/
PLAN_DIR         = .agents/jira-plans/
```

**Git:** Branch checks from `WORKSPACE_ROOT` resolve to `GIT_ROOT`. Do not modify `asure.ptm.webapi/`.

---

## Workflow

```
- [ ] Step 0: Load plan + confirm branch + staleness gate
- [ ] Step 1: Load implementation skills
- [ ] Step 2: Execute per plan (TDD vertical slices)
      - [ ] After each slice: typecheck + scoped tests (must pass before next slice)
- [ ] Step 3: Final verify (typecheck + all plan specs; broaden if shared files touched)
- [ ] Step 4: Summarize for PR / user
- [ ] Step 5: Commit and push ticket branch when explicitly shipping the implementation
```

---

### Step 0 — Load plan

1. Read `.agents/jira-plans/{ISSUE_KEY}.md`.
2. **Halt** if missing → run `jira-ticket-plan` first.
3. Parse frontmatter: `issue`, `branch`, `recommendedBranch`, `deliveryStatus`, `backendInvestigation`, `backendEscalation`, `webapiSha`, `generated`, `ticketComplexity`.
4. If `backendEscalation: true` → portal-only scope unless user directs otherwise.
5. **Delivery warning:** if plan **Existing delivery** shows `deliveryStatus` is `merged` or `merged-multiple` and current branch equals bare `{ISSUE_KEY}` (not the recommended suffix from Step 2) → warn user before implementing; confirm follow-up scope or re-run delivery check.
6. Confirm branch at **workspace root**:

```bash
cd "$WORKSPACE_ROOT" && git rev-parse --show-toplevel
git rev-parse --abbrev-ref HEAD
```

#### Plan staleness gate

**Skip** when `backendInvestigation` is `skipped` or `absent`.

When `backendInvestigation: required`:

```bash
cd "$WORKSPACE_ROOT"
SYNC_BRANCH="origin/develop"
git -C asure.ptm.webapi fetch origin develop 2>/dev/null || git -C asure.ptm.webapi fetch origin main 2>/dev/null || true
if git -C asure.ptm.webapi rev-parse --verify origin/develop >/dev/null 2>&1; then
  SYNC_BRANCH="origin/develop"
elif git -C asure.ptm.webapi rev-parse --verify origin/main >/dev/null 2>&1; then
  SYNC_BRANCH="origin/main"
fi
current=$(git -C asure.ptm.webapi rev-parse --short=7 "$SYNC_BRANCH" 2>/dev/null || echo "")
```

Also read **Synced branch** from plan Backend investigation table when present (prefer over default).

- Parse `webapiSha`; if key absent → treat as stale (same as `N/A`).
- Normalize both SHAs to **7 characters** for comparison.
- **On fetch failure or empty `current` → halt**; do not proceed to Step 2.
- If `current` != plan `webapiSha` (7-char) → offer:
  - **(a)** continue stale — Step 4 summary: `API evidence: continued stale @ {plan webapiSha}`
  - **(b)** halt + **6.2-lite refresh** (see below)
  - **(c)** user confirms API unchanged
- Optional: warn if plan `generated` > 14 days (invalid ISO → skip).

#### 6.2-lite refresh

Read-only re-investigation when user chooses staleness option **(b)**:

1. Re-run jira-ticket-plan Step 6.2 **cache-miss path** (fetch, index lookup, sub-agent as needed)
2. Update plan file: `webapiSha`, Backend investigation section, `routes` frontmatter
3. Re-run `export-plan-metadata.sh {ISSUE_KEY}`
4. User confirms before resuming implement Step 2
5. **No WebAPI edits**; no portal code during refresh

---

### Step 1 — Load skills

- `tdd`, `vue-best-practices`, vue/vitest rules from `.cursor/rules/`
- Bugs: `systematic-debugging` — complete investigation plan before fixes

---

### Step 2 — Execute

Follow plan: investigation (bugs) → backend evidence → implementation steps → tests.

Respect **blocked pending backend** steps when `backendEscalation: true`.

#### Verify each slice (required)

After every meaningful edit batch (one vertical slice, one plan step, or one file group):

```bash
cd asure.ptm.portal.web.ui.new/assureptmdashboard
npm run typecheck
npm run test:unit -- tests/unit/{relevant}.spec.ts
```

- **Do not proceed** to the next slice while typecheck or scoped tests fail.
- If the plan lists multiple spec files, run each touched spec before handoff.
- Run full `npm run test:unit` when shared modules change (stores, composables, filter utils, global config).

Also follow [`.cursor/rules/ptm-portal-assureptmdashboard-completion.mdc`](../../../.cursor/rules/ptm-portal-assureptmdashboard-completion.mdc) for failure handling.

#### Test integrity (required)

| Do | Don't |
|----|-------|
| Fix **production code** first when a test exposes a real bug or regression | Delete, skip (`it.skip` / `describe.skip`), or weaken assertions to get green |
| Update tests **only** when behavior intentionally changed (ticket AC, agreed product change) | Rewrite unrelated expectations or mocks without reading why the test exists |
| Extend specs for **new** behavior per plan Test strategy | Change snapshots wholesale without reviewing diff |
| If a test looks wrong: investigate, note in summary, ask user if behavior is ambiguous | Assume the test is stale and patch it to match broken code |

Before editing `.spec.ts` files, read [`.cursor/rules/ptm-portal-vue-test-vitest-skills.mdc`](../../../.cursor/rules/ptm-portal-vue-test-vitest-skills.mdc) and load the `tdd` skill (vertical slices).

---

### Step 3 — Final verify

Re-run before handoff — not a substitute for per-slice verification in Step 2.

```bash
cd asure.ptm.portal.web.ui.new/assureptmdashboard
npm run typecheck
npm run test:unit -- tests/unit/{specs from plan}.spec.ts
```

- Run every spec file listed in the plan **plus** any spec mapped to changed source files.
- When shared modules changed, run full `npm run test:unit` instead of scoped only.
- All commands must exit 0 before Step 4.

---

### Step 4 — Summarize

- **Verification (required):** commands run, exit codes, and any spec updates with rationale
- Backend escalation pointer when applicable
- Stale continue note when **(a)** chosen
- Files changed, tests added/updated, open questions, suggested PR title
- Optional: suggest [`jira-ticket-plan-audit`](../jira-ticket-plan-audit/SKILL.md)

---

### Step 5 — Commit and push

Use this step only when the user asks to ship, commit, push, or otherwise complete the ticket branch. The Asure portal repo is Bitbucket-hosted; do not use GitHub PR tooling for this workflow.

1. Confirm the target repo, branch, and remote:

```bash
cd /Users/rchoi/Developer/asure/asure.ptm.portal.web.ui.new
git status --short --branch
git remote -v
git rev-parse --abbrev-ref HEAD
```

2. Halt if the current branch is `main`, `develop`, or does not match the ticket branch from the plan. Resolve the branch mismatch before committing.
3. Stage only portal implementation and test files needed for the ticket. Do not stage `.agents/jira-plans/*.review.md` or other generated review sidecars unless the user explicitly asks.
4. Confirm no WebAPI files are staged or modified for commit:

```bash
git status --short
```

5. Commit with a ticket-prefixed message, for example:

```bash
git commit -m "AS-4773: Add POA history modal"
```

6. Push the active ticket branch to Bitbucket:

```bash
git push origin HEAD
```

7. Report the pushed branch and Bitbucket PR creation URL or branch URL. Do not claim a PR was opened unless it was actually created.

---

## Handoff from jira-ticket-plan

Load with same `{ISSUE_KEY}` when `mode: plan-and-ship`. Re-check staleness gate if plan age or webapi SHA may have drifted.
