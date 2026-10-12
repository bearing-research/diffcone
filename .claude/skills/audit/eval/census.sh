#!/bin/bash
# census.sh TREE LABEL: the plan-only census (scripts/census.py) run from the
# diffcone source tree TREE over the clones in $E/census-work (made on the
# first run, reused after, so every version plans the same commits). Writes
# $E/census-LABEL.json; compare versions with census_compare.py.
: "${E:?set E to the evaluation work directory}"
T=${1:?usage: census.sh TREE LABEL}; label=${2:?usage: census.sh TREE LABEL}
cd "$T" || exit 9
uv run python scripts/census.py run --work "$E/census-work" -o "$E/census-$label.json" --jobs 4 \
  > "$E/census-$label.log" 2>&1
echo "$label exit=$?" | tee -a "$E/census-$label.log"
