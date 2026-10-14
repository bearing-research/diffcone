#!/bin/bash
# evidence-corpus-one.sh NAME TREE OUTDIR: evidence plans checked against
# real runs on $E/repos/NAME, from the diffcone source tree TREE. Finds the
# last 6 first-parent commits that change a .py file, records the suite at
# each one's parent, then runs `diffcone corpus --evidence auto --coverage`
# from the oldest parent to HEAD (commits changing no .py file are
# skipped), so each pair is planned from a recording of its own base, as on
# a CI whose main records every commit (or advances the store with `run
# --collect`). Writes OUTDIR/NAME.json (the
# corpus report; evidence_table.py tabulates a directory of them), NAME.log
# (progress, then "exit <code> <seconds>s") and NAME.collect.log. One
# repository at a time: a recording and a corpus run each take the machine
# (evaluation.md, "Evidence corpus"). The clone is checked out at each
# parent while recording (its env is an editable install of the clone) and
# put back after; old recordings are removed first, so each run records
# anew with TREE's recorder.
: "${E:?set E to the evaluation work directory}"
name=${1:?usage: evidence-corpus-one.sh NAME TREE OUTDIR}
T=$(cd "${2:?usage: evidence-corpus-one.sh NAME TREE OUTDIR}" && pwd)
O=${3:?usage: evidence-corpus-one.sh NAME TREE OUTDIR}
R=$E/repos/$name
mkdir -p "$O"
roots=(--source-root .); [ -d "$R/src" ] && roots=(--source-root src --source-root .)
# --rootdir: a repository with no pytest configuration of its own would
# otherwise take the first one above it (E inside a checkout of diffcone
# finds diffcone's), and its node ids would match no target.
cmd="$R/.venv/bin/python -m pytest --rootdir=$R"
[[ $name == pre-commit ]] && cmd="$cmd --ignore=tests/languages"
branch=$(git -C "$R" rev-parse --abbrev-ref HEAD)
head=$(git -C "$R" rev-parse HEAD)
start=$(date +%s)
[ -z "$(git -C "$R" status --porcelain --untracked-files=no)" ] || { echo "$R has changes to tracked files" > "$O/$name.log"; exit 2; }
py=()
for c in $(git -C "$R" rev-list --first-parent HEAD); do
  git -C "$R" rev-parse -q --verify "$c^" >/dev/null || break
  [ -n "$(git -C "$R" diff --name-only "$c^" "$c" -- '*.py')" ] && py+=("$c")
  [ ${#py[@]} -eq 6 ] && break
done
[ ${#py[@]} -gt 0 ] || { echo "no Python-changing commit in reach" > "$O/$name.log"; exit 2; }
base=$(git -C "$R" rev-parse "${py[${#py[@]}-1]}^")
rm -f "$R"/.diffcone/evidence/*.sqlite
: > "$O/$name.collect.log"
for c in "${py[@]}"; do
  git -C "$R" checkout -q --detach "$c^" || break
  echo "== $(git -C "$R" rev-parse --short HEAD)" >> "$O/$name.collect.log"
  PATH="$R/.venv/bin:$PATH" timeout 3600 uv run --project "$T" diffcone collect --repo "$R" \
    "${roots[@]}" --command "$cmd" >> "$O/$name.collect.log" 2>&1
  echo "collect exit $?" >> "$O/$name.collect.log"
  git -C "$R" checkout -q -- .  # a suite that rewrites a tracked file (arrow's junit.xml)
done
if [[ $branch == HEAD ]]; then git -C "$R" checkout -q --detach "$head"; else git -C "$R" checkout -q "$branch"; fi
[ "$(git -C "$R" rev-parse HEAD)" = "$head" ] || { echo "could not restore $name to $head" >&2; exit 2; }
n=$(ls "$R"/.diffcone/evidence/*.sqlite 2>/dev/null | wc -l | tr -d ' ')
# Failing tests still make a recording (collect exits non-zero then).
if [ "$n" -ne ${#py[@]} ]; then
  echo "recorded $n of ${#py[@]} bases $(( $(date +%s) - start ))s" > "$O/$name.log"
  exit 2
fi
PATH="$R/.venv/bin:$PATH" timeout 7200 uv run --project "$T" diffcone corpus --repo "$R" \
  --range "$base..$head" --discover pytest "${roots[@]}" --command "$cmd" \
  --evidence auto --coverage --format json -o "$O/$name.json" > "$O/$name.log" 2>&1
echo "exit $? $(( $(date +%s) - start ))s" >> "$O/$name.log"
