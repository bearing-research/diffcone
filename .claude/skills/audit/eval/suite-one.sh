#!/bin/bash
# suite-one.sh NAME: run NAME's whole suite once (15 min cap) to tell "runs"
# from "collects only"; the summary goes to $E/logs/suite-NAME.txt.
: "${E:?set E to the evaluation work directory}"
n=$1; R=$E/repos/$n
extra=(); [[ $n == pre-commit ]] && extra=(--ignore=tests/languages)
mkdir -p "$E/logs"
cd "$R" || exit 9
start=$(date +%s)
PATH="$R/.venv/bin:$PATH" timeout 900 .venv/bin/python -m pytest -q ${extra[@]+"${extra[@]}"} \
  -W ignore::DeprecationWarning > "$E/logs/suite-$n.txt" 2>&1
s=$?
echo "exit $s secs $(( $(date +%s) - start ))" >> "$E/logs/suite-$n.txt"
echo "$n exit $s: $(grep -E '(passed|failed|error)' "$E/logs/suite-$n.txt" | tail -1)"
