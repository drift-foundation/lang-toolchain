from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lang import mir
from lang.ssa_codegen import emit_module_object
from lang.types import ERROR, INT, STR

ROOT = Path(__file__).resolve().parent.parent


def _find_clang() -> str | None:
    for name in ("clang-15", "clang"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _build_get_code_fn() -> mir.Function:
    """fn get_code(err: Error) -> Int { code = error_event(err); return code }"""
    err_param = mir.Param("_err", ERROR)
    bb0 = mir.BasicBlock(name="bb0", params=[err_param])
    bb0.instructions.append(mir.ErrorEvent(dest="_code", error=err_param.name))
    bb0.terminator = mir.Return(value="_code")
    return mir.Function(name="get_code", params=[err_param], return_type=INT, entry="bb0", blocks={"bb0": bb0})


def _build_main_fn(code: int) -> mir.Function:
    """Call runtime error_new_dummy, then get_code(err), return it."""
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb0.instructions.append(mir.Const(dest="_code_in", type=INT, value=code))
    bb0.instructions.append(mir.Const(dest="_key", type=STR, value=""))
    bb0.instructions.append(mir.Const(dest="_payload", type=STR, value=""))
    bb0.instructions.append(
        mir.Call(dest="_err", callee="drift_error_new_dummy", args=["_code_in", "_key", "_payload"], ret_type=ERROR, err_dest=None, normal=None, error=None)
    )
    bb0.instructions.append(mir.Call(dest="_out", callee="get_code", args=["_err"], ret_type=INT, err_dest=None, normal=None, error=None))
    bb0.terminator = mir.Return(value="_out")
    return mir.Function(name="main", params=[], return_type=INT, entry="bb0", blocks={"bb0": bb0})


def test_error_event_executes(tmp_path: Path) -> None:
    pytest.importorskip("llvmlite")
    clang = _find_clang()
    if not clang:
        pytest.skip("clang not found")

    get_code_fn = _build_get_code_fn()
    main_fn = _build_main_fn(42)
    obj_path = tmp_path / "err_evt.o"
    emit_module_object([get_code_fn, main_fn], struct_layouts={}, entry="main", out_path=obj_path)

    exe_path = tmp_path / "err_evt"
    runtime_sources = [ROOT / "lang" / "runtime" / "error_dummy.c"]
    link_cmd = [clang, str(obj_path)] + [str(src) for src in runtime_sources] + ["-o", str(exe_path)]
    subprocess.run(link_cmd, check=True, capture_output=True, text=True)

    res = subprocess.run([str(exe_path)], capture_output=True, text=True)
    assert res.returncode == 42
