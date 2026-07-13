---
name: jira-ticket-plan
description: >-
  Fetch a Jira issue from a ticket link or key in context, resolve the expected git branch
  for the ticket, and produce a Vue/TDD-grounded implementation plan saved locally to
  .agents/jira-plans/ (personal, never committed). For bugs, follow systematic-debugging
  investigation before fix steps. Optional plan-and-ship handoff to jira-ticket-implement.
  Jira is read-only (no comments or status changes). Use when the user passes a Jira URL,
  mentions a ticket key (e.g. AS-12345), or asks to plan or review work from Jira.
---

# Jira Ticket Plan

Turn a Jira ticket into a **reviewed, test-aware implementation plan** for the Asure PTM Portal. **Fetch and validate the ticket before resolving the expected git branch.** Writes plans to **`.agents/jira-plans/{ISSUE_KEY}.md`** — **personal local files only, never committed to git.**

**Plan path constant:** `PLAN_DIR=.agents/jira-plans`

## Workspace layout

Open Cursor at the **asure root** monorepo folder. You work from the workspace root; all plan file paths cite workspace-relative paths (e.g. `asure.ptm.portal.web.ui.new/assureptmdashboard/src/...`).

```text
WORKSPACE_ROOT   = asure/                          # Cursor workspace root
GIT_ROOT         = asure.ptm.portal.web.ui.new/  # from git rev-parse at WORKSPACE_ROOT
PORTAL_UI        = asure.ptm.portal.web.ui.new/assureptmdashboard/
WEBAPI           = asure.ptm.webapi/
MONOREPO_DOCS    = docs/
PLAN_DIR         = .agents/jira-plans/
CACHE_DIR        = .agents/jira-plans/.cache/
EXPORT_DIR       = .agents/jira-plans/exports/
ESCALATIONS_DIR  = .agents/jira-plans/_escalations/
```

**Git:** `asure/.git` is a **gitdir pointer** to the portal repo. Branch operations from `WORKSPACE_ROOT` resolve to `GIT_ROOT` via `git rev-parse --show-toplevel`. Sibling repos (`asure.ptm.webapi/`, e-filing) have their own `.git` — branch operations apply to the portal repo only.

**Caller-owned branch policy:** This skill may determine the expected branch and verify whether the current branch matches it, but it must not create, switch, delete, or rename branches unless the caller explicitly delegates branch management. In the CrewAI ticket-flow pilot, branch management is owned by the orchestrator.

## When to use

- Jira URL or bare key (`AS-12345`) in message or `@` context
- "Plan AS-12345" / "Review this Jira ticket and outline the Vue work"
- "Plan and implement AS-12345" → `mode: plan-and-ship` (handoff after Step 8)
- User pastes ticket body after MCP failure (resume path)

## Non-goals

- Do not implement app features in this skill (use `jira-ticket-implement` for that)
- **Never modify `asure.ptm.webapi/`** — no write, patch, delete, reformat, or commit in that tree (read-only grounding only)
- Do not infer API contracts from memory when backend investigation is required
- Do not invent ticket content if fetch fails and user has not pasted content
- **Do not write to Jira** — no comments, no status transitions, no field updates, **no MCP `createIssue`** (read-only via MCP)
- **Do not commit plan files** — never `git add`, `git commit`, or include plans in PRs (personal workflow)
- Do not commit plan files — use `.agents/jira-plans/` at the monorepo root (gitignored)

---

## Workflow

Execute steps **0 → 8** (and **9** when `plan-and-ship`):

```
- [ ] Step 0: Intake ticket identity + mode
- [ ] Step 1: Fetch Jira issue (or pasted-ticket resume)
- [ ] Step 0.5: Classify ticket complexity (provisional → confirm)
- [ ] Step 2: Existing delivery check (git + PR history)
- [ ] Step 3: Resolve expected git branch for the ticket
- [ ] Step 4: Classify bug vs feature
- [ ] Step 5: Load skills from disk
- [ ] Step 6: Prior art + recon
      - [ ] 6.0 Prior art
      - [ ] 6.1 Portal recon → revise ticketComplexity
      - [ ] 6.2 Backend investigation (skip if trivial-ui)
      - [ ] 6.2b Post-6.2 upgrade if Backend-preferred: yes
- [ ] Step 7: Plan validation
- [ ] Step 8: Write plan + README + export + escalation pointer
- [ ] Step 9: (plan-and-ship only) Hand off to jira-ticket-implement
```

---

### Step 0 — Intake

