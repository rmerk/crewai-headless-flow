# WebAPI contract index (Phase 2)

> **Best-effort v1** — run [`sync-webapi-index.sh`](scripts/sync-webapi-index.sh) after `git pull` in `asure.ptm.webapi`. Do not treat index as authoritative for DTO shapes or escalation decisions without sub-agent confirmation.

Living read-only index of `asure.ptm.webapi` routes at `origin/develop` tip. **Personal tooling — never committed.**

**Index path:** `.agents/cache/webapi-develop/index.json`

**Sync script:** [scripts/sync-webapi-index.sh](scripts/sync-webapi-index.sh)

---

## When to query

In Step 5.2, **after cache miss** and before spawning sub-agent:

1. Read `index.json` if present.
2. Look up routes from Step 5.1 normalized route list.
3. If hit **and** index `sha` matches current tip (`rev-parse --short=7`):
   - Merge index evidence; set `Sub-agent: index`
   - Set `Grounded: partial` **only** (never `yes`) unless index entry includes DTO/filter field names
   - **Write cache file** at `.agents/jira-plans/.cache/{routeHash}.md` (same format as sub-agent cache; note `source: index` in body)
4. If **partial** hit (route known, DTO/controller unknown) → sub-agent with `{NARROWED_FILES}` only.
5. If miss or SHA mismatch → sub-agent as usual.

**Never** use index when its `sha` ≠ plan `webapiSha` or current tip.

**Never** use index alone for 5.2b `backend-preferred`, Backend escalation, or Jira stub — sub-agent required when escalation is possible.

---

## Index JSON shape (v1)

```json
{
  "sha": "abc1234",
  "syncBranch": "origin/develop",
  "generated": "2026-05-20T12:00:00Z",
  "routes": {
    "GET /api/clients/{id}": {
      "controller": "ClientsController",
      "file": "PTMWebAPI/Controllers/ClientsController.cs",
      "methods": ["GetClient"]
    }
  }
}
```

SHA is always **7 characters** (`git rev-parse --short=7`). Fields are best-effort from `git show` scan — no .NET build required.

---

## Guards

| Condition | Action |
|-----------|--------|
| `index.json` missing | Skip index step; cache → sub-agent only |
| `index.json` invalid JSON | Log in Review notes (`index: invalid JSON`); skip index |
| Index `sha` ≠ current tip | Skip index; treat as miss |
| `WEBAPI` absent when running sync script | Script exits 1; no write |
| Partial index hit | Sub-agent with narrowed scope only |
| Index hit without DTO fields | `Grounded: partial` max; spawn sub-agent if escalation possible |

---

## Refresh index

From workspace root (asure monorepo):

```bash
.agents/skills/jira-ticket-plan/scripts/sync-webapi-index.sh
```

Run manually when WebAPI changes significantly or before heavy planning sessions.
