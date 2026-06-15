#!/usr/bin/env bash
# Codzienny audyt wczorajszej doby — cron np. 30 0 * * *
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
YDAY="$(date -d yesterday +%F)"
exec uv run python -m planner audit --date "$YDAY"
