#!/usr/bin/env bash
# Sync WebAPI route index from origin/develop tip (read-only, no .NET build).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_agents_root="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "${_agents_root}/.." && pwd 2>/dev/null || pwd)}"
WEBAPI="${WEBAPI:-$WORKSPACE_ROOT/asure.ptm.webapi}"
INDEX_DIR="${INDEX_DIR:-${_agents_root}/cache/webapi-develop}"
INDEX_FILE="${INDEX_DIR}/index.json"
SYNC_BRANCH="${SYNC_BRANCH:-origin/develop}"
API_ROOT="PTMWebAPI"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 required" >&2
  exit 1
fi

if [[ ! -d "$WEBAPI/.git" ]]; then
  echo "error: WEBAPI repo not found at $WEBAPI" >&2
  echo "Clone asure.ptm.webapi as a sibling of the portal repo." >&2
  exit 1
fi

cd "$WEBAPI"
git fetch origin develop 2>/dev/null || git fetch origin main 2>/dev/null || {
  echo "error: git fetch failed in $WEBAPI" >&2
  exit 1
}

if git rev-parse --verify "$SYNC_BRANCH" >/dev/null 2>&1; then
  :
elif git rev-parse --verify origin/main >/dev/null 2>&1; then
  SYNC_BRANCH="origin/main"
else
  echo "error: no origin/develop or origin/main ref" >&2
  exit 1
fi

SHA=$(git rev-parse --short=7 "$SYNC_BRANCH")
GENERATED=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

mkdir -p "$INDEX_DIR"

TMP_ROUTES=$(mktemp)
TMP_JSON=$(mktemp)
trap 'rm -f "$TMP_ROUTES" "$TMP_JSON"' EXIT

git ls-tree -r --name-only "$SYNC_BRANCH" -- "$API_ROOT" 2>/dev/null \
  | grep -E 'Controller\.cs$' || true > "$TMP_ROUTES"

if [[ -s "$TMP_ROUTES" ]]; then
  python3 - <<'PY' "$SYNC_BRANCH" "$TMP_ROUTES" "$TMP_JSON"
import json, re, subprocess, sys
from pathlib import Path

sync_branch, list_file, out_file = sys.argv[1], sys.argv[2], sys.argv[3]
routes = {}

http_attr_re = re.compile(
    r'\[Http(Get|Post|Put|Delete|Patch)(?:\("([^"]*)"\))?\]', re.I
)
route_re = re.compile(r'\[Route\("([^"]*)"\)\]', re.I)
class_re = re.compile(r'class\s+(\w+)')
method_re = re.compile(
    r'(?:(?:public|private|protected|internal)\s+[\w<>,\[\]\.?]+\s+)?(\w+)\s*\([^)]*\)\s*\{',
    re.M,
)

def join_routes(base, segment):
    base = (base or "").strip("/")
    segment = (segment or "").strip("/")
    if base and segment:
        full = f"/{base}/{segment}"
    elif base:
        full = f"/{base}"
    elif segment:
        full = f"/{segment}"
    else:
        full = "/"
    return re.sub(r"/+", "/", full)

files = [ln.strip() for ln in Path(list_file).read_text().splitlines() if ln.strip()]

for path in files:
    try:
        content = subprocess.check_output(
            ["git", "show", f"{sync_branch}:{path}"], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        continue

    ctrl_match = class_re.search(content)
    ctrl_name = ctrl_match.group(1) if ctrl_match else path.split("/")[-1]

    base_route = ""
    rm = route_re.search(content)
    if rm:
        base_route = rm.group(1)

    # Find method names near Http* attributes
    for hm in http_attr_re.finditer(content):
        verb = hm.group(1).upper()
        method_route = hm.group(2) or ""
        full = join_routes(base_route, method_route)
        if full == "/" and not base_route and not method_route:
            slug = ctrl_name.replace("Controller", "").lower()
            full = f"/api/{slug}"
        key = f"{verb} {full}"

        # Method name: search backwards from attribute for method signature
        pos = hm.start()
        preceding = content[max(0, pos - 400):pos]
        method_names = method_re.findall(preceding)
        action = method_names[-1] if method_names else ctrl_name.replace("Controller", "")

        entry = routes.setdefault(key, {
            "controller": ctrl_name,
            "file": path,
            "methods": [],
        })
        if action not in entry["methods"]:
            entry["methods"].append(action)

Path(out_file).write_text(json.dumps(routes, indent=2), encoding="utf-8")
PY
  if [[ $? -ne 0 ]]; then
    echo "error: index extraction failed" >&2
    exit 1
  fi
else
  echo "{}" > "$TMP_JSON"
fi

python3 - <<'PY' "$SHA" "$SYNC_BRANCH" "$GENERATED" "$TMP_JSON" "$INDEX_FILE"
import json, sys
from pathlib import Path

sha, branch, generated, routes_path, out_path = sys.argv[1:6]
routes_raw = Path(routes_path).read_text(encoding="utf-8").strip() or "{}"
routes = json.loads(routes_raw)
doc = {
    "sha": sha,
    "syncBranch": branch,
    "generated": generated,
    "routes": routes,
}
text = json.dumps(doc, indent=2) + "\n"
Path(out_path).write_text(text, encoding="utf-8")
PY

python3 -m json.tool "$INDEX_FILE" > /dev/null

ROUTE_COUNT=$(python3 -c "import json; print(len(json.load(open('$INDEX_FILE'))['routes']))")
echo "Wrote $INDEX_FILE (sha=$SHA, routes=$ROUTE_COUNT)"