Extract ticket identity from browse URL, board URL (`selectedIssue=`), or bare key.

**Regex:** key `\b([A-Z][A-Z0-9]+-\d+)\b` · site `https://([^.]+\.atlassian\.net)/`

**Halt:** zero keys → ask for URL or key.

**jiraUrl:** when URL present, set `https://{site}.atlassian.net/browse/{ISSUE_KEY}`; else `N/A` until Step 1 resolves site.

**Provisional complexity (key-only):** when only a bare key is available (no Jira body yet), set in-memory provisional `ticketComplexity` using Step 0.5 keyword lists (default `portal-api` if uncertain). Step 0.5 **must confirm or revise** after Step 1.

**mode:**

| User intent | mode |
|-------------|------|
| "plan and implement", "plan and ship", "plan + implement" | `plan-and-ship` |
| default | `plan-only` |

Multiple keys → use emphasized key or ask.

---

### Step 1 — Fetch Jira issue (Atlassian MCP)

Read MCP schemas before calling. **Read-only tools only:** `getJiraIssue`, `getAccessibleAtlassianResources`. Do not call `transitionJiraIssue` or any comment/update/write tool even if the user asks.

**cloudId:** URL hostname first → fallback `getAccessibleAtlassianResources` (zero → halt; multiple without URL → ask user to pick site or paste URL; one → use it).

On success: `ticketSource: jira-mcp`. If `jiraUrl` was `N/A` and site is known, set `jiraUrl` from site + key.

**Pasted-ticket mode:** user supplies summary + description (+ optional metadata) → `ticketSource: pasted`, skip `getJiraIssue`, continue.

| Failure | Action |
|---------|--------|
| MCP unavailable | Ask paste or connect MCP |
| Auth / not found | Report; ask confirm or paste |
| Empty resources | Halt; paste or connect MCP |

---

### Step 0.5 — Classify ticket complexity

Run **after Step 1** so summary/AC exist. Confirm or revise provisional label from Step 0; revise again after Step 6.1.

Set frontmatter `ticketComplexity`:

| Class | Criteria | Behavior |
|-------|----------|----------|
| `trivial-ui` | CSS/copy/labels only; no DataService/API in ticket **and** 6.1 | Skip 6.2/cache/index; Step 7 trimmed |
| `portal-api` | DataService/API/route/DTO signals | Run 6.2 + full Step 7 |
| `full-stack-escalation` | 6.2b when `Backend-preferred: yes` only | Run 6.2; escalation + Jira stub when grounded |

**Heuristics (document only, not code):**

- API keywords: `endpoint`, `controller`, `DTO`, `WebAPI`, `DataService`, `HTTP`, `filter field`, `server-side`, `API`
- UI keywords: `styling`, `copy`, `label`, `tooltip`, `color`, `font`, `spacing`, `wording`
- **If uncertain → `portal-api`**
- Never set `full-stack-escalation` until 6.2b when `Backend-preferred: yes`

**Required rules:**

- After Step 1: confirm/revise provisional `ticketComplexity`; after 6.1: **must** upgrade `trivial-ui` → `portal-api` if any `DataService`/API caller found.
- **`ticketComplexity: trivial-ui` always wins** — skip 6.2/cache/index even if ticket text mentions API, unless 6.1 finds callers (upgrade to `portal-api` first).
- Do **not** set `full-stack-escalation` until after 6.2 when **Backend investigation** table shows `Backend-preferred: yes`.
- `trivial-ui` still runs 6.1 and full Step 6.0 prior art; only skips 6.2 and trims Step 7.

Record one-line class in plan **Classification** section (note any revision from provisional).

---

### Step 2 — Existing delivery check

Run **after Step 1** succeeds, **before** branch selection. Read-only git — no repo writes.

**Repos:**

| Repo | Path | Integration branch |
|------|------|--------------------|
| Portal | `asure.ptm.portal.web.ui.new/` | `origin/develop` → fallback `origin/main` |
| WebAPI | `asure.ptm.webapi/` | `origin/develop` → fallback `origin/main` |

**Procedure:**

1. From **workspace root**, run:

```bash
.agents/skills/jira-ticket-plan/scripts/check-ticket-delivery.sh {ISSUE_KEY}
```

2. Parse JSON stdout: `deliveryStatus`, `recommendedBranch`, `deliveryCheckedAt`, per-repo `mergedPullRequests`, `featureCommits`, `remoteBranches`, `mergedBranchNames`, `openRemoteBranches`, `changedFiles` (portal).
3. Record in plan **Existing delivery** section and frontmatter (`deliveryStatus`, `deliveryCheckedAt`, `recommendedBranch`).
4. Feed `recommendedBranch` into Step 3.

