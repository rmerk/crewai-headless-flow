# Backend investigation cache

Personal route-scoped cache for Step 5.2 backend investigations. **Never commit** cache files.

**Cache path:** `.agents/jira-plans/.cache/{routeHash}.md`

**Lookup order:** route cache (this doc) → [webapi-index.md](webapi-index.md) index (Phase 2) → sub-agent. Never use index when its `sha` ≠ current tip.

---

## Normalization rules

Extract route signatures from Step 5.1 UI `DataService` calls or sub-agent contract summary.

1. **Method:** uppercase HTTP verb (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`). Default `GET` when method is implicit in portal code.
2. **Path:** API path template with `{param}` placeholders (not concrete IDs). Strip query strings. Collapse repeated slashes. No trailing slash except root `/`.
3. **Prefix:** include leading `/` (e.g. `/api/clients/{id}`).
4. **Sort:** alphabetically by normalized `METHOD path` line before hashing.
5. **Dedupe:** identical normalized lines appear once.

Example normalized routes:

```text
GET /api/clients/{id}
POST /api/processes/{processId}
```

---

## `routeHash` algorithm

1. Build sorted list of normalized `METHOD path` lines (one per line, `\n`-joined).
2. Compute SHA-256 hex digest of that string (UTF-8).
3. Use first 16 hex chars as `{routeHash}` filename stem.

```bash
# Example (requires sorted input)
printf '%s\n' "GET /api/foo" "POST /api/bar" | sort | sha256sum | cut -c1-16
```

---

## Collision handling

If two distinct route sets produce the same `{routeHash}` (extremely unlikely):

1. Detect collision when writing: existing cache file has different `routes` list for same hash.
2. Append a prefix derived from first route: `{firstRouteSlug}-{routeHash}` where slug = alphanumeric from first route (max 32 chars).
3. Document collision in Review notes (`backendInvestigation: cache collision resolved`).

---

## Cache file format

```yaml
---
webapiSha: abc1234
syncBranch: origin/develop
routes: ["GET /api/...", "POST /api/..."]
generated: 2026-05-20T12:00:00Z
---

# Backend investigation cache

{evidence body — same content merged into plan Backend investigation section}
```

| Field | Required | Notes |
|-------|----------|-------|
| `webapiSha` | yes | 7-char SHA from `git rev-parse --short=7 origin/develop` (or fallback branch) |
| `syncBranch` | yes | e.g. `origin/develop` — must match branch used for SHA |
| `routes` | yes | Normalized route list (may be empty — see guards) |
| `generated` | yes | ISO-8601 timestamp |

---

## Valid / invalid cache

**Valid cache hit** when ALL of:

- File exists at `.agents/jira-plans/.cache/{routeHash}.md`
- YAML frontmatter parses cleanly
- `webapiSha` and `syncBranch` match current tip after successful `git fetch`
- User has not requested refresh (see override below)

**Invalid cache** — delete file and sub-agent:

- Frontmatter missing or unparseable
- `webapiSha` or `syncBranch` mismatch after successful fetch
- Body empty or truncated (< 50 chars after frontmatter)

On cache hit: set **Backend investigation** `Sub-agent: cached` and copy evidence body from cache file.

---

## Fetch-fail behavior

If `git fetch origin develop` (or fallback branch) **fails**:

- Do **not** claim cache hit based on SHA comparison
- Proceed to sub-agent or Open question per existing 5.2 rules
- If using stale cache without SHA match attempt, document `Grounded: partial` in plan

---

## Zero routes

If `routes` list is **empty** after Step 5.1 normalization:

- Skip entire cache lookup block
- Proceed directly to sub-agent (or Open question if API required but routes unresolved)

---

## User refresh override

When ticket or user says **"refresh API"**, **"regenerate investigation"**, or similar:

- Ignore cache hit even if SHA matches
- Run sub-agent and overwrite cache file

---

## First write

```bash
mkdir -p .agents/jira-plans/.cache
```

Never `git add` cache files.
