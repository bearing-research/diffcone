#!/bin/bash
# check-one.sh NAME TREE: scripts/fixture_check.py and scripts/collection_check.py
# for one repository, run from the diffcone source tree TREE (a checkout or an
# export of the commit under test); outputs go to $E/out/NAME.{fx,cc}.txt
# (+ fx.json). Tabulate with checks_table.py $E.
: "${E:?set E to the evaluation work directory}"
n=${1:?usage: check-one.sh NAME TREE}; T=${2:?usage: check-one.sh NAME TREE}
R=$E/repos/$n
if [[ -d $R/src ]]; then roots=(--source-root src --source-root .); else roots=(--source-root .); fi
cmd="$R/.venv/bin/python -m pytest"
[[ $n == pre-commit ]] && cmd="$cmd --ignore=tests/languages"
mkdir -p "$E/out"
cd "$T" || exit 9
uv run --frozen python scripts/fixture_check.py --repo "$R" --command "$cmd" "${roots[@]}" \
  --limit 15 -o "$E/out/$n.fx.json" > "$E/out/$n.fx.txt" 2>&1
echo "exit $?" >> "$E/out/$n.fx.txt"
uv run --frozen python scripts/collection_check.py --repo "$R" --command "$cmd" "${roots[@]}" \
  --limit 15 > "$E/out/$n.cc.txt" 2>&1
echo "exit $?" >> "$E/out/$n.cc.txt"
echo "$n fx $(tail -1 "$E/out/$n.fx.txt") cc $(tail -1 "$E/out/$n.cc.txt")"
