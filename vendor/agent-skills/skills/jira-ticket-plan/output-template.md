---
issue: {ISSUE_KEY}
generated: {ISO-8601 date}
branch: {branch name or N/A}
jiraUrl: {https://site.atlassian.net/browse/ISSUE_KEY or N/A}
ticketSource: jira-mcp | pasted
mode: plan-only | plan-and-ship
path: .agents/jira-plans/{ISSUE_KEY}.md
personal: true
workspaceRoot: asure
ticketComplexity: trivial-ui | portal-api | full-stack-escalation
backendInvestigation: required | skipped | absent
backendEscalation: true | false
webapiSha: {short SHA of origin/develop when investigation required, else N/A}
routes: []
deliveryStatus: none | in-progress | merged | merged-multiple | partial | failed
deliveryCheckedAt: {ISO-8601 date}
recommendedBranch: {branch name from Step 2}
---

# Jira Ticket Plan: {ISSUE_KEY}

## Ticket summary

| Field | Value |
|-------|-------|
| **Key** | {ISSUE_KEY} |
| **Type** | {issuetype or Unknown if pasted without type} |
| **Status** | {status or N/A if pasted} |
| **Priority** | {priority or N/A if pasted} |
| **Summary** | {summary} |
| **Source** | jira-mcp \| pasted |
| **Jira** | {jiraUrl or N/A} |

**User-facing problem:** {one paragraph from description / AC}

**Acceptance criteria / requirements:** {bulleted list from ticket, or "Not specified in ticket"}

---

## Git branch

| Field | Value |
|-------|-------|
| **Branch** | `{branch name or N/A}` |
| **Recommended** | `{recommendedBranch from Step 2}` |
| **Delivery basis** | {Step 2 deliveryStatus — e.g. merged-multiple → AS-12345-a} |
| **Action** | used current branch \| checked out existing \| created new branch \| skipped (not a git repo) \| skipped (fetch failed) \| skipped (user declined local-only branch) |
| **Base** | {integration branch used for create, e.g. `develop` — omit if stayed on current or branch skipped} |
| **Committed** | no — personal local plan only |

> When branch step was skipped, set **Branch** to `N/A`, omit **Base**, and record the skip reason in **Action**.

---

## Existing delivery

> From Step 2 `check-ticket-delivery.sh`. Omit section only when Step 2 was skipped (not a git repo).

| Repo | Status | Merged PRs | Latest merge | Open branches |
|------|--------|------------|--------------|---------------|
| Portal | {none \| in-progress \| merged \| merged-multiple \| failed} | {#99, #101 or —} | {short SHA or —} | {origin/AS-12345 or —} |
| WebAPI | {status or absent} | {# or —} | {short SHA or —} | {branches or —} |

**Recommendation:** Use branch `{recommendedBranch}` ({one-line rationale — e.g. AS-12345 already merged to main}).

**Files touched (portal, latest merge):** {bulleted paths from Step 2 changedFiles, or "None" / "N/A"}

---

## Prior art

> Related plans in `.agents/jira-plans/`. Omit if none found.

| Plan | Generated | Relevance |
|------|-----------|-----------|
| [{OTHER_KEY}]({OTHER_KEY}.md) | {date from frontmatter} | {shared components, keywords, or area} |

**Takeaways:** {what to reuse or avoid from prior plans — file paths, test patterns, dead ends}

---

## Classification

- **Path:** Bug | Feature
- **Complexity:** trivial-ui | portal-api | full-stack-escalation
- **Rationale:** {why this path and complexity were chosen based on issuetype, ticket content, and 5.1 recon; note if unknown issuetype defaulted to Feature; note any upgrade from provisional class}

---

## Investigation plan

> **Bugs only.** Omit this section for features. Do not propose code fixes here — investigation only (systematic-debugging Phase 1).

### Reproduction

- [ ] {step 1}
- [ ] {step 2}
- **Expected vs actual:** {from ticket}
- **Reproducibility:** {every time / intermittent / unknown}

### Evidence to collect

- [ ] {logs, network traces, stack traces, screenshots, etc.}
- [ ] {git history / recent commits in suspected files}
- [ ] {API request/response at component boundaries}

### Suspected areas

| Area | Rationale |
|------|-----------|
| {file or component} | {why it might be involved} |

### Root-cause hypotheses (pre-investigation)

1. {hypothesis} — evidence needed: {what would confirm or rule out}

---

## Component map

> **Features only.** Omit for trivial one-file changes. From vue-best-practices §1.2.

| Component / composable | Responsibility | Props / emits |
|------------------------|----------------|---------------|
| {name} | {one sentence} | {contract summary} |

---

## Implementation plan

Ordered steps with file ownership under `asure.ptm.portal.web.ui.new/assureptmdashboard/` (workspace-relative from asure root):

1. **{Step title}** — `asure.ptm.portal.web.ui.new/assureptmdashboard/{path/to/file}`
   - {what to change and why}
2. **{Step title}** — `asure.ptm.portal.web.ui.new/assureptmdashboard/{path/to/file}`
   - {what to change and why}

**Backend contract notes (read-only):** {short pointer — see **Backend investigation** when `backendInvestigation: required`; if absent write "Not grounded — clone asure.ptm.webapi"; if skipped note why; no WebAPI edits}

**Repo conventions to follow:**

- `DataService` for API calls
- Kendo filter fields in **PascalCase**
- PtmModal with **`v-if`** pattern
- Composition API + `<script setup lang="ts">`

---

## Backend investigation

> Omit entire section if `backendInvestigation: skipped` or `absent`.

| Field | Value |
|-------|-------|
| **Synced branch** | origin/develop @ {short SHA or N/A} |
| **Sub-agent** | explore readonly \| cached \| index |
| **Grounded** | yes \| partial \| failed |
| **Backend-preferred** | yes \| no |

**Evidence (read-only):**

- `asure.ptm.webapi/{path/to/File.cs}` — `{symbol}` — {behavior}

**Contract summary:** {routes, DTOs, filters}

**Portal implication:** {what UI must send/display}

---

## Backend escalation

> Include only when `backendEscalation: true`. Omit section otherwise.

**Recommendation:** Backend change is the preferred/easiest fix.

**Suggested backend work:** {concrete change — files/symbols; no edits from this workspace}

**Portal workaround (if any):** {optional UI-only path and tradeoffs}

**User action required:** Track backend work separately; confirm before planning WebAPI edits in this session.

> **Omit entire `### Suggested backend Jira` subsection when Grounded is `failed`.** Step 7 post-write validation: plan file must not contain that heading when Grounded is `failed`.

### Suggested backend Jira (copy-paste — do not auto-create)

> Include only when **Grounded** is `yes` or `partial`. Omit when `failed`. If `partial`, prefix description with "Evidence incomplete — verify before filing."

**Title:** {ISSUE_KEY}-backend: {one-line fix}

**Description:**
- Problem: {from investigation}
- Suggested change: {files/symbols from investigation}
- Portal ticket: {ISSUE_KEY} — workaround: {portal-only path if any}

---

## Test strategy

Behaviors to verify (TDD vertical slices — one behavior per RED→GREEN cycle):

| # | Behavior | Spec file | Notes |
|---|----------|-----------|-------|
| 1 | {user-observable behavior} | `asure.ptm.portal.web.ui.new/assureptmdashboard/tests/unit/{Name}.spec.ts` | {tracer bullet / follow-up} |
| 2 | {behavior} | `asure.ptm.portal.web.ui.new/assureptmdashboard/tests/unit/{Name}.spec.ts` | |

**Implementation-time skill reads:**

- `.cursor/rules/vue-agents-skills.mdc` for `.vue` edits
- `.cursor/rules/ptm-portal-vue-test-vitest-skills.mdc` for specs (`vite-plus/test`, `@pinia/testing`)

**Verification:** from workspace root:

```bash
cd asure.ptm.portal.web.ui.new/assureptmdashboard
npm run typecheck
npm run test:unit -- tests/unit/{spec}.spec.ts
```

Implementer: run typecheck + listed specs after **each** vertical slice; fix source first on failure—update specs only for intentional behavior changes.

---

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| {risk from edge-case-hunter or ticket ambiguity} | {mitigation} |

---

## Review notes

- **critical-review:** {key findings integrated or dismissed with rationale}
- **edge-case-hunter:** {unhandled boundaries addressed in plan}
- **tdd:** {how vertical slices cover critical behaviors}
- **grill-me:** {resolved decision branches and recommended defaults}
- **Context7:** {libraries consulted} OR fallback to {project docs used}
- **ticketSource:** {jira-mcp or pasted — if pasted, note what fields user supplied}
- **priorArt:** {plans consulted or "none"}
- **deliveryCheck:** {deliveryStatus and PR refs from Step 2, or "skipped"}
- **backendInvestigation:** {required \| skipped \| absent — if skipped, one-line rationale}
- **ticketComplexity:** {final class and any upgrades from provisional}

---

## Open questions

> Blocking only. Omit section if none remain after in-repo exploration.

1. {question} — {why it blocks}

---

## Implement handoff

> **plan-and-ship mode only.** Omit for plan-only runs.

After this plan is saved, load [`jira-ticket-implement`](.agents/skills/jira-ticket-implement/SKILL.md) with `{ISSUE_KEY}` and implement per Implementation plan + Test strategy above.

After implementation, optionally run [`jira-ticket-plan-audit`](.agents/skills/jira-ticket-plan-audit/SKILL.md) before opening a PR.
