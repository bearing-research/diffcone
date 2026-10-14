# Validating a round: strata, census, checks, corpus, evidence corpus

The scripts are in `eval/` beside this file. They build and read a work
directory `E` (an environment variable; put it in the session scratchpad,
which a reboot wipes: check it exists before relying on it). Run diffcone
from a **tree**: a checkout of the commit under test, or an export of it so
later commits don't move it:

```bash
REPO=$(git rev-parse --show-toplevel); E=$S/eval; mkdir -p $E/trees/main $E/trees/v0.3.0
git -C "$REPO" archive main | tar -x -C $E/trees/main
git -C "$REPO" archive v0.3.0 | tar -x -C $E/trees/v0.3.0  # the last release, for comparison
```

Results go into `internal/evaluation.md` as a dated section, newest first,
with the commits measured. Findings go into `internal/audit.md`.

## Environments (about 5 minutes for all, plus pandas)

`eval/fixenv.sh NAME` clones one repository into `$E/repos/NAME` (shallow)
and builds `$E/repos/NAME/.venv` the way the project's CI installs its
tests, then checks collection. `eval/envs.md` has each recipe in words and
the last measured status; update it when a recipe changes.

```bash
export E; cd .claude/skills/audit/eval
xargs -P 6 -n1 ./one-env.sh < names.txt     # logs in $E/logs/env-NAME.log
./one-env.sh pandas                          # pandas separately: a meson build
```

A failing env is a recipe problem until shown otherwise; fix the recipe,
don't drop the repository silently.

## strata (evidence precision; run for every planning or indexer change)

strata is the user's repository and diffcone's first CI deployment; plans
must not get less precise there without a reason. Clone it to scratch,
never use the user's own checkout.

```bash
export STRATA=$S/strata
eval/strata_setup.sh <tree>        # clone, env, record at the base commit (~10 min)
eval/strata_check.sh <tree> before # selected/total and the selection by rule
eval/strata_check.sh <tree2> after
```

`PY` picks the interpreter (default 3.13). strata's CI records on 3.12,
3.13 and 3.14, and the recorder sees different code on each (3.14
evaluates annotations lazily), so measure 3.13 and 3.14 (`PY=3.14
STRATA=$S/strata314 eval/strata_setup.sh <tree>`). Baselines at main
after audit E10: 653 of 6231 on 3.13, 657 on 3.14.

Record again after any store-format change (`STORE_FORMAT`) or recorder
change. Compare the counts by rule; every change needs an explanation (a
closed miss, a new rule's cost). The last measurement is in
`internal/evaluation.md`.

## Census (static precision)

```bash
eval/census.sh $E/trees/v0.3.0 v0.3.0   # first run clones into $E/census-work
eval/census.sh $E/trees/main main
python3 eval/census_compare.py $E/census-v0.3.0.json $E/census-main.json
```

Plans 8 commits of each of the 42 census repositories without running
anything. Report the mean selection per plan for each version; for every
repository whose selection moved, find the rule that moved it (plan one
commit with each tree and diff the reasons) and say whether it is a closed
miss (expected) or a cost to examine.

## Fixture and collection checks (discovery recall)

```bash
ls $E/repos | xargs -P 4 -I{} eval/check-one.sh {} $E/trees/main
python3 eval/checks_table.py $E
```

`fixture_check` compares discovered fixture dependencies with pytest's
closures (any missed fixture is a finding); `collection_check` compares
discovery with real collection (every collected test that isn't a target
must be behind an incomplete-discovery note).

## Corpus (recall on real suites; the strongest oracle, slow)

```bash
ls $E/repos | grep -v '^pandas$' | \
  xargs -P 4 -I{} eval/corpus-one.sh {} $E/trees/main $E/corpus
```

Runs `diffcone corpus --coverage` over the last 6 Python-changing commits
of each repository: each suite runs at both snapshots, so a test whose
outcome changed but wasn't selected is a **miss**, and coverage shows
whether every test that executed changed code was selected. Pass
`--outcome-only` as a 4th argument where coverage can't run. Takes hours;
watch for repositories still running after 2 hours (a hanging test of the
project's own; check with `ps`, and leave the user's other processes
alone).

Read each `$E/corpus/NAME.txt`: "missed" lines and recall below 100 % are
findings to reproduce. Tests "outside the target list" in a plan whose
discovery said so (exit 3) are not misses. A repository whose validation
can't run (an error before outcomes) is a finding about `validate` until
shown to be the project's own problem.

## Evidence corpus (recall and precision of evidence plans; run for every evidence or recorder change)

The census and the corpus above check static plans. Evidence plans
(`--evidence`) are checked on strata and on this set of small suites,
each recorded and replayed with `diffcone corpus --evidence auto --coverage`:

```bash
export E; cd .claude/skills/audit/eval
for n in flask jinja marshmallow pluggy typer boltons arrow more-itertools; do
  ./evidence-corpus-one.sh $n <tree> $E/evcorpus/<name>   # one at a time
done
python3 evidence_table.py $E/evcorpus/<name> [$E/evcorpus/<baseline name>]
```

`evidence-corpus-one.sh` finds the last 6 first-parent commits that
change a `.py` file and records the suite at each one's parent with the
tree's recorder (in the clone itself, since its env is an editable install
of it; the clone is put back after), then validates those commits, each
planned from the recording of its own base, as on a CI whose main records
every commit. (One recording for all six makes each later pair carry every
earlier change, and the numbers say little about any one change.) About
5-30 minutes per repository (typer's coverage runs are the slowest); run
them one after another, and never beside another heavy job (a pandas
recording or plan, the full gate): the machine has 16 GB. black is not in
the set: its test data holds deliberately invalid `.py` files under the
source root, and `collect` refuses a checkout with analysis errors.

Why a plan selects what it does is not in the corpus report: plan one pair
from its recording (`diffcone plan --repo $E/repos/NAME --base C^ --head C
--discover pytest --evidence auto --format json`) and count the selected
targets' `rules`; `analysis.evidence.escalated_modules` and the reasons'
`detail` name the chains.

Read the table: any **missed** above 0 is a finding to reproduce (a test
whose outcome changed, or that executed changed code, unselected). A
selection that grew against the baseline needs an explanation by rule;
"hook flagged" means the recording selects everything for every change
(something ran outside every test that no record shows; the collect log
says what), which is a precision finding of its own. The last baseline is
in `internal/evaluation.md`.
