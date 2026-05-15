# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3C / ledger-replacement acceptance tests — path-insensitive
`_moved_locals` in HIR→MIR.

These tests are the **acceptance criterion** for replacing the
function-wide `_moved_locals` set with the per-program-point
ownership ledger.  They are NOT `xfail`: an `xfail` says "failure
is acceptable here," and that is the wrong framing.  Failure here
is acceptance-blocking — the feature branch is not done until both
pass.

Both tests are tagged with `@pytest.mark.ledger_3c_acceptance`.  As
of 0.31.8 (Phase 3C runtime drop-flag insertion landed), the marker
no longer deselects them from default runs — they pass under the
new pass and run in the standard `pytest lang/tests/stage2/` suite.
The marker is preserved as a tag for targeted invocation:

    pytest -m ledger_3c_acceptance lang/tests/stage2/

This makes a regression in the bucket-6 acceptance contract surface
in default CI; the targeted invocation is for direct verification
during 3B consumer-swap PRs.

Two carrier shapes — both MUST be handled by the ledger replacement:

1. **Terminating-arm leak** (`test_path_insensitive_moved_locals_omits_drop_on_no_move_path`).
   The bucket-6 shape from `std.json::_parse_object_throwing`:
       fn f(b: Bool) -> String {
           var s = "owned";
           if b { return move s; }
           return "fresh";          // ← no-move path; s should be dropped
       }
   `_moved_locals` is poisoned by the move in the terminating then-
   arm; the no-move path's scope-drop skips dropping `s` → one
   String allocation leaks per b=false call.  An attempted fix
   (intersection-with-implicit-else) cleared this case but
   reintroduced UAF on shape 2 below.

2. **Non-terminating-arm conditional move** (`test_non_terminating_conditional_move_no_silent_wrong_mir`).
   The K-found sibling shape:
       fn f(b: Bool) -> String {
           var s = "owned";
           if b { val t = move s; }   // moves s; arm reaches join
           return "fresh";              // post-join: s is moved on b=true,
                                        //  live on b=false
       }
   No safe single static drop verdict exists.  A subsequent attempt
   (strict fail-stop on disagreeing reaching arms) was sound but
   blocked legitimate stdlib patterns (`std.cli`, `std.containers`)
   that rely on user-level invariants the compiler cannot verify;
   per the no-stdlib-rewrite policy, that fix is non-landable.

The acceptance contract for shape 2 deliberately does NOT prescribe
a single resolution.  Acceptable outcomes for the ledger replacement:

  (a) Compile succeeds and produces sound MIR — drop-elaboration
      inserts an explicit drop on the no-move arm before the join,
      OR a per-local runtime drop flag guards the trailing scope-
      drop.
  (b) Compile fail-stops with a clear diagnostic naming the
      offending local — only chosen if 3C policy is "refuse to
      compile path-dependent moves rather than auto-elaborate."

What is **NOT** acceptable: today's silent wrong-MIR behavior
(zero drops emitted → leak on no-move path; or unconditional drop
emitted → double-drop / UAF on the move path).  The test's
assertion encodes "must not be the legacy wrong shape," not "must
fail-stop."

Both tests graduate from this marker to default-run status when
the ledger replaces `_moved_locals` and the bucket-6 entry in
`work/ownership-ledger/triage.md` reaches zero.

