#!/usr/bin/env bash
# Verify jira-ticket-plan workflow scripts and SKILL guards.
# Manual follow-ups (no live Jira): trivial-ui skip 5.2, corrupt cache rm, escalation pointer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
SKILL_MD="${SKILL_DIR}/SKILL.md"
FIXTURES="${SCRIPT_DIR}/fixtures"
TMP_PLAN_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_PLAN_DIR"' EXIT

pass=0
fail=0

ok() { echo "PASS: $1"; pass=$((pass + 1)); }
bad() { echo "FAIL: $1"; fail=$((fail + 1)); }

echo "=== bash syntax ==="
for s in "${SCRIPT_DIR}/export-plan-metadata.sh" "${SCRIPT_DIR}/sync-webapi-index.sh" "${SCRIPT_DIR}/check-ticket-delivery.sh" "${SCRIPT_DIR}/verify-jira-workflow.sh"; do
  if bash -n "$s"; then ok "bash -n $(basename "$s")"; else bad "bash -n $(basename "$s")"; fi
done

echo "=== delivery check fixtures ==="
DELIVERY_SCRIPT="${SCRIPT_DIR}/check-ticket-delivery.sh"
if DELIVERY_FIXTURE=1 DELIVERY_FIXTURE_SCENARIO=none "$DELIVERY_SCRIPT" AS-12345 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['issue'] == 'AS-12345'
assert d['deliveryStatus'] == 'none'
assert d['recommendedBranch'] == 'AS-12345'
assert len(d['repos']) == 2
"; then
  ok "delivery fixture none"
else
  bad "delivery fixture none"
fi

if DELIVERY_FIXTURE=1 DELIVERY_FIXTURE_SCENARIO=merged "$DELIVERY_SCRIPT" AS-12345 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['deliveryStatus'] == 'merged'
assert d['recommendedBranch'] == 'AS-12345-a'
"; then
  ok "delivery fixture merged → suffix -a"
else
  bad "delivery fixture merged → suffix -a"
fi

if DELIVERY_FIXTURE=1 DELIVERY_FIXTURE_SCENARIO=merged-a "$DELIVERY_SCRIPT" AS-12345 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['deliveryStatus'] == 'merged-multiple'
assert d['recommendedBranch'] == 'AS-12345-b'
"; then
  ok "delivery fixture merged-a → suffix -b"
else
  bad "delivery fixture merged-a → suffix -b"
fi

if DELIVERY_FIXTURE=1 DELIVERY_FIXTURE_SCENARIO=in-progress "$DELIVERY_SCRIPT" AS-12345 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['deliveryStatus'] == 'in-progress'
assert d['recommendedBranch'] == 'AS-12345'
"; then
  ok "delivery fixture in-progress"
else
  bad "delivery fixture in-progress"
fi

echo "=== export rejects placeholders ==="
cp "${FIXTURES}/placeholder-plan.md" "${TMP_PLAN_DIR}/AS-PLACE.md"
if PLAN_DIR="$TMP_PLAN_DIR" "${SCRIPT_DIR}/export-plan-metadata.sh" AS-PLACE 2>/dev/null; then
  bad "export should reject placeholder frontmatter"
else
  ok "export rejects placeholders"
fi
[[ ! -f "${TMP_PLAN_DIR}/exports/AS-PLACE.json" ]] && ok "no partial JSON on reject" || bad "partial JSON written on reject"

echo "=== export happy path ==="
cp "${FIXTURES}/valid-plan.md" "${TMP_PLAN_DIR}/AS-99999.md"
if PLAN_DIR="$TMP_PLAN_DIR" "${SCRIPT_DIR}/export-plan-metadata.sh" AS-99999; then
  ok "export valid plan"
else
  bad "export valid plan"
fi
if python3 -c "import json; d=json.load(open('${TMP_PLAN_DIR}/exports/AS-99999.json')); assert d['issue']=='AS-99999'; assert 'GET /api/test/{id}' in d['routes']"; then
  ok "export JSON fields"
else
  bad "export JSON fields"
fi

echo "=== route hash vector ==="
expected_hash=$(printf '%s\n' "GET /api/foo" "POST /api/bar" | sort | shasum -a 256 | cut -c1-16)
if [[ ${#expected_hash} -eq 16 ]]; then
  ok "route hash length 16 ($expected_hash)"
else
  bad "route hash"
fi

echo "=== SKILL grep guards ==="
if grep -q "regardless of class" "$SKILL_MD" 2>/dev/null; then
  bad "SKILL still contains 'regardless of class'"
else
  ok "no regardless-of-class contradiction"
fi
if grep -q "\-\-short=7" "$SKILL_MD"; then
  ok "SKILL documents --short=7"
else
  bad "SKILL missing --short=7"
fi
if grep -q "Export metadata (required)" "$SKILL_MD"; then
  ok "export required in SKILL"
else
  bad "export not marked required"
fi
if grep -q "always wins" "$SKILL_MD"; then
  ok "trivial-ui wins rule present"
else
  bad "trivial-ui wins rule missing"
fi
if grep -q "Existing delivery check" "$SKILL_MD"; then
  ok "SKILL documents delivery check step"
else
  bad "SKILL missing delivery check step"
fi

echo "=== index sync (optional) ==="
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "${SKILL_DIR}/../../../Developer/asure" 2>/dev/null && pwd || pwd)}"
if [[ -d "${WORKSPACE_ROOT}/asure.ptm.webapi/.git" ]]; then
  if WORKSPACE_ROOT="$WORKSPACE_ROOT" "${SCRIPT_DIR}/sync-webapi-index.sh" >/dev/null 2>&1; then
    python3 -m json.tool "${HOME}/.agents/cache/webapi-develop/index.json" >/dev/null && ok "index sync + valid JSON" || bad "index JSON invalid"
  else
    bad "index sync failed"
  fi
else
  echo "SKIP: webapi repo not present"
fi

echo "=== summary ==="
echo "Passed: $pass  Failed: $fail"
[[ "$fail" -eq 0 ]]
