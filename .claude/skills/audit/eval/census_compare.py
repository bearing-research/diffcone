"""Selected tests per census project across versions; mean selection over plans.

    census_compare.py E/census-v0.4.0.json E/census-main.json ...

Each file is `scripts/census.py run -o` output named census-<label>.json;
only commits planned by every version are compared. A plan that failed
(an "error" entry: a crash, a timeout) is listed per version under
ERRORS at the end, and the script exits 1 when any version has one: a
version that crashes where another planned is a regression, not a
missing row."""
import json, sys
runs = [(p.split("census-")[1].removesuffix(".json"), {r["repo"]: r for r in json.load(open(p))}) for p in sys.argv[1:]]
names = [n for n, _ in runs]
repos = sorted(set().union(*[set(d) for _, d in runs]))
tot = {n: [0, 0] for n in names}; frac = {n: [0.0, 0] for n in names}
rows = []
errors = []
for repo in repos:
    for n, d in runs:
        r = d.get(repo)
        bad = [c for c in (r["commits"] if r else []) if "error" in c]
        if bad:
            errors.append(f"{n:10s} {repo}: {len(bad)} plan(s) failed: {bad[0]['error'][:120]}")
    commits = None
    per = {}
    for n, d in runs:
        r = d.get(repo)
        cs = {c["sha"]: c for c in (r["commits"] if r else []) if "selected" in c}
        per[n] = cs
        commits = set(cs) if commits is None else commits & set(cs)
    commits = sorted(commits or [])
    if not commits:
        rows.append((repo, "no common commits")); continue
    t = sum(per[names[0]][s]["targets"] for s in commits)
    cells = []
    for n in names:
        sel = sum(per[n][s]["selected"] for s in commits)
        tot[n][0] += sel; tot[n][1] += t
        for s in commits:
            if per[n][s]["targets"]:
                frac[n][0] += per[n][s]["selected"] / per[n][s]["targets"]; frac[n][1] += 1
        cells.append(sel)
    mark = "" if len(set(cells)) == 1 else "  *"
    rows.append((repo, " -> ".join(f"{c:7d}" for c in cells) + f"  of {t:7d} ({len(commits)} plans){mark}"))
print("columns:", " -> ".join(names))
for repo, line in rows: print(f"{repo:32s} {line}")
for n in names:
    print(f"{n:10s} total {tot[n][0]} of {tot[n][1]}; mean selection over {frac[n][1]} plans: {100*frac[n][0]/max(frac[n][1],1):.1f}%")
if errors:
    print("ERRORS")
    for line in errors: print("  " + line)
    sys.exit(1)
