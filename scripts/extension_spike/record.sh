#!/bin/bash
# Step 2: evidence at X on the ordinary build, pandas' CI marker filter.
# The store is moved to $SP/data/store-X.sqlite.
source "$(dirname "$0")/common.sh"
cd "$W"
start=$(date +%s)
uv run diffcone collect --repo "$E" --cache-dir "$SP/cache" \
  --command "$E/.venv/bin/python -m pytest" -- \
  pandas -r fE -n 8 -m "$MARKERS"
status=$?
echo "exit $status seconds $(( $(date +%s) - start ))"
mv "$E"/.diffcone/evidence/"$X"-*.sqlite "$SP/data/store-X.sqlite"
rm -rf "$E/.diffcone"
