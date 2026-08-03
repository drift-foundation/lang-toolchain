# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Shared HIR flow/effect authorities.

Two analyses used to live as drifting per-consumer copies and each shipped
real bugs (a lambda throwing only inside a match arm / nested block was
classified nothrow and its call site lowered without error dispatch —
runtime SIGABRT; the lambda value-less-body guard grew a second, weaker
terminal-form whitelist).  This module is their single home:

* Terminal flow — `block_exits` classifies every CFG path out of a block as
  FALLTHROUGH / THROWS / RETURNS_VALUE / RETURNS_BARE, with the phase-2
  checker's semantics (literal-`if` folding, try = body+arms, loop terminal
  iff no reachable loop-local break, statement-position match by arm blocks,
  terminal-`throws` calls via an injected resolver).  `is_terminal_block` is
  the boolean view the phase-2 checker consumes.

* Throw effect — `lambda_body_can_throw` decides whether executing a lambda
  body can raise.  Every HIR variant is DELIBERATELY classified: handled
  specially, recursed reflectively, or declared a leaf.  A variant this
  module does not know (e.g. a newly added node) is treated as can-throw —
  over-approximating is safe (the value gets a checked call); silently
  meaning "nothrow" is how the SIGABRT class shipped.  A nested, UNINVOKED
  `HLambda` is a traversal boundary: constructing or passing a throwing
  lambda does not throw — only invoking it does (direct IIFEs descend).

