"""Recheck the forward misses of analyse.py on a same-order rerun of just the
tests involved (roadmap item 7): a miss that disappears was order noise.

usage: rerun_check.py ROOT RERUN_PROF RERUN_TRACE PANDAS_GIT
(reads forward_miss_pairs.json, which analyse.py writes)
"""

# ruff: noqa: F821 (E, O and selected_for come from analyse.py, exec'd below)
import json
import sys
from collections import Counter
from pathlib import Path

sys.argv = ["analyse", sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]]
src = (
    Path("analyse.py")
    .read_text()
    .split("# ---------------------------------------------------------------- recent edits")[0]
)
exec(compile(src.replace("print(", "(lambda *a, **k: None)("), "analyse", "exec"))
pairs = json.loads(Path("forward_miss_pairs.json").read_text())
result = Counter()
still = []
for fk_s, t in pairs:
    path, qual = fk_s.split("::", 1)
    fk = (path, qual)
    if t not in O or t not in E:
        result["test not rerun"] += 1
        continue
    in_o, in_e = fk in O[t], fk in E[t] or bool(selected_for(fk) & {t})
    result[(in_o, in_e)] += 1
    if in_o and not in_e:
        still.append((fk_s, t))
print(result)
print("still missed:", still[:20])
