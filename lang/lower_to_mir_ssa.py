"""Skeleton strict-SSA lowering for Drift MIR.

This is a scaffold to guide the eventual rewrite of lower_to_mir into a strict
SSA form. It is wired behind --ssa-check/--ssa-check-mode for structural checking,
while legacy lowering still drives codegen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

from . import ast, mir
from .checker import CheckedProgram
from .ssa_env import SSAEnv, SSAContext
from .types import ERROR, INT
from .types import (
    Type,
    ReferenceType,
    BOOL,
    I64,
    INT,
    F64,
    STR,
    UNIT,
    array_element_type,
    array_of,
    ref_of,
)


@dataclass
class LoweredFunction:
    blocks: Dict[str, mir.BasicBlock]
    env: SSAEnv
    entry: str = "bb0"


def lower_function_ssa(fn_def: ast.FunctionDef, checked: CheckedProgram) -> LoweredFunction:
    """Lower a single function into SSA MIR blocks (scaffold only)."""
    fn_info = checked.functions[fn_def.name]
    ctx = SSAContext()
    env = SSAEnv(ctx=ctx, frame_name=fn_def.name)
    blocks: Dict[str, mir.BasicBlock] = {}
    block_counter = 0

    def fresh_block(prefix: str) -> str:
        nonlocal block_counter
        block_counter += 1
        return f"{prefix}{block_counter}"

    entry_name = "bb0"
    entry_block = mir.BasicBlock(name=entry_name, params=[])
    blocks[entry_name] = entry_block

    # Bind params to fresh SSA names
    for idx, p in enumerate(fn_def.params):
        ty = fn_info.signature.params[idx]
        ssa_name = env.fresh_ssa(p.name, ty)
        entry_block.params.append(mir.Param(name=ssa_name, type=ty))
        env.bind_user(p.name, ssa_name, ty)

    # Lower body (placeholder)
    current, env = lower_block_in_env(fn_def.body.statements, env, blocks, entry_block, checked, fresh_block)
    if current.terminator is None:
        # Implicit return for functions that fall through.
        current.terminator = mir.Return()

    return LoweredFunction(blocks=blocks, env=env, entry=entry_name)


def lower_block_in_env(
    stmts: List[ast.Stmt],
    env: SSAEnv,
    blocks: Dict[str, mir.BasicBlock],
    current: mir.BasicBlock,
    checked: CheckedProgram,
    fresh_block: callable,
) -> Tuple[mir.BasicBlock, SSAEnv]:
    """Lower a sequence of statements under a given SSA env (scaffold)."""
    for stmt in stmts:
        if isinstance(stmt, ast.LetStmt):
            current, env = lower_let(stmt, env, current, checked, blocks, fresh_block)
        elif isinstance(stmt, ast.AssignStmt):
            current, env = lower_assign(stmt, env, current, checked, blocks, fresh_block)
        elif isinstance(stmt, ast.IfStmt):
            current, env = lower_if(stmt, env, blocks, current, checked, fresh_block)
        elif isinstance(stmt, ast.ReturnStmt):
            # Scaffold: return without building pair ABI; assumes expression lowered elsewhere.
            if stmt.value is None:
                current.terminator = mir.Return()
            else:
                val_ssa, _, current, env = lower_expr_to_ssa(
                    stmt.value, env, current, checked, blocks, fresh_block
                )
                current.terminator = mir.Return(value=val_ssa)
            return current, env
        elif isinstance(stmt, ast.WhileStmt):
            current, env = lower_while(stmt, env, blocks, current, checked, fresh_block)
        elif isinstance(stmt, ast.ExprStmt):
            _, _, current, env = lower_expr_to_ssa(stmt.value, env, current, checked, blocks, fresh_block)
        elif isinstance(stmt, ast.TryStmt):
            current, env = lower_try_stmt(stmt, env, blocks, current, checked, fresh_block)
        elif isinstance(stmt, ast.ThrowStmt):
            err_ssa, _, current, env = lower_expr_to_ssa(stmt.expr, env, current, checked, blocks, fresh_block)
            current.terminator = mir.Throw(error=err_ssa, loc=stmt.loc)
            return current, env
        elif isinstance(stmt, ast.RaiseStmt):
            err_ssa, _, current, env = lower_expr_to_ssa(stmt.value, env, current, checked, blocks, fresh_block)
            current.terminator = mir.Throw(error=err_ssa, loc=stmt.loc)
            return current, env
        elif isinstance(stmt, ast.ForStmt):
            current, env = lower_for(stmt, env, blocks, current, checked, fresh_block)
        else:
            # Other statements (if/while/try) to be filled in later.
            pass
    return current, env


def lower_let(
    stmt: ast.LetStmt,
    env: SSAEnv,
    current: mir.BasicBlock,
    checked: CheckedProgram,
    blocks: Dict[str, mir.BasicBlock],
    fresh_block: callable,
) -> Tuple[mir.BasicBlock, SSAEnv]:
    val_ssa, val_ty, current, env = lower_expr_to_ssa(stmt.value, env, current, checked, blocks, fresh_block)
    dest_ssa = env.fresh_ssa(stmt.name, val_ty)
    current.instructions.append(mir.Move(dest=dest_ssa, source=val_ssa))
    capture_key = None
    if getattr(stmt, "capture", False):
        capture_key = stmt.capture_alias or stmt.name
    env.bind_user(stmt.name, dest_ssa, val_ty, capture_key=capture_key)
    return current, env


def lower_assign(
    stmt: ast.AssignStmt,
    env: SSAEnv,
    current: mir.BasicBlock,
    checked: CheckedProgram,
    blocks: Dict[str, mir.BasicBlock],
    fresh_block: callable,
) -> Tuple[mir.BasicBlock, SSAEnv]:
    # Target is a simple name (rebinding)
    if isinstance(stmt.target, ast.Name):
        if not env.has_user(stmt.target.ident):
            raise LoweringError(f"assignment to undeclared variable {stmt.target.ident}")
        val_ssa, val_ty, current, env = lower_expr_to_ssa(stmt.value, env, current, checked, blocks, fresh_block)
        dest_ssa = env.fresh_ssa(stmt.target.ident, val_ty)
        current.instructions.append(mir.Move(dest=dest_ssa, source=val_ssa))
        env.bind_user(stmt.target.ident, dest_ssa, val_ty)
        return current, env
    # Field update
    if isinstance(stmt.target, ast.Attr):
        base_ssa, base_ty, current, env = lower_expr_to_ssa(stmt.target.value, env, current, checked, blocks, fresh_block)
        field_ty = _lookup_field_type(base_ty, stmt.target.attr, checked)
        val_ssa, val_ty, current, env = lower_expr_to_ssa(stmt.value, env, current, checked, blocks, fresh_block)
        if val_ty != field_ty:
            raise LoweringError(f"type mismatch setting field {stmt.target.attr}: expected {field_ty}, got {val_ty}")
        current.instructions.append(
            mir.FieldSet(base=base_ssa, field=stmt.target.attr, value=val_ssa, loc=getattr(stmt, "loc", None))
        )
        return current, env
    # Array element update
    if isinstance(stmt.target, ast.Index):
        base_ssa, base_ty, current, env = lower_expr_to_ssa(stmt.target.value, env, current, checked, blocks, fresh_block)
        idx_ssa, idx_ty, current, env = lower_expr_to_ssa(stmt.target.index, env, current, checked, blocks, fresh_block)
        elem_ty = array_element_type(base_ty)
        if elem_ty is None:
            raise LoweringError(f"type {base_ty} is not indexable")
        val_ssa, val_ty, current, env = lower_expr_to_ssa(stmt.value, env, current, checked, blocks, fresh_block)
        if val_ty != elem_ty:
            raise LoweringError(f"type mismatch storing into array element: expected {elem_ty}, got {val_ty}")
        current.instructions.append(
            mir.ArraySet(base=base_ssa, index=idx_ssa, value=val_ssa, loc=getattr(stmt, "loc", None))
        )
        return current, env
    raise LoweringError(f"unsupported assignment target {stmt.target}")


def lower_for(
    stmt: ast.ForStmt,
    env: SSAEnv,
    blocks: Dict[str, mir.BasicBlock],
    current: mir.BasicBlock,
    checked: CheckedProgram,
    fresh_block: callable,
) -> Tuple[mir.BasicBlock, SSAEnv]:
    iter_ssa, iter_ty, current, env = lower_expr_to_ssa(stmt.iter_expr, env, current, checked, blocks, fresh_block)
    elem_ty = array_element_type(iter_ty)
    if elem_ty is None:
        raise LoweringError(f"for-loop expects array iterable, got {iter_ty}")
    # Bind iterable to a temp SSA name to avoid re-evaluation.
    iter_tmp = env.fresh_ssa("for_arr", iter_ty)
    current.instructions.append(mir.Move(dest=iter_tmp, source=iter_ssa))
    env.ctx.ssa_types[iter_tmp] = iter_ty
    env.bind_user(f"__for_arr_{stmt.var}", iter_tmp, iter_ty)
    # Length
    len_ssa = env.fresh_ssa("for_len", I64)
    current.instructions.append(mir.ArrayLen(dest=len_ssa, base=iter_tmp))
    env.ctx.ssa_types[len_ssa] = I64
    env.bind_user(f"__for_len_{stmt.var}", len_ssa, I64)
    # Index
    idx_ssa = env.fresh_ssa("for_idx", I64)
    current.instructions.append(mir.Const(dest=idx_ssa, type=I64, value=0))
    env.ctx.ssa_types[idx_ssa] = I64
    env.bind_user(f"__for_idx_{stmt.var}", idx_ssa, I64)
    # while idx < len
    cond_expr = ast.Binary(
        loc=stmt.loc,
        op="<",
        left=ast.Name(loc=stmt.loc, ident=f"__for_idx_{stmt.var}"),
        right=ast.Name(loc=stmt.loc, ident=f"__for_len_{stmt.var}"),
    )
    # body: let var = arr[idx]; body stmts; idx = idx + 1
    loop_var_let = ast.LetStmt(
        loc=stmt.loc,
        name=stmt.var,
        type_expr=None,
        value=ast.Index(
            loc=stmt.loc,
            value=ast.Name(loc=stmt.loc, ident=f"__for_arr_{stmt.var}"),
            index=ast.Name(loc=stmt.loc, ident=f"__for_idx_{stmt.var}"),
        ),
        mutable=False,
        capture=False,
        capture_alias=None,
    )
    incr = ast.AssignStmt(
        loc=stmt.loc,
        target=ast.Name(loc=stmt.loc, ident=f"__for_idx_{stmt.var}"),
        value=ast.Binary(
            loc=stmt.loc,
            op="+",
            left=ast.Name(loc=stmt.loc, ident=f"__for_idx_{stmt.var}"),
            right=ast.Literal(loc=stmt.loc, value=1),
        ),
    )
    body_block = ast.Block(statements=[loop_var_let] + stmt.body.statements + [ast.ExprStmt(loc=stmt.loc, value=incr.value) if False else incr])
    while_stmt = ast.WhileStmt(loc=stmt.loc, condition=cond_expr, body=body_block)
    return lower_while(while_stmt, env, blocks, current, checked, fresh_block)


def lower_if(
    stmt: ast.IfStmt,
    env: SSAEnv,
    blocks: Dict[str, mir.BasicBlock],
    current: mir.BasicBlock,
    checked: CheckedProgram,
    fresh_block: callable,
) -> Tuple[mir.BasicBlock, SSAEnv]:
    """SSA if/else lowering with block params as φ (scaffold)."""
    cond_ssa, _, current, env = lower_expr_to_ssa(stmt.condition, env, current, checked, blocks, fresh_block)

    then_name = fresh_block("bb_then")
    else_name = fresh_block("bb_else")
    join_name = fresh_block("bb_join")

    then_block = mir.BasicBlock(name=then_name)
    else_block = mir.BasicBlock(name=else_name)
    join_block = mir.BasicBlock(name=join_name)

    blocks[then_name] = then_block
    blocks[else_name] = else_block
    blocks[join_name] = join_block

    live_users = list(env.snapshot_live_user_names())
    current.terminator = mir.CondBr(
        cond=cond_ssa,
        then=mir.Edge(target=then_name, args=[]),
        els=mir.Edge(target=else_name, args=[]),
    )

    # Join params (fresh SSA) and branch envs.
    join_params: list[mir.Param] = []
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        phi_ssa = env.fresh_ssa(f"{u}_phi", ty=ty)
        join_params.append(mir.Param(name=phi_ssa, type=ty))
    join_block.params = join_params

    then_env = env.clone_for_block({u: env.lookup_user(u) for u in live_users})
    else_env = env.clone_for_block({u: env.lookup_user(u) for u in live_users})

    then_block, then_env = lower_block_in_env(stmt.then_block.statements, then_env, blocks, then_block, checked, fresh_block)
    join_referenced = False
    if then_block.terminator is None:
        then_block.terminator = mir.Br(
            target=mir.Edge(target=join_name, args=[then_env.lookup_user(u) for u in live_users])
        )
        join_referenced = True

    if stmt.else_block:
        else_block, else_env = lower_block_in_env(
            stmt.else_block.statements, else_env, blocks, else_block, checked, fresh_block
        )
        if else_block.terminator is None:
            else_block.terminator = mir.Br(
                target=mir.Edge(target=join_name, args=[else_env.lookup_user(u) for u in live_users])
            )
            join_referenced = True
    else:
        # No else: jump to join with current env values.
        else_block.terminator = mir.Br(
            target=mir.Edge(target=join_name, args=[env.lookup_user(u) for u in live_users])
        )
        join_referenced = True

    if not join_referenced:
        # Both branches terminate; no join needed.
        blocks.pop(join_name, None)
        return then_block, then_env
    join_env_map = {u: join_params[i].name for i, u in enumerate(live_users)}
    join_env = env.clone_for_block(join_env_map)

    return join_block, join_env


def lower_while(
    stmt: ast.WhileStmt,
    env: SSAEnv,
    blocks: Dict[str, mir.BasicBlock],
    current: mir.BasicBlock,
    checked: CheckedProgram,
    fresh_block: callable,
) -> Tuple[mir.BasicBlock, SSAEnv]:
    """SSA while lowering with block params as φ (scaffold)."""
    header_name = fresh_block("bb_while_header")
    body_name = fresh_block("bb_while_body")
    after_name = fresh_block("bb_while_after")

    header_block = mir.BasicBlock(name=header_name)
    body_block = mir.BasicBlock(name=body_name)
    after_block = mir.BasicBlock(name=after_name)

    blocks[header_name] = header_block
    blocks[body_name] = body_block
    blocks[after_name] = after_block

    live_users = list(env.snapshot_live_user_names())
    init_args = [env.lookup_user(u) for u in live_users]
    current.terminator = mir.Br(target=mir.Edge(target=header_name, args=init_args))

    # Header params (phi-like)
    header_params: list[mir.Param] = []
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        phi_ssa = env.fresh_ssa(f"{u}_phi", ty=ty)
        header_params.append(mir.Param(name=phi_ssa, type=ty))
    header_block.params = header_params

    header_env = env.clone_for_block({u: header_params[i].name for i, u in enumerate(live_users)})

    # Lower condition
    cond_ssa, _, header_block, header_env = lower_expr_to_ssa(
        stmt.condition, header_env, header_block, checked, blocks, fresh_block
    )

    # Prepare body env starting from header env
    body_env = header_env.clone_for_block({u: header_env.lookup_user(u) for u in live_users})

    header_block.terminator = mir.CondBr(
        cond=cond_ssa,
        then=mir.Edge(target=body_name, args=[]),
        els=mir.Edge(target=after_name, args=[header_env.lookup_user(u) for u in live_users]),
    )

    # Lower body
    body_block, body_env = lower_block_in_env(stmt.body.statements, body_env, blocks, body_block, checked, fresh_block)
    if body_block.terminator is None:
        body_block.terminator = mir.Br(
            target=mir.Edge(target=header_name, args=[body_env.lookup_user(u) for u in live_users])
        )

    # After env maps live users to header params (loop exit).
    after_params: list[mir.Param] = []
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        out_ssa = env.fresh_ssa(f"{u}_out", ty=ty)
        after_params.append(mir.Param(name=out_ssa, type=ty))
    after_block.params = after_params

    after_env = env.clone_for_block({u: after_params[i].name for i, u in enumerate(live_users)})
    return after_block, after_env


def lower_expr_to_ssa(
    expr: ast.Expr,
    env: SSAEnv,
    current: mir.BasicBlock,
    checked: CheckedProgram,
    blocks: Dict[str, mir.BasicBlock] | None = None,
    fresh_block: callable | None = None,
) -> Tuple[str, Type, mir.BasicBlock, SSAEnv]:
    """Lower an expression to SSA, emitting instructions into the current block.

    Returns (ssa_name, type, current_block, env) where current_block/env reflect any
    control-flow split (e.g., try/else lowering).
    """
    lowered_idx = _maybe_lower_args_view_index(expr, env, current, checked)
    if lowered_idx is not None:
        result_ssa, result_ty, current, env = lowered_idx
        return result_ssa, result_ty, current, env
    # Error.attrs indexing -> __exc_attrs_get_dv (DiagnosticValue).
    if isinstance(expr, ast.Index) and isinstance(expr.value, ast.Attr) and expr.value.attr == "attrs":
        base_ssa, base_ty, current, env = lower_expr_to_ssa(expr.value.value, env, current, checked, blocks, fresh_block)
        if base_ty != ERROR and base_ty.name not in checked.exceptions:
            raise LoweringError("attrs access only supported on Error values")
        key_ssa, key_ty, current, env = lower_expr_to_ssa(expr.index, env, current, checked, blocks, fresh_block)
        if key_ty != STR:
            raise LoweringError("attrs index expects String key")
        dest = env.fresh_ssa("exc_attr_dv", Type("DiagnosticValue"))
        current.instructions.append(
            mir.Call(
                dest=dest,
                callee="__exc_attrs_get_dv",
                args=[base_ssa, key_ssa],
                ret_type=Type("DiagnosticValue"),
                err_dest=None,
                normal=None,
                error=None,
                loc=getattr(expr, "loc", None),
            )
        )
        env.ctx.ssa_types[dest] = Type("DiagnosticValue")
        return dest, Type("DiagnosticValue"), current, env
    if isinstance(expr, ast.ExceptionCtor):
        # Materialize event code.
        code_ssa = env.fresh_ssa("exc_code", I64)
        loc = getattr(expr, "loc", None)
        current.instructions.append(mir.Const(dest=code_ssa, type=I64, value=expr.event_code, loc=loc))
        env.ctx.ssa_types[code_ssa] = I64
        # Create the base Error (seeded with empty key/payload for now).
        empty_str = env.fresh_ssa("exc_empty", STR)
        current.instructions.append(mir.Const(dest=empty_str, type=STR, value="", loc=loc))
        env.ctx.ssa_types[empty_str] = STR
        dest_err = env.fresh_ssa("exc", ERROR)
        current.instructions.append(
            mir.Call(
                dest=dest_err,
                callee="drift_error_new_dummy",
                args=[code_ssa, empty_str, empty_str],
                ret_type=ERROR,
                err_dest=None,
                normal=None,
                error=None,
                loc=loc,
            )
        )
        env.ctx.ssa_types[dest_err] = ERROR
        # Add attrs via typed helper.
        if expr.fields and expr.arg_order:
            for name in expr.arg_order:
                if name not in expr.fields:
                    continue
                key_const = env.fresh_ssa(f"{name}_key", STR)
                current.instructions.append(mir.Const(dest=key_const, type=STR, value=name, loc=loc))
                env.ctx.ssa_types[key_const] = STR
                val_ssa, val_ty, current, env = lower_expr_to_ssa(
                    expr.fields[name], env, current, checked, blocks, fresh_block
                )
                # Wrap field into DiagnosticValue via runtime helpers for primitives.
                dv_dest = env.fresh_ssa(f"{name}_dv", Type("DiagnosticValue"))
                if val_ty in (INT, I64):
                    current.instructions.append(
                        mir.Call(
                            dest=dv_dest,
                            callee="drift_dv_int",
                            args=[val_ssa],
                            ret_type=Type("DiagnosticValue"),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                elif val_ty == BOOL:
                    current.instructions.append(
                        mir.Call(
                            dest=dv_dest,
                            callee="drift_dv_bool",
                            args=[val_ssa],
                            ret_type=Type("DiagnosticValue"),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                elif val_ty == STR:
                    current.instructions.append(
                        mir.Call(
                            dest=dv_dest,
                            callee="drift_dv_string",
                            args=[val_ssa],
                            ret_type=Type("DiagnosticValue"),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                else:
                    raise LoweringError(f"exception field '{name}' type {val_ty} not yet supported for attrs")
                env.ctx.ssa_types[dv_dest] = Type("DiagnosticValue")
                # Add typed attr.
                current.instructions.append(
                    mir.Call(
                        dest=env.fresh_ssa(f"{name}_add", UNIT),
                        callee="drift_error_add_attr_dv",
                        args=[dest_err, key_const, dv_dest],
                        ret_type=UNIT,
                        err_dest=None,
                        normal=None,
                        error=None,
                        loc=loc,
                    )
                )
                # Legacy string arg for backward compatibility (string only).
                if val_ty == STR:
                    current.instructions.append(
                        mir.Call(
                            dest=env.fresh_ssa(f"{name}_add_arg", UNIT),
                            callee="drift_error_add_arg",
                            args=[dest_err, key_const, val_ssa],
                            ret_type=UNIT,
                            err_dest=None,
                            normal=None,
                            error=None,
                        loc=loc,
                    )
                )
        # Capture any ^ locals as typed diagnostics.
        if env.capture_env:
            frame_const = env.fresh_ssa("frame_name", STR)
            current.instructions.append(mir.Const(dest=frame_const, type=STR, value=env.frame_name, loc=loc))
            env.ctx.ssa_types[frame_const] = STR
            for cap_key, cap_ssa, cap_ty in env.snapshot_captures():
                # Convert captured value to DiagnosticValue.
                dv_dest = env.fresh_ssa(f"{cap_key}_dv", Type("DiagnosticValue"))
                if cap_ty in (INT, I64):
                    current.instructions.append(
                        mir.Call(
                            dest=dv_dest,
                            callee="drift_dv_int",
                            args=[cap_ssa],
                            ret_type=Type("DiagnosticValue"),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                elif cap_ty == BOOL:
                    current.instructions.append(
                        mir.Call(
                            dest=dv_dest,
                            callee="drift_dv_bool",
                            args=[cap_ssa],
                            ret_type=Type("DiagnosticValue"),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                elif cap_ty == STR:
                    current.instructions.append(
                        mir.Call(
                            dest=dv_dest,
                            callee="drift_dv_string",
                            args=[cap_ssa],
                            ret_type=Type("DiagnosticValue"),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                else:
                    raise LoweringError(f"captured local '{cap_key}' type {cap_ty} not supported for diagnostics")
                env.ctx.ssa_types[dv_dest] = Type("DiagnosticValue")
                key_const = env.fresh_ssa(f"{cap_key}_key", STR)
                current.instructions.append(mir.Const(dest=key_const, type=STR, value=cap_key, loc=loc))
                env.ctx.ssa_types[key_const] = STR
                current.instructions.append(
                    mir.Call(
                        dest=env.fresh_ssa(f"{cap_key}_add", UNIT),
                        callee="drift_error_add_local_dv",
                        args=[dest_err, frame_const, key_const, dv_dest],
                        ret_type=UNIT,
                        err_dest=None,
                        normal=None,
                        error=None,
                        loc=loc,
                    )
                )
        return dest_err, ERROR, current, env
    # Names
    if isinstance(expr, ast.Name):
        ssa = env.lookup_user(expr.ident)
        ty = env.ctx.ssa_types.get(ssa)
        if ty is None:
            raise LoweringError(f"unknown type for SSA name '{ssa}'")
        return ssa, ty, current, env
    # Literals
    if isinstance(expr, ast.Literal):
        lit_val = expr.value
        lit_ty = _type_of_literal(lit_val)
        dest = env.fresh_ssa("lit", lit_ty)
        current.instructions.append(mir.Const(dest=dest, type=lit_ty, value=lit_val, loc=getattr(expr, "loc", None)))
        env.ctx.ssa_types[dest] = lit_ty
        return dest, lit_ty, current, env
    if isinstance(expr, ast.ArrayLiteral):
        if not expr.elements:
            raise LoweringError("empty array literals not supported in SSA lowering")
        elem_ssa: List[str] = []
        elem_ty: Optional[Type] = None
        for e in expr.elements:
            v, ty, current, env = lower_expr_to_ssa(e, env, current, checked, blocks, fresh_block)
            if elem_ty is None:
                elem_ty = ty
            elif ty != elem_ty:
                raise LoweringError("heterogeneous array literal not supported in SSA lowering")
            elem_ssa.append(v)
        assert elem_ty is not None
        arr_ty = array_of(elem_ty)
        dest = env.fresh_ssa("arr", arr_ty)
        current.instructions.append(
            mir.ArrayLiteral(dest=dest, elem_type=elem_ty, elements=elem_ssa, loc=getattr(expr, "loc", None))
        )
        env.ctx.ssa_types[dest] = arr_ty
        return dest, arr_ty, current, env
    # Binary ops
    if isinstance(expr, ast.Binary):
        lhs, lhs_ty, current, env = lower_expr_to_ssa(expr.left, env, current, checked, blocks, fresh_block)
        rhs, rhs_ty, current, env = lower_expr_to_ssa(expr.right, env, current, checked, blocks, fresh_block)
        loc = getattr(expr, "loc", None)
        # String equality/inequality routed via runtime helper.
        if lhs_ty == STR and rhs_ty == STR and expr.op in {"==", "!="}:
            dest = env.fresh_ssa("strcmp", BOOL)
            current.instructions.append(
                mir.Call(
                    dest=dest,
                    callee="drift_string_eq",
                    args=[lhs, rhs],
                    ret_type=BOOL,
                    err_dest=None,
                    normal=None,
                    error=None,
                    loc=loc,
                )
            )
            env.ctx.ssa_types[dest] = BOOL
            # If op is !=, add a not.
            if expr.op == "!=":
                inv_dest = env.fresh_ssa("not", BOOL)
                current.instructions.append(
                    mir.Unary(dest=inv_dest, op="not", operand=dest, loc=loc)
                )
                env.ctx.ssa_types[inv_dest] = BOOL
                return inv_dest, BOOL, current, env
            return dest, BOOL, current, env
        dest = env.fresh_ssa("bin")
        current.instructions.append(mir.Binary(dest=dest, op=expr.op, left=lhs, right=rhs, loc=loc))
        if expr.op in {"==", "!=", "<", "<=", ">", ">="}:
            res_ty = BOOL
        else:
            res_ty = lhs_ty
        env.ctx.ssa_types[dest] = res_ty
        return dest, res_ty, current, env
    # Calls (simple name callee, no error edges in scaffold)
    if isinstance(expr, ast.Call):
        # Optional<T> builtin methods.
        if isinstance(expr.func, ast.Attr) and isinstance(expr.func.value, ast.Name) and env.has_user(expr.func.value.ident):
            base_ssa = env.lookup_user(expr.func.value.ident)
            base_ty = env.ctx.ssa_types.get(base_ssa)
            # DiagnosticValue methods
            if base_ty and base_ty.name == "DiagnosticValue":
                loc = getattr(expr, "loc", None)
                method = expr.func.attr
                if method == "get":
                    if len(expr.args) != 1:
                        raise LoweringError("DiagnosticValue.get expects one argument")
                    key_ssa, key_ty, current, env = lower_expr_to_ssa(expr.args[0], env, current, checked, blocks, fresh_block)
                    if key_ty != STR:
                        raise LoweringError("DiagnosticValue.get expects String key")
                    dest = env.fresh_ssa("dv_get", Type("DiagnosticValue"))
                    current.instructions.append(
                        mir.Call(
                            dest=dest,
                            callee="drift_dv_get",
                            args=[base_ssa, key_ssa],
                            ret_type=Type("DiagnosticValue"),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                    env.ctx.ssa_types[dest] = Type("DiagnosticValue")
                    return dest, Type("DiagnosticValue"), current, env
                if method == "index":
                    if len(expr.args) != 1:
                        raise LoweringError("DiagnosticValue.index expects one argument")
                    idx_ssa, idx_ty, current, env = lower_expr_to_ssa(expr.args[0], env, current, checked, blocks, fresh_block)
                    if idx_ty not in (INT, I64):
                        raise LoweringError("DiagnosticValue.index expects Int index")
                    dest = env.fresh_ssa("dv_index", Type("DiagnosticValue"))
                    current.instructions.append(
                        mir.Call(
                            dest=dest,
                            callee="drift_dv_index",
                            args=[base_ssa, idx_ssa],
                            ret_type=Type("DiagnosticValue"),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                    env.ctx.ssa_types[dest] = Type("DiagnosticValue")
                    return dest, Type("DiagnosticValue"), current, env
                if method == "as_int":
                    dest = env.fresh_ssa("dv_as_int", Type("Optional", (INT,)))
                    current.instructions.append(
                        mir.Call(
                            dest=dest,
                            callee="drift_dv_as_int",
                            args=[base_ssa],
                            ret_type=Type("Optional", (INT,)),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                    env.ctx.ssa_types[dest] = Type("Optional", (INT,))
                    return dest, Type("Optional", (INT,)), current, env
                if method == "as_bool":
                    dest = env.fresh_ssa("dv_as_bool", Type("Optional", (BOOL,)))
                    current.instructions.append(
                        mir.Call(
                            dest=dest,
                            callee="drift_dv_as_bool",
                            args=[base_ssa],
                            ret_type=Type("Optional", (BOOL,)),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                    env.ctx.ssa_types[dest] = Type("Optional", (BOOL,))
                    return dest, Type("Optional", (BOOL,)), current, env
                if method == "as_float":
                    dest = env.fresh_ssa("dv_as_float", Type("Optional", (Type("Float64"),)))
                    current.instructions.append(
                        mir.Call(
                            dest=dest,
                            callee="drift_dv_as_float",
                            args=[base_ssa],
                            ret_type=Type("Optional", (Type("Float64"),)),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                    env.ctx.ssa_types[dest] = Type("Optional", (Type("Float64"),))
                    return dest, Type("Optional", (Type("Float64"),)), current, env
                if method == "as_string":
                    dest = env.fresh_ssa("dv_as_string", Type("Optional", (STR,)))
                    current.instructions.append(
                        mir.Call(
                            dest=dest,
                            callee="drift_dv_as_string",
                            args=[base_ssa],
                            ret_type=Type("Optional", (STR,)),
                            err_dest=None,
                            normal=None,
                            error=None,
                            loc=loc,
                        )
                    )
                    env.ctx.ssa_types[dest] = Type("Optional", (STR,))
                    return dest, Type("Optional", (STR,)), current, env
            if base_ty and base_ty.name == "Optional" and base_ty.args:
                inner_ty = base_ty.args[0]
                loc = getattr(expr, "loc", None)
                if expr.func.attr == "is_some":
                    dest = env.fresh_ssa("opt_is_some", BOOL)
                    current.instructions.append(mir.FieldGet(dest=dest, base=base_ssa, field="is_some", loc=loc))
                    env.ctx.ssa_types[dest] = BOOL
                    return dest, BOOL, current, env
                if expr.func.attr == "is_none":
                    tag = env.fresh_ssa("opt_is_some", BOOL)
                    current.instructions.append(mir.FieldGet(dest=tag, base=base_ssa, field="is_some", loc=loc))
                    env.ctx.ssa_types[tag] = BOOL
                    inv_dest = env.fresh_ssa("opt_is_none", BOOL)
                    current.instructions.append(mir.Unary(dest=inv_dest, op="not", operand=tag, loc=loc))
                    env.ctx.ssa_types[inv_dest] = BOOL
                    return inv_dest, BOOL, current, env
                if expr.func.attr == "unwrap_or":
                    if not (blocks and fresh_block):
                        raise LoweringError("unwrap_or lowering requires block context")
                    if len(expr.args) != 1:
                        raise LoweringError("Optional.unwrap_or expects exactly one argument")
                    default_ssa, default_ty, current, env = lower_expr_to_ssa(
                        expr.args[0], env, current, checked, blocks, fresh_block
                    )
                    if default_ty != inner_ty:
                        raise LoweringError("unwrap_or default type mismatch")
                    tag = env.fresh_ssa("opt_is_some", BOOL)
                    current.instructions.append(mir.FieldGet(dest=tag, base=base_ssa, field="is_some", loc=loc))
                    env.ctx.ssa_types[tag] = BOOL
                    some_val = env.fresh_ssa("opt_value", inner_ty)
                    current.instructions.append(mir.FieldGet(dest=some_val, base=base_ssa, field="value", loc=loc))
                    env.ctx.ssa_types[some_val] = inner_ty
                    then_name = fresh_block("bb_optional_some")
                    else_name = fresh_block("bb_optional_none")
                    join_name = fresh_block("bb_optional_join")
                    then_param = mir.Param(name=env.fresh_ssa("opt_val_param", ty=inner_ty), type=inner_ty)
                    else_param = mir.Param(name=env.fresh_ssa("opt_default_param", ty=inner_ty), type=inner_ty)
                    then_block = mir.BasicBlock(name=then_name, params=[then_param])
                    else_block = mir.BasicBlock(name=else_name, params=[else_param])
                    join_param = mir.Param(name=env.fresh_ssa("opt_res", ty=inner_ty), type=inner_ty)
                    join_block = mir.BasicBlock(name=join_name, params=[join_param])
                    blocks[then_name] = then_block
                    blocks[else_name] = else_block
                    blocks[join_name] = join_block
                    then_block.terminator = mir.Br(target=mir.Edge(target=join_name, args=[then_param.name]))
                    else_block.terminator = mir.Br(target=mir.Edge(target=join_name, args=[else_param.name]))
                    current.terminator = mir.CondBr(
                        cond=tag,
                        then=mir.Edge(target=then_name, args=[some_val]),
                        els=mir.Edge(target=else_name, args=[default_ssa]),
                    )
                    join_env = env.clone_for_block(env.user_env)
                    env.ctx.ssa_types[join_param.name] = inner_ty
                    return join_param.name, inner_ty, join_block, join_env
        # Bare name call
        if isinstance(expr.func, ast.Name):
            callee = expr.func.ident
            # Struct constructor shortcut.
            if callee in checked.structs:
                struct_info = checked.structs[callee]
                # Positional args map in field order; kwargs override by name.
                arg_values: Dict[str, str] = {}
                for kw in expr.kwargs:
                    v, _, current, env = lower_expr_to_ssa(kw.value, env, current, checked, blocks, fresh_block)
                    arg_values[kw.name] = v
                pos_args = []
                for a in expr.args:
                    v, _, current, env = lower_expr_to_ssa(a, env, current, checked, blocks, fresh_block)
                    pos_args.append(v)
                arg_ssa: List[str] = []
                pos_idx = 0
                for field in struct_info.field_order:
                    if field in arg_values:
                        arg_ssa.append(arg_values[field])
                    else:
                        if pos_idx >= len(pos_args):
                            raise LoweringError(f"missing constructor arg '{field}' for {callee}")
                        arg_ssa.append(pos_args[pos_idx])
                        pos_idx += 1
                dest = env.fresh_ssa(callee, Type(callee))
                current.instructions.append(mir.StructInit(dest=dest, type=Type(callee), args=arg_ssa))
                env.ctx.ssa_types[dest] = Type(callee)
                return dest, Type(callee), current, env
        elif isinstance(expr.func, ast.Attr) and isinstance(expr.func.value, ast.Name):
            # Method-style call on a resolved value; use the base type if known.
            base_ssa = env.lookup_user(expr.func.value.ident) if env.has_user(expr.func.value.ident) else None
            base_ty = env.ctx.ssa_types.get(base_ssa) if base_ssa else None
            method_callee = None
            if base_ty and base_ty.name in checked.structs:
                struct_info = checked.structs[base_ty.name]
                if struct_info.methods and expr.func.attr in struct_info.methods:
                    method_callee = f"{base_ty.name}.{expr.func.attr}"
                    # If this is an args-view key helper (no args), synthesize ArgKey directly.
                    if not expr.args and base_ty.name.endswith("ArgsView"):
                        key_ty = Type(base_ty.name.replace("ArgsView", "ArgKey"))
                        key_const = env.fresh_ssa(f"{expr.func.attr}_key", STR)
                        current.instructions.append(
                            mir.Const(dest=key_const, type=STR, value=expr.func.attr, loc=getattr(expr, "loc", None))
                        )
                        env.ctx.ssa_types[key_const] = STR
                        dest = env.fresh_ssa(expr.func.attr, key_ty)
                        current.instructions.append(mir.StructInit(dest=dest, type=key_ty, args=[key_const]))
                        env.ctx.ssa_types[dest] = key_ty
                        return dest, key_ty, current, env
            callee = method_callee or f"{expr.func.value.ident}.{expr.func.attr}"
        else:
            raise LoweringError("unsupported call callee shape in SSA lowering")
        if callee not in checked.functions:
            raise LoweringError(f"unknown function '{callee}' in SSA lowering")
        arg_ssa: List[str] = []
        for a in expr.args:
            v, _, current, env = lower_expr_to_ssa(a, env, current, checked, blocks, fresh_block)
            arg_ssa.append(v)
        ret_ty = checked.functions[callee].signature.return_type
        dest = env.fresh_ssa(callee, ret_ty)
        current.instructions.append(
            mir.Call(dest=dest, callee=callee, args=arg_ssa, ret_type=ret_ty, err_dest=None, normal=None, error=None, loc=getattr(expr, "loc", None))
        )
        env.ctx.ssa_types[dest] = ret_ty
        return dest, ret_ty, current, env
    # Array indexing
    if isinstance(expr, ast.Index):
        # Try args-view special-case first.
        lowered_idx = _maybe_lower_args_view_index(expr, env, current, checked)
        if lowered_idx is not None:
            dest, dest_ty, current, env = lowered_idx
            return dest, dest_ty, current, env
        base_ssa, base_ty, current, env = lower_expr_to_ssa(expr.value, env, current, checked, blocks, fresh_block)
        idx_ssa, idx_ty, current, env = lower_expr_to_ssa(expr.index, env, current, checked, blocks, fresh_block)
        elem_ty = array_element_type(base_ty)
        if elem_ty is None:
            raise LoweringError(f"type {base_ty} is not indexable")
        dest = env.fresh_ssa("idx", elem_ty)
        current.instructions.append(
            mir.ArrayGet(dest=dest, base=base_ssa, index=idx_ssa, loc=getattr(expr, "loc", None))
        )
        env.ctx.ssa_types[dest] = elem_ty
        return dest, elem_ty, current, env
    # Borrow/addr-of: create a new SSA name with ref type, move from the base.
    if isinstance(expr, ast.Unary) and expr.op in {"&", "&mut"}:
        operand_ssa, operand_ty, current, env = lower_expr_to_ssa(expr.operand, env, current, checked, blocks, fresh_block)
        ref_ty = ref_of(operand_ty, mutable=(expr.op == "&mut"))
        dest = env.fresh_ssa("ref", ref_ty)
        current.instructions.append(mir.Move(dest=dest, source=operand_ssa))
        env.ctx.ssa_types[dest] = ref_ty
        return dest, ref_ty, current, env
    if isinstance(expr, ast.Unary):
        operand_ssa, operand_ty, current, env = lower_expr_to_ssa(expr.operand, env, current, checked, blocks, fresh_block)
        out_ty = operand_ty if expr.op == "-" else BOOL
        dest = env.fresh_ssa("unary", out_ty)
        current.instructions.append(
            mir.Unary(dest=dest, op=expr.op, operand=operand_ssa, loc=getattr(expr, "loc", None))
        )
        env.ctx.ssa_types[dest] = out_ty
        return dest, out_ty, current, env
    if isinstance(expr, ast.Attr):
        base_ssa, base_ty, current, env = lower_expr_to_ssa(expr.value, env, current, checked, blocks, fresh_block)
        exc_info = checked.exceptions.get(base_ty.name)
        if exc_info and expr.attr == "args":
            view_ty = Type(exc_info.args_view_type)
            dest = env.fresh_ssa("args_view", view_ty)
            current.instructions.append(mir.StructInit(dest=dest, type=view_ty, args=[base_ssa]))
            env.ctx.ssa_types[dest] = view_ty
            return dest, view_ty, current, env
        if expr.attr == "attrs":
            dest = env.fresh_ssa("attrs_view", Type("ErrorAttrs"))
            current.instructions.append(mir.Move(dest=dest, source=base_ssa))
            env.ctx.ssa_types[dest] = Type("ErrorAttrs")
            return dest, Type("ErrorAttrs"), current, env
        field_ty = _lookup_field_type(base_ty, expr.attr, checked)
        dest = env.fresh_ssa(expr.attr, field_ty)
        current.instructions.append(mir.FieldGet(dest=dest, base=base_ssa, field=expr.attr, loc=getattr(expr, "loc", None)))
        env.ctx.ssa_types[dest] = field_ty
        return dest, field_ty, current, env
    if isinstance(expr, ast.TryCatchExpr):
        if blocks is None or fresh_block is None:
            raise LoweringError("try/catch expression lowering requires block context")
        return _lower_try_catch_expr(expr, env, current, checked, blocks, fresh_block)
    raise LoweringError(f"lower_expr_to_ssa scaffold does not handle {expr}")


def _infer_expr_type(expr: ast.Expr, checked: CheckedProgram) -> Type:
    # Placeholder: not used by SSA lowering paths; kept for compatibility.
    raise LoweringError("type inference scaffold incomplete")


def _type_of_literal(value: object) -> Type:
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int):
        return INT
    if isinstance(value, float):
        return F64
    if isinstance(value, str):
        return STR
    raise LoweringError(f"unsupported literal {value!r}")


class LoweringError(Exception):
    pass


def _lookup_field_type(base_ty: Type, field: str, checked: CheckedProgram) -> Type:
    if isinstance(base_ty, ReferenceType) and base_ty.args:
        base_ty = base_ty.args[0]
    if base_ty == ERROR:
        if field == "code":
            return I64
        if field == "payload":
            return STR
        raise LoweringError(f"Error has no field '{field}'")
    struct_info = checked.structs.get(base_ty.name)
    if struct_info:
        if field not in struct_info.field_types:
            raise LoweringError(f"unknown field '{field}' on struct {base_ty.name}")
        return struct_info.field_types[field]
    exc_info = checked.exceptions.get(base_ty.name)
    if exc_info:
        if field not in exc_info.arg_types:
            raise LoweringError(f"unknown field '{field}' on exception {base_ty.name}")
        return exc_info.arg_types[field]
    raise LoweringError(f"type {base_ty} has no fields")


def _ssa_read_error_event(err_ssa: str, env: SSAEnv, current: mir.BasicBlock) -> tuple[str, mir.BasicBlock]:
    """Emit MIR to read the error event/code into an SSA name."""
    dest = env.fresh_ssa("err_event", I64)
    current.instructions.append(mir.ErrorEvent(dest=dest, error=err_ssa))
    env.ctx.ssa_types[dest] = I64
    return dest, current


def _callee_name(call: ast.Call) -> Optional[str]:
    if isinstance(call.func, ast.Name):
        return call.func.ident
    if isinstance(call.func, ast.Attr) and isinstance(call.func.value, ast.Name):
        return f"{call.func.value.ident}.{call.func.attr}"
    return None


def _lower_try_catch_expr(
    expr: ast.TryCatchExpr,
    env: SSAEnv,
    current: mir.BasicBlock,
    checked: CheckedProgram,
    blocks: Dict[str, mir.BasicBlock],
    fresh_block: callable,
) -> Tuple[str, Type, mir.BasicBlock, SSAEnv]:
    """Lower try/catch expression to SSA using error edges (with event dispatch)."""
    if not isinstance(expr.attempt, ast.Call):
        raise LoweringError("try/catch expression lowering currently supports call attempts only")
    callee = _callee_name(expr.attempt)
    if callee is None:
        raise LoweringError("try/catch expression lowering supports simple name/attr callees only")
    if callee not in checked.functions:
        raise LoweringError(f"unknown function '{callee}' in try")
    ret_ty = checked.functions[callee].signature.return_type
    if not expr.catch_arms:
        raise LoweringError("try/catch expression requires at least one catch arm")
    live_users = list(env.snapshot_live_user_names())
    # Join block: result + live users.
    join_name = fresh_block("bb_try_join")
    res_param = mir.Param(name=env.fresh_ssa("try_res", ty=ret_ty), type=ret_ty)
    env.ctx.ssa_types[res_param.name] = ret_ty
    join_params: List[mir.Param] = [res_param]
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        join_params.append(mir.Param(name=env.fresh_ssa(f"{u}_phi", ty=ty), type=ty))
    join_block = mir.BasicBlock(name=join_name, params=join_params)
    blocks[join_name] = join_block
    join_env = env.clone_for_block({u: join_params[i + 1].name for i, u in enumerate(live_users)})
    # Build catch blocks.
    event_catches: List[tuple[int, str]] = []
    catch_alls: List[str] = []
    catch_envs: Dict[str, SSAEnv] = {}
    for idx, arm in enumerate(expr.catch_arms):
        catch_name = fresh_block(f"bb_try_expr_catch{idx}")
        catch_params: List[mir.Param] = [mir.Param(name=env.fresh_ssa("err_val", ty=ERROR), type=ERROR)]
        for u in live_users:
            ty = env.ctx.ssa_types[env.lookup_user(u)]
            catch_params.append(mir.Param(name=env.fresh_ssa(f"{u}_catch", ty=ty), type=ty))
        catch_block = mir.BasicBlock(name=catch_name, params=catch_params)
        blocks[catch_name] = catch_block
        catch_env = env.clone_for_block({u: p.name for u, p in zip(live_users, catch_params[1:])})
        if arm.binder:
            binder_ty = ERROR
            if arm.event and arm.event in checked.exceptions:
                binder_ty = Type(arm.event)
            catch_env.bind_user(arm.binder, catch_params[0].name, binder_ty)
        if not arm.block.statements:
            raise LoweringError("catch block must produce a value")
        catch_stmts = arm.block.statements
        for stmt in catch_stmts[:-1]:
            catch_block, catch_env = lower_block_in_env([stmt], catch_env, blocks, catch_block, checked, fresh_block)
        last_stmt = catch_stmts[-1]
        if isinstance(last_stmt, ast.ExprStmt):
            fallback_ssa, fallback_ty, catch_block, catch_env = lower_expr_to_ssa(
                last_stmt.value, catch_env, catch_block, checked, blocks, fresh_block
            )
        elif isinstance(last_stmt, ast.ReturnStmt) and last_stmt.value is not None:
            fallback_ssa, fallback_ty, catch_block, catch_env = lower_expr_to_ssa(
                last_stmt.value, catch_env, catch_block, checked, blocks, fresh_block
            )
        else:
            raise LoweringError("catch block must end with an expression to yield a value")
        if fallback_ty != ret_ty:
            raise LoweringError("try/catch result type mismatch")
        if catch_block.terminator is None:
            catch_block.terminator = mir.Br(
                target=mir.Edge(target=join_name, args=[fallback_ssa] + [catch_env.lookup_user(u) for u in live_users])
            )
        catch_envs[catch_name] = catch_env
        if arm.event:
            exc_info = checked.exceptions.get(arm.event)
            if exc_info is None:
                raise LoweringError(f"unknown event '{arm.event}' in catch")
            event_catches.append((exc_info.event_code, catch_name))
        else:
            catch_alls.append(catch_name)
    if len(catch_alls) > 1:
        raise LoweringError("multiple catch-all arms in try expression are not supported")
    # Dispatch block for error routing.
    dispatch_name = fresh_block("bb_try_expr_dispatch")
    dispatch_params: List[mir.Param] = [mir.Param(name=env.fresh_ssa("err_disp", ty=ERROR), type=ERROR)]
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        dispatch_params.append(mir.Param(name=env.fresh_ssa(f"{u}_disp", ty=ty), type=ty))
    dispatch_block = mir.BasicBlock(name=dispatch_name, params=dispatch_params)
    blocks[dispatch_name] = dispatch_block
    dispatch_env = env.clone_for_block({u: p.name for u, p in zip(live_users, dispatch_params[1:])})
    err_param_name = dispatch_params[0].name
    event_code, dispatch_block = _ssa_read_error_event(err_param_name, dispatch_env, dispatch_block)
    cursor_block = dispatch_block
    for idx, (event_code_val, catch_name) in enumerate(event_catches):
        cmp_dest = env.fresh_ssa(f"err_evt_cmp_expr{idx}", BOOL)
        env.ctx.ssa_types[cmp_dest] = BOOL
        const_name = env.fresh_ssa(f"err_evt_const_expr{idx}", I64)
        cursor_block.instructions.append(mir.Const(dest=const_name, type=I64, value=event_code_val))
        env.ctx.ssa_types[const_name] = I64
        cursor_block.instructions.append(
            mir.Binary(dest=cmp_dest, op="==", left=event_code, right=const_name, loc=expr.loc)
        )
        match_edge = mir.Edge(target=catch_name, args=[err_param_name] + [dispatch_env.lookup_user(u) for u in live_users])
        next_block_name = fresh_block(f"bb_try_expr_dispatch_next{idx}")
        next_block = mir.BasicBlock(name=next_block_name)
        blocks[next_block_name] = next_block
        cursor_block.terminator = mir.CondBr(
            cond=cmp_dest,
            then=match_edge,
            els=mir.Edge(target=next_block_name, args=[]),
        )
        cursor_block = next_block
    if catch_alls:
        target_catch = catch_alls[0]
        cursor_block.terminator = mir.Br(
            target=mir.Edge(target=target_catch, args=[err_param_name] + [dispatch_env.lookup_user(u) for u in live_users])
        )
    else:
        cursor_block.terminator = mir.Throw(error=err_param_name, loc=expr.loc)
    # Lower call attempt.
    call_args: List[str] = []
    for a in expr.attempt.args:
        v, _, current, env = lower_expr_to_ssa(a, env, current, checked, blocks, fresh_block)
        call_args.append(v)
    call_dest = env.fresh_ssa(callee, ret_ty)
    env.ctx.ssa_types[call_dest] = ret_ty
    call_err = env.fresh_ssa("err", ERROR)
    current.terminator = mir.Call(
        dest=call_dest,
        err_dest=call_err,
        callee=callee,
        args=call_args,
        ret_type=ret_ty,
        normal=mir.Edge(target=join_name, args=[call_dest] + [env.lookup_user(u) for u in live_users]),
        error=mir.Edge(target=dispatch_name, args=[call_err] + [env.lookup_user(u) for u in live_users]),
        loc=getattr(expr, "loc", None),
    )
    return res_param.name, ret_ty, join_block, join_env


def lower_try_stmt(
    stmt: ast.TryStmt,
    env: SSAEnv,
    blocks: Dict[str, mir.BasicBlock],
    current: mir.BasicBlock,
    checked: CheckedProgram,
    fresh_block: callable,
) -> Tuple[mir.BasicBlock, SSAEnv]:
    """Lower a try/catch statement with a call in the try block (call as last statement)."""
    if not stmt.catches:
        raise LoweringError("try/catch lowering requires at least one catch")
    if not stmt.body.statements:
        raise LoweringError("try/catch requires non-empty try body")
    # Lower all but the last statement using the normal block lowering.
    prelude_stmts = stmt.body.statements[:-1]
    try_final = stmt.body.statements[-1]
    if prelude_stmts:
        current, env = lower_block_in_env(prelude_stmts, env, blocks, current, checked, fresh_block)
        if current.terminator is not None:
            raise LoweringError("try body terminates before final call")
    if not isinstance(try_final, ast.ExprStmt):
        raise LoweringError("try/catch lowering currently expects final statement to be a call expression")
    try_expr = try_final.value
    if not isinstance(try_expr, ast.Call):
        raise LoweringError("try/catch lowering currently supports call expressions only")
    callee = _callee_name(try_expr)
    if callee is None:
        raise LoweringError("try/catch lowering currently supports name/attr callees only")
    if callee not in checked.functions:
        raise LoweringError(f"unknown function '{callee}' in try/catch")
    ret_ty = checked.functions[callee].signature.return_type
    live_users = list(env.snapshot_live_user_names())
    event_catches: List[tuple[int, ast.CatchClause, str]] = []
    catch_alls: List[tuple[ast.CatchClause, str]] = []
    catch_blocks: List[mir.BasicBlock] = []
    catch_envs: Dict[str, SSAEnv] = {}
    for idx, clause in enumerate(stmt.catches):
        catch_name = fresh_block(f"bb_try_catch{idx}")
        catch_params: List[mir.Param] = [mir.Param(name=env.fresh_ssa("err_val", ty=ERROR), type=ERROR)]
        for u in live_users:
            ty = env.ctx.ssa_types[env.lookup_user(u)]
            catch_params.append(mir.Param(name=env.fresh_ssa(f"{u}_catch", ty=ty), type=ty))
        catch_block = mir.BasicBlock(name=catch_name, params=catch_params)
        blocks[catch_name] = catch_block
        catch_env = env.clone_for_block({u: p.name for u, p in zip(live_users, catch_params[1:])})
        if clause.binder:
            binder_ty = ERROR
            if clause.event and clause.event in checked.exceptions:
                binder_ty = Type(clause.event)
            catch_env.bind_user(clause.binder, catch_params[0].name, binder_ty)
        catch_block, catch_env = lower_block_in_env(clause.block.statements, catch_env, blocks, catch_block, checked, fresh_block)
        catch_blocks.append(catch_block)
        catch_envs[catch_block.name] = catch_env
        if clause.event:
            if clause.event_code is None:
                exc_info = checked.exceptions.get(clause.event)
                if exc_info is None:
                    raise LoweringError(f"unknown event '{clause.event}' in catch")
                clause_event_code = exc_info.event_code
            else:
                clause_event_code = clause.event_code
            event_catches.append((clause_event_code, clause, catch_name))
        else:
            catch_alls.append((clause, catch_name))
    # Join block threads locals; no value result for statement try.
    join_name = fresh_block("bb_try_join")
    join_params: List[mir.Param] = []
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        join_params.append(mir.Param(name=env.fresh_ssa(f"{u}_phi", ty=ty), type=ty))
    join_block = mir.BasicBlock(name=join_name, params=join_params)
    blocks[join_name] = join_block
    join_env = env.clone_for_block({u: join_params[i].name for i, u in enumerate(live_users)})
    for catch_block in catch_blocks:
        catch_env = catch_envs[catch_block.name]
        if catch_block.terminator is None:
            catch_block.terminator = mir.Br(
                target=mir.Edge(target=join_name, args=[catch_env.lookup_user(u) for u in live_users])
            )
    # Dispatch block for error routing.
    dispatch_name = fresh_block("bb_try_dispatch")
    dispatch_params: List[mir.Param] = [mir.Param(name=env.fresh_ssa("err_disp", ty=ERROR), type=ERROR)]
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        dispatch_params.append(mir.Param(name=env.fresh_ssa(f"{u}_disp", ty=ty), type=ty))
    dispatch_block = mir.BasicBlock(name=dispatch_name, params=dispatch_params)
    blocks[dispatch_name] = dispatch_block
    dispatch_env = env.clone_for_block({u: p.name for u, p in zip(live_users, dispatch_params[1:])})
    err_param_name = dispatch_params[0].name
    # Read error event/code.
    event_code, dispatch_block = _ssa_read_error_event(err_param_name, dispatch_env, dispatch_block)
    # Build dispatch chain.
    cursor_block = dispatch_block
    for idx, (event_code_val, clause, catch_name) in enumerate(event_catches):
        cmp_dest = env.fresh_ssa(f"err_evt_cmp{idx}", BOOL)
        env.ctx.ssa_types[cmp_dest] = BOOL
        const_name = env.fresh_ssa(f"err_evt_const{idx}", I64)
        cursor_block.instructions.append(mir.Const(dest=const_name, type=I64, value=event_code_val))
        env.ctx.ssa_types[const_name] = I64
        cursor_block.instructions.append(
            mir.Binary(dest=cmp_dest, op="==", left=event_code, right=const_name, loc=stmt.loc)
        )
        match_edge = mir.Edge(target=catch_name, args=[err_param_name] + [dispatch_env.lookup_user(u) for u in live_users])
        next_block_name = fresh_block(f"bb_try_dispatch_next{idx}")
        next_block = mir.BasicBlock(name=next_block_name)
        blocks[next_block_name] = next_block
        cursor_block.terminator = mir.CondBr(
            cond=cmp_dest,
            then=match_edge,
            els=mir.Edge(target=next_block_name, args=[]),
        )
        cursor_block = next_block
    if catch_alls:
        target_catch = catch_alls[0][1]
        cursor_block.terminator = mir.Br(
            target=mir.Edge(target=target_catch, args=[err_param_name] + [dispatch_env.lookup_user(u) for u in live_users])
        )
    else:
        cursor_block.terminator = mir.Throw(error=err_param_name, loc=stmt.loc)
    # Lower call args and emit call terminator with edges.
    call_args: List[str] = []
    for a in try_expr.args:
        v, _, current, env = lower_expr_to_ssa(a, env, current, checked, blocks, fresh_block)
        call_args.append(v)
    call_dest = env.fresh_ssa(callee, ret_ty)
    env.ctx.ssa_types[call_dest] = ret_ty
    call_err = env.fresh_ssa("err", ERROR)
    current.terminator = mir.Call(
        dest=call_dest,
        err_dest=call_err,
        callee=callee,
        args=call_args,
        ret_type=ret_ty,
        normal=mir.Edge(target=join_name, args=[env.lookup_user(u) for u in live_users]),
        error=mir.Edge(target=dispatch_name, args=[call_err] + [env.lookup_user(u) for u in live_users]),
        loc=stmt.loc,
    )
    return join_block, join_env
def _maybe_lower_args_view_index(
    expr: ast.Expr, env: SSAEnv, current: mir.BasicBlock, checked: CheckedProgram
) -> Optional[Tuple[str, Type, mir.BasicBlock, SSAEnv]]:
    """Lower args-view indexing to __exc_args_get(Error, String) returning Optional<String>."""
    if not isinstance(expr, ast.Index):
        return None
    # Lower base to SSA and get its type.
    base_ssa, base_ty, current, env = lower_expr_to_ssa(expr.value, env, current, checked, None, None)
    if base_ty.name not in checked.structs:
        return None
    struct_info = checked.structs[base_ty.name]
    if "error" not in struct_info.field_types or struct_info.field_types["error"] != ERROR:
        return None
    # Derive key name without re-evaluating the base when using leading-dot sugar.
    key_name_ssa: Optional[str] = None
    direct_value = False
    # Helper to compare bases by identity or name.
    def _same_base(a: ast.Expr, b: ast.Expr) -> bool:
        return (a is b) or (isinstance(a, ast.Name) and isinstance(b, ast.Name) and a.ident == b.ident)
    # Method sugar: view[view.a()]
    if isinstance(expr.index, ast.Call):
        func = expr.index.func
        if isinstance(func, ast.Attr) and _same_base(func.value, expr.value):
            key_name_ssa = env.fresh_ssa("exc_key_name", STR)
            current.instructions.append(mir.Const(dest=key_name_ssa, type=STR, value=func.attr))
            env.ctx.ssa_types[key_name_ssa] = STR
            direct_value = True
    if key_name_ssa is None and isinstance(expr.index, ast.Attr) and _same_base(expr.index.value, expr.value):
        # Leading-dot case: index is Attr on the same base; use the attr name directly and return String.
        key_name_ssa = env.fresh_ssa("exc_key_name", STR)
        current.instructions.append(mir.Const(dest=key_name_ssa, type=STR, value=expr.index.attr))
        env.ctx.ssa_types[key_name_ssa] = STR
        direct_value = True
    if key_name_ssa is None:
        # TODO: restore ArgKey lowering; for now, constant key for debugging.
        key_name_ssa = env.fresh_ssa("exc_key_name", STR)
        current.instructions.append(mir.Const(dest=key_name_ssa, type=STR, value="a"))
        env.ctx.ssa_types[key_name_ssa] = STR
    # Lower: tmp_error = base.error; call the appropriate helper.
    err_ssa = env.fresh_ssa("exc_err", ERROR)
    current.instructions.append(mir.FieldGet(dest=err_ssa, base=base_ssa, field="error"))
    env.ctx.ssa_types[err_ssa] = ERROR
    if direct_value:
        dest = env.fresh_ssa("exc_arg_val", STR)
        current.instructions.append(
            mir.Call(dest=dest, callee="__exc_args_get_required", args=[err_ssa, key_name_ssa], ret_type=STR)
        )
        env.ctx.ssa_types[dest] = STR
        return dest, STR, current, env
    dest = env.fresh_ssa("exc_arg", Type("Optional", (STR,)))
    current.instructions.append(
        mir.Call(dest=dest, callee="__exc_args_get", args=[err_ssa, key_name_ssa], ret_type=Type("Optional", (STR,)))
    )
    env.ctx.ssa_types[dest] = Type("Optional", (STR,))
    return dest, Type("Optional", (STR,)), current, env
