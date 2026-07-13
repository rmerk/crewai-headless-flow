#!/usr/bin/env bash
# Read-only git history check for existing ticket delivery (commits, merged PRs, branches).
# Portal + WebAPI. JSON to stdout; human summary to stderr.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY="${1:-}"

usage() {
  echo "Usage: check-ticket-delivery.sh AS-12345" >&2
  exit 1
}

[[ -n "$KEY" ]] || usage

if [[ ! "$KEY" =~ ^[A-Z][A-Z0-9]+-[0-9]+$ ]]; then
  echo "error: invalid issue key: $KEY" >&2
  exit 1
fi

if [[ "${DELIVERY_FIXTURE:-}" == "1" ]]; then
  SCENARIO="${DELIVERY_FIXTURE_SCENARIO:-none}"
  python3 - <<'PY' "$KEY" "$SCENARIO"
import json, sys
key, scenario = sys.argv[1], sys.argv[2]

def repo(name, status, merged_prs=None, latest_merge=None, branches=None, files=None):
    return {
        "name": name,
        "present": True,
        "fetchOk": True,
        "integrationBranch": "origin/main",
        "status": status,
        "mergedPullRequests": merged_prs or [],
        "featureCommits": [],
        "remoteBranches": branches or [],
        "mergedBranchNames": [],
        "openRemoteBranches": [],
        "latestMergeSha": latest_merge,
        "changedFiles": files or [],
    }

if scenario == "none":
    payload = {
        "issue": key,
        "deliveryStatus": "none",
        "recommendedBranch": key,
        "deliveryCheckedAt": "2026-01-01T00:00:00Z",
        "repos": [
            repo("portal", "none"),
            repo("webapi", "none"),
        ],
    }
elif scenario == "merged":
    payload = {
        "issue": key,
        "deliveryStatus": "merged",
        "recommendedBranch": f"{key}-a",
        "deliveryCheckedAt": "2026-01-01T00:00:00Z",
        "repos": [
            repo(
                "portal",
                "merged",
                merged_prs=[{"sha": "abc1234", "message": f"Merged in {key} (pull request #99)", "prNumber": 99}],
                latest_merge="abc1234",
                branches=[f"origin/{key}"],
                files=["assureptmdashboard/src/Foo.vue"],
            ),
            repo("webapi", "none"),
        ],
    }
elif scenario == "merged-a":
    payload = {
        "issue": key,
        "deliveryStatus": "merged-multiple",
        "recommendedBranch": f"{key}-b",
        "deliveryCheckedAt": "2026-01-01T00:00:00Z",
        "repos": [
            repo(
                "portal",
                "merged-multiple",
                merged_prs=[
                    {"sha": "def5678", "message": f"Merged in {key}-a (pull request #101)", "prNumber": 101},
                    {"sha": "abc1234", "message": f"Merged in {key} (pull request #99)", "prNumber": 99},
                ],
                latest_merge="def5678",
                branches=[f"origin/{key}", f"origin/{key}-a"],
            ),
            repo("webapi", "none"),
        ],
    }
elif scenario == "in-progress":
    portal = repo(
        "portal",
        "in-progress",
        branches=[f"origin/{key}"],
    )
    portal["openRemoteBranches"] = [f"origin/{key}"]
    payload = {
        "issue": key,
        "deliveryStatus": "in-progress",
        "recommendedBranch": key,
        "deliveryCheckedAt": "2026-01-01T00:00:00Z",
        "repos": [
            portal,
            repo("webapi", "none"),
        ],
    }
else:
    print(f"error: unknown DELIVERY_FIXTURE_SCENARIO: {scenario}", file=sys.stderr)
    sys.exit(1)

print(json.dumps(payload, indent=2))
PY
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 required" >&2
  exit 1
fi

resolve_workspace_root() {
  if [[ -n "${WORKSPACE_ROOT:-}" ]]; then
    echo "$WORKSPACE_ROOT"
    return
  fi
  local dir="$SCRIPT_DIR"
  while [[ "$dir" != "/" ]]; do
    if [[ -d "$dir/asure.ptm.portal.web.ui.new" ]] || [[ -d "$dir/.agents/jira-plans" ]]; then
      echo "$dir"
      return
    fi
    dir="$(dirname "$dir")"
  done
  pwd
}

WORKSPACE_ROOT="$(resolve_workspace_root)"
GIT_ROOT="${GIT_ROOT:-$WORKSPACE_ROOT/asure.ptm.portal.web.ui.new}"
WEBAPI_ROOT="${WEBAPI_ROOT:-$WORKSPACE_ROOT/asure.ptm.webapi}"

