"""SSA-first end-to-end runner (parse -> SSA -> optional codegen/run)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIFTC = ROOT / ".venv" / "bin" / "python3"
DRIFTC_MODULE = "lang.driftc"


def _have_llvmlite() -> bool:
    try:
        import llvmlite  # noqa: F401
    except ImportError:
        return False
    return True


def _run_case(case_dir: Path) -> str:
    expected = json.loads((case_dir / "expected.json").read_text())
    requires_llvmlite = expected.get("skip_if", {}).get("requires_llvmlite", False)
    if requires_llvmlite and not _have_llvmlite():
        return "skipped (llvmlite missing)"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    # Run SSA path; keep legacy codegen bypassed for now.
    env["SSA_ONLY"] = "1"
    cmd = [
        str(DRIFTC),
        "-m",
        DRIFTC_MODULE,
        str(case_dir / "main.drift"),
        "-o",
        str(case_dir / "a.o"),
        "--ssa-check",
        "--ssa-check-mode=fail",
        "--ssa-simplify",
    ]
    compile_res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)

    compile_err = expected.get("compile_error")
    if compile_err is not None:
        if compile_res.returncode == 0:
            return f"FAIL (expected compile error containing {compile_err!r}, got success)"
        if compile_err not in compile_res.stderr:
            return f"FAIL (compile error mismatch; wanted substring {compile_err!r})"
        return "ok"

    if compile_res.returncode != 0:
        return f"FAIL (compile failed: {compile_res.stderr})"

    if expected.get("run", False):
        return "skipped (run not supported with SSA_ONLY=1 yet)"
    return "ok"


def main() -> int:
    failures: list[tuple[Path, str]] = []
    for case_dir in sorted((ROOT / "tests" / "e2e").iterdir()):
        if not (case_dir / "main.drift").exists():
            continue
        status = _run_case(case_dir)
        print(f"{case_dir.name}: {status}")
        if not status.startswith("ok") and not status.startswith("skipped"):
            failures.append((case_dir, status))
    if failures:
        for case, status in failures:
            print(f"[e2e] {case.name}: {status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
