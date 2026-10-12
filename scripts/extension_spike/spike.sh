#!/bin/bash
# extension_spike.py with the spike's paths: spike.sh SUBCOMMAND [extra args].
source "$(dirname "$0")/common.sh"
cd "$W"
cmd=$1
shift
py=(uv run python scripts/extension_spike.py --data "$SP/data")
start=$(date +%s)
case $cmd in
  buildmap) "${py[@]}" buildmap --repo "$E" --rev "$X" "$@" ;;
  surface) "${py[@]}" surface --repo "$E" --x "$X" --python "$E/.venv/bin/python" "$@" ;;
  oracle) "${py[@]}" oracle --raw "$SP/data/oracle-raw" "$@" ;;
  precision|recall|outcomes|special|misses|variants)
    PYTHONUNBUFFERED=1 "${py[@]}" "$cmd" --repo "$E" --x "$X" \
      --store "$SP/data/store-X.sqlite" --cache "$SP/cache" "$@" ;;
  *) echo "unknown: $cmd" >&2; exit 2 ;;
esac
status=$?
echo "$cmd exit $status seconds $(( $(date +%s) - start ))"
exit $status