if [[ ! -d "$GIT_ROOT/.git" ]] && [[ ! -f "$GIT_ROOT/.git" ]]; then
  echo "error: portal git repo not found: $GIT_ROOT" >&2
  exit 1
fi

resolve_integration_branch() {
  local repo_path="$1"
  if git -C "$repo_path" rev-parse --verify origin/develop >/dev/null 2>&1; then
    echo "origin/develop"
  elif git -C "$repo_path" rev-parse --verify origin/main >/dev/null 2>&1; then
    echo "origin/main"
  else
    echo ""
  fi
}

fetch_integration() {
  local repo_path="$1"
  local branch_ref
  branch_ref="$(resolve_integration_branch "$repo_path")"
  if [[ -z "$branch_ref" ]]; then
    return 1
  fi
  local remote="${branch_ref#origin/}"
  git -C "$repo_path" fetch origin "$remote" >/dev/null 2>&1
}

export WORKSPACE_ROOT GIT_ROOT WEBAPI_ROOT KEY
python3 - <<'PY'
import json, os, re, subprocess, sys
from datetime import datetime, timezone

key = os.environ["KEY"]
git_root = os.environ["GIT_ROOT"]
webapi_root = os.environ["WEBAPI_ROOT"]

MERGE_RE = re.compile(
    rf"Merged in {re.escape(key)}(?:-[a-z]|-revert(?:-[a-z])?)?\s*\(pull request #(\d+)\)",
    re.I,
)
FEATURE_BOUNDARY_RE = re.compile(rf"(?<![A-Z0-9-]){re.escape(key)}(?![0-9])", re.I)
BRANCH_SUFFIX_RE = re.compile(
    rf"^(?:origin/)?{re.escape(key)}(?:-[a-z]|-revert(?:-[a-z])?)?$"
)


