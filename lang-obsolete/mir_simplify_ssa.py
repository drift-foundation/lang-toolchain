"""Simple SSA MIR simplification: fold constant scalars and drop dead pure defs.

Limited scope:
- Constant folding for integer/scalar Binary ops when both operands are Consts.
- Remove unused Const/Copy/Binary/Unary instructions.
- Do not reorder or cross side-effecting instructions; stores/calls/etc. are barriers and untouched.
"""

from __future__ import annotations

from typing import Dict, Set

from . import mir
from .types import BOOL, INT, Type


_INT_BIN_OPS = {"+", "-", "*", "/", "==", "!=", "<", "<=", ">", ">="}


def _fold_binary(op: str, lhs_val, rhs_val):
    if not isinstance(lhs_val, (int, bool)) or not isinstance(rhs_val, (int, bool)):
        return None
    if op == "+":
        return lhs_val + rhs_val
    if op == "-":
        return lhs_val - rhs_val
    if op == "*":
        return lhs_val * rhs_val
    if op == "/":
        return lhs_val // rhs_val if rhs_val != 0 else None
    if op == "==":
        return lhs_val == rhs_val
    if op == "!=":
        return lhs_val != rhs_val
    if op == "<":
        return lhs_val < rhs_val
    if op == "<=":
        return lhs_val <= rhs_val
    if op == ">":
        return lhs_val > rhs_val
    if op == ">=":
        return lhs_val >= rhs_val
    return None


def simplify_function(fn: mir.Function) -> mir.Function:
    """Return a simplified copy of the MIR function."""
    # Global use-count so we don't drop defs that are only used across blocks (e.g., args threaded into joins).
    global_uses: Dict[str, int] = {}

    def note_use(val: str) -> None:
        global_uses[val] = global_uses.get(val, 0) + 1

    # First pass: collect uses across the whole function.
    for block in fn.blocks.values():
        for instr in block.instructions:
            if isinstance(instr, mir.Move):
                note_use(instr.source)
            elif isinstance(instr, mir.Binary):
                note_use(instr.left)
                note_use(instr.right)
            elif isinstance(instr, mir.Unary):
                note_use(instr.operand)
            elif isinstance(instr, mir.Copy):
                note_use(instr.source)
            elif isinstance(instr, mir.FieldGet):
                note_use(instr.base)
            elif isinstance(instr, mir.FieldSet):
                note_use(instr.base)
                note_use(instr.value)
            elif isinstance(instr, mir.ArrayGet):
                note_use(instr.base)
                note_use(instr.index)
            elif isinstance(instr, mir.ArraySet):
                note_use(instr.base)
                note_use(instr.index)
                note_use(instr.value)
            elif isinstance(instr, mir.StructInit):
                for a in instr.args:
                    note_use(a)
            elif isinstance(instr, mir.ArrayInit):
                for a in instr.elements:
                    note_use(a)
            elif isinstance(instr, mir.ArrayLiteral):
                for a in instr.elements:
                    note_use(a)
            elif isinstance(instr, mir.ArrayLen):
                note_use(instr.base)
            elif isinstance(instr, mir.Store):
                note_use(instr.base)
                note_use(instr.value)
            elif isinstance(instr, mir.Call):
                for a in instr.args:
                    note_use(a)
            elif isinstance(instr, mir.CallWithCtx):
                note_use(instr.ctx_dest)
                if instr.ctx_arg:
                    note_use(instr.ctx_arg)
                for a in instr.args:
                    note_use(a)
            elif isinstance(instr, mir.ConsoleWrite):
                note_use(instr.value)
            elif isinstance(instr, mir.ConsoleWriteln):
                note_use(instr.value)
            elif isinstance(instr, mir.Drop):
                note_use(instr.value)

        term = block.terminator
        if term:
            if isinstance(term, mir.CondBr):
                note_use(term.cond)
                for edge in (term.then, term.els):
                    for a in edge.args:
                        note_use(a)
            elif isinstance(term, mir.Br):
                for a in term.target.args:
                    note_use(a)
            elif isinstance(term, mir.Return):
                if term.value is not None:
                    note_use(term.value)
                if term.error is not None:
                    note_use(term.error)
            elif isinstance(term, mir.Call) and (term.normal or term.error):
                for a in term.args:
                    note_use(a)
                for edge in (term.normal, term.error):
                    if edge:
                        for a in edge.args:
                            note_use(a)
            elif isinstance(term, mir.Throw):
                note_use(term.error)

    blocks: Dict[str, mir.BasicBlock] = {}
    for name, block in fn.blocks.items():
        const_values: Dict[str, object] = {}
        const_types: Dict[str, Type] = {}

        new_instrs: list[mir.Instruction] = []
        for instr in block.instructions:
            # Fold pure binary ops with const operands.
            if isinstance(instr, mir.Binary) and instr.op in _INT_BIN_OPS:
                lhs_val = const_values.get(instr.left)
                rhs_val = const_values.get(instr.right)
                folded = _fold_binary(instr.op, lhs_val, rhs_val)
                if folded is not None:
                    # Prefer an explicit type on the instruction, otherwise inherit from operands, otherwise fall back.
                    result_ty = getattr(instr, "type", None)
                    if isinstance(folded, bool):
                        result_ty = BOOL
                    if result_ty is None:
                        lty = const_types.get(instr.left)
                        rty = const_types.get(instr.right)
                        if lty is not None and lty == rty:
                            result_ty = lty
                    if result_ty is None:
                        result_ty = INT
                    new_instrs.append(mir.Const(dest=instr.dest, type=result_ty, value=folded))
                    const_values[instr.dest] = folded
                    const_types[instr.dest] = result_ty
                    continue
            # Dead pure defs: drop if never used anywhere in the function.
            if isinstance(instr, (mir.Const, mir.Copy, mir.Binary, mir.Unary, mir.Move, mir.ArrayLen)):
                dest = getattr(instr, "dest", None)
                if dest and global_uses.get(dest, 0) == 0:
                    continue
            if isinstance(instr, mir.Const):
                const_values[instr.dest] = instr.value
                const_types[instr.dest] = instr.type
            new_instrs.append(instr)
        new_block = mir.BasicBlock(name=block.name, params=list(block.params), instructions=new_instrs, terminator=block.terminator)
        blocks[name] = new_block
    return mir.Function(
        name=fn.name,
        params=fn.params,
        return_type=fn.return_type,
        entry=fn.entry,
        module=fn.module,
        source=fn.source,
        blocks=blocks,
    )