Consumers inject call-resolution knowledge (`CallInfo` lookups live above
this layer) as plain predicates, so this module depends only on HIR nodes.
"""

from __future__ import annotations

import enum
from typing import Callable, Iterable

from lang.driftc.stage1 import hir_nodes as H


class Exit(enum.Enum):
	"""One kind of CFG path out of a block."""

	FALLTHROUGH = "fallthrough"      # control reaches the end of the block
	THROWS = "throws"                # throw / rethrow / terminal-`throws` call
	RETURNS_VALUE = "returns_value"  # `return expr;`
	RETURNS_BARE = "returns_bare"    # `return;`


# `is_terminal_call(expr) -> bool`: True iff `expr` is a statement-position
# call whose resolved callee is a terminal-`throws` function (control never
# returns; it exits by throwing).  Resolution requires CallInfo/signature
# maps that live above this module, so consumers inject it.
TerminalCallPredicate = Callable[["H.HExpr"], bool]


def _never_terminal_call(_expr: "H.HExpr") -> bool:
	return False


def block_exits(block: "H.HBlock", *, is_terminal_call: TerminalCallPredicate = _never_terminal_call, call_can_throw: "CallCanThrowPredicate | None" = None) -> frozenset[Exit]:
	"""
	The set of exit kinds over every CFG path through `block`.

	Statements after the first no-fallthrough statement are unreachable and
	contribute nothing (same capping rule as the phase-2 checker).  Non-tail
	statements still contribute their own exiting paths (an `if` whose then
	arm returns adds RETURNS_* even though the block continues).

	`call_can_throw` (optional) feeds the shared throw-effect decision so
	catch arms of a try whose attempt CANNOT throw are treated as the dead
	code they are; when omitted, catch arms are conservatively reachable.
	"""
	exits: set[Exit] = set()
	for stmt in block.statements:
		stmt_ex = _stmt_exits(stmt, is_terminal_call, call_can_throw)
		exits |= stmt_ex - {Exit.FALLTHROUGH}
		if Exit.FALLTHROUGH not in stmt_ex:
			return frozenset(exits)
	exits.add(Exit.FALLTHROUGH)
	return frozenset(exits)


def _stmt_exits(stmt: "H.HStmt", is_terminal_call: TerminalCallPredicate, call_can_throw: "CallCanThrowPredicate | None" = None) -> frozenset[Exit]:
	if isinstance(stmt, H.HReturn):
		return frozenset({Exit.RETURNS_VALUE if stmt.value is not None else Exit.RETURNS_BARE})
	if isinstance(stmt, (H.HThrow, H.HRethrow)):
		return frozenset({Exit.THROWS})
	if isinstance(stmt, H.HBlock):
		return block_exits(stmt, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
	if isinstance(stmt, H.HUnsafeBlock):
		return block_exits(stmt.block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
	if isinstance(stmt, H.HIf):
		# Constant-fold a literal-bool condition: only the taken branch
		# matters.  Load-bearing for the `while true` desugaring, which
		# produces `if true { user_body } else { break }` — without the fold
		# the synthesized else-break would force non-terminal.
		if isinstance(stmt.cond, H.HLiteralBool):
			if stmt.cond.value:
				return block_exits(stmt.then_block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
			if stmt.else_block is None:
				return frozenset({Exit.FALLTHROUGH})
			return block_exits(stmt.else_block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
		then_ex = block_exits(stmt.then_block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
		if stmt.else_block is None:
			# An `if` without `else` always permits fallthrough.
			return then_ex | {Exit.FALLTHROUGH}
		return then_ex | block_exits(stmt.else_block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
	if isinstance(stmt, H.HTry):
		# The construct falls through iff the body OR any REACHABLE catch arm
		# falls through; the exiting kinds are the union of body and
		# reachable-arm kinds.  Catch arms are dead code when the attempt
		# cannot throw (decided by the shared effect walker via
		# `call_can_throw`; conservatively reachable when no predicate is
		# supplied).  A catch-all arm HANDLES the body's throw path — a
		# caught body-throw lands IN an arm (whose exits are included), so
		# the body's THROWS member does not escape the construct.  Without a
		# catch-all, a body throw of an unmatched event escapes, so THROWS
		# is retained.
		body_ex = set(block_exits(stmt.body, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw))
		catches_reachable = True
		if call_can_throw is not None:
			catches_reachable = _block_can_throw(stmt.body, call_can_throw)
		if not catches_reachable:
			return frozenset(body_ex)
		if any(arm.event_fqn is None for arm in stmt.catches):
			body_ex.discard(Exit.THROWS)
		for arm in stmt.catches:
			body_ex |= block_exits(arm.block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
		return frozenset(body_ex)
	if isinstance(stmt, H.HLoop):
		# A loop falls through iff its body has a reachable loop-local
		# `break`.  Otherwise the only function exits are the return/throw
		# paths inside the body (body fallthrough = next iteration, not an
		# exit); a body with no exits at all is an infinite loop (empty set).
		body_ex = block_exits(stmt.body, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw) - {Exit.FALLTHROUGH}
		if block_contains_reachable_break(stmt.body, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw):
			return frozenset(body_ex | {Exit.FALLTHROUGH})
		return frozenset(body_ex)
	if isinstance(stmt, H.HExprStmt):
		# A statement-position call to a terminal-`throws` function exits by
		# throwing — control never returns from it.
		if is_terminal_call(stmt.expr):
			return frozenset({Exit.THROWS})
		if isinstance(stmt.expr, H.HMatchExpr):
			# Statement-position match: decided by arm BLOCKS only.  An arm
			# `result` expression is value-position — a terminal-throws call
			# there is rejected elsewhere; users must use the block form
			# (`B => { fail(); }`).  Mirrors the phase-2 checker exactly.
			combined = set()
			for arm in stmt.expr.arms:
				combined |= block_exits(arm.block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
			return frozenset(combined if combined else {Exit.FALLTHROUGH})
		return frozenset({Exit.FALLTHROUGH})
	if isinstance(stmt, (H.HBreak, H.HContinue)):
		# `break`/`continue` transfer control WITHIN the enclosing loop: they
		# are not function exits, but execution never reaches the next
		# statement of THIS block either — the empty set.  (The loop-level
		# consequences — a reachable `break` giving the loop a FALLTHROUGH
		# exit — are handled by the HLoop case via
		# `block_contains_reachable_break`.)  Classifying them FALLTHROUGH
		# let the reachable-break scan walk past a `continue` to a dead
		# `break`, falsely un-terminating permanently-divergent loops.
		return frozenset()
	# HLet, HLocalConst, HAssign, HAugAssign, HAssert and any other
	# statement form do not terminate the function.
	return frozenset({Exit.FALLTHROUGH})


def is_terminal_block(block: "H.HBlock", *, is_terminal_call: TerminalCallPredicate = _never_terminal_call, call_can_throw: "CallCanThrowPredicate | None" = None) -> bool:
	"""True iff no CFG path falls off the end of `block` (phase-2 contract)."""
	return Exit.FALLTHROUGH not in block_exits(block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)


def is_terminal_stmt(stmt: "H.HStmt", *, is_terminal_call: TerminalCallPredicate = _never_terminal_call, call_can_throw: "CallCanThrowPredicate | None" = None) -> bool:
	"""True iff control cannot flow past `stmt` (phase-2 contract)."""
	return Exit.FALLTHROUGH not in _stmt_exits(stmt, is_terminal_call, call_can_throw)


def block_contains_reachable_break(block: "H.HBlock", *, is_terminal_call: TerminalCallPredicate = _never_terminal_call, call_can_throw: "CallCanThrowPredicate | None" = None) -> bool:
	"""
	True iff `block` contains an `HBreak` reachable from the block entry,
	with the same constant-folding rules as `block_exits`.  Does NOT recurse
	into nested `HLoop` bodies — a break there binds to the inner loop.
	"""
	for stmt in block.statements:
		if _stmt_contains_reachable_break(stmt, is_terminal_call, call_can_throw):
			return True
		if Exit.FALLTHROUGH not in _stmt_exits(stmt, is_terminal_call, call_can_throw):
			# Code after a function-level terminator is unreachable and
			# cannot contribute a reachable break.
			return False
	return False


def _stmt_contains_reachable_break(stmt: "H.HStmt", is_terminal_call: TerminalCallPredicate, call_can_throw: "CallCanThrowPredicate | None" = None) -> bool:
	if isinstance(stmt, H.HBreak):
		return True
	if isinstance(stmt, (H.HReturn, H.HThrow, H.HRethrow, H.HContinue)):
		return False
	if isinstance(stmt, H.HBlock):
		return block_contains_reachable_break(stmt, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
	if isinstance(stmt, H.HUnsafeBlock):
		return block_contains_reachable_break(stmt.block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
	if isinstance(stmt, H.HIf):
		if isinstance(stmt.cond, H.HLiteralBool):
			if stmt.cond.value:
				return block_contains_reachable_break(stmt.then_block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
			if stmt.else_block is None:
				return False
			return block_contains_reachable_break(stmt.else_block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw)
		if block_contains_reachable_break(stmt.then_block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw):
			return True
		if stmt.else_block is not None and block_contains_reachable_break(stmt.else_block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw):
			return True
		return False
	if isinstance(stmt, H.HTry):
		if block_contains_reachable_break(stmt.body, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw):
			return True
		if call_can_throw is not None and not _block_can_throw(stmt.body, call_can_throw):
			# Attempt cannot throw: the catch arms are dead code and their
			# breaks are not reachable.
			return False
		return any(block_contains_reachable_break(arm.block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw) for arm in stmt.catches)
	if isinstance(stmt, H.HExprStmt) and isinstance(stmt.expr, H.HMatchExpr):
		return any(block_contains_reachable_break(arm.block, is_terminal_call=is_terminal_call, call_can_throw=call_can_throw) for arm in stmt.expr.arms)
	if isinstance(stmt, H.HLoop):
		# Breaks inside a nested loop bind to that inner loop.
		return False
	# HLet/HLocalConst/HAssign/HAugAssign/HExprStmt(non-match)/HAssert:
	# expression evaluation cannot syntactically contain `break` in Drift v1.
	return False


# ---------------------------------------------------------------------------
# Throw-effect traversal
# ---------------------------------------------------------------------------

# `call_can_throw(expr) -> bool`: True iff the resolved call `expr` (HCall /
# HMethodCall / HInvoke) can raise, per the consumer's CallInfo knowledge.
# The traversal still recurses into operands either way (an argument
# expression may itself throw).
CallCanThrowPredicate = Callable[["H.HExpr"], bool]

# Node classes with no throw effect and no children that could carry one.
_LEAF_TYPES: tuple[type, ...] = (
	H.HVar,
	H.HSelfRef,
	H.HTypeNameRef,
	H.HTraitIs,
	H.HTraitAnd,
	H.HTraitOr,
	H.HTraitNot,
	H.HLiteralInt,
	H.HLiteralString,
	H.HLiteralBool,
	H.HLiteralUint,
	H.HLiteralUint64,
	H.HLiteralFloat,
	H.HFnPtrConst,
	H.HParam,
	H.HExplicitCapture,
	H.HQualifiedMember,
	H.HBreak,
	H.HContinue,
	H.HPlaceField,
	H.HPlaceDeref,
)

# Node classes whose children are traversed reflectively (every dataclass
# field that holds HIR descends).  Reflective — not hand-listed fields — so
# a node growing a new child slot (the `kwargs` class of omission) cannot
# silently escape the walk.
_RECURSIVE_TYPES: tuple[type, ...] = (
	H.HPlaceExpr,
	H.HPlaceIndex,
	H.HFString,
	H.HFStringHole,
	H.HTypeApp,
	H.HCast,
	H.HTernary,
	H.HResultOk,
	H.HMatchExpr,
	H.HMatchArm,
	H.HField,
	H.HIndex,
	H.HBorrow,
	H.HMove,
	H.HCopy,
	H.HExceptionInit,
	H.HKwArg,
	H.HUnary,
	H.HBinary,
	H.HArrayLiteral,
	H.HMapEntry,
	H.HMapLiteral,
	H.HUnsafeExpr,
	H.HBlock,
	H.HUnsafeBlock,
	H.HExprStmt,
	H.HLet,
	H.HLocalConst,
	H.HAssign,
	H.HAugAssign,
	H.HIf,
	H.HLoop,
	H.HReturn,
	H.HAssert,
	H.HCatchArm,
	H.HTryExprArm,
)


def _children(node: object) -> Iterable[object]:
	fields = getattr(node, "__dataclass_fields__", None)
	if fields is None:
		return
	for name in fields:
		val = getattr(node, name, None)
		if val is None:
			continue
		if isinstance(val, (list, tuple)):
			for item in val:
				yield item
		elif isinstance(val, dict):
			for item in val.values():
				yield item
		else:
			yield val


def lambda_body_can_throw(lam: "H.HLambda", *, call_can_throw: CallCanThrowPredicate) -> bool:
	"""
	True iff EXECUTING `lam`'s body can raise.

	Nested `HLambda` values inside the body are boundaries (constructing a
	throwing lambda does not throw); a direct IIFE (`HCall(fn=HLambda)` /
	`HInvoke(callee=HLambda)`) executes its lambda, so its body IS descended.
	Unknown node variants are conservatively treated as can-throw.
	"""
	if lam.body_expr is not None and _can_throw(lam.body_expr, call_can_throw):
		return True
	if lam.body_block is not None and _can_throw(lam.body_block, call_can_throw):
		return True
	return False


def _block_can_throw(block: "H.HBlock", call_can_throw: CallCanThrowPredicate) -> bool:
	"""
	Reachability-aware statement walk: a throw sitting AFTER a statement that
	loses FALLTHROUGH (return / break / continue / divergent construct) is
	dead code and contributes no effect — the same sequential-capping and
	literal-`if` folding rules as `block_exits`, so the effect and exit
	authorities agree on what can execute.
	"""
	for stmt in block.statements:
		if _stmt_can_throw(stmt, call_can_throw):
			return True
		if Exit.FALLTHROUGH not in _stmt_exits(stmt, _never_terminal_call, call_can_throw):
			return False
	return False


def _stmt_can_throw(stmt: "H.HStmt", call_can_throw: CallCanThrowPredicate) -> bool:
	if isinstance(stmt, (H.HThrow, H.HRethrow)):
		return True
	if isinstance(stmt, (H.HBreak, H.HContinue)):
		return False
	if isinstance(stmt, H.HBlock):
		return _block_can_throw(stmt, call_can_throw)
	if isinstance(stmt, H.HUnsafeBlock):
		return _block_can_throw(stmt.block, call_can_throw)
	if isinstance(stmt, H.HIf):
		# Literal-condition fold: the untaken arm cannot execute (mirrors
		# `_stmt_exits`).  A literal condition itself has no effect.
		if isinstance(stmt.cond, H.HLiteralBool):
			if stmt.cond.value:
				return _block_can_throw(stmt.then_block, call_can_throw)
			if stmt.else_block is None:
				return False
			return _block_can_throw(stmt.else_block, call_can_throw)
		if _can_throw(stmt.cond, call_can_throw):
			return True
		if _block_can_throw(stmt.then_block, call_can_throw):
			return True
		return stmt.else_block is not None and _block_can_throw(stmt.else_block, call_can_throw)
	if isinstance(stmt, H.HLoop):
		return _block_can_throw(stmt.body, call_can_throw)
	if isinstance(stmt, H.HTry):
		# Catch arms only run when the body can actually throw; with a
		# catch-all the body throw is swallowed and only handler effects
		# escape; without one the unmatched throw escapes.
		if not _block_can_throw(stmt.body, call_can_throw):
			return False
		if not any(arm.event_fqn is None for arm in stmt.catches):
			return True
		return any(_block_can_throw(arm.block, call_can_throw) for arm in stmt.catches)
	if isinstance(stmt, H.HAssert):
		# The message expression is evaluated ONLY on the failure path
		# (lowering builds it inside the fail block).  A literal-true
		# condition can never fail, so its message is dead; a literal-false
		# condition always fails, so only the message runs.
		if isinstance(stmt.cond, H.HLiteralBool):
			if stmt.cond.value:
				return False
			return stmt.msg is not None and _can_throw(stmt.msg, call_can_throw)
		if _can_throw(stmt.cond, call_can_throw):
			return True
		return stmt.msg is not None and _can_throw(stmt.msg, call_can_throw)
	# Expression-carrying statements (HExprStmt, HLet, HAssign, HAugAssign,
	# HReturn, HLocalConst, ...): the effect is their expressions'.
	return any(_can_throw(child, call_can_throw) for child in _children(stmt))


def _can_throw(node: object, call_can_throw: CallCanThrowPredicate) -> bool:
	if node is None:
		return False
	if isinstance(node, H.HBlock):
		# Blocks reached from expression context (match-arm bodies, try-expr
		# arm bodies) get the same reachability-aware statement walk.
		return _block_can_throw(node, call_can_throw)
	if isinstance(node, H.HStmt):
		return _stmt_can_throw(node, call_can_throw)
	if isinstance(node, H.HLambda):
		# Traversal boundary: an uninvoked lambda value does not execute its
		# body.  Its `share`-capture initializers DO run at construction
		# time, so those stay in the enclosing effect.
		for _cap in (node.explicit_captures or []):
			_sv = getattr(_cap, "share_value", None)
			if _sv is not None and _can_throw(_sv, call_can_throw):
				return True
		return False
	if isinstance(node, (H.HCall, H.HInvoke, H.HMethodCall)):
		if call_can_throw(node):
			return True
		# A direct IIFE executes its lambda body right here.
		callee = node.fn if isinstance(node, H.HCall) else (node.callee if isinstance(node, H.HInvoke) else None)
		if isinstance(callee, H.HLambda):
			if lambda_body_can_throw(callee, call_can_throw=call_can_throw):
				return True
		# Operands (receiver / non-lambda callee / args / kwargs) may throw
		# even when the call itself cannot.  Reflective descent covers every
		# operand slot; the HLambda boundary above keeps uninvoked lambda
		# ARGUMENTS from leaking their bodies into this decision.
		return any(_can_throw(child, call_can_throw) for child in _children(node))
	if isinstance(node, H.HTryExpr):
		# Catch arms only run when the attempt can actually throw; an
		# effectless attempt makes every handler dead code.  When the
		# attempt CAN throw: a catch-all swallows it (only handler effects
		# escape), otherwise the unmatched throw itself escapes.
		if not _can_throw(node.attempt, call_can_throw):
			return False
		if not any(arm.event_fqn is None for arm in node.arms):
			return True
		for arm in node.arms:
			if _can_throw(arm.block, call_can_throw):
				return True
			if getattr(arm, "result", None) is not None and _can_throw(arm.result, call_can_throw):
				return True
		return False
	if isinstance(node, H.HBinary) and node.op in (H.BinaryOp.AND, H.BinaryOp.OR):
		# Short-circuit lowering: the RHS only evaluates when the literal LHS
		# does not decide the result (`false and rhs` / `true or rhs` never
		# run rhs).  Fold literal booleans exactly as lowering does; any
		# non-literal LHS keeps both sides reachable.
		if _can_throw(node.left, call_can_throw):
			return True
		if isinstance(node.left, H.HLiteralBool):
			decided = (node.op is H.BinaryOp.AND and not node.left.value) or (node.op is H.BinaryOp.OR and node.left.value)
			if decided:
				return False
		return _can_throw(node.right, call_can_throw)
	if isinstance(node, H.HTernary):
		# Only the selected branch of a literal condition evaluates.
		if isinstance(node.cond, H.HLiteralBool):
			taken = node.then_expr if node.cond.value else node.else_expr
			return _can_throw(taken, call_can_throw)
		if _can_throw(node.cond, call_can_throw):
			return True
		return _can_throw(node.then_expr, call_can_throw) or _can_throw(node.else_expr, call_can_throw)
	if isinstance(node, _LEAF_TYPES):
		return False
	if isinstance(node, _RECURSIVE_TYPES):
		return any(_can_throw(child, call_can_throw) for child in _children(node))
	if isinstance(node, (H.HExpr, H.HStmt, H.HNode)):
		# Deliberately-unknown variant (e.g. a node added after this module):
		# over-approximate.  Silently meaning "nothrow" is how the
		# match-arm/nested-block SIGABRT class shipped.
		return True
	# Non-HIR field payloads (Span, str, int, enums, parser type exprs...).
	return False
