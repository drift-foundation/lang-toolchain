"""Skeleton strict-SSA lowering for Drift MIR.

This is a scaffold to guide the eventual rewrite of lower_to_mir into a strict
SSA form. It is wired behind --ssa-check/--ssa-check-mode for structural checking,
while legacy lowering still drives codegen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from . import ast, mir
from .checker import CheckedProgram
from .ssa_env import SSAEnv, SSAContext
from .types import ERROR, INT
from .types import Type, ReferenceType, BOOL, I64, INT, F64, STR, array_element_type, array_of, ref_of


@dataclass
class LoweredFunction:
    blocks: Dict[str, mir.BasicBlock]
    env: SSAEnv
    entry: str = "bb0"


def lower_function_ssa(fn_def: ast.FunctionDef, checked: CheckedProgram) -> LoweredFunction:
    """Lower a single function into SSA MIR blocks (scaffold only)."""
    fn_info = checked.functions[fn_def.name]
    ctx = SSAContext()
    env = SSAEnv(ctx=ctx)
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
    env.bind_user(stmt.name, dest_ssa, val_ty)
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
    if isinstance(expr, ast.ExceptionCtor):
        # Materialize event code.
        code_ssa = env.fresh_ssa("exc_code", INT)
        loc = getattr(expr, "loc", None)
        current.instructions.append(mir.Const(dest=code_ssa, type=INT, value=expr.event_code, loc=loc))
        env.ctx.ssa_types[code_ssa] = INT
        # Thread first payload field if present; otherwise use empty string literal.
        payload_ssa: Optional[str] = None
        if expr.fields:
            first_expr = next(iter(expr.fields.values()))
            payload_ssa, payload_ty, current, env = lower_expr_to_ssa(
                first_expr, env, current, checked, blocks, fresh_block
            )
            if payload_ty != STR:
                raise LoweringError("exception payload lowering currently supports String only")
        if payload_ssa is None:
            payload_ssa = env.fresh_ssa("exc_payload", STR)
            current.instructions.append(mir.Const(dest=payload_ssa, type=STR, value="", loc=loc))
            env.ctx.ssa_types[payload_ssa] = STR
        dest_err = env.fresh_ssa("exc", ERROR)
        current.instructions.append(
            mir.Call(
                dest=dest_err,
                callee="drift_error_new_dummy",
                args=[code_ssa, payload_ssa],
                ret_type=ERROR,
                err_dest=None,
                normal=None,
                error=None,
                loc=loc,
            )
        )
        env.ctx.ssa_types[dest_err] = ERROR
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
        # Bare name call
        if isinstance(expr.func, ast.Name):
            callee = expr.func.ident
        elif isinstance(expr.func, ast.Attr) and isinstance(expr.func.value, ast.Name):
            # Method-style call on a resolved value; treat as direct call to attr.
            callee = f"{expr.func.value.ident}.{expr.func.attr}"
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
            return INT
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
    dest = env.fresh_ssa("err_event", INT)
    current.instructions.append(mir.ErrorEvent(dest=dest, error=err_ssa))
    env.ctx.ssa_types[dest] = INT
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
            catch_env.bind_user(arm.binder, catch_params[0].name, ERROR)
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
        const_name = env.fresh_ssa(f"err_evt_const_expr{idx}", INT)
        cursor_block.instructions.append(mir.Const(dest=const_name, type=INT, value=event_code_val))
        env.ctx.ssa_types[const_name] = INT
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
            catch_env.bind_user(clause.binder, catch_params[0].name, ERROR)
        catch_block, catch_env = lower_block_in_env(clause.block.statements, catch_env, blocks, catch_block, checked, fresh_block)
        catch_blocks.append(catch_block)
        catch_envs[catch_name] = catch_env
        if clause.event:
            exc_info = checked.exceptions.get(clause.event)
            if exc_info is None:
                raise LoweringError(f"unknown event '{clause.event}' in catch")
            event_catches.append((exc_info.event_code, clause, catch_name))
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
        const_name = env.fresh_ssa(f"err_evt_const{idx}", INT)
        cursor_block.instructions.append(mir.Const(dest=const_name, type=INT, value=event_code_val))
        env.ctx.ssa_types[const_name] = INT
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