**Script exit codes:** `0` success (including `none`); `1` invalid key / portal repo missing; `2` portal fetch failed → set `deliveryStatus: failed`, Open question; **do not infer suffix**.

**Delivery status:**

| Status | Meaning | Step 3 implication |
|--------|---------|-------------------|
| `none` | No commits/PRs/branches | Use `{ISSUE_KEY}` branch |
| `in-progress` | Remote branch exists, not merged | Stay on / checkout that branch |
| `merged` | At least one merge to integration | Recommend next free suffix (`-a`, `-b`, …) |
| `merged-multiple` | Multiple merge commits for same key | Note follow-up history; still compute next suffix |
| `partial` | Portal OK, WebAPI absent/failed | Document which repo was checked |
| `failed` | Portal fetch failed | Open question; no suffix guess |

When status is `merged` / `merged-multiple` → lead chat summary with "Work already merged" + PR refs. When re-planning, scope plan toward follow-up/review, not greenfield.

---

### Step 3 — Resolve expected git branch for the ticket

Run only after Steps 1–2 succeed. Run git from **workspace root** (`WORKSPACE_ROOT`).

```bash
cd "$WORKSPACE_ROOT" && git rev-parse --show-toplevel   # expect: asure.ptm.portal.web.ui.new
git rev-parse --abbrev-ref HEAD
```

**Detached HEAD (`HEAD`):** treat as non-matching.

**Target branch:** use Step 2 `recommendedBranch` (not ad-hoc guess).

**Match regex:** `^{ISSUE_KEY}(-revert(-[a-z])?|-[a-z])?$` → stay when current branch equals target; else report the target branch and let the caller decide whether to switch/create it.

**Integration branch:** `develop` → `main` → `origin/HEAD`. On fetch/pull failure → stop; offer local-only after confirm.

| Failure | Action |
|---------|--------|
| Dirty tree | Stop; report status |
| Not a git repo | branch `N/A`, skip |
| Step 2 `failed` | Do not auto-suffix; Open question |

When Step 2 status is `in-progress` → prefer the existing remote branch as the target instead of proposing a duplicate.

---

### Step 4 — Classify

Bug/Defect/Incident → bug path. Story/Task/Epic/Improvement/Sub-task → feature. Unknown → feature (note assumption).

---

### Step 5 — Load skills

**Required** (halt if missing): bug → `systematic-debugging` (`/Users/rchoi/.agents/skills/systematic-debugging/SKILL.md`); feature → `vue-best-practices` (`/Users/rchoi/.agents/skills/vue-best-practices/SKILL.md`); all portal plans → `tdd` (`/Users/rchoi/.agents/skills/tdd/SKILL.md`) + `vue-testing-best-practices` (`/Users/rchoi/.agents/skills/vue-testing-best-practices/SKILL.md`).

PTM workspace: read `AGENTS.md`, `CLAUDE.md`, relevant `.cursor/rules/*.mdc` from workspace root.

---

### Step 6 — Prior art + recon

#### 6.0 Prior art (before codebase search)

1. Build keyword set from ticket summary + component/route names (drop stopwords: `the`, `a`, `an`, `and`, `or`, `to`, `for`, `in`, `on`, `with`, `fix`, `update`, `change`, `add`, `remove`).
2. **If Step 2 found merged delivery:** cross-link **Existing delivery**; reuse portal `changedFiles` to seed 6.1 search.
3. **Primary:** score `.agents/jira-plans/exports/*.json` (`issue`, `routes`, `portalFiles`, `ticketComplexity`).
4. **Fallback:** when exports empty, scan other plan frontmatter + **Backend investigation** route lists only.
5. **Exclude** current `{ISSUE_KEY}`.
6. Score: shared route OR ≥2 keyword hits; cap top 3; else "None found".
7. Note in Review notes (`priorArt`).

#### 6.1 Portal codebase recon

1. Search **`PORTAL_UI`** for routes, components, stores, `DataService` callers.
2. Candidate files with rationale (workspace-relative under `PORTAL_UI`).
3. Extract normalized API routes (see [backend-cache.md](backend-cache.md)).
4. **Revise `ticketComplexity`:** any `DataService`/API caller → upgrade `trivial-ui` to `portal-api`.
5. If **`WEBAPI`** absent → `backendInvestigation: absent`; Open question to clone.
6. Monorepo docs: **`docs/`** and **`PORTAL_UI/docs/`**.
7. **Step 6.2 decision:**
   - **`trivial-ui` after 6.1 revision → skip 6.2** → `backendInvestigation: skipped`
   - **`portal-api` or later `full-stack-escalation` → run 6.2**

