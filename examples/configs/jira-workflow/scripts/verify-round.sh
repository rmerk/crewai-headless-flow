#!/usr/bin/env bash
# Review-round verification for Asure portal (cwd = portal git root).
set -euo pipefail
cd assureptmdashboard
npm run typecheck
npm run test:unit
