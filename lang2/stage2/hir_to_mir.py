# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
HIR → MIR lowering (expressions/statements, if/loop).

Pipeline placement:
  AST (lang2/stage0/ast.py) → HIR (lang2/stage1/hir_nodes.py) → MIR (this file) → SSA → LLVM/obj

This module lowers sugar-free HIR into explicit MIR instructions/blocks.
Currently supported:
  - literals, vars, unary/binary ops, field/index reads
  - let/assign/expr/return statements
  - `if` with then/else/join blocks
  - `loop` with break/continue
  - plain calls, method calls, DV construction
Remaining TODO: try/ternary/exception sugar and any complex call shapes beyond
direct names/receivers.
"""

from __future__ import annotations

from typing import List, Set

from .. import stage1 as H
from . import mir_nodes as M


class MirBuilder:
	"""
	Helper to construct a MIR function incrementally.

	Manages:
	- function scaffold (params, locals, blocks)
	- current block pointer
	- temp naming for intermediate values

	Entry point for this stage:
	  - build a MirBuilder with the function name
	  - use HIRToMIR to populate it
	  - read out builder.func when done
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

	def new_block(self, name_hint: str) -> M.BasicBlock:
		"""
		Create a new basic block with a unique name derived from name_hint.
		Caller is responsible for setting it as current via set_block.
		"""
		base = name_hint
		suffix = 0
		name = base
		while name in self.func.blocks:
			suffix += 1
			name = f"{base}{suffix}"
		block = M.BasicBlock(name=name)
		self.func.blocks[name] = block
		return block

	def set_block(self, block: M.BasicBlock) -> None:
		"""Switch the current insertion block."""
		self.block = block


