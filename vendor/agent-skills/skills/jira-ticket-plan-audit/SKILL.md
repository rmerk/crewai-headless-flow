---
name: jira-ticket-plan-audit
description: >-
  Audit a PR diff against a saved Jira implementation plan at .agents/jira-plans/{KEY}.md.
  Checks file coverage, test strategy, backend escalation scope, and scope creep. Use after
  jira-ticket-implement or when user says audit AS-12345 against plan.
---

# Jira Ticket Plan Audit

Compare **git diff** against a saved plan from [`jira-ticket-plan`](../jira-ticket-plan/SKILL.md). Read-only — no code changes.

**Plan path:** `.agents/jira-plans/{ISSUE_KEY}.md`

## Workspace layout

```text
WORKSPACE_ROOT   = asure/
GIT_ROOT         = asure.ptm.portal.web.ui.new/
PORTAL_PREFIX    = asure.ptm.portal.web.ui.new/assureptmdashboard/
PLAN_DIR         = .agents/jira-plans/
```

---

## Workflow

```
- [ ] Step 0: Load plan + parse frontmatter
- [ ] Step 1: Collect git diff (committed + working tree)
- [ ] Step 2: Normalize paths and run checks
- [ ] Step 3: Output audit checklist
```

---

### Step 0 — Load plan

Read plan; halt if missing. Parse `backendEscalation`, `backendInvestigation`, `ticketComplexity`.

---

### Step 1 — Collect diff

From **workspace root**:

```bash
cd "$WORKSPACE_ROOT" && git rev-parse --show-toplevel
cd "$GIT_ROOT"
git diff develop...HEAD --name-only 2>/dev/null || git diff --name-only
git diff --name-only
git diff --cached --name-only
```

**Union** all path lists (dedupe). Tag each path with source:

- `committed` — from `develop...HEAD` (or fallback)
- `unstaged` — working tree
- `staged` — index

**Empty union:** report "no changes yet" — not a failure.

Note diff base in output (`develop...HEAD` or fallback).

---

### Step 2 — Path normalization

Normalize plan and diff paths before compare:

1. Remove `asure.ptm.portal.web.ui.new/` prefix if present
2. Remove leading `./`
3. **If plan path starts with `assureptmdashboard/` and diff path does not** → strip `assureptmdashboard/` from plan path
4. **If diff path starts with `assureptmdashboard/` and plan path does not** → strip from diff path
5. Compare case-sensitive

**Examples:**

| Plan path | Diff path (GIT_ROOT) | Normalized match |
|-----------|----------------------|------------------|
| `asure.ptm.portal.web.ui.new/assureptmdashboard/src/App.vue` | `assureptmdashboard/src/App.vue` | `src/App.vue` |
| `asure.ptm.portal.web.ui.new/assureptmdashboard/src/App.vue` | `src/App.vue` | `src/App.vue` |

Extract paths from **Implementation plan** steps and **Test strategy** spec column.

---

### Step 3 — Checks

| Check | Pass criteria | Severity |
|-------|---------------|----------|
| Plan files touched | In diff union OR step **blocked pending backend** / N/A | warn |
| Test specs | In diff union or N/A | warn |
| Backend escalation | `backendEscalation: true` → no `asure.ptm.webapi/` in diff | **error** |
| Scope creep | Extra diff files not in plan | info (>3 → warn) |
| Empty diff | No paths | info |

**Blocked steps:** marked **blocked pending backend** → N/A, not missing.

---

### Audit output template

```markdown
# Plan audit: {ISSUE_KEY}

**Diff base:** develop...HEAD | {fallback}
**Plan generated:** {from frontmatter}

## Checklist

- [ ] Implementation plan files: {N}/{M} touched ({blocked} blocked/N/A)
- [ ] Test specs: {status}
- [ ] Backend escalation scope: {pass/fail}
- [ ] Scope creep: {N} extra files

## Diff sources

- committed: {n} | unstaged: {n} | staged: {n}

## Missing from diff

{list or none}

## Extra in diff (not in plan)

{list or none}
```

Optional write: `.agents/jira-plans/{KEY}-audit.md`

---

## Related skills

| Skill | Path |
|-------|------|
| jira-ticket-plan | `.agents/skills/jira-ticket-plan/SKILL.md` |
| jira-ticket-implement | `.agents/skills/jira-ticket-implement/SKILL.md` |
