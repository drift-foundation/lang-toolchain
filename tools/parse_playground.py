#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

from lark import UnexpectedInput

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lang import parser


def main() -> int:
    roots = sys.argv[1:] or ["playground"]
    failed = False
    any_files = False

    for root in roots:
        files = sorted(Path(root).glob("*.drift"))
        if not files:
            print(f"[warn] no .drift files under {root}", file=sys.stderr)
            continue
        any_files = True
        for path in files:
            try:
                parser.parse_program(path.read_text())
                print(f"[ok] {path}")
            except UnexpectedInput as exc:
                failed = True
                print(f"[parse error] {path}: {exc}", file=sys.stderr)

    if not any_files:
        print("no drift files found in provided roots", file=sys.stderr)
        return 1

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
