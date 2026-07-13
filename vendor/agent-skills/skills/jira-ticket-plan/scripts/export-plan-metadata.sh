#!/usr/bin/env bash
# Export redacted plan metadata JSON (no ticket body). Personal tooling only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLAN_DIR="${PLAN_DIR:-$("${SCRIPT_DIR}/resolve-agents-root.sh")}"
EXPORT_DIR="${PLAN_DIR}/exports"
KEY="${1:-}"

usage() {
  echo "Usage: export-plan-metadata.sh AS-12345" >&2
  exit 1
}

[[ -n "$KEY" ]] || usage

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 required" >&2
  exit 1
fi

PLAN_FILE="${PLAN_DIR}/${KEY}.md"
if [[ ! -f "$PLAN_FILE" ]]; then
  echo "error: plan file not found: $PLAN_FILE" >&2
  exit 1
fi

mkdir -p "$EXPORT_DIR"
OUT="${EXPORT_DIR}/${KEY}.json"

python3 - <<'PY' "$PLAN_FILE" "$OUT"
import json, re, sys
from pathlib import Path

plan_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
text = plan_path.read_text(encoding="utf-8")

PLACEHOLDER_RE = re.compile(r"\{[A-Z_0-9-]+|\{ISO-8601")

if not text.startswith("---"):
    print("error: invalid plan frontmatter", file=sys.stderr)
    sys.exit(1)

parts = text.split("---", 2)
if len(parts) < 3:
    print("error: invalid plan frontmatter", file=sys.stderr)
    sys.exit(1)

fm_block = parts[1]
if PLACEHOLDER_RE.search(fm_block):
    print("error: plan frontmatter contains unresolved placeholders", file=sys.stderr)
    sys.exit(1)

fm = {}
for line in fm_block.strip().splitlines():
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        continue
    if ":" not in stripped:
        continue
    k, _, v = stripped.partition(":")
    fm[k.strip()] = v.strip()

def parse_bool(val, default=False):
    if not val:
        return default
    return val.lower() in ("true", "yes", "1")

def parse_routes_frontmatter(val):
    if not val or val == "[]":
        return []
    # YAML-ish list: ["GET /a", "POST /b"] or single line
    if val.startswith("["):
        try:
            parsed = json.loads(val.replace("'", '"'))
            if isinstance(parsed, list):
                return [str(r) for r in parsed]
        except json.JSONDecodeError:
            pass
    return []

def parse_routes_from_contract_summary(body):
    routes = []
    section = re.search(
        r"## Backend investigation\b.*?\*\*Contract summary:\*\*\s*(.+?)(?:\n\*\*|\n## |\Z)",
        body,
        re.S,
    )
    if not section:
        return routes
    chunk = section.group(1)
    for rm in re.finditer(r"(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s,\]`\]]+)", chunk):
        routes.append(f"{rm.group(1)} {rm.group(2)}")
    return list(dict.fromkeys(routes))

body = parts[2]
routes = parse_routes_frontmatter(fm.get("routes", ""))
if not routes:
    routes = parse_routes_from_contract_summary(body)

portal_files = []
for m in re.finditer(
    r"`(asure\.ptm\.portal\.web\.ui\.new/assureptmdashboard/[^`]+)`", body
):
    portal_files.append(m.group(1))
portal_files = list(dict.fromkeys(portal_files))

issue = fm.get("issue", plan_path.stem)
if PLACEHOLDER_RE.search(issue):
    print("error: unresolved issue key in frontmatter", file=sys.stderr)
    sys.exit(1)

meta = {
    "issue": issue,
    "generated": fm.get("generated", ""),
    "ticketComplexity": fm.get("ticketComplexity", "portal-api"),
    "backendEscalation": parse_bool(fm.get("backendEscalation", "false")),
    "webapiSha": fm.get("webapiSha", "N/A"),
    "routes": routes,
    "portalFiles": portal_files,
}

out_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {out_path}")
PY
