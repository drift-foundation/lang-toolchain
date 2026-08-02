# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Structural pin for the whole-borrow of a control-flow-value result
(work/control-flow-rvalue-ownership).

`inspect(match b { … })` materializes the match result into a borrow
temp.  The bug: in argument position the expected type of the match
collapsed to VOID, so both the match result local and the materialized
`__borrow_tmp` were typed VOID and NEVER drop-registered — the borrowed
result LEAKED (18 bytes, memcheck-only; base+ASan clean).

This pins the sound lowering directly on the MIR, independent of the
memcheck e2e lane:

  1. the match result is MOVED out of its result temp (not a shallow
     LoadLocal that would co-own the backing), and
  2. that value is stored into a `__borrow_tmp` which is registered for
     cleanup (appears in a `CleanupHook`), so the aggregate is dropped
     exactly once.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc import driftc as D
from lang.driftc.core.function_id import function_symbol
import lang.driftc.stage2.hir_to_mir as HM

_SRC = """\
module m;
struct Node { text: String, }
fn make_a() nothrow -> Node { return Node(text = "aa" + ""); }
fn make_b() nothrow -> Node { return Node(text = "bb" + ""); }
fn inspect(n: &Node) nothrow -> Int { return n.text.byte_length(); }
pub fn main() nothrow -> Int {
	return inspect(match true { true => { make_a() }, false => { make_b() } }) - 2;
}
"""


def _capture_main_mir(tmp_path: Path) -> list:
	src = tmp_path / "main.drift"
	src.write_text(_SRC)
	modules, tt, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], stdlib_root=stdlib_root(), test_build_only=True)
	assert not pdiags, [d.message for d in pdiags]
	fh, sig, _ = flatten_modules(modules)
	main_id = [i for i, s in sig.items() if i.name == "main" and not s.is_method][0]

	# The full pipeline lowers stdlib too, and MIR temp IDs / synthesized
	# local names are FUNCTION-LOCAL — so a MoveOut from one function could
	# accidentally match a StoreLocal/CleanupHook from another.  Capture ONLY
	# the emissions of the repro's `main` by gating on the lowering
	# instance's own `_current_fn_id`.
	cls = HM.HIRToMIR
	captured: list = []
	orig_init = cls.__init__

	def init2(self, *a, **k):
		orig_init(self, *a, **k)
		oe = self.b.emit

		def e2(ins):
			if getattr(self, "_current_fn_id", None) == main_id:
				captured.append(ins)
			return oe(ins)
		self.b.emit = e2
	cls.__init__ = init2
	try:
		D.compile_to_llvm_ir_for_tests(
			func_hirs=fh, signatures=sig, exc_env=exc,
			entry=function_symbol(main_id), type_table=tt,
			module_exports=mexp, module_deps=mdeps, origin_by_fn_id={},
			enforce_entrypoint=True,
			reserved_namespace_policy=D.ReservedNamespacePolicy.ALLOW_DEV)
	finally:
		cls.__init__ = orig_init
	return captured


def test_whole_match_borrow_moves_result_into_registered_borrow_temp(tmp_path) -> None:
	from lang.driftc.stage2 import MoveOut, StoreLocal
	from lang.driftc.stage2.mir_nodes import CleanupHook

	instrs = _capture_main_mir(tmp_path)

	# 1. The match result is MOVED OUT of its result temp (not LoadLocal).
	match_moveouts = [
		i for i in instrs
		if isinstance(i, MoveOut) and str(getattr(i, "local", "")).startswith("__match")
	]
	assert match_moveouts, "match result must be MoveOut of its result temp, not shallow-loaded"

	# 2. A moved match result is stored into a borrow temp...
	moved_dests = {getattr(i, "dest", None) for i in match_moveouts}
	borrow_stores = [
		i for i in instrs
		if isinstance(i, StoreLocal)
		and str(getattr(i, "local", "")).startswith("__borrow_tmp")
		and getattr(i, "value", None) in moved_dests
	]
	assert borrow_stores, "moved match result must be stored into a __borrow_tmp"
	borrow_locals = {i.local for i in borrow_stores}

	# 3. ...and that borrow temp is registered for cleanup (drops the aggregate once).
	def _hook_names(hook) -> set:
		out = set()
		for cand in getattr(hook, "candidates", []) or []:
			out.add(cand[0] if isinstance(cand, (tuple, list)) else cand)
		return out

	registered = set()
	for i in instrs:
		if isinstance(i, CleanupHook):
			registered |= _hook_names(i)
	assert borrow_locals & registered, (
		f"borrow temp holding the match result must be in a CleanupHook "
		f"(else it leaks): {borrow_locals} not in {sorted(n for n in registered if 'borrow' in str(n))}")
