"""Tabulate evidence-corpus-one.sh outputs: evidence_table.py DIR [BASELINE_DIR].

Per repository: the pairs validated, misses (an outcome change or a test
that executed changed code, unselected), selected of targets summed over the
pairs, mean savings, and whether the recording selects everything (the hook
phase flagged). With BASELINE_DIR, also the selection there and the change.
A miss anywhere is a failure of the round; a larger selection is a cost to
explain by rule."""

import json
import pathlib
import sys

HOOK = "during a hook: every change selects every test"


def load(d: pathlib.Path, name: str):
    f = d / f"{name}.json"
    if not f.exists():
        log = d / f"{name}.log"
        return None, log.read_text().strip().splitlines()[-1] if log.exists() else "not run"
    data = json.loads(f.read_text())
    pairs = [e for e in data["entries"] if not e["skipped"]]
    collect = d / f"{name}.collect.log"
    return {
        "validated": data["totals"]["validated"],
        "errors": data["totals"]["errors"],
        "missed": data["totals"]["outcome_missed"]
        + sum((e.get("coverage") or {}).get("counts", {}).get("missed", 0) for e in pairs),
        "selected": sum(e["selected"] for e in pairs),
        "targets": sum(e["targets"] for e in pairs),
        "savings": data["totals"]["mean_savings"],
        "hook": collect.exists() and HOOK in collect.read_text(),
    }, None


new = pathlib.Path(sys.argv[1])
base = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else None
names = sorted(p.stem for p in new.glob("*.log") if not p.name.endswith(".collect.log"))
head = "| repo | pairs | missed | selected | mean savings | hook flagged |"
rule = "|---|---|---|---|---|---|"
if base:
    head += " baseline selected | change |"
    rule += "---|---|"
print(head)
print(rule)
for name in names:
    r, why = load(new, name)
    if r is None:
        print(f"| {name} | {why} |")
        continue
    savings = "-" if r["savings"] is None else f"{r['savings']:.0%}"
    errors = f" ({r['errors']} errors)" if r["errors"] else ""
    row = (
        f"| {name} | {r['validated']}{errors} "
        f"| **{r['missed']}** | {r['selected']} of {r['targets']} | {savings} "
        f"| {'yes' if r['hook'] else 'no'} |"
    )
    if base:
        b, why = load(base, name)
        row += (
            f" {why} | |"
            if b is None
            else f" {b['selected']} | {r['selected'] - b['selected']:+d} |"
        )
    print(row)
