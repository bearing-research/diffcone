#!/bin/bash
# Step 5b: the line-traced oracle over the marker-filtered suite at X.
source "$(dirname "$0")/common.sh"
cd "$O"
rm -rf "$SP/data/oracle-raw"
start=$(date +%s)
EXTSPIKE_OUT="$SP/data/oracle-raw" PYTHONPATH="$W/scripts/extension_spike:$W/src" \
  .venv/bin/python -m pytest -p extspike_oracle -p no:cacheprovider pandas -r fE -n 8 \
  -m "$MARKERS" -q --junitxml="$SP/data/oracle-junit.xml"
echo "exit $? seconds $(( $(date +%s) - start ))"
