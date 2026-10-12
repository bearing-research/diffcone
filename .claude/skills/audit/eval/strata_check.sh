#!/usr/bin/env bash
# strata_check.sh TREE NAME [RECORDING]: plan strata BASE -> HEAD (strata_args.sh)
# from its recording with the diffcone source tree TREE; writes
# $STRATA/../strata-NAME.json and prints selected/total and the selection by
# rule. Compare two trees (before and after a change) by name; explain every
# difference by rule. RECORDING defaults to the one strata_setup.sh made.
set -u
: "${STRATA:?set STRATA to the strata validation clone (strata_setup.sh)}"
TREE="$(cd "${1:?usage: strata_check.sh TREE NAME [RECORDING]}" && pwd)"
NAME="${2:?usage: strata_check.sh TREE NAME [RECORDING]}"
source "$(dirname "$0")/strata_args.sh"
REC="${3:-$(ls "$STRATA"/.diffcone/evidence/${BASE}-*.sqlite 2>/dev/null | head -1)}"
[ -f "$REC" ] || { echo "no recording at $BASE (run strata_setup.sh first)" >&2; exit 2; }
OUT="$(dirname "$STRATA")/strata-$NAME.json"
cd "$TREE" && uv run diffcone plan --repo "$STRATA" --base "$BASE" --head "$HEAD" \
  --discover pytest "${ROOTS[@]}" --command "$STRATA/.venv/bin/python -m pytest" \
  --evidence "$REC" --format json -o "$OUT" -- "${PYTEST_ARGS[@]}"
rc=$?
echo "plan exit $rc (0 complete, 1 degraded, 2 no plan, 3 discovery may be short)"
[ -f "$OUT" ] || exit "$rc"
python3 -I - "$OUT" "$REC" <<'PY'
import json, sys
from collections import Counter
plan = json.load(open(sys.argv[1]))
a = plan["analysis"]
c = a["counts"]
print(f"recording: {sys.argv[2]}")
print(f"evidence: {'yes' if a.get('evidence') else 'NO (static plan)'}")
print(f"selected {c['selected']} of {c['targets']} (always_run {c['always_run']})")
print(f"fallbacks: {len(plan['fallback_decisions'])}, analysis errors: {len(plan['analysis_errors'])}")
sel = plan["selected_targets"]
print("by rule (targets carrying the rule; a target may carry several):")
for rule, n in Counter(r for t in sel for r in t["rules"]).most_common():
    print(f"  {n:6d}  {rule}")
PY
exit "$rc"
