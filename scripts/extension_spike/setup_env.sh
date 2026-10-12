#!/bin/bash
# Step 0: the ordinary pandas environment at X (fixenv.sh's recipe, E=$SP).
set -euo pipefail
source "$(dirname "$0")/common.sh"
start=$(date +%s)
clone_at_x "$E"
E=$SP bash "$W/.claude/skills/audit/eval/fixenv.sh" pandas || echo "fixenv exit $?"
echo "env done in $(( $(date +%s) - start )) s"
du -sh "$E/.venv" "$E/build"
