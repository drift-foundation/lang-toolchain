#!/usr/bin/env python3
"""Scan all parsed units for concrete overload pairs whose signatures are
identical after erasing each parameter's outer mode {T, &T, &mut T}.

Run from the repo root with the project venv:
    .venv/bin/python work/reject-redundant-call-borrows/recount/overload_mode_erasure_scan.py <units.pkl>

The units pickle is produced by the collection scripts in this directory
(list of tuples; the parsed `Program` is located by type). Results feed
PLAN.md §R2: 3 T-vs-& pairs (json._encode_node + two test fixtures),
0 &-vs-&mut pairs, repo-wide.
"""
import collections
import pickle
import sys

sys.path.insert(0, ".")
from lang.driftc.parser.ast import Program  # noqa: E402


def ty_key(te, erase_outer=False):
    if te is None:
        return "?"
    name = getattr(te, "name", "?")
    args = getattr(te, "args", []) or []
    if erase_outer and name in ("&", "&mut"):
        return ty_key(args[0] if args else None)
    return name + ("<" + ",".join(ty_key(a) for a in args) + ">" if args else "")


def outer_mode(te):
    n = getattr(te, "name", None)
    return n if n in ("&", "&mut") else "val"


def main(units_path):
    units = pickle.load(open(units_path, "rb"))
    groups = collections.defaultdict(list)
    for idx, rec in enumerate(units):
        path = rec[0]
        prog = next((el for el in rec if isinstance(el, Program)), None)
        if prog is None:
            continue
        items = [(None, f) for f in getattr(prog, "functions", []) or []]
        for imp in getattr(prog, "implements", []) or []:
            tgt = (getattr(imp, "target", None) or getattr(imp, "type_name", None)
                   or getattr(imp, "target_type", None))
            tname = getattr(tgt, "name", str(tgt))
            for m in getattr(imp, "methods", []) or getattr(imp, "functions", []) or []:
                items.append((tname, m))
        for owner, f in items:
            params = [p for p in getattr(f, "params", []) if getattr(p, "name", "") != "self"]
            groups[(idx, path, owner, f.name, len(params))].append(params)

    t_vs_ref, ref_vs_mut = [], []
    for key, cands in groups.items():
        if len(cands) < 2:
            continue
        for i in range(len(cands)):
            for j in range(i + 1, len(cands)):
                a, b = cands[i], cands[j]
                if ([ty_key(p.type_expr, True) for p in a]
                        != [ty_key(p.type_expr, True) for p in b]):
                    continue
                ma = [outer_mode(p.type_expr) for p in a]
                mb = [outer_mode(p.type_expr) for p in b]
                if ma == mb:
                    continue
                diff = [(x, y) for x, y in zip(ma, mb) if x != y]
                (t_vs_ref if any("val" in d for d in diff) else ref_vs_mut).append((key, diff))

    print("== T vs &T/&mut T pairs (same unit) ==", len(t_vs_ref))
    for k, d in t_vs_ref:
        print("  ", k[1], "owner=" + str(k[2]), k[3], d)
    print("== &T vs &mut T pairs (same unit) ==", len(ref_vs_mut))
    for k, d in ref_vs_mut:
        print("  ", k[1], "owner=" + str(k[2]), k[3], d)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else
         "work/reject-redundant-call-borrows/recount/units.pkl")
