#!/bin/bash
# Step 6: the full marker-filtered suite on an ordinary build of commit C
# ("after") and of C with only its compiled files put back to C^ ("before"):
# a test whose outcome differs noticed the compiled change itself, not a
# Python or test file edited in the same commit. In the oracle clone with
# its tracing removed: run it after the oracle.
source "$(dirname "$0")/common.sh"
cd "$O"
export PATH=$O/.venv/bin:$PATH   # meson regenerates with cython from PATH
git update-index --no-assume-unchanged meson.build
git checkout -q -- meson.build
compiled='\.(pyx|pxd|pxi|pxi\.in|c|cc|cpp|h|hpp)$|(^|/)meson\.build$'
for c in "$@"; do
  files=$(git diff-tree --no-commit-id -r --name-only "$c^" "$c" | grep -E "$compiled")
  echo "$c compiled files: $(echo $files)"
  for side in before after; do
    out="$SP/data/outcome-$c-$side.xml"
    [[ -s $out ]] && continue
    git checkout -q --detach "$c"
    if [[ $side == before ]]; then
      git checkout -q "$c^" -- $files
    fi
    start=$(date +%s)
    .venv/bin/python -c "import pandas" > "$SP/logs/build-$c-$side.log" 2>&1
    echo "$c $side: build exit $? in $(( $(date +%s) - start )) s"
    start=$(date +%s)
    .venv/bin/python -m pytest -p no:cacheprovider pandas -q -n 8 -m "$MARKERS" \
      --junitxml="$out.tmp" > "$SP/logs/suite-$c-$side.log" 2>&1
    status=$?
    [[ -s $out.tmp ]] && mv "$out.tmp" "$out"
    echo "$c $side: suite exit $status in $(( $(date +%s) - start )) s: $(tail -1 "$SP/logs/suite-$c-$side.log")"
    git checkout -q "$c" -- .
  done
done
git checkout -q --detach "$X"
