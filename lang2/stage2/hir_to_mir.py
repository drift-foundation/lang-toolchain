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
	- ternary expressions (diamond CFG + hidden temp)
	- `throw` lowered to Error/ResultErr + return, with try-stack routing to the
    nearest catch block (event codes from optional exception metadata)
	- `try` with multiple catch arms: dispatch block compares `ErrorEvent` codes
    against per-arm constants (from the optional exception env; fallback 0),
    jumps to matching catch/catch-all, rethrows as FnResult.Err when nothing
    matches
 Remaining TODO: rethrow/result-driven try sugar and any complex call
 names/receivers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Set, Mapping

from lang2 import stage1 as H
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
		"""Allocate a fresh temporary ValueId for intermediate results."""
		self._temp_counter += 1
		return f"t{self._temp_counter}"

	def emit(self, instr: M.MInstr) -> M.ValueId | None:
		"""
		Append a MIR instruction to the current block and return its dest, if any.
		"""
		self.block.instructions.append(instr)
		if hasattr(instr, "dest"):
			return getattr(instr, "dest")
		return None

	def set_terminator(self, term: M.MTerminator) -> None:
		"""Set the terminator for the current block."""
		self.block.terminator = term

	def ensure_local(self, name: M.LocalId) -> None:
		"""Record a local name on the function if it hasn't been seen yet."""
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
	Lower sugar-free HIR into MIR using per-node visitors.

	Supported constructs:
	  - literals, vars, unary/binary ops, field/index reads
	  - let/assign/expr/return
	  - `if` with then/else/join
	  - `loop` with break/continue
	  - plain calls, method calls, DV construction
	  - ternary expressions (diamond CFG + hidden temp)
	  - `throw` → ConstructError + ResultErr + Return, with try-stack routing
	  - `try` with multiple catch arms (dispatch via ErrorEvent codes, catch-all,
	    rethrow as FnResult.Err when no arm matches)

	Entry points (stage API):
	  - lower_expr: lower a single HIR expression to a MIR ValueId
	  - lower_stmt: lower a single HIR statement, appending MIR to the builder
	  - lower_block: lower an HIR block (list of statements)
	Helper visitors are prefixed with an underscore; public surface is the
	lower_* methods above.
	"""

	def __init__(self, builder: MirBuilder, exc_env: Mapping[str, int] | None = None):
		"""
		Create a lowering context.

		`exc_env` (optional) maps DV/exception type names to event codes so
		throw lowering can emit real codes instead of placeholders.
		"""
		self.b = builder
		# Stack of (continue_target, break_target) block names for nested loops.
		self._loop_stack: list[tuple[str, str]] = []
		# Stack of try contexts for nested try/catch (innermost on top).
		self._try_stack: list["_TryCtx"] = []
		# Optional exception environment: maps DV/exception type name -> event code.
		self._exc_env = exc_env

	# --- Expression lowering ---

	def lower_expr(self, expr: H.HExpr) -> M.ValueId:
		"""
		Entry point: lower a single HIR expression to a MIR ValueId.

		Dispatches to a private _visit_expr_* helper. Public stage API: callers
		should only invoke lower_expr/stmt/block; helpers stay private.
		"""
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

	def _visit_expr_HTernary(self, expr: H.HTernary) -> M.ValueId:
		"""
		Lower ternary expression by building a diamond CFG that stores into a
		hidden local and reloads it at the join. SSA will place φs as needed.
		"""
		# Allocate a hidden local for the ternary result.
		temp_local = f"__tern_tmp{self.b.new_temp()}"
		self.b.ensure_local(temp_local)

		# Evaluate condition in the current block.
		cond_val = self.lower_expr(expr.cond)

		# Create then/else/join blocks.
		then_block = self.b.new_block("tern_then")
		else_block = self.b.new_block("tern_else")
		join_block = self.b.new_block("tern_join")

		# Branch on condition from the current block.
		self.b.set_terminator(
			M.IfTerminator(cond=cond_val, then_target=then_block.name, else_target=else_block.name)
		)

		# Then branch: compute then_expr, store to temp, jump to join.
		self.b.set_block(then_block)
		then_val = self.lower_expr(expr.then_expr)
		self.b.emit(M.StoreLocal(local=temp_local, value=then_val))
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=join_block.name))

		# Else branch: compute else_expr, store to temp, jump to join.
		self.b.set_block(else_block)
		else_val = self.lower_expr(expr.else_expr)
		self.b.emit(M.StoreLocal(local=temp_local, value=else_val))
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=join_block.name))

		# Join: load the temp as the value of the ternary and continue.
		self.b.set_block(join_block)
		dest = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=dest, local=temp_local))
		return dest

	# --- Statement lowering ---

	def lower_stmt(self, stmt: H.HStmt) -> None:
		"""
		Entry point: lower a single HIR statement into MIR (appends to builder).

		Dispatches to a private _visit_stmt_* helper. Public stage API: callers
		should only invoke lower_expr/stmt/block; helpers stay private.
		"""
		method = getattr(self, f"_visit_stmt_{type(stmt).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No MIR lowering for stmt {type(stmt).__name__}")
		method(stmt)

	def lower_block(self, block: H.HBlock) -> None:
		"""Entry point: lower an HIR block (list of statements) into MIR."""
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

	def _visit_stmt_HThrow(self, stmt: H.HThrow) -> None:
		"""
		Lower `throw expr` into:
		  - construct an Error (event code + diagnostic payload),
		  - wrap it in FnResult.Err,
		  - return from the current function.

		This matches the ABI model where functions return `FnResult<R, Error>`.

		Event codes are taken from exception metadata when available (via
		`exc_env`), otherwise 0 as a placeholder.
		"""
		if self.b.block.terminator is not None:
			return

		# Payload of the error (already a DiagnosticValue expression in HIR).
		payload_val = self.lower_expr(stmt.value)

		# Event code from exception metadata if available; otherwise 0.
		code_const = self._lookup_error_code(stmt.value)
		code_val = self.b.new_temp()
		self.b.emit(M.ConstInt(dest=code_val, value=code_const))

		# Build the Error value.
		err_val = self.b.new_temp()
		self.b.emit(M.ConstructError(dest=err_val, code=code_val, payload=payload_val))

		# If we are inside a try, route to the catch block instead of returning.
		if self._try_stack and self.b.block.terminator is None:
			ctx = self._try_stack[-1]
			self.b.ensure_local(ctx.error_local)
			self.b.emit(M.StoreLocal(local=ctx.error_local, value=err_val))
			self.b.set_terminator(M.Goto(target=ctx.dispatch_block_name))
			return

		# Otherwise, wrap into FnResult.Err and return.
		res_val = self.b.new_temp()
		self.b.emit(M.ConstructResultErr(dest=res_val, error=err_val))
		self.b.set_terminator(M.Return(value=res_val))

	def _visit_stmt_HTry(self, stmt: H.HTry) -> None:
		"""
		Lower a try/catch with multiple arms into explicit blocks with a dispatch:

		  entry -> try_body
		  try_body -> try_cont (falls through)
		  throw in try_body -> try_dispatch
		  try_dispatch: ErrorEvent + event-code chain -> matching catch arm or catch-all
		  unmatched + no catch-all -> rethrow as FnResult.Err
		  each catch arm -> try_cont (if it falls through)

		Notes/assumptions:
		  - We expect well-formed arms (at most one catch-all, catch-all last).
		    Until the checker enforces this, we defensively reject multiple
		    catch-alls here.
		  - Rethrow currently means “propagate as FnResult.Err out of this function,”
		    not unwinding to an outer try in the same function.
		"""
		if self.b.block.terminator is not None:
			return
		if not stmt.catches:
			raise RuntimeError("HTry lowering requires at least one catch arm")

		body_block = self.b.new_block("try_body")
		dispatch_block = self.b.new_block("try_dispatch")
		cont_block = self.b.new_block("try_cont")

		# Hidden local to carry the Error into the dispatch/catch blocks.
		error_local = f"__try_err{self.b.new_temp()}"
		self.b.ensure_local(error_local)

		# Create catch blocks for each arm.
		catch_blocks: list[tuple[H.HCatchArm, M.BasicBlock]] = []
		catch_all_block: M.BasicBlock | None = None
		for idx, arm in enumerate(stmt.catches):
			cb = self.b.new_block(f"try_catch_{idx}")
			catch_blocks.append((arm, cb))
			if arm.event_name is None:
				if catch_all_block is not None:
					raise RuntimeError("multiple catch-all arms are not supported")
				catch_all_block = cb

		# Entry: jump into body and register try context so throws can target dispatch.
		self.b.set_terminator(M.Goto(target=body_block.name))
		self._try_stack.append(
			_TryCtx(
				error_local=error_local,
				dispatch_block_name=dispatch_block.name,
				cont_block_name=cont_block.name,
			)
		)

		# Lower try body.
		self.b.set_block(body_block)
		self.lower_block(stmt.body)
		if self.b.block.terminator is None:
			self.b.set_terminator(M.Goto(target=cont_block.name))

		# Pop context before lowering dispatch/catches so throws inside catch go to outer try.
		self._try_stack.pop()

		# Dispatch: load error, project event code, branch to arms.
		self.b.set_block(dispatch_block)
		err_tmp = self.b.new_temp()
		self.b.emit(M.LoadLocal(dest=err_tmp, local=error_local))
		code_tmp = self.b.new_temp()
		self.b.emit(M.ErrorEvent(dest=code_tmp, error=err_tmp))

		# Chain event-specific arms with IfTerminator, else falling through.
		event_arms = [(arm, cb) for arm, cb in catch_blocks if arm.event_name is not None]
		current_block = dispatch_block
		for idx, (arm, cb) in enumerate(event_arms):
			arm_code = self._lookup_catch_event_code(arm.event_name)
			arm_code_const = self.b.new_temp()
			self.b.emit(M.ConstInt(dest=arm_code_const, value=arm_code))
			cmp_tmp = self.b.new_temp()
			self.b.emit(M.BinaryOpInstr(dest=cmp_tmp, op=M.BinaryOp.EQ, left=code_tmp, right=arm_code_const))

			# Chain to the next dispatch block; the final "else" is resolved after the loop.
			else_block = self.b.new_block("try_dispatch_next")
			self.b.set_terminator(M.IfTerminator(cond=cmp_tmp, then_target=cb.name, else_target=else_block.name))
			current_block = else_block
			self.b.set_block(current_block)

		# Final dispatch resolution: either a catch-all or propagate as Err.
		self.b.set_block(current_block)
		if catch_all_block is not None:
			self.b.set_terminator(M.Goto(target=catch_all_block.name))
		else:
			res_val = self.b.new_temp()
			self.b.emit(M.ConstructResultErr(dest=res_val, error=err_tmp))
			self.b.set_terminator(M.Return(value=res_val))

		# Lower each catch arm: bind error if requested, emit ErrorEvent for handler logic, then body.
		for arm, cb in catch_blocks:
			self.b.set_block(cb)
			err_again = self.b.new_temp()
			self.b.emit(M.LoadLocal(dest=err_again, local=error_local))
			if arm.binder:
				self.b.ensure_local(arm.binder)
				self.b.emit(M.StoreLocal(local=arm.binder, value=err_again))
			code_again = self.b.new_temp()
			self.b.emit(M.ErrorEvent(dest=code_again, error=err_again))
			self.lower_block(arm.block)
			if self.b.block.terminator is None:
				self.b.set_terminator(M.Goto(target=cont_block.name))

		# Continue in cont.
		self.b.set_block(cont_block)

	# --- Helpers ---

	def _lookup_error_code(self, payload_expr: H.HExpr) -> int:
		"""
		Best-effort event code lookup from exception metadata.

		If the payload is an HDVInit with a known DV/exception name and an
		exception env was provided, return that code; otherwise return 0.
		"""
		if isinstance(payload_expr, H.HDVInit) and self._exc_env is not None:
			return self._exc_env.get(payload_expr.dv_type_name, 0)
		return 0

	def _lookup_catch_event_code(self, event_name: str) -> int:
		"""
		Lookup event code for a catch arm by exception/event name.

		Uses the same exception env mapping (name -> code) as throw lowering;
		fallback to 0 if unknown.
		"""
		if self._exc_env is not None:
			return self._exc_env.get(event_name, 0)
		return 0


__all__ = ["MirBuilder", "HIRToMIR"]


@dataclass
class _TryCtx:
	"""
	Internal try/catch context to route throws to the correct catch block.

	error_local: hidden local where the thrown Error is stored.
	dispatch_block_name: block that projects the event code and dispatches to arms.
	cont_block_name: continuation block after the try/catch completes.
	"""

	error_local: str
	dispatch_block_name: str
	cont_block_name: str
