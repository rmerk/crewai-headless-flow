# WebAPI investigation sub-agent prompt (template)

Copy into **Task** (`subagent_type: explore`, `readonly: true`). Replace `{PLACEHOLDERS}` before sending.

---

## Context

- **WORKSPACE_ROOT:** asure/
- **WEBAPI:** asure.ptm.webapi/ (read-only — no writes, patches, or commits)
- **Ticket:** {ISSUE_KEY} — {TICKET_SUMMARY}
- **Acceptance criteria:** {AC_BULLETS}
- **Portal UI files/routes (from Step 5.1):** {UI_FILES_AND_ROUTES}
- **Narrowed files (partial index hit — optional):** {NARROWED_FILES}

Parent may have cache or index context — still cite **fresh evidence** at synced branch tip. When `{NARROWED_FILES}` is set, prioritize those paths; do not re-crawl entire WebAPI.

On success, parent writes `.agents/jira-plans/.cache/{routeHash}.md`.

## Required first steps

```bash
cd asure.ptm.webapi
git fetch origin develop
git rev-parse HEAD
git rev-parse --short=7 origin/develop
```

- Read contracts from **`origin/develop` tip** (prefer `git show origin/develop:PTMWebAPI/...` if local HEAD differs).
- If `develop` missing on `origin`, use repo default branch and state which branch you used.
- Record **7-char** SHA (`--short=7`) for parent plan `webapiSha`.

## Investigation targets

- Controllers / routers / minimal APIs for endpoints the UI calls
- Request/response DTOs and property names (**PascalCase** for Kendo filters)
- HTTP verbs, paths, query/body shapes
- Validation and error responses relevant to the ticket

## Hard rules

- **Read-only** under `asure.ptm.webapi/` — do not modify any file
- Do not infer routes or DTOs from memory — cite files at synced branch tip
- Cite evidence as workspace-relative paths: `asure.ptm.webapi/.../File.cs`

## Return format (structured)

Parent parses **`Portal-only viable?`** and **`Backend-preferred?`** lines exactly as shown — use `yes` or `no` only.

### Evidence

- `asure.ptm.webapi/{path}` — `{Symbol}` — {behavior} — branch @ {SHORT_SHA}

### Contract summary

{Routes, payloads, filter field names}

### Portal implication

{What the UI must send, display, or handle}

### Portal-only viable?

yes — {rationale}

OR

no — {rationale}

### Backend-preferred?

yes — {minimal backend change — files/symbols only; human/backend ticket, not agent implementation}

OR

no — {rationale}
