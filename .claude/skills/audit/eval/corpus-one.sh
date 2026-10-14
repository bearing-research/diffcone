#!/bin/bash
# corpus-one.sh NAME TREE OUTDIR [--outcome-only]: diffcone corpus over the
# last 6 Python-changing commits of $E/repos/NAME, run from the diffcone
# source tree TREE, with --coverage unless --outcome-only. Writes
# OUTDIR/NAME.txt (the report, then "exit <code> <seconds>s") and NAME.log.
# All of them: ls "$E/repos" | grep -v '^pandas$' | xargs -P 4 -I{} ./corpus-one.sh {} TREE OUTDIR
: "${E:?set E to the evaluation work directory}"
name=${1:?usage: corpus-one.sh NAME TREE OUTDIR [--outcome-only]}
T=${2:?usage: corpus-one.sh NAME TREE OUTDIR}; O=${3:?usage: corpus-one.sh NAME TREE OUTDIR}
cov=(--coverage); [[ ${4:-} == --outcome-only ]] && cov=()
R=$E/repos/$name
mkdir -p "$O"
roots=(--source-root .); [ -d "$R/src" ] && roots=(--source-root src --source-root .)
cmd="$R/.venv/bin/python -m pytest --rootdir=."  # see evidence-corpus-one.sh
[[ $name == pre-commit ]] && cmd="$cmd --ignore=tests/languages"
# The clones are shallow: start from the furthest ancestor that exists.
n=40; while [ $n -gt 6 ] && ! git -C "$R" rev-parse -q --verify "HEAD~$n" >/dev/null; do n=$((n-1)); done
start=$(date +%s)
PATH="$R/.venv/bin:$PATH" timeout 7200 uv run --project "$T" diffcone corpus --repo "$R" \
  --range "HEAD~$n..HEAD" --max 6 --discover pytest "${roots[@]}" --command "$cmd" \
  ${cov[@]+"${cov[@]}"} --format text -o "$O/$name.txt" > "$O/$name.log" 2>&1
echo "exit $? $(( $(date +%s) - start ))s" >> "$O/$name.txt"
