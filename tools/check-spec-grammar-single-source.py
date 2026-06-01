#!/usr/bin/env python3
"""
Guardrail: ensure canonical spec/grammar paths exist and staged copies are stubs.
"""
from __future__ import annotations

from pathlib import Path
import sys


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    spec = root / "doc" / "design" / "drift-lang-spec.md"
    grammar = root / "doc" / "design" / "drift-lang-grammar.md"

    errors: list[str] = []

    # Canonical files must exist, be non-trivial, and carry the marker.
    markers = {"spec": "CANONICAL-SPEC", "grammar": "CANONICAL-GRAMMAR"}
    for path, label in ((spec, "spec"), (grammar, "grammar")):
        if not path.exists():
            errors.append(f"missing canonical {label}: {path}")
        else:
            text = path.read_text(encoding="utf-8")
            if len(text.strip()) < 100:
                errors.append(f"canonical {label} too short (stub?): {path}")
            marker = markers[label]
            if marker not in text:
                errors.append(f"canonical {label} missing marker '{marker}': {path}")

    # Staged copies (if present) must be stubs.
    for staged in root.glob("staged/**/drift-lang-*.md"):
        text = staged.read_text(encoding="utf-8").strip()
        if "canonical" not in text.lower() or len(text.splitlines()) > 20:
            errors.append(f"staged copy is not a stub: {staged}")

    if errors:
        sys.stderr.write("Spec/grammar guardrail failures:\n")
        for e in errors:
            sys.stderr.write(f"- {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
