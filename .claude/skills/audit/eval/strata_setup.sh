#!/usr/bin/env bash
# strata_setup.sh TREE: make the strata validation clone at $STRATA (a scratch
# directory, never the user's own checkout): clone bearing-research/strata,
# check out the base commit, build its environment, and record it with the
# diffcone source tree TREE as strata's CI does (about 10 minutes). A
# recording is tied to the store format: re-run this after a format change.
# PY picks the Python (default 3.13, the baseline's; uv would pick the newest).
set -euo pipefail
: "${STRATA:?set STRATA to a scratch directory for the strata clone}"
TREE="$(cd "${1:?usage: strata_setup.sh TREE}" && pwd)"
source "$(dirname "$0")/strata_args.sh"
[[ -d $STRATA/.git ]] || git clone --quiet https://github.com/bearing-research/strata "$STRATA"
git -C "$STRATA" checkout -q "$BASE"
(cd "$STRATA" && UV_PROJECT_ENVIRONMENT="$STRATA/.venv" uv sync -q --frozen -p "${PY:-3.13}" --all-extras --group dev)
rm -f "$STRATA"/.diffcone/evidence/*.sqlite
cd "$TREE" && uv run diffcone collect --repo "$STRATA" "${ROOTS[@]}" \
  --command "$STRATA/.venv/bin/python -m pytest" -- "${PYTEST_ARGS[@]}"
ls "$STRATA"/.diffcone/evidence/
