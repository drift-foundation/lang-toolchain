# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""R-2 A/B rvalue-temp gate for reject-redundant-call-borrows (D5 §D).

The rule rejects the source-written `probe(&mk(sess))` spelling, so the
"explicit baseline" cannot exist as an e2e fixture. The baseline here is
PROGRAMMATIC HIR: the explicit-shape program is parsed, then every
`HBorrow.source_written` flag is cleared in the HIR — producing exactly
the compiler-synthesized borrow shape the rule permits — and driven
through the FULL pipeline (HIR → MIR → LLVM → clang link → EXECUTION),
in the plain lane and under ASan/UBSan; the memcheck lane runs the plain
binary under valgrind when available. Drop parity asserted against the
bare-spelling fixture `rvalue_arg_temp_drop_bare` (mid=0, after=1 —
scope-end drop, exactly once).

A borrow-checker-only harness does NOT satisfy this gate — the gate is
about runtime temp lifetime, so the baseline must run (ratified D5
constraint, review 2026-07-29).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_EXPLICIT_SHAPE = """\
module main;

import std.core as core;

struct Session { drops: Int }

struct Token { session: &mut Session }

implement core.Destructible for Token {
	pub fn destroy(self: Token) nothrow -> Void {
		self.session.drops = self.session.drops + 1;
	}
}

fn mk(sess: &mut Session) nothrow -> Token {
	return Token(session = sess);
}

fn probe(t: &Token) nothrow -> Int {
	return 1;
}

pub fn main() nothrow -> Int {
	var sess = Session(drops = 0);
	var mid: Int = -1;
	{
		val a = probe(&mk(sess));
		if a != 1 { return 3; }
		mid = sess.drops;
	}
	val after = sess.drops;
	if mid != 0 { return 1; }
	if after != 1 { return 2; }
	return 0;
}
"""


def _clear_source_written(node, seen: set[int]) -> int:
	"""Recursively clear HBorrow.source_written across a HIR tree."""
	from lang.driftc.stage1 import hir_nodes as H

	if id(node) in seen:
		return 0
	seen.add(id(node))
	cleared = 0
	if isinstance(node, H.HBorrow) and getattr(node, "source_written", False):
		node.source_written = False
		cleared += 1
	for field_name in getattr(node, "__dataclass_fields__", {}) or {}:
		val = getattr(node, field_name, None)
		if isinstance(val, (list, tuple)):
			for item in val:
				if hasattr(item, "__dataclass_fields__"):
					cleared += _clear_source_written(item, seen)
		elif hasattr(val, "__dataclass_fields__"):
			cleared += _clear_source_written(val, seen)
	return cleared


def _build_ir(tmp_path: Path) -> tuple[str, int]:
	"""Parse the explicit shape, clear source_written programmatically,
	compile the FULL pipeline to LLVM IR. Returns (ir, cleared_count)."""
	from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
	from lang.driftc.module_lowered import flatten_modules
	from lang.driftc import driftc as D
	from lang.driftc.core.function_id import function_symbol

	src = tmp_path / "main.drift"
	src.write_text(_EXPLICIT_SHAPE)
	modules, type_table, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], stdlib_root=stdlib_root(), test_build_only=True
	)
	assert not pdiags, [d.message for d in pdiags]
	func_hirs, signatures, _ = flatten_modules(modules)
	cleared = 0
	seen: set[int] = set()
	for fn_id, fh in func_hirs.items():
		if fn_id.module == "main":
			cleared += _clear_source_written(fh, seen)
	assert cleared == 1, f"expected to clear exactly the ONE outer explicit borrow, cleared {cleared}"
	main_id = [i for i, s in signatures.items() if i.name == "main" and not s.is_method][0]
	origin: dict = {}
	ir, checked = D.compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc,
		entry=function_symbol(main_id),
		type_table=type_table,
		module_exports=mexp,
		module_deps=mdeps,
		origin_by_fn_id=origin,
		enforce_entrypoint=True,
		reserved_namespace_policy=D.ReservedNamespacePolicy.ALLOW_DEV,
	)
	errors = [d for d in getattr(checked, "diagnostics", []) if getattr(d, "severity", None) == "error"]
	assert not errors, [d.message for d in errors]
	return ir, cleared


def _rt_archive(profile: str) -> Path:
	from lang.versions import DRIFT_RT_ABI_VERSION

	return ROOT / "build" / "runtime_libs" / profile / f"libdrift_rt_abi{DRIFT_RT_ABI_VERSION}.a"


def _link(ir: str, tmp_path: Path, *, asan: bool) -> Path:
	ll = tmp_path / ("ab_asan.ll" if asan else "ab.ll")
	ll.write_text(ir)
	out = tmp_path / ("ab_asan.bin" if asan else "ab.bin")
	cmd = ["clang", "-fuse-ld=gold"]
	profile = "default"
	if asan:
		cmd += ["-fsanitize=address", "-g", "-fsanitize=undefined", "-fno-sanitize-recover=undefined"]
		profile = "asan_ubsan"
	cmd += ["-O2", "-x", "ir", str(ll), "-x", "none", str(_rt_archive(profile)), "-lz", "-Wl,--as-needed", "-o", str(out)]
	res = subprocess.run(cmd, capture_output=True, text=True, timeout=sanitizer_timeout(240))
	assert res.returncode == 0, f"link failed:\n{res.stderr[-1200:]}"
	return out


def test_programmatic_baseline_full_pipeline_plain_and_memcheck(tmp_path: Path) -> None:
	"""Baseline (source_written=False, explicit shape) compiles through
	the full pipeline and EXECUTES with the pinned drop behavior; also
	run under valgrind when available (memcheck half of the gate)."""
	ir, _ = _build_ir(tmp_path)
	binary = _link(ir, tmp_path, asan=False)
	run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, f"baseline drop parity broken (exit {run.returncode})"
	if shutil.which("valgrind"):
		vg = subprocess.run(
			["valgrind", "--error-exitcode=97", "--leak-check=full", "--errors-for-leak-kinds=definite", str(binary)],
			capture_output=True, text=True, timeout=sanitizer_timeout(240),
		)
		assert vg.returncode == 0, f"valgrind: exit {vg.returncode}\n{vg.stderr[-1200:]}"


def test_programmatic_baseline_full_pipeline_asan(tmp_path: Path) -> None:
	"""ASan/UBSan half of the gate for the same baseline binary."""
	archive = _rt_archive("asan_ubsan")
	if not archive.exists():
		import pytest

		pytest.skip(f"asan runtime archive not built: {archive}")
	ir, _ = _build_ir(tmp_path)
	binary = _link(ir, tmp_path, asan=True)
	run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=sanitizer_timeout(60))
	assert run.returncode == 0, run.stderr[-1200:]
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-1200:]


def test_explicit_source_shape_is_rejected(tmp_path: Path) -> None:
	"""The A-half sanity: the SAME program with source_written intact
	(i.e. compiled from source) is rejected by the rule — proving the
	baseline really is only reachable programmatically."""
	src = tmp_path / "main.drift"
	src.write_text(_EXPLICIT_SHAPE)
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(tmp_path / "x.bin")],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode != 0
	assert "E_REDUNDANT_ARG_BORROW" in (res.stderr + res.stdout)
