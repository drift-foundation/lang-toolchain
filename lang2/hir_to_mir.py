# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
HIR → MIR lowering (straight-line subset).

Pipeline placement:
  AST → HIR (sugar-free) → MIR (this file) → SSA → LLVM/obj

This module lowers sugar-free HIR into explicit MIR instructions/blocks.
For this first step, only straight-line code is supported:
  - literals, vars, unary/binary ops, field/index reads
  - let/assign/expr/return statements
Control-flow constructs (if/loop/break/continue) and calls/DV are still
NotImplemented and will be added incrementally.
"""

from __future__ import annotations

from typing import List, Set

from . import hir_nodes as H
from . import mir_nodes as M


class MirBuilder:
	"""
	Helper to construct a MIR function incrementally.

	Manages:
	- function scaffold (params, locals, blocks)
	- current block pointer
	- temp naming for intermediate values
	"""

	def __init__(self, name: str):
		entry_block = M.BasicBlock(name="entry")
		self.func = M.MirFunc(
			name=name,
			params=[],
			locals=[],
			blocks={"entry": entry_block},
			entry="entry",
		)
		self.block = entry_block
		self._temp_counter = 0
		self._locals_set: Set[M.LocalId] = set()

	def new_temp(self) -> M.ValueId:
		self._temp_counter += 1
		return f"t{self._temp_counter}"

	def emit(self, instr: M.MInstr) -> M.ValueId | None:
		self.block.instructions.append(instr)
		if hasattr(instr, "dest"):
			return getattr(instr, "dest")
		return None

	def set_terminator(self, term: M.MTerminator) -> None:
		self.block.terminator = term

	def ensure_local(self, name: M.LocalId) -> None:
		if name not in self._locals_set:
			self._locals_set.add(name)
			self.func.locals.append(name)


class HIRToMIR:
	"""
	Lower sugar-free HIR into MIR.

	For now, only straight-line constructs are handled. Control flow and calls
	will be added in later steps.
	"""

	def __init__(self, builder: MirBuilder):
		self.b = builder

	# --- Expression lowering ---

	def lower_expr(self, expr: H.HExpr) -> M.ValueId:
		method = getattr(self, f"visit_expr_{type(expr).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No MIR lowering for expr {type(expr).__name__}")
		return method(expr)

	def visit_expr_HLiteralInt(self, expr: H.HLiteralInt) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=dest, value=expr.value))
		return dest

	def visit_expr_HLiteralBool(self, expr: H.HLiteralBool) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstBool(dest=dest, value=expr.value))
		return dest

	def visit_expr_HLiteralString(self, expr: H.HLiteralString) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstString(dest=dest, value=expr.value))
		return dest

	def visit_expr_HVar(self, expr: H.HVar) -> M.ValueId:
		self.b.ensure_local(expr.name)
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=expr.name))
		return dest

	def visit_expr_HUnary(self, expr: H.HUnary) -> M.ValueId:
		operand = self.lower_expr(expr.expr)
		dest = self.b.new_temp()
		self.b.emit(M.UnaryOpInstr(dest=dest, op=expr.op, operand=operand))
		return dest

	def visit_expr_HBinary(self, expr: H.HBinary) -> M.ValueId:
		left = self.lower_expr(expr.left)
		right = self.lower_expr(expr.right)
		dest = self.b.new_temp()
		self.b.emit(M.BinaryOpInstr(dest=dest, op=expr.op, left=left, right=right))
		return dest

	def visit_expr_HField(self, expr: H.HField) -> M.ValueId:
		subject = self.lower_expr(expr.subject)
		dest = self.b.new_temp()
		self.b.emit(M.LoadField(dest=dest, subject=subject, field=expr.name))
		return dest

	def visit_expr_HIndex(self, expr: H.HIndex) -> M.ValueId:
		subject = self.lower_expr(expr.subject)
		index = self.lower_expr(expr.index)
		dest = self.b.new_temp()
		self.b.emit(M.LoadIndex(dest=dest, subject=subject, index=index))
		return dest

	# Stubs for unhandled expressions
	def visit_expr_HCall(self, expr: H.HCall) -> M.ValueId:
		raise NotImplementedError("Call lowering not implemented yet")

	def visit_expr_HMethodCall(self, expr: H.HMethodCall) -> M.ValueId:
		raise NotImplementedError("Method call lowering not implemented yet")

	def visit_expr_HDVInit(self, expr: H.HDVInit) -> M.ValueId:
		raise NotImplementedError("DV init lowering not implemented yet")

	# --- Statement lowering ---

	def lower_stmt(self, stmt: H.HStmt) -> None:
		method = getattr(self, f"visit_stmt_{type(stmt).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No MIR lowering for stmt {type(stmt).__name__}")
		method(stmt)

	def visit_stmt_HExprStmt(self, stmt: H.HExprStmt) -> None:
		# Evaluate and discard
		self.lower_expr(stmt.expr)

	def visit_stmt_HLet(self, stmt: H.HLet) -> None:
		self.b.ensure_local(stmt.name)
		val = self.lower_expr(stmt.value)
		self.b.emit(M.StoreLocal(local=stmt.name, value=val))

	def visit_stmt_HAssign(self, stmt: H.HAssign) -> None:
		val = self.lower_expr(stmt.value)
		if isinstance(stmt.target, H.HVar):
			self.b.ensure_local(stmt.target.name)
			self.b.emit(M.StoreLocal(local=stmt.target.name, value=val))
		elif isinstance(stmt.target, H.HField):
			subject = self.lower_expr(stmt.target.subject)
			self.b.emit(M.StoreField(subject=subject, field=stmt.target.name, value=val))
		elif isinstance(stmt.target, H.HIndex):
			subject = self.lower_expr(stmt.target.subject)
			index = self.lower_expr(stmt.target.index)
			self.b.emit(M.StoreIndex(subject=subject, index=index, value=val))
		else:
			raise NotImplementedError(f"Unsupported assignment target: {type(stmt.target).__name__}")

	def visit_stmt_HReturn(self, stmt: H.HReturn) -> None:
		if self.b.block.terminator is not None:
			return
		val = self.lower_expr(stmt.value) if stmt.value is not None else None
		self.b.set_terminator(M.Return(value=val))

	def visit_stmt_HBreak(self, stmt: H.HBreak) -> None:
		raise NotImplementedError("Break lowering not implemented yet")

	def visit_stmt_HContinue(self, stmt: H.HContinue) -> None:
		raise NotImplementedError("Continue lowering not implemented yet")

	def visit_stmt_HIf(self, stmt: H.HIf) -> None:
		raise NotImplementedError("If lowering not implemented yet")

	def visit_stmt_HLoop(self, stmt: H.HLoop) -> None:
		raise NotImplementedError("Loop lowering not implemented yet")


__all__ = ["MirBuilder", "HIRToMIR"]