class HIRToMIR:
	"""
	Lower sugar-free HIR into MIR.

	For now, straight-line constructs and `if` are handled. Loops, break/continue,
	and calls will be added in later steps.

	Entry points (stage API):
	  - lower_expr: lower a single HIR expression to a MIR ValueId
	  - lower_stmt: lower a single HIR statement, appending MIR to the builder
	  - lower_block: lower an HIR block (list of statements)
	Helper visitors are prefixed with an underscore; public surface is the
	lower_* methods above.
	"""

	def __init__(self, builder: MirBuilder):
		self.b = builder
		# Stack of (continue_target, break_target) block names for nested loops.
		self._loop_stack: list[tuple[str, str]] = []

	# --- Expression lowering ---

	def lower_expr(self, expr: H.HExpr) -> M.ValueId:
		method = getattr(self, f"_visit_expr_{type(expr).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No MIR lowering for expr {type(expr).__name__}")
		return method(expr)

	def _visit_expr_HLiteralInt(self, expr: H.HLiteralInt) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=dest, value=expr.value))
		return dest

	def _visit_expr_HLiteralBool(self, expr: H.HLiteralBool) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstBool(dest=dest, value=expr.value))
		return dest

	def _visit_expr_HLiteralString(self, expr: H.HLiteralString) -> M.ValueId:
		dest = self.b.new_temp()
		self.b.emit(M.ConstString(dest=dest, value=expr.value))
		return dest

	def _visit_expr_HVar(self, expr: H.HVar) -> M.ValueId:
		self.b.ensure_local(expr.name)
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=expr.name))
		return dest

	def _visit_expr_HUnary(self, expr: H.HUnary) -> M.ValueId:
		operand = self.lower_expr(expr.expr)
		dest = self.b.new_temp()
		self.b.emit(M.UnaryOpInstr(dest=dest, op=expr.op, operand=operand))
		return dest

	def _visit_expr_HBinary(self, expr: H.HBinary) -> M.ValueId:
		left = self.lower_expr(expr.left)
		right = self.lower_expr(expr.right)
		dest = self.b.new_temp()
		self.b.emit(M.BinaryOpInstr(dest=dest, op=expr.op, left=left, right=right))
		return dest

	def _visit_expr_HField(self, expr: H.HField) -> M.ValueId:
		subject = self.lower_expr(expr.subject)
		dest = self.b.new_temp()
		self.b.emit(M.LoadField(dest=dest, subject=subject, field=expr.name))
		return dest

	def _visit_expr_HIndex(self, expr: H.HIndex) -> M.ValueId:
		subject = self.lower_expr(expr.subject)
		index = self.lower_expr(expr.index)
		dest = self.b.new_temp()
		self.b.emit(M.LoadIndex(dest=dest, subject=subject, index=index))
		return dest

	# Stubs for unhandled expressions
	def _visit_expr_HCall(self, expr: H.HCall) -> M.ValueId:
		"""
		Plain function call. For now only direct function names are supported;
		indirect/function-valued calls will be added later if needed.
		"""
		if not isinstance(expr.fn, H.HVar):
			raise NotImplementedError("Only direct function-name calls are supported in MIR lowering")
		arg_vals = [self.lower_expr(a) for a in expr.args]
		dest = self.b.new_temp()
		self.b.emit(M.Call(dest=dest, fn=expr.fn.name, args=arg_vals))
		return dest

	def _visit_expr_HMethodCall(self, expr: H.HMethodCall) -> M.ValueId:
		receiver = self.lower_expr(expr.receiver)
		arg_vals = [self.lower_expr(a) for a in expr.args]
		dest = self.b.new_temp()
		self.b.emit(
			M.MethodCall(dest=dest, receiver=receiver, method_name=expr.method_name, args=arg_vals)
		)
		return dest

	def _visit_expr_HDVInit(self, expr: H.HDVInit) -> M.ValueId:
		arg_vals = [self.lower_expr(a) for a in expr.args]
		dest = self.b.new_temp()
		self.b.emit(M.ConstructDV(dest=dest, dv_type_name=expr.dv_type_name, args=arg_vals))
		return dest

	# --- Statement lowering ---

	def lower_stmt(self, stmt: H.HStmt) -> None:
		method = getattr(self, f"_visit_stmt_{type(stmt).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No MIR lowering for stmt {type(stmt).__name__}")
		method(stmt)

	def lower_block(self, block: H.HBlock) -> None:
		for stmt in block.statements:
			self.lower_stmt(stmt)

	def _visit_stmt_HExprStmt(self, stmt: H.HExprStmt) -> None:
		# Evaluate and discard
		self.lower_expr(stmt.expr)

	def _visit_stmt_HLet(self, stmt: H.HLet) -> None:
		self.b.ensure_local(stmt.name)
		val = self.lower_expr(stmt.value)
		self.b.emit(M.StoreLocal(local=stmt.name, value=val))

	def _visit_stmt_HAssign(self, stmt: H.HAssign) -> None:
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

	def _visit_stmt_HReturn(self, stmt: H.HReturn) -> None:
		if self.b.block.terminator is not None:
			return
		val = self.lower_expr(stmt.value) if stmt.value is not None else None
		self.b.set_terminator(M.Return(value=val))

	def _visit_stmt_HBreak(self, stmt: H.HBreak) -> None:
		# Break jumps to the innermost loop's break target.
		if not self._loop_stack:
			raise NotImplementedError("break outside of loop not supported yet")
		_, break_target = self._loop_stack[-1]
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=break_target))

	def _visit_stmt_HContinue(self, stmt: H.HContinue) -> None:
		# Continue jumps to the innermost loop's continue target (loop header).
		if not self._loop_stack:
			raise NotImplementedError("continue outside of loop not supported yet")
		continue_target, _ = self._loop_stack[-1]
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=continue_target))

	def _visit_stmt_HIf(self, stmt: H.HIf) -> None:
		# If the current block already ended, do nothing.
		if self.b.block.terminator is not None:
			return

		# 1) Evaluate condition in the current block.
		cond_val = self.lower_expr(stmt.cond)

		# 2) Create then/else/join blocks.
		then_block = self.b.new_block("if_then")
		else_block = self.b.new_block("if_else") if stmt.else_block is not None else None
		join_block = self.b.new_block("if_join")

		# 3) Emit conditional terminator on current block.
		then_target = then_block.name
		else_target = else_block.name if else_block is not None else join_block.name
		self.b.set_terminator(
			M.IfTerminator(cond=cond_val, then_target=then_target, else_target=else_target)
		)

		# 4) Lower then block.
		self.b.set_block(then_block)
		self.lower_block(stmt.then_block)
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=join_block.name))

		# 5) Lower else block if present.
		if else_block is not None:
			self.b.set_block(else_block)
			self.lower_block(stmt.else_block)
			if self.b.block.terminator is None:
				self.b.set_terminator(M.Goto(target=join_block.name))

		# 6) Continue in join block.
		self.b.set_block(join_block)

	def _visit_stmt_HLoop(self, stmt: H.HLoop) -> None:
		# If the current block already ended, do nothing.
		if self.b.block.terminator is not None:
			return

		# Create loop blocks.
		header = self.b.new_block("loop_header")
		body = self.b.new_block("loop_body")
		exit_block = self.b.new_block("loop_exit")

		# Jump from current block to loop header.
		self.b.set_terminator(M.Goto(target=header.name))

		# Record loop context: continue -> header, break -> exit.
		self._loop_stack.append((header.name, exit_block.name))

		# Header: fall through to body.
		self.b.set_block(header)
		self.b.set_terminator(M.Goto(target=body.name))

		# Body: lower statements.
		self.b.set_block(body)
		self.lower_block(stmt.body)
		if self.b.block.terminator is None:
			# If body falls through, loop back.
			self.b.set_terminator(M.Goto(target=header.name))

		# Pop loop context and continue in exit block.
		self._loop_stack.pop()
		self.b.set_block(exit_block)


__all__ = ["MirBuilder", "HIRToMIR"]