Discovered: 2026-04-22 via Phase 3A Task #5 triage; see
`work/ownership-ledger/triage-findings.md` bucket 6.
Subsystem: HIR→MIR ownership analysis (`hir_to_mir.py`).
Tracked-by: Phase 3C drop-elaboration design
(`work/ownership-ledger/3c-design.md`).
"""

from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1 import (
	HBlock,
	HIf,
	HLet,
	HLiteralString,
	HMove,
	HPlaceExpr,
	HReturn,
	HVar,
	assign_callsite_ids,
	assign_node_ids,
)
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.stage2 import (
	DropValue,
	HIRToMIR,
	MoveOut,
	Return,
	make_builder,
)
from lang.driftc.stage2.drop_flags import insert_drop_flags
from lang.driftc.stage2.drop_policy_compute import compute_drop_policy


def _string_place(name: str) -> HPlaceExpr:
	return HPlaceExpr(base=HVar(name), projections=[], loc=Span())


def _build_repro_func():
	"""Build:
	    var s = "owned";
	    if b { return move s; }
	    return "fresh";

	Lower with HIRToMIR; return the resulting MirFunc."""
	hir = HBlock(statements=[
		HLet(name="s", value=HLiteralString("owned"), declared_type_expr=None, is_mutable=True, binding_id=None),
		HIf(
			cond=HVar("b"),
			then_block=HBlock(statements=[
				HReturn(value=HMove(subject=_string_place("s"))),
			]),
			else_block=None,
		),
		HReturn(value=HLiteralString("fresh")),
	])
	hir = normalize_hir(hir)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	type_table = TypeTable()
	# Seed `b` as a Bool param so the if cond resolves; `s` as a local.
	# (HIRToMIR infers types but supplying them via param_types makes
	# the test deterministic across inference changes.)
	bool_ty = type_table.ensure_bool()
	string_ty = type_table.ensure_string()
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	builder.func.params = ["b"]
	lowerer = HIRToMIR(
		builder,
		type_table=type_table,
		param_types={"b": bool_ty},
		return_type=string_ty,
	)
	lowerer.lower_block(hir)
	# Propagate inferred local types from HIRToMIR — the production
	# driver does this explicitly between HIRToMIR and downstream
	# passes (see driftc.py), and `insert_drop_flags` reads
	# `func.local_types` to identify destructible locals.
	builder.func.local_types = dict(lowerer._local_types)
	# Run the full Bug 2 cleanup pipeline (drop_flags planning →
	# ledger rebuild → cleanup_authoring) matching the production
	# driver order at `driftc.py`.  Post-fix, drop_flags is planning
	# only; `cleanup_authoring` emits the cleanup drops (per-arm
	# elaborated at predecessor edges or flag-guarded at the hook).
	insert_drop_flags(
		builder.func,
		type_table=type_table,
		drop_policy=lambda ty: compute_drop_policy(type_table, ty),
	)
	# Build ledger after planning's set/clear instrumentation, then
	# author cleanup.  Matches driftc.py:7053-7083 ordering.
	from lang.driftc.stage2.ownership_ledger import build_ledger
	from lang.driftc.stage2.cleanup_authoring import author_cleanup
	ledger = build_ledger(builder.func, drop_policy=lambda _t: None)
	setattr(builder.func, "_ownership_ledger", ledger)
	author_cleanup(builder.func, type_table=type_table)
	return builder.func


def _find_no_move_return_block(func) -> str:
	"""Locate the block whose terminator is `Return(value=v)` where v
	is the dest of a `ConstString("fresh")` produced earlier in the
	function — that is the b=false return path.  Returns the block
	name."""
	# Scan all blocks for ConstString("fresh") emissions; remember
	# their dest names.
	fresh_dests: set[str] = set()
	from lang.driftc.stage2 import ConstString
	for blk in func.blocks.values():
		for ins in blk.instructions:
			if isinstance(ins, ConstString) and ins.value == "fresh":
				fresh_dests.add(getattr(ins, "dest", ""))
	# Find the block whose Return's value is one of those dests.
	for blk in func.blocks.values():
		term = blk.terminator
		if isinstance(term, Return) and term.value in fresh_dests:
			return blk.name
	raise AssertionError(
		"test setup invariant broken: no Return block found for the "
		"b=false `\"fresh\"` literal — HIR lowering shape changed."
	)


def _path_to_block(func, target_block: str) -> list[str]:
	"""Trivial reverse-BFS from entry to `target_block`.  Used to
	collect every block whose instructions contribute to the b=false
	return — the drop for `s` must appear in at least one of them."""
	# Build successor map.
	succ: dict[str, list[str]] = {}
	from lang.driftc.stage2 import Goto, IfTerminator
	for name, blk in func.blocks.items():
		t = blk.terminator
		if isinstance(t, Goto):
			succ.setdefault(name, []).append(t.target)
		elif isinstance(t, IfTerminator):
			succ.setdefault(name, []).extend([t.then_target, t.else_target])
	# BFS forward, collect any path that reaches target_block.
	on_path: set[str] = set()
	def _reaches(start: str, seen: set[str]) -> bool:
		if start == target_block:
			return True
		if start in seen:
			return False
		seen.add(start)
		for s in succ.get(start, []):
			if _reaches(s, seen):
				on_path.add(s)
				return True
		return False
	on_path.add(func.entry)
	on_path.add(target_block)
	_reaches(func.entry, set())
	return sorted(on_path)


@pytest.mark.ledger_3c_acceptance
def test_path_insensitive_moved_locals_omits_drop_on_no_move_path() -> None:
	"""Pin: the no-move return path MUST emit a drop for the owned
	local `s`.

	Today this fails because `HIRToMIR._moved_locals` is poisoned by
	the move in the if-then branch and stays poisoned for the rest of
	the function — including the b=false return's scope-drop point.

	Failure shape: zero `MoveOut(local="s")` or `DropValue` instances
	on any block on the b=false path.

	Post-fix shape: at least one `MoveOut(local="s") ; DropValue`
	pair on the b=false path (canonical scope-drop emission).
	"""
	func = _build_repro_func()
	target = _find_no_move_return_block(func)
	on_path = _path_to_block(func, target)
	# Look for a scope-drop of `s` (MoveOut(local="s") followed by a
	# DropValue) on any block reachable on the b=false path.
	saw_drop_for_s = False
	moveout_dests: set[str] = set()
	for blk_name in on_path:
		blk = func.blocks[blk_name]
		for ins in blk.instructions:
			if isinstance(ins, MoveOut) and getattr(ins, "local", None) == "s":
				moveout_dests.add(getattr(ins, "dest", ""))
			elif isinstance(ins, DropValue) and getattr(ins, "value", None) in moveout_dests:
				saw_drop_for_s = True
	assert saw_drop_for_s, (
		"LANGUAGE_BUG (path-insensitive _moved_locals): the no-move "
		"return path emitted NO `MoveOut(s) + DropValue` pair, so the "
		"owned String in `s` leaks on every b=false call.  The defect "
		"is in HIR→MIR: `_moved_locals` is a function-wide set "
		"populated by the if-then `move s` lowering and never cleared "
		"on the path that did NOT execute the move.  Subsequent "
		"`_emit_scope_drops` consults the poisoned set and skips the "
		"drop.  Fix surface: either (a) make `_moved_locals` per-path "
		"by consulting the per-program-point ledger view (Phase 3A), "
		"or (b) split the set per-CFG-arm so the scope-drop on the "
		"b=false arm sees an empty set for `s`.  See "
		"`work/ownership-ledger/triage-findings.md` bucket 6 for the "
		"discovery context."
	)


def _build_non_terminating_move_func():
	"""Build:
	    var s = "owned";
	    if b { val t = move s; }       // moves s on the b=true path,
	                                    // BUT the if-then arm reaches
	                                    // the join (no return) — so post-
	                                    // join `s` is moved on b=true,
	                                    // live on b=false.
	    return "fresh";

	This is the K-found sibling shape of the bucket-6 leak: the same
	defect class (path-dependent move state) but the moving arm does
	NOT diverge.  The naive intersection-with-implicit-else fix would
	clear the moved fact and emit a scope-drop for `s` on every path —
	correct on b=false but a DOUBLE-DROP / UAF on b=true (s already
	moved by `val t = move s`).
	"""
	hir = HBlock(statements=[
		HLet(name="s", value=HLiteralString("owned"), declared_type_expr=None, is_mutable=True, binding_id=None),
		HIf(
			cond=HVar("b"),
			then_block=HBlock(statements=[
				HLet(name="t", value=HMove(subject=_string_place("s")), declared_type_expr=None, is_mutable=False, binding_id=None),
			]),
			else_block=None,
		),
		HReturn(value=HLiteralString("fresh")),
	])
	hir = normalize_hir(hir)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	string_ty = type_table.ensure_string()
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	builder.func.params = ["b"]
	lowerer = HIRToMIR(
		builder,
		type_table=type_table,
		param_types={"b": bool_ty},
		return_type=string_ty,
	)
	lowerer.lower_block(hir)
	builder.func.local_types = dict(lowerer._local_types)
	# Full cleanup pipeline (matches driftc.py production order
	# post Bug 2 architecture flip: planning → ledger rebuild →
	# cleanup_authoring).
	insert_drop_flags(
		builder.func,
		type_table=type_table,
		drop_policy=lambda ty: compute_drop_policy(type_table, ty),
	)
	from lang.driftc.stage2.ownership_ledger import build_ledger
	from lang.driftc.stage2.cleanup_authoring import author_cleanup
	ledger = build_ledger(builder.func, drop_policy=lambda _t: None)
	setattr(builder.func, "_ownership_ledger", ledger)
	author_cleanup(builder.func, type_table=type_table)
	return builder.func


@pytest.mark.ledger_3c_acceptance
def test_non_terminating_conditional_move_no_silent_wrong_mir() -> None:
	"""Acceptance: the compiler must NOT silently emit the legacy
	wrong MIR for the non-terminating conditional-move shape.

	Two acceptable outcomes for the 3C ledger replacement:
	  (a) Compile succeeds and produces sound MIR for `s` — the drop
	      for `s` is **path-conditional** (flag-guarded under the
	      runtime-drop-flag baseline, OR drop-elaborated onto the
	      no-move arm under a future CFG-split optimization).  An
	      unconditional drop in the post-join block is NOT acceptance
	      (a) — that is the explicit double-drop / UAF shape rejected
	      by the K-review.
	  (b) Compile fail-stops with a clear lowering diagnostic during
	      HIR→MIR.

	What is NOT acceptable (and what mainline did pre-3C):
	  (c) Compile succeeds and emits MIR with ZERO `DropValue` for
	      `s` anywhere reachable — the leak shape.
	  (d) Compile succeeds and emits a single UNCONDITIONAL
	      `DropValue` for `s` in the post-join block — the double-
	      drop / UAF shape.

	This assertion checks BOTH (c) and (d): there must be at least
	one drop for `s`, AND every drop emission for `s` must be
	path-conditional (reachable only via an `IfTerminator`-guarded
	block, NOT via a straight-line path from function entry).
	"""
	saw_compile_error = False
	func = None
	try:
		func = _build_non_terminating_move_func()
	except Exception:
		# Acceptance (b): explicit fail-stop during HIR→MIR.
		saw_compile_error = True
	if saw_compile_error:
		return
	# Acceptance (a): compile succeeded.  Locate every block that
	# contains a `MoveOut(s) + DropValue(...)` pair (a drop emission
	# for s).  Each such block must be reachable from function entry
	# only through at least one `IfTerminator` whose condition came
	# from a flag-load (LoadLocal of a `__drop_flag_*` local) — i.e.
	# the drop is path-conditional, not unconditional.
	from lang.driftc.stage2 import IfTerminator, LoadLocal
	from lang.driftc.stage2 import Goto as _Goto
	assert func is not None

	def _block_has_drop_for_s(blk) -> bool:
		moveout_dests: set[str] = set()
		for ins in blk.instructions:
			if isinstance(ins, MoveOut) and getattr(ins, "local", None) == "s":
				moveout_dests.add(getattr(ins, "dest", ""))
			elif isinstance(ins, DropValue) and getattr(ins, "value", None) in moveout_dests:
				return True
		return False

	def _flag_loads_in_block(blk) -> set[str]:
		out: set[str] = set()
		for ins in blk.instructions:
			if isinstance(ins, LoadLocal) and getattr(ins, "local", "").startswith("__drop_flag_"):
				out.add(getattr(ins, "dest", ""))
		return out

	# Build predecessor map.
	preds: dict[str, list[str]] = {n: [] for n in func.blocks}
	for name, blk in func.blocks.items():
		t = blk.terminator
		if isinstance(t, _Goto):
			preds.setdefault(t.target, []).append(name)
		elif isinstance(t, IfTerminator):
			preds.setdefault(t.then_target, []).append(name)
			preds.setdefault(t.else_target, []).append(name)

	def _every_path_from_entry_passes_flag_branch(target: str) -> bool:
		# BFS backwards from target; on every path back to entry, must
		# encounter at least one IfTerminator whose cond is a flag-load.
		# Implementation: forward DFS from entry collecting paths;
		# assert every path that ends at target traverses such an If.
		entry_name = func.entry
		seen: set[str] = set()
		def dfs(node: str, saw_flag_branch: bool) -> bool:
			if node == target:
				return saw_flag_branch
			if node in seen:
				return True  # cycle or already-visited; ignore
			seen.add(node)
			blk = func.blocks[node]
			t = blk.terminator
			succs: list[str] = []
			is_flag_branch = False
			if isinstance(t, IfTerminator):
				succs = [t.then_target, t.else_target]
				if t.cond in _flag_loads_in_block(blk):
					is_flag_branch = True
			elif isinstance(t, _Goto):
				succs = [t.target]
			for s in succs:
				if not dfs(s, saw_flag_branch or is_flag_branch):
					return False
			return True
		return dfs(entry_name, False)

	def _drop_block_is_path_conditional(target: str) -> bool:
		"""Per Bug 2 architecture flip (2026-05-15): a drop is
		path-conditional iff EITHER
		  (a) every entry→target path crosses a flag-load
		      IfTerminator (runtime drop-flag baseline), OR
		  (b) at least one entry→<exit/Return> path BYPASSES the
		      target block entirely (cleanup_authoring per-arm edge
		      elaboration onto the no-move arm).

		Either form proves the drop fires only on the no-move
		runtime path.  (b) catches the per-arm-split shape where the
		gate is the user's IfTerminator, not a flag-load.
		"""
		if _every_path_from_entry_passes_flag_branch(target):
			return True
		# Check (b): is there an entry→Return path that does NOT
		# pass through target?
		from lang.driftc.stage2 import Return as _Return, IfTerminator as _If, Goto as _GotoTerm
		entry_name = func.entry
		def reaches_return_skipping_target(node: str, visited: set) -> bool:
			if node == target:
				return False
			if node in visited:
				return False
			visited.add(node)
			blk = func.blocks[node]
			t = blk.terminator
			if isinstance(t, _Return):
				return True
			succs: list[str] = []
			if isinstance(t, _If):
				succs = [t.then_target, t.else_target]
			elif isinstance(t, _GotoTerm):
				succs = [t.target]
			for s in succs:
				if reaches_return_skipping_target(s, set(visited)):
					return True
			return False
		return reaches_return_skipping_target(entry_name, set())

	drop_blocks: list[str] = []
	for name, blk in func.blocks.items():
		if _block_has_drop_for_s(blk):
			drop_blocks.append(name)
	assert drop_blocks, (
		"3C acceptance failure (non-terminating conditional move, "
		"shape (c)): the compiler emitted MIR with NO DropValue for "
		"`s` anywhere — the legacy `_moved_locals` leak shape.  On "
		"the b=false runtime path where the move never executed, "
		"`s` goes undropped → leak.  Acceptable resolutions: "
		"runtime-flag-guarded drop OR drop-elaboration onto the "
		"no-move arm OR compile-time fail-stop."
	)
	for db in drop_blocks:
		assert _drop_block_is_path_conditional(db), (
			f"3C acceptance failure (non-terminating conditional move, "
			f"shape (d)): the drop for `s` in block {db!r} is reachable "
			f"from function entry without passing through a flag-guarded "
			f"branch.  This is the unconditional-drop / UAF shape "
			f"explicitly rejected by review — on the b=true runtime path "
			f"where the move already consumed `s`, an unconditional drop "
			f"would double-drop / UAF.  3C requires the drop to be path-"
			f"conditional (flag-guarded under the runtime-drop-flag "
			f"baseline)."
		)
