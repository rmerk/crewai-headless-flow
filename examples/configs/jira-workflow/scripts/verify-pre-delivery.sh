#!/usr/bin/env bash
# Pre-delivery verification for Asure portal (cwd = portal git root).
set -euo pipefail
cd assureptmdashboard
npm run test:ci
