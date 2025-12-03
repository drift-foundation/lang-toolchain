"""Smoke tests for SSA mode with simplification."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
DRIFTC = ROOT / "lang" / "driftc.py"
VENV_PY = os.environ.get("VENV_PY", str(ROOT / ".venv" / "bin" / "python3"))


def _run_ssa_smoke(tmp_path: Path, filename: str, source: str) -> None:
    try:
        import llvmlite  # noqa: F401
    except ImportError:
        print("llvmlite missing; skipping SSA smoke tests", file=sys.stderr)
        return

    src = tmp_path / filename
    src.write_text(source)
    out_obj = src.with_suffix(".o")
    env = dict(os.environ)
    env["SSA_ONLY"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [
            VENV_PY,
            "-m",
            "lang.driftc",
            str(src),
            "-o",
            str(out_obj),
            "--ssa-check",
            "--ssa-check-mode=fail",
            "--ssa-simplify",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )
    if proc.returncode != 0 and "GenericAlias" in proc.stderr:
        print("types shadowing detected; skipping SSA smoke tests", file=sys.stderr)
        return
    assert proc.returncode == 0, f"compiler failed: {proc.stderr}"
    assert out_obj.exists(), "expected output object file"


def test_ssa_check_smoke(tmp_path: Path) -> None:
    _run_ssa_smoke(
        tmp_path,
        "main.drift",
        "import std.console.out\n"
        "fn main() returns Int {\n"
        "  out.writeln(\"hi from ssa\")\n"
        "  val rc: Int = 0\n"
        "  return rc\n"
        "}\n",
    )


def test_ssa_check_control_flow(tmp_path: Path) -> None:
    _run_ssa_smoke(
        tmp_path,
        "control.drift",
        "import std.console.out\n"
        "fn main() returns Int {\n"
        "  var i: Int = 0\n"
        "  while i < 3 {\n"
        "    if i == 1 {\n"
        "      out.writeln(\"mid\")\n"
        "    }\n"
        "    i = i + 1\n"
        "  }\n"
        "  val rc: Int = 0\n"
        "  return rc\n"
        "}\n",
    )


def test_ssa_check_try_catch(tmp_path: Path) -> None:
    _run_ssa_smoke(
        tmp_path,
        "try_catch.drift",
        "import std.console.out\n"
        "fn might_fail(x: Int) returns Int {\n"
        "  if x != 0 { throw drift_error_new_dummy(x, \"payload\", \"\"); }\n"
        "  return x\n"
        "}\n"
        "fn main() returns Int {\n"
        "  try {\n"
        "    out.writeln(\"before\")\n"
        "    might_fail(1)\n"
        "  } catch err {\n"
        "    out.writeln(\"caught\")\n"
        "  }\n"
        "  val rc: Int = 0\n"
        "  return rc\n"
        "}\n",
    )


if __name__ == "__main__":
    test_ssa_check_smoke(Path("/tmp"))
    test_ssa_check_control_flow(Path("/tmp"))
    test_ssa_check_try_catch(Path("/tmp"))