def run(cmd, cwd=None):
    try:
        return subprocess.check_output(cmd, cwd=cwd, text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return ""


def resolve_integration(repo_path):
    for ref in ("origin/develop", "origin/main"):
        if run(["git", "-C", repo_path, "rev-parse", "--verify", ref]).strip():
            return ref
    return ""


def fetch_repo(repo_path, integration):
    if not integration:
        return False
    remote = integration.replace("origin/", "")
    try:
        subprocess.check_call(
            ["git", "-C", repo_path, "fetch", "origin", remote],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def list_matching_branches(repo_path):
    out = run(["git", "-C", repo_path, "branch", "-r"], cwd=repo_path)
    matches = []
    for line in out.splitlines():
        branch = line.strip().lstrip("*").strip()
        if branch.endswith("/HEAD"):
            continue
        short = branch.split("/", 1)[-1] if "/" in branch else branch
        if BRANCH_SUFFIX_RE.match(branch) or BRANCH_SUFFIX_RE.match(short):
            matches.append(branch if branch.startswith("origin/") else f"origin/{short}")
    return sorted(set(matches))


def merged_branches(repo_path, integration, candidates):
    out = run(["git", "-C", repo_path, "branch", "-r", "--merged", integration])
    merged = set()
    for line in out.splitlines():
        merged.add(line.strip().lstrip("*").strip())
    names = []
    for b in candidates:
        if b in merged:
            names.append(b.replace("origin/", ""))
    return names


def collect_merges(repo_path, integration):
    grep_key = key.replace("-", "\\-")
    log = run(
        [
            "git",
            "-C",
            repo_path,
            "log",
            integration,
            "--grep",
            f"Merged in {key}",
            "-i",
            "--format=%H %s",
        ]
    )
    merges = []
    for line in log.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        m = MERGE_RE.search(subject)
        merges.append(
            {
                "sha": sha[:7],
                "fullSha": sha,
                "message": subject.strip(),
                "prNumber": int(m.group(1)) if m else None,
            }
        )
    return merges


def collect_feature_commits(repo_path, integration):
    log = run(
        [
            "git",
            "-C",
            repo_path,
            "log",
            integration,
            "--grep",
            key,
            "-i",
            "--no-merges",
            "--format=%H %s",
        ]
    )
    commits = []
    for line in log.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        if FEATURE_BOUNDARY_RE.search(subject):
            commits.append({"sha": sha[:7], "message": subject.strip()})
    return commits[:20]


def changed_files(repo_path, integration, merge_sha):
    if not merge_sha:
        return []
    diff = run(
        [
            "git",
            "-C",
            repo_path,
            "diff",
            "--name-only",
            f"{merge_sha}^",
            merge_sha,
        ]
    )
    files = [f.strip() for f in diff.splitlines() if f.strip()]
    return files[:30]


def repo_status(merges, feature_commits, remote_branches, merged_names, open_branches):
    if merges:
        return "merged-multiple" if len(merges) > 1 else "merged"
    if open_branches:
        return "in-progress"
    if feature_commits or remote_branches:
        return "in-progress" if remote_branches else "none"
    return "none"


def recommend_branch(merged_names):
    base = key
    if base not in merged_names:
        return base
    for letter in "abcdefghijklmnopqrstuvwxyz":
        candidate = f"{base}-{letter}"
        if candidate not in merged_names:
            return candidate
    return f"{base}-z"


def analyze_repo(name, repo_path, include_files):
    present = os.path.isdir(repo_path) and (
        os.path.isdir(os.path.join(repo_path, ".git"))
        or os.path.isfile(os.path.join(repo_path, ".git"))
    )
    if not present:
        return {
            "name": name,
            "present": False,
            "fetchOk": False,
            "integrationBranch": "",
            "status": "absent",
            "mergedPullRequests": [],
            "featureCommits": [],
            "remoteBranches": [],
            "mergedBranchNames": [],
            "openRemoteBranches": [],
            "latestMergeSha": None,
            "changedFiles": [],
        }

    integration = resolve_integration(repo_path)
    fetch_ok = fetch_repo(repo_path, integration) if integration else False
    if not integration or not fetch_ok:
        return {
            "name": name,
            "present": True,
            "fetchOk": False,
            "integrationBranch": integration or "N/A",
            "status": "failed",
            "mergedPullRequests": [],
            "featureCommits": [],
            "remoteBranches": [],
            "mergedBranchNames": [],
            "openRemoteBranches": [],
            "latestMergeSha": None,
            "changedFiles": [],
        }

    remote_branches = list_matching_branches(repo_path)
    merged_names = merged_branches(repo_path, integration, remote_branches)
    open_branches = [
        b for b in remote_branches if b.replace("origin/", "") not in merged_names
    ]
    merges = collect_merges(repo_path, integration)
    features = collect_feature_commits(repo_path, integration)
    latest_merge = merges[0]["fullSha"] if merges else None
    files = changed_files(repo_path, integration, latest_merge) if include_files else []
    status = repo_status(merges, features, remote_branches, merged_names, open_branches)

    return {
        "name": name,
        "present": True,
        "fetchOk": True,
        "integrationBranch": integration,
        "status": status,
        "mergedPullRequests": merges,
        "featureCommits": features,
        "remoteBranches": remote_branches,
        "mergedBranchNames": merged_names,
        "openRemoteBranches": open_branches,
        "latestMergeSha": merges[0]["sha"] if merges else None,
        "changedFiles": files,
    }


portal = analyze_repo("portal", git_root, include_files=True)
webapi = analyze_repo("webapi", webapi_root, include_files=False)

repos = [portal, webapi]
portal_failed = portal["present"] and not portal["fetchOk"]

if portal_failed:
    delivery_status = "failed"
    recommended = key
elif not portal["present"]:
    delivery_status = "failed"
    recommended = key
else:
    portal_merged = portal["mergedBranchNames"]
    recommended = recommend_branch(portal_merged)

    if portal["status"] in ("merged", "merged-multiple"):
        delivery_status = portal["status"]
    elif portal["status"] == "in-progress" or webapi["status"] == "in-progress":
        delivery_status = "in-progress"
        if portal["openRemoteBranches"]:
            recommended = portal["openRemoteBranches"][0].replace("origin/", "")
        elif webapi["openRemoteBranches"]:
            recommended = webapi["openRemoteBranches"][0].replace("origin/", "")
    elif webapi["present"] and not webapi["fetchOk"]:
        delivery_status = "partial"
    elif portal["status"] == "none" and webapi["status"] in ("merged", "merged-multiple"):
        delivery_status = webapi["status"]
    else:
        delivery_status = "none"

payload = {
    "issue": key,
    "deliveryStatus": delivery_status,
    "recommendedBranch": recommended,
    "deliveryCheckedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "repos": repos,
}

print(json.dumps(payload, indent=2))

# Human summary on stderr
print(f"Delivery check for {key}: {delivery_status}", file=sys.stderr)
print(f"Recommended branch: {recommended}", file=sys.stderr)
for r in repos:
    if not r["present"]:
        print(f"  {r['name']}: absent", file=sys.stderr)
        continue
    prs = ", ".join(f"#{p['prNumber']}" for p in r["mergedPullRequests"] if p.get("prNumber"))
    print(
        f"  {r['name']}: {r['status']} @ {r['integrationBranch']}"
        + (f" PRs: {prs}" if prs else ""),
        file=sys.stderr,
    )

if portal_failed:
    sys.exit(2)
PY
