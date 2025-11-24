from __future__ import annotations

from typing import Dict, Tuple, List, Optional

from . import ast, mir
from .checker import CheckedProgram
from .types import BOOL, F64, I64, STR, Type


class LoweringError(Exception):
    pass


def lower_straightline(checked: CheckedProgram) -> mir.Program:
    """
    Minimal lowering supporting:
    - params/literals/names/binary ops
    - return
    - ternary expressions
    - if/else conditionals
    """
    if not checked.program.functions:
        raise LoweringError("no functions to lower")
    fn_def = checked.program.functions[0]
    fn_info = checked.functions[fn_def.name]

    temp_counter = 0
    block_counter = 0
    blocks: Dict[str, mir.BasicBlock] = {}

    def fresh_val() -> str:
        nonlocal temp_counter
        temp_counter += 1
        return f"_t{temp_counter}"

    def fresh_block(prefix: str) -> str:
        nonlocal block_counter
        block_counter += 1
        return f"{prefix}{block_counter}"

    block_params = [mir.Param(name=p.name, type=fn_info.signature.params[idx]) for idx, p in enumerate(fn_def.params)]
    entry = mir.BasicBlock(name="bb0", params=block_params)
    blocks[entry.name] = entry

    def lower_expr(expr: ast.Expr, current_block: mir.BasicBlock, temp_types: Dict[str, Type]) -> Tuple[str, Type, mir.BasicBlock]:
        if isinstance(expr, ast.Literal):
            dest = fresh_val()
            lit_val = expr.value
            lit_type = _type_of_literal(lit_val)
            current_block.instructions.append(mir.Const(dest=dest, type=lit_type, value=lit_val))
            temp_types[dest] = lit_type
            return dest, lit_type, current_block
        if isinstance(expr, ast.Name):
            ty = _lookup_type(expr.ident, block_params, temp_types)
            return expr.ident, ty, current_block
        if isinstance(expr, ast.Binary):
            lhs, lhs_ty, current_block = lower_expr(expr.left, current_block, temp_types)
            rhs, rhs_ty, current_block = lower_expr(expr.right, current_block, temp_types)
            dest = fresh_val()
            current_block.instructions.append(mir.Binary(dest=dest, op=expr.op, left=lhs, right=rhs))
            temp_types[dest] = lhs_ty  # naive: assume types match
            return dest, lhs_ty, current_block
        if isinstance(expr, ast.Ternary):
            cond_val, _, current_block = lower_expr(expr.condition, current_block, temp_types)

            then_name = fresh_block("bb_then")
            else_name = fresh_block("bb_else")
            join_name = fresh_block("bb_join")

            then_block = mir.BasicBlock(name=then_name)
            else_block = mir.BasicBlock(name=else_name)
            blocks[then_name] = then_block
            blocks[else_name] = else_block

            current_block.terminator = mir.CondBr(
                cond=cond_val,
                then=mir.Edge(target=then_name),
                els=mir.Edge(target=else_name),
            )

            temp_types_then = temp_types.copy()
            v_then, ty_then, then_block = lower_expr(expr.then_value, then_block, temp_types_then)
            temp_types_else = temp_types.copy()
            v_else, ty_else, else_block = lower_expr(expr.else_value, else_block, temp_types_else)
            if ty_then != ty_else:
                raise LoweringError("ternary branches must have the same type")

            phi_name = fresh_val()
            join_block = mir.BasicBlock(name=join_name, params=[mir.Param(name=phi_name, type=ty_then)])
            blocks[join_name] = join_block

            then_block.terminator = mir.Br(target=mir.Edge(target=join_name, args=[v_then]))
            else_block.terminator = mir.Br(target=mir.Edge(target=join_name, args=[v_else]))

            temp_types[phi_name] = ty_then
            return phi_name, ty_then, join_block
        raise LoweringError(f"unsupported expression: {expr}")

    def lower_stmt(stmt: ast.Stmt, current_block: mir.BasicBlock, temp_types: Dict[str, Type]) -> Optional[mir.BasicBlock]:
        if isinstance(stmt, ast.ReturnStmt):
            if stmt.value is None:
                current_block.terminator = mir.Return()
            else:
                val, _, current_block = lower_expr(stmt.value, current_block, temp_types)
                current_block.terminator = mir.Return(value=val)
            return None
        if isinstance(stmt, ast.IfStmt):
            return _lower_if(stmt, current_block, temp_types)
        raise LoweringError(f"unsupported statement: {stmt}")

    def lower_block(stmts: List[ast.Stmt], current_block: mir.BasicBlock, temp_types: Dict[str, Type]) -> Optional[mir.BasicBlock]:
        block = current_block
        for stmt in stmts:
            block = lower_stmt(stmt, block, temp_types)
            if block is None:
                return None
        return block

    def _lower_if(stmt: ast.IfStmt, current_block: mir.BasicBlock, temp_types: Dict[str, Type]) -> Optional[mir.BasicBlock]:
        cond_val, _, current_block = lower_expr(stmt.condition, current_block, temp_types)
        then_name = fresh_block("bb_then")
        else_name = fresh_block("bb_else")
        then_block = mir.BasicBlock(name=then_name)
        else_block = mir.BasicBlock(name=else_name)
        blocks[then_name] = then_block
        blocks[else_name] = else_block
        current_block.terminator = mir.CondBr(cond=cond_val, then=mir.Edge(target=then_name), els=mir.Edge(target=else_name))

        end_then = lower_block(stmt.then_block.statements, then_block, temp_types.copy())
        end_else = lower_block(stmt.else_block.statements, else_block, temp_types.copy()) if stmt.else_block else else_block

        # If both branches return, control does not continue.
        if end_then is None and end_else is None:
            return None

        join_name = fresh_block("bb_join")
        join_block = mir.BasicBlock(name=join_name)
        blocks[join_name] = join_block

        if end_then is not None:
            end_then.terminator = mir.Br(target=mir.Edge(target=join_name))
        if end_else is not None:
            end_else.terminator = mir.Br(target=mir.Edge(target=join_name))
        return join_block

    temp_types: Dict[str, Type] = {}
    current_block: Optional[mir.BasicBlock] = entry
    for stmt in fn_def.body.statements:
        if current_block is None:
            break
        current_block = lower_stmt(stmt, current_block, temp_types)

    if current_block is not None and current_block.terminator is None:
        raise LoweringError("function did not terminate with return")

    fn = mir.Function(
        name=fn_def.name,
        params=[mir.Param(name=p.name, type=fn_info.signature.params[idx]) for idx, p in enumerate(fn_def.params)],
        return_type=fn_info.signature.return_type,
        entry=entry.name,
        blocks=blocks,
    )
    return mir.Program(functions={fn.name: fn})


def _lookup_type(name: str, params, temps: Dict[str, Type]) -> Type:
    for p in params:
        if p.name == name:
            return p.type
    if name in temps:
        return temps[name]
    return Type("<unknown>")


def _type_of_literal(value) -> Type:
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int):
        return I64
    if isinstance(value, float):
        return F64
    if isinstance(value, str):
        return STR
    return Type("<unknown>")
