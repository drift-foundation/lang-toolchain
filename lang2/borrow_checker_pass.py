#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Borrow-check pass (Phase 1/2): track moves per Place and coarse-grained loans.

Scope:
- Operates as a forward dataflow over a CFG derived from HIR.
- Tracks place states (UNINIT/VALID/MOVED) and flags use-after-move.
- Handles implicit moves for non-Copy values used by value.
- Adds explicit borrow handling (& / &mut) with shared-vs-mut conflicts, with
  coarse function-long regions and temporary-borrow dropping as an NLL
  approximation (full region analysis still TODO).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Mapping, Callable, Tuple, Set

from lang2 import stage1 as H
from lang2.borrow_checker import Place, PlaceBase, PlaceKind, PlaceState, place_from_expr, merge_place_state
from lang2.core.diagnostics import Diagnostic
from lang2.core.types_core import TypeKind, TypeTable, TypeId


@dataclass
class Terminator:
	"""CFG terminator describing control-flow edges out of a basic block."""

	kind: str  # "jump", "branch", "return", "throw"
	targets: List[int]
	cond: Optional[H.HExpr] = None
	value: Optional[H.HExpr] = None


@dataclass
class BasicBlock:
	"""Basic block of HIR statements with a single terminator."""

	id: int
	statements: List[H.HStmt] = field(default_factory=list)
	terminator: Optional[Terminator] = None


class LoanKind(Enum):
	"""Kinds of borrows supported in Phase 2."""

	SHARED = auto()
	MUT = auto()


@dataclass(frozen=True)
class Loan:
	"""A loan of a place for the lifetime of its reference (coarse-grained for now)."""

	place: Place
	kind: LoanKind
	region_id: int
	temporary: bool = False


@dataclass
class _FlowState:
	"""Dataflow state at a CFG point: place validity + active loans."""

	place_states: Dict[Place, PlaceState] = field(default_factory=dict)
	loans: Set[Loan] = field(default_factory=set)


