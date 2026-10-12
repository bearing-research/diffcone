"""Tabulate check-one.sh outputs: checks_table.py E (the evaluation work directory)."""
import re, sys, pathlib
E = pathlib.Path(sys.argv[1])
names = (pathlib.Path(__file__).parent / "names.txt").read_text().split() + ["pandas"]
print("| repo | fx checked | fx missed | collected | targets | missing | extra | notes |")
print("|---|---|---|---|---|---|---|---|")
for n in names:
    fx = (E / "out" / f"{n}.fx.txt"); cc = (E / "out" / f"{n}.cc.txt")
    if not fx.exists(): print(f"| {n} | - |"); continue
    f = fx.read_text(); c = cc.read_text() if cc.exists() else ""
    chk = re.search(r"tests checked: (\d+)", f); mis = re.search(r"missed fixture: (\d+)", f)
    col = re.search(r"collected (\d+)\s+targets (\d+)", c); m = re.search(r"missing (\d+).*extra (\d+)", c)
    notes = re.search(r"notes: (.*)", c)
    print(f"| {n} | {chk and chk[1]} | {mis and mis[1]} | {col and col[1]} | {col and col[2]} | {m and m[1]} | {m and m[2]} | {notes and notes[1]} |")