#### 6.2 Backend investigation (sub-agent)

**Skip entirely** when final `ticketComplexity: trivial-ui` → `backendInvestigation: skipped`.

**Run** when `portal-api` (or when upgraded from trivial-ui) and WEBAPI present.

If `WEBAPI` absent: `backendInvestigation: absent` (set in 6.1).

##### Investigation cache (before sub-agent)

See [backend-cache.md](backend-cache.md). Skip when `trivial-ui` or `backendInvestigation: absent`.

1. Compute `routeHash` from 6.1 normalized routes.
2. **If `routes` list empty after 6.1 → skip cache; proceed to sub-agent.**
3. `git fetch origin develop` (or fallback). **If fetch fails → do not claim cache hit.**
4. Cache hit when valid YAML + matching `webapiSha` + `syncBranch` (7-char SHA) → `Sub-agent: cached`. **If corrupt → `rm` cache; sub-agent.**
5. **User override:** "refresh API" / "regenerate" → ignore cache hit.
6. **On cache write collision** (same hash, different `routes`): use `{firstRouteSlug}-{routeHash}` filename; note in Review notes.

```bash
mkdir -p .agents/jira-plans/.cache
```

##### WebAPI index lookup (after cache miss)

See [webapi-index.md](webapi-index.md).

- Index missing / invalid JSON → skip; log if invalid
- Index hit + SHA match → merge; `Sub-agent: index`; **`Grounded: partial` only** unless DTO fields in index entry
- **Write cache file** on index hit (same as sub-agent cache; note `source: index`)
- Partial hit → sub-agent with `{NARROWED_FILES}` only
- **Never** use index alone for 6.2b or Jira stub
- Never use index when `sha` ≠ current `--short=7` tip

##### Mandatory sync (before sub-agent)

```bash
cd "$WORKSPACE_ROOT"
cd asure.ptm.webapi
git fetch origin develop
git rev-parse --short=7 origin/develop
```

Fall back to `origin/main` per webapi workflow rule; **state branch used**. Fetch failure → `Grounded: failed`; Open question.

##### Spawn sub-agent (Task tool)

`subagent_type: explore`, `readonly: true`. Fill [webapi-investigate-prompt.md](webapi-investigate-prompt.md) including `{NARROWED_FILES}` when partial index.

Parent merges output; set `backendInvestigation: required` when 6.2 ran. Copy **`Backend-preferred`** from sub-agent into plan table (`yes` | `no`).

On sub-agent success → write/overwrite cache at `.agents/jira-plans/.cache/{routeHash}.md` (apply collision rule if needed).

Capture **`webapiSha`:** `git -C asure.ptm.webapi rev-parse --short=7 {syncBranch}` for frontmatter and investigation table.

##### Backend escalation

When **`Backend-preferred: yes`** in investigation table (from sub-agent structured output):

1. Set `backendEscalation: true`; add **Backend escalation** section
2. Portal-only implementation or **blocked pending backend** steps
3. Jira stub only when **Grounded** is `yes` or `partial`; omit when `failed`
4. If `partial`, prefix stub with "Evidence incomplete — verify before filing."
5. If `Grounded: failed` with backend preferred → omit stub; Open question: "Backend preferred but API not grounded — clone/sync webapi or refresh investigation."

#### 6.2b — Post-6.2 complexity upgrade

**Only when** **Backend investigation** table shows **`Backend-preferred: yes`** (parsed from sub-agent, not free-text guess):

1. Upgrade `ticketComplexity` to `full-stack-escalation`
2. Set `backendEscalation: true`
3. Fill escalation + Jira stub (Grounded guard applies)

Do **not** set `full-stack-escalation` before 6.2 completes.

---

### Step 7 — Plan validation

#### 7.1 Planner-owned post-draft self-critique

After the initial complete draft plan is formulated in memory, but before the final deliverable is written, run this fixed critique loop:

1. Apply `critical-review` from `/Users/rchoi/.agents/skills/critical-review/SKILL.md` to challenge scope, assumptions, implementation detail, and test depth.
2. Apply `edge-case-hunter` from `/Users/rchoi/.agents/skills/edge-case-hunter/SKILL.md` to trace restore/clear paths, lifecycle transitions, route/tab context, and other missed boundary conditions.
3. Revise the draft exactly once to absorb the real findings from both critique passes.
4. Emit the final planner output only after that revise-before-return pass completes.