@dataclass
class BorrowChecker:
	"""
	Phase-1/2 borrow checker: move tracking + coarse loans on typed HIR (CFG/dataflow).

	Inputs:
	- type_table: to answer Copy vs move-only.
	- fn_types: mapping var identities to TypeId (params/locals as available).
	"""

	type_table: TypeTable
	fn_types: Mapping[PlaceBase, TypeId]
	base_lookup: Callable[[object], Optional[PlaceBase]] = lambda hv: PlaceBase(
		PlaceKind.LOCAL,
		getattr(hv, "binding_id", -1) if getattr(hv, "binding_id", None) is not None else -1,
		hv.name if hasattr(hv, "name") else str(hv),
	)
	diagnostics: List[Diagnostic] = field(default_factory=list)
	enable_auto_borrow: bool = False

	def _is_copy(self, ty: Optional[TypeId]) -> bool:
		"""Return True if the type is Copy per the core type table."""
		if ty is None:
			return False
		td = self.type_table.get(ty)
		# Conservatively treat scalars and references as Copy; everything else (incl. Unknown) is move-only.
		return td.kind in (TypeKind.SCALAR, TypeKind.REF)

	def _state_for(self, state: _FlowState, place: Place) -> PlaceState:
		"""Lookup helper with UNINIT default for missing places."""
		return state.place_states.get(place, PlaceState.UNINIT)

	def _set_state(self, state: _FlowState, place: Place, value: PlaceState) -> None:
		"""Mutate the local state map for a given place."""
		state.place_states[place] = value

	def _new_region(self) -> int:
		"""Allocate a coarse region id (function-scoped today)."""
		if not hasattr(self, "_next_region"):
			self._next_region = 1  # type: ignore[attr-defined]
		rid = self._next_region  # type: ignore[attr-defined]
		self._next_region += 1  # type: ignore[attr-defined]
		return rid

	def _diagnostic(self, message: str) -> None:
		"""Append an error-level diagnostic with no span."""
		self.diagnostics.append(Diagnostic(message=message, severity="error", span=None))

	def _consume_place_use(self, state: _FlowState, place: Place) -> None:
		"""Consume a place in value position, marking moves and flagging use-after-move."""
		curr = self._state_for(state, place)
		if curr is PlaceState.MOVED:
			self._diagnostic(f"use after move of '{place.base.name}'")
			return
		ty = self.fn_types.get(place.base)
		if self._is_copy(ty):
			return
		for loan in state.loans:
			if self._places_overlap(place, loan.place):
				self._diagnostic(f"cannot move '{place.base.name}' while borrowed")
				return
		self._set_state(state, place, PlaceState.MOVED)

	def _places_overlap(self, a: Place, b: Place) -> bool:
		"""
		Conservative overlap: any projection of the same base conflicts.

		This is whole-place (base-level) overlap; field-level precision can be
		added later if the spec allows disjoint field borrows.
		"""
		return a.base == b.base

	def _eval_temporary(self, state: _FlowState, expr: H.HExpr) -> None:
		"""
		Evaluate an expression whose value does not escape (expr stmt / cond).

		New loans created during evaluation are dropped immediately to model
		temporary borrow lifetimes (coarse NLL approximation).
		"""
		before = set(state.loans)
		self._visit_expr(state, expr, as_value=True)
		new_loans = state.loans - before
		state.loans -= new_loans

	def _borrow_place(self, state: _FlowState, place: Place, kind: LoanKind, *, temporary: bool = False) -> None:
		"""
		Process a borrow of `place` with the given kind, enforcing lvalue validity
		and active-loan conflict rules.
		"""
		curr = self._state_for(state, place)
		if curr is PlaceState.MOVED or curr is PlaceState.UNINIT:
			self._diagnostic(f"cannot borrow from moved or uninitialized '{place.base.name}'")
			return
		for loan in state.loans:
			if not self._places_overlap(place, loan.place):
				continue
			if kind is LoanKind.SHARED and loan.kind is LoanKind.MUT:
				self._diagnostic(f"cannot take shared borrow while mutable borrow active on '{place.base.name}'")
				return
			if kind is LoanKind.MUT:
				self._diagnostic(f"cannot take mutable borrow while borrow active on '{place.base.name}'")
				return
		state.loans.add(Loan(place=place, kind=kind, region_id=self._new_region(), temporary=temporary))

	def _drop_overlapping_loans(self, state: _FlowState, place: Place) -> None:
		"""Remove any loans that overlap the given place (assignment invalidates borrows)."""
		state.loans = {loan for loan in state.loans if not self._places_overlap(place, loan.place)}

	def _visit_expr(self, state: _FlowState, expr: H.HExpr, *, as_value: bool = True) -> None:
		"""
		Traverse expressions and consume moves for lvalues in value position.

		This is the single place to extend when new HIR forms appear (e.g.,
		dereference or pattern matching). The walker must visit all
		subexpressions so that moves through calls, arithmetic, literals, etc.
		are properly tracked.
		"""
		if isinstance(expr, (H.HVar, H.HField, H.HIndex)):
			if as_value:
				place = place_from_expr(expr, base_lookup=self.base_lookup)
				if place is not None:
					self._consume_place_use(state, place)
			return
		if isinstance(expr, H.HBorrow):
			place = place_from_expr(expr.subject, base_lookup=self.base_lookup)
			if place is None:
				self._diagnostic("cannot borrow from a non-lvalue expression")
				return
			self._borrow_place(state, place, LoanKind.MUT if expr.is_mut else LoanKind.SHARED)
			return
		if isinstance(expr, H.HCall):
			pre_loans = set(state.loans)
			self._visit_expr(state, expr.fn, as_value=True)
			for arg in expr.args:
				if self.enable_auto_borrow:
					place = place_from_expr(arg, base_lookup=self.base_lookup)
					if place is not None:
						self._borrow_place(state, place, LoanKind.SHARED, temporary=True)
						continue
				self._visit_expr(state, arg, as_value=True)
			if self.enable_auto_borrow:
				new_loans = state.loans - pre_loans
				state.loans -= {ln for ln in new_loans if ln.temporary}
			return
		if isinstance(expr, H.HMethodCall):
			pre_loans = set(state.loans)
			if self.enable_auto_borrow:
				recv_place = place_from_expr(expr.receiver, base_lookup=self.base_lookup)
				if recv_place is not None:
					self._borrow_place(state, recv_place, LoanKind.SHARED, temporary=True)
				else:
					self._visit_expr(state, expr.receiver, as_value=True)
			else:
				self._visit_expr(state, expr.receiver, as_value=True)
			for arg in expr.args:
				if self.enable_auto_borrow:
					place = place_from_expr(arg, base_lookup=self.base_lookup)
					if place is not None:
						self._borrow_place(state, place, LoanKind.SHARED, temporary=True)
						continue
				self._visit_expr(state, arg, as_value=True)
			if self.enable_auto_borrow:
				new_loans = state.loans - pre_loans
				state.loans -= {ln for ln in new_loans if ln.temporary}
			return
		if isinstance(expr, H.HBinary):
			self._visit_expr(state, expr.left, as_value=True)
			self._visit_expr(state, expr.right, as_value=True)
			return
		if isinstance(expr, H.HUnary):
			self._visit_expr(state, expr.expr, as_value=True)
			return
		if isinstance(expr, H.HTernary):
			self._visit_expr(state, expr.cond, as_value=True)
			self._visit_expr(state, expr.then_expr, as_value=True)
			self._visit_expr(state, expr.else_expr, as_value=True)
			return
		if isinstance(expr, H.HResultOk):
			self._visit_expr(state, expr.value, as_value=True)
			return
		if isinstance(expr, H.HTryResult):
			self._visit_expr(state, expr.expr, as_value=True)
			return
		if isinstance(expr, H.HArrayLiteral):
			for el in expr.elements:
				self._visit_expr(state, el, as_value=True)
			return
		if isinstance(expr, H.HDVInit):
			for a in expr.args:
				self._visit_expr(state, a, as_value=True)
			return
		# Literals and other rvalues need no action.

	def _transfer_block(self, block: BasicBlock, in_state: _FlowState) -> _FlowState:
		"""
		Transfer function for a single basic block: walk statements and mutate
		state to produce the outgoing place-state map.
		"""
		state = _FlowState(place_states=dict(in_state.place_states), loans=set(in_state.loans))
		for stmt in block.statements:
			if isinstance(stmt, H.HLet):
				self._visit_expr(state, stmt.value, as_value=True)
				if getattr(stmt, "binding_id", None) is not None:
					base = PlaceBase(PlaceKind.LOCAL, stmt.binding_id, stmt.name)
				else:
					base = self.base_lookup(H.HVar(stmt.name))
				if base is not None:
					self._set_state(state, Place(base), PlaceState.VALID)
			elif isinstance(stmt, H.HAssign):
				self._visit_expr(state, stmt.value, as_value=True)
				tgt = place_from_expr(stmt.target, base_lookup=self.base_lookup)
				if tgt is not None:
					self._set_state(state, tgt, PlaceState.VALID)
					self._drop_overlapping_loans(state, tgt)
				else:
					self._diagnostic("assignment target is not an lvalue")
			elif isinstance(stmt, H.HReturn):
				if stmt.value is not None:
					self._eval_temporary(state, stmt.value)
			elif isinstance(stmt, H.HExprStmt):
				self._eval_temporary(state, stmt.expr)
			elif isinstance(stmt, H.HThrow):
				self._eval_temporary(state, stmt.value)
			# other stmts: continue

		# Terminator expressions
		term = block.terminator
		if term and term.kind == "branch" and term.cond is not None:
			self._eval_temporary(state, term.cond)
		# return/throw values were evaluated (and temp-borrow dropped) in the stmt loop

		return state

	def check_block(self, block: H.HBlock) -> List[Diagnostic]:
		"""Run move tracking on a HIR block by building a CFG and flowing states."""
		self.diagnostics.clear()
		blocks, entry_id = self._build_cfg(block)
		in_states: Dict[int, _FlowState] = {b.id: _FlowState() for b in blocks}
		worklist = [entry_id]
		while worklist:
			bid = worklist.pop()
			blk = blocks[bid]
			in_state = in_states[bid]
			out_state = self._transfer_block(blk, in_state)
			succs = blk.terminator.targets if blk.terminator else []
			for succ in succs:
				prev = in_states.get(succ, _FlowState())
				merged = self._merge_states(prev, out_state)
				if merged != prev:
					in_states[succ] = merged
					worklist.append(succ)
		return self.diagnostics

	def _merge_states(self, a: _FlowState, b: _FlowState) -> _FlowState:
		"""Join two place-state maps using merge_place_state as the meet operator."""
		result = _FlowState(place_states=dict(a.place_states), loans=set(a.loans))
		for place, state_b in b.place_states.items():
			state_a = result.place_states.get(place, PlaceState.UNINIT)
			if state_a is state_b:
				continue
			result.place_states[place] = merge_place_state(state_a, state_b)
		# Coarse region model: keep any loan that is active along any path (union).
		result.loans |= b.loans
		return result

	def _build_cfg(self, block: H.HBlock) -> Tuple[List[BasicBlock], int]:
		"""
		Lower a structured HIR block into a rudimentary CFG.

		Each HIf/HLoop/HTry introduces new blocks with branch/jump terminators.
		Tail statements after a control construct are placed in a continuation
		block so successors join correctly. Return/throw terminate a block with
		no successors.
		"""
		blocks: List[BasicBlock] = []

		def new_block() -> BasicBlock:
			bb = BasicBlock(id=len(blocks))
			blocks.append(bb)
			return bb

		exit_block = new_block()
		exit_block.terminator = Terminator(kind="jump", targets=[])
		exit_id = exit_block.id

		def add_backedge(body_ids: List[int], body_entry: int, exit_id: int) -> None:
			for bid in body_ids:
				bb = blocks[bid]
				if bb.terminator and exit_id in bb.terminator.targets and bb.terminator.kind == "jump":
					if body_entry not in bb.terminator.targets:
						bb.terminator.targets.append(body_entry)

		def build(stmts: List[H.HStmt], cont: int) -> Tuple[int, List[int]]:
			bb = new_block()
			ids = [bb.id]
			idx = 0
			while idx < len(stmts):
				stmt = stmts[idx]
				if isinstance(stmt, H.HIf):
					tail = stmts[idx + 1 :]
					cont_entry, cont_ids = (cont, [])
					if tail:
						cont_entry, cont_ids = build(tail, cont)
					then_entry, then_ids = build(stmt.then_block.statements, cont_entry)
					else_entry, else_ids = build(stmt.else_block.statements if stmt.else_block else [], cont_entry)
					bb.terminator = Terminator(kind="branch", targets=[then_entry, else_entry], cond=stmt.cond)
					ids.extend(then_ids + else_ids + cont_ids)
					return bb.id, ids
				if isinstance(stmt, H.HLoop):
					tail = stmts[idx + 1 :]
					cont_entry, cont_ids = (cont, [])
					if tail:
						cont_entry, cont_ids = build(tail, cont)
					body_entry, body_ids = build(stmt.body.statements, cont_entry)
					bb.terminator = Terminator(kind="branch", targets=[body_entry, cont_entry], cond=None)
					add_backedge(body_ids, body_entry, cont_entry)
					ids.extend(body_ids + cont_ids)
					return bb.id, ids
				if isinstance(stmt, H.HTry):
					tail = stmts[idx + 1 :]
					cont_entry, cont_ids = (cont, [])
					if tail:
						cont_entry, cont_ids = build(tail, cont)
					body_entry, body_ids = build(stmt.body.statements, cont_entry)
					catch_entries = []
					catch_ids: List[int] = []
					for arm in stmt.catches:
						entry, ids_arm = build(arm.block.statements, cont_entry)
						catch_entries.append(entry)
						catch_ids.extend(ids_arm)
					targets = [body_entry] + catch_entries
					bb.terminator = Terminator(kind="branch", targets=targets, cond=None)
					ids.extend(body_ids + catch_ids + cont_ids)
					return bb.id, ids
				if isinstance(stmt, (H.HReturn, H.HThrow)):
					bb.statements.append(stmt)
					bb.terminator = Terminator(kind="return" if isinstance(stmt, H.HReturn) else "throw", targets=[], value=stmt.value)
					return bb.id, ids
				bb.statements.append(stmt)
				idx += 1
			if bb.terminator is None:
				bb.terminator = Terminator(kind="jump", targets=[cont])
			return bb.id, ids

		entry, _ = build(block.statements, exit_id)
		return blocks, entry
