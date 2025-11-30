from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("llvmlite")

from lang import mir
from lang.ssa_codegen import emit_module_object
from lang.types import ERROR, INT, UNIT

ROOT = Path(__file__).resolve().parent.parent


def _find_clang() -> str | None:
    for name in ("clang-15", "clang"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _build_callee() -> mir.Function:
    """callee(flag): returns {0, null} on flag==0, else {1, err_ptr}."""
    entry = mir.BasicBlock(name="bb_entry", params=[mir.Param("_flag", INT)])
    entry.instructions.append(mir.Const(dest="_zero", type=INT, value=0))
    entry.instructions.append(mir.Binary(dest="_is_zero", op="==", left="_flag", right="_zero"))
    entry.terminator = mir.CondBr(
        cond="_is_zero",
        then=mir.Edge(target="bb_ok", args=[]),
        els=mir.Edge(target="bb_err", args=[]),
    )

    ok = mir.BasicBlock(name="bb_ok", params=[])
    ok.instructions.append(mir.Const(dest="_err_null", type=ERROR, value=None))
    ok.instructions.append(mir.Const(dest="_ok_val", type=INT, value=0))
    ok.terminator = mir.Return(value="_ok_val", error="_err_null")

    err = mir.BasicBlock(name="bb_err", params=[])
    err.instructions.append(
        mir.Call(
            dest="_err_ptr",
            callee="drift_error_new_dummy",
            args=["_flag"],
            ret_type=ERROR,
            err_dest=None,
            normal=None,
            error=None,
        )
    )
    err.instructions.append(mir.Const(dest="_fail_val", type=INT, value=1))
    err.terminator = mir.Return(value="_fail_val", error="_err_ptr")

    return mir.Function(
        name="callee",
        params=[mir.Param("_flag", INT)],
        return_type=INT,
        entry="bb_entry",
        module="<tests>",
        source=None,
        blocks={"bb_entry": entry, "bb_ok": ok, "bb_err": err},
    )


def _build_throwing_callee() -> mir.Function:
    """callee_throw(flag): returns {0, null} on flag==0, else throw err."""
    entry = mir.BasicBlock(name="bb_entry", params=[mir.Param("_flag", INT)])
    entry.instructions.append(mir.Const(dest="_zero", type=INT, value=0))
    entry.instructions.append(mir.Binary(dest="_is_zero", op="==", left="_flag", right="_zero"))
    entry.terminator = mir.CondBr(
        cond="_is_zero",
        then=mir.Edge(target="bb_ok", args=[]),
        els=mir.Edge(target="bb_err", args=[]),
    )

    ok = mir.BasicBlock(name="bb_ok", params=[])
    ok.instructions.append(mir.Const(dest="_err_null", type=ERROR, value=None))
    ok.instructions.append(mir.Const(dest="_ok_val", type=INT, value=0))
    ok.terminator = mir.Return(value="_ok_val", error="_err_null")

    err = mir.BasicBlock(name="bb_err", params=[])
    err.instructions.append(
        mir.Call(
            dest="_err_ptr",
            callee="drift_error_new_dummy",
            args=["_flag"],
            ret_type=ERROR,
            err_dest=None,
            normal=None,
            error=None,
        )
    )
    err.terminator = mir.Throw(error="_err_ptr")

    return mir.Function(
        name="callee_throw",
        params=[mir.Param("_flag", INT)],
        return_type=INT,
        entry="bb_entry",
        module="<tests>",
        source=None,
        blocks={"bb_entry": entry, "bb_ok": ok, "bb_err": err},
    )


def _build_void_callee() -> mir.Function:
    """maybe_fail_void(flag): return (err = null) or throw err (Error* ABI only)."""
    entry = mir.BasicBlock(name="bb_entry", params=[mir.Param("_flag", INT)])
    entry.instructions.append(mir.Const(dest="_zero", type=INT, value=0))
    entry.instructions.append(mir.Binary(dest="_is_zero", op="==", left="_flag", right="_zero"))
    entry.terminator = mir.CondBr(
        cond="_is_zero",
        then=mir.Edge(target="bb_ok", args=[]),
        els=mir.Edge(target="bb_err", args=[]),
    )

    ok = mir.BasicBlock(name="bb_ok", params=[])
    ok.instructions.append(mir.Const(dest="_err_null", type=ERROR, value=None))
    ok.terminator = mir.Return(value=None, error="_err_null")

    err = mir.BasicBlock(name="bb_err", params=[])
    err.instructions.append(
        mir.Call(
            dest="_err_ptr",
            callee="drift_error_new_dummy",
            args=["_flag"],
            ret_type=ERROR,
            err_dest=None,
            normal=None,
            error=None,
        )
    )
    err.terminator = mir.Throw(error="_err_ptr")

    return mir.Function(
        name="maybe_fail_void",
        params=[mir.Param("_flag", INT)],
        return_type=UNIT,
        entry="bb_entry",
        module="<tests>",
        source=None,
        blocks={"bb_entry": entry, "bb_ok": ok, "bb_err": err},
    )


def _build_main(flag_value: int, expect_value: bool = True) -> mir.Function:
    entry = mir.BasicBlock(name="bb0", params=[])
    ok = mir.BasicBlock(name="bb_ok", params=[])
    err = mir.BasicBlock(name="bb_err", params=[])

    entry.instructions.append(mir.Const(dest="_flag", type=INT, value=flag_value))
    entry.terminator = mir.Call(
        dest="_call_res" if expect_value else None,
        callee="callee",
        args=["_flag"],
        ret_type=INT if expect_value else UNIT,
        err_dest=None,
        normal=mir.Edge(target="bb_ok", args=[]),
        error=mir.Edge(target="bb_err", args=[]),
    )

    ok.instructions.append(mir.Const(dest="_ok_exit", type=INT, value=0))
    ok.terminator = mir.Return(value="_ok_exit")

    err.instructions.append(mir.Const(dest="_err_exit", type=INT, value=1))
    err.terminator = mir.Return(value="_err_exit")

    return mir.Function(
        name="main",
        params=[],
        return_type=INT,
        entry="bb0",
        module="<tests>",
        source=None,
        blocks={"bb0": entry, "bb_ok": ok, "bb_err": err},
    )


def _build_main_for_callee(callee_name: str, flag_value: int) -> mir.Function:
    entry = mir.BasicBlock(name="bb0", params=[])
    ok = mir.BasicBlock(name="bb_ok", params=[])
    err = mir.BasicBlock(name="bb_err", params=[])

    entry.instructions.append(mir.Const(dest="_flag", type=INT, value=flag_value))
    entry.terminator = mir.Call(
        dest="_call_res",
        callee=callee_name,
        args=["_flag"],
        ret_type=INT,
        err_dest=None,
        normal=mir.Edge(target="bb_ok", args=[]),
        error=mir.Edge(target="bb_err", args=[]),
    )

    ok.instructions.append(mir.Const(dest="_ok_exit", type=INT, value=0))
    ok.terminator = mir.Return(value="_ok_exit")

    err.instructions.append(mir.Const(dest="_err_exit", type=INT, value=1))
    err.terminator = mir.Return(value="_err_exit")

    return mir.Function(
        name="main",
        params=[],
        return_type=INT,
        entry="bb0",
        module="<tests>",
        source=None,
        blocks={"bb0": entry, "bb_ok": ok, "bb_err": err},
    )


def _compile_and_run(
    callee_fn: mir.Function, flag_value: int, tmp_path: Path, clang: str, expect_value: bool = True, label: str = "call_edges"
) -> int:
    main_fn = _build_main(flag_value, expect_value=expect_value)
    obj_path = tmp_path / f"{label}_{flag_value}.o"
    emit_module_object([callee_fn, main_fn], struct_layouts={}, entry="main", out_path=obj_path)
    exe_path = tmp_path / f"{label}_{flag_value}"
    runtime_sources = [ROOT / "tests" / "mir_codegen" / "runtime" / "error_dummy.c"]
    link_cmd = [clang, str(obj_path)] + [str(src) for src in runtime_sources] + ["-o", str(exe_path)]
    subprocess.run(link_cmd, check=True, capture_output=True, text=True)
    res = subprocess.run([str(exe_path)], capture_output=True, text=True)
    return res.returncode


def test_call_with_edges_executes_both_paths(tmp_path: Path) -> None:
    pytest.importorskip("llvmlite")
    clang = _find_clang()
    if not clang:
        pytest.skip("clang not found")

    ok_exit = _compile_and_run(_build_callee(), 0, tmp_path, clang)
    err_exit = _compile_and_run(_build_callee(), 1, tmp_path, clang)

    assert ok_exit == 0
    assert err_exit == 1


def test_call_with_edges_handles_throw(tmp_path: Path) -> None:
    pytest.importorskip("llvmlite")
    clang = _find_clang()
    if not clang:
        pytest.skip("clang not found")

    ok_exit = _compile_and_run(_build_throwing_callee(), 0, tmp_path, clang)
    err_exit = _compile_and_run(_build_throwing_callee(), 1, tmp_path, clang)

    assert ok_exit == 0
    assert err_exit == 1


def test_call_with_edges_void_error_ptr_only(tmp_path: Path) -> None:
    pytest.importorskip("llvmlite")
    clang = _find_clang()
    if not clang:
        pytest.skip("clang not found")

    ok_exit = _compile_and_run(_build_void_callee(), 0, tmp_path, clang, expect_value=False, label="call_edges_void")
    err_exit = _compile_and_run(_build_void_callee(), 1, tmp_path, clang, expect_value=False, label="call_edges_void")

    assert ok_exit == 0
    assert err_exit == 1