This self-critique loop is planner-owned and additive. It does not replace the separate Python-owned `plan-reviewer` stage that runs later in the CrewAI workflow.

Use `vue-testing-best-practices` while shaping test strategy, test file selection, Vue-specific assertions, and verification guidance during both the initial draft and the critique-driven revision.

#### 7.2 Class-specific validation

**`trivial-ui`:** apply the planner-owned `critical-review` → `edge-case-hunter` loop above plus `tdd`; skip Context7 unless Vue Router/Pinia/Kendo/Ionic touched.

**`portal-api` / `full-stack-escalation`:** full validation per `.cursor/rules/ptm-portal-plan-validation.mdc`, plus the planner-owned `critical-review` → `edge-case-hunter` loop above and `vue-testing-best-practices` for test-plan grounding.

---

### Step 8 — Deliverable

Fill [output-template.md](output-template.md) → **`.agents/jira-plans/{ISSUE_KEY}.md`**.

Write only the final, post-critique, revised plan to disk. Do not stop after the first draft once the planner has formulated it; the required `critical-review` → `edge-case-hunter` → single revision loop must happen before the saved plan is considered complete.

**Frontmatter:** include `ticketComplexity`, `routes` (JSON list from 6.1), `webapiSha` (`--short=7` or `N/A`), `backendInvestigation`, `backendEscalation`, `deliveryStatus`, `deliveryCheckedAt`, `recommendedBranch`.

**Post-write validation:** if **Grounded: failed**, plan must **not** contain `### Suggested backend Jira`.

#### Escalation inbox pointer

When `backendEscalation: true`:

```bash
mkdir -p .agents/jira-plans/_escalations
echo '.agents/jira-plans/{ISSUE_KEY}.md' > .agents/jira-plans/_escalations/{ISSUE_KEY}.md
```

When `backendEscalation: false` → delete `_escalations/{ISSUE_KEY}.md` if present.

#### Personal README index

Update **`.agents/jira-plans/README.md`**. **Escalation column procedure:**

1. If `_escalations/{KEY}.md` exists, read one-line path; expand `~`; verify target file exists
2. Column value: `yes` (escalation true + valid link) | `no` | `broken link` (pointer or target missing)

#### Export metadata (required)

**Must run** after successful write:

```bash
.agents/skills/jira-ticket-plan/scripts/export-plan-metadata.sh {ISSUE_KEY}
```

Creates `exports/{KEY}.json` for prior art. On non-zero exit → Review notes warning; do not fail plan write. Ensure `exports/README.md` exists (retention note — optional prune >90 days).

#### Chat summary

Lead with backend recommendation when `backendEscalation: true`. When Step 2 found merged delivery, lead with PR refs and recommended follow-up branch. Remind: plans are local-only.

---

### Step 9 — Implement handoff (plan-and-ship only)

Include **Implement handoff** in plan; point to **`jira-ticket-implement`**. Do not implement app code here.

For stale plans at implement time, see **6.2-lite refresh** in [`jira-ticket-implement`](../jira-ticket-implement/SKILL.md).

---

## 6.2-lite refresh (cross-reference)

When implement staleness gate offers refresh: read-only re-run of Step 6.2 cache-miss path (fetch, index/sub-agent). Update plan `webapiSha`, Backend investigation, `routes` frontmatter; re-run export script. User confirms before resuming implement Step 2. No WebAPI edits.

---

## Related skills

| Skill | Path |
|-------|------|
| check-ticket-delivery | `.agents/skills/jira-ticket-plan/scripts/check-ticket-delivery.sh` |
| jira-ticket-implement | `.agents/skills/jira-ticket-implement/SKILL.md` |
| jira-ticket-plan-audit | `.agents/skills/jira-ticket-plan-audit/SKILL.md` |
| systematic-debugging | `/Users/rchoi/.agents/skills/systematic-debugging/SKILL.md` |
| vue-best-practices | `/Users/rchoi/.agents/skills/vue-best-practices/SKILL.md` |
| vue-testing-best-practices | `/Users/rchoi/.agents/skills/vue-testing-best-practices/SKILL.md` |
| critical-review | `/Users/rchoi/.agents/skills/critical-review/SKILL.md` |
| edge-case-hunter | `/Users/rchoi/.agents/skills/edge-case-hunter/SKILL.md` |
| tdd | `/Users/rchoi/.agents/skills/tdd/SKILL.md` |
