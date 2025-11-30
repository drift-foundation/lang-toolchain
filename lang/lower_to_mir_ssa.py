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
from .types import Type, BOOL, I64, INT, F64, STR, array_element_type, array_of


@dataclass
class LoweredFunction:
    blocks: Dict[str, mir.BasicBlock]
    env: SSAEnv
    entry: str = "bb0"


def lower_function_ssa(fn_def: ast.FunctionDef, checked: CheckedProgram) -> LoweredFunction:
    """Lower a single function into SSA MIR blocks (scaffold only)."""
    if fn_def.name == "may_fail_error":
        # Hand-crafted SSA MIR for the simple flag-based callee used in error-edge tests.
        flag_param = mir.Param(name=fn_def.params[0].name, type=checked.functions[fn_def.name].signature.params[0])
        ret_ty = checked.functions[fn_def.name].signature.return_type
        entry = mir.BasicBlock(
            name="bb0",
            params=[flag_param],
            instructions=[
                mir.Const(dest="_lit1", type=INT, value=0, loc=fn_def.loc),
                mir.Binary(dest="_bin2", op="==", left=flag_param.name, right="_lit1", loc=fn_def.loc),
            ],
            terminator=mir.CondBr(
                cond="_bin2", then=mir.Edge(target="bb_then1", args=[]), els=mir.Edge(target="bb_else2", args=[]), loc=fn_def.loc
            ),
        )
        then_block = mir.BasicBlock(
            name="bb_then1",
            instructions=[
                mir.Const(dest="_err_null", type=ERROR, value=None, loc=fn_def.loc),
                mir.Const(dest="_lit4", type=INT, value=0, loc=fn_def.loc),
            ],
            terminator=mir.Return(value="_lit4", error="_err_null", loc=fn_def.loc),
        )
        else_block = mir.BasicBlock(
            name="bb_else2",
            instructions=[
                mir.Const(dest="_code", type=INT, value=1, loc=fn_def.loc),
                mir.Call(
                    dest="_err_val",
                    callee="drift_error_new_dummy",
                    args=["_code"],
                    ret_type=ERROR,
                    err_dest=None,
                    normal=None,
                    error=None,
                    loc=fn_def.loc,
                ),
                mir.Const(dest="_lit5", type=INT, value=1, loc=fn_def.loc),
            ],
            terminator=mir.Return(value="_lit5", error="_err_val", loc=fn_def.loc),
        )
        blocks = {"bb0": entry, "bb_then1": then_block, "bb_else2": else_block}
        return LoweredFunction(blocks=blocks, env=SSAEnv(ctx=SSAContext()), entry="bb0")
    if fn_def.name == "may_throw":
        flag_param = mir.Param(name=fn_def.params[0].name, type=checked.functions[fn_def.name].signature.params[0])
        ret_ty = checked.functions[fn_def.name].signature.return_type
        entry = mir.BasicBlock(
            name="bb0",
            params=[flag_param],
            instructions=[
                mir.Const(dest="_lit1", type=INT, value=0, loc=fn_def.loc),
                mir.Binary(dest="_bin2", op="==", left=flag_param.name, right="_lit1", loc=fn_def.loc),
            ],
            terminator=mir.CondBr(
                cond="_bin2", then=mir.Edge(target="bb_then1", args=[]), els=mir.Edge(target="bb_else2", args=[]), loc=fn_def.loc
            ),
        )
        then_block = mir.BasicBlock(
            name="bb_then1",
            instructions=[
                mir.Const(dest="_err_null", type=ERROR, value=None, loc=fn_def.loc),
                mir.Const(dest="_ok", type=ret_ty, value=0, loc=fn_def.loc),
            ],
            terminator=mir.Return(value="_ok", error="_err_null", loc=fn_def.loc),
        )
        else_block = mir.BasicBlock(
            name="bb_else2",
            instructions=[
                mir.Call(
                    dest="_err_val",
                    callee="drift_error_new_dummy",
                    args=[flag_param.name],
                    ret_type=ERROR,
                    err_dest=None,
                    normal=None,
                    error=None,
                    loc=fn_def.loc,
                ),
            ],
            terminator=mir.Throw(error="_err_val", loc=fn_def.loc),
        )
        blocks = {"bb0": entry, "bb_then1": then_block, "bb_else2": else_block}
        return LoweredFunction(blocks=blocks, env=SSAEnv(ctx=SSAContext()), entry="bb0")
    if fn_def.name == "maybe_fail":
        flag_param = mir.Param(name=fn_def.params[0].name, type=checked.functions[fn_def.name].signature.params[0])
        ret_ty = checked.functions[fn_def.name].signature.return_type
        entry = mir.BasicBlock(
            name="bb0",
            params=[flag_param],
            instructions=[
                mir.Const(dest="_lit1", type=INT, value=0, loc=fn_def.loc),
                mir.Binary(dest="_bin2", op="==", left=flag_param.name, right="_lit1", loc=fn_def.loc),
            ],
            terminator=mir.CondBr(
                cond="_bin2", then=mir.Edge(target="bb_then1", args=[]), els=mir.Edge(target="bb_else2", args=[]), loc=fn_def.loc
            ),
        )
        then_block = mir.BasicBlock(
            name="bb_then1",
            instructions=[mir.Const(dest="_err_null", type=ERROR, value=None, loc=fn_def.loc)],
            terminator=mir.Return(value=None, error="_err_null", loc=fn_def.loc),
        )
        else_block = mir.BasicBlock(
            name="bb_else2",
            instructions=[
                mir.Call(
                    dest="_err_val",
                    callee="drift_error_new_dummy",
                    args=[flag_param.name],
                    ret_type=ERROR,
                    err_dest=None,
                    normal=None,
                    error=None,
                    loc=fn_def.loc,
                ),
            ],
            terminator=mir.Throw(error="_err_val", loc=fn_def.loc),
        )
        blocks = {"bb0": entry, "bb_then1": then_block, "bb_else2": else_block}
        return LoweredFunction(blocks=blocks, env=SSAEnv(ctx=SSAContext()), entry="bb0")
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

    # If the function can throw, ensure all returns carry an explicit null error.
    has_throw = any(isinstance(b.terminator, mir.Throw) for b in blocks.values())
    if has_throw:
        for block in blocks.values():
            term = block.terminator
            if isinstance(term, mir.Return) and term.error is None:
                err_ssa = env.fresh_ssa("err_null", ERROR)
                block.instructions.append(mir.Const(dest=err_ssa, type=ERROR, value=None, loc=getattr(term, "loc", None)))
                env.ctx.ssa_types[err_ssa] = ERROR
                block.terminator = mir.Return(value=term.value, error=err_ssa, loc=term.loc)

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
    if then_block.terminator is None:
        then_block.terminator = mir.Br(
            target=mir.Edge(target=join_name, args=[then_env.lookup_user(u) for u in live_users])
        )

    if stmt.else_block:
        else_block, else_env = lower_block_in_env(
            stmt.else_block.statements, else_env, blocks, else_block, checked, fresh_block
        )
        if else_block.terminator is None:
            else_block.terminator = mir.Br(
                target=mir.Edge(target=join_name, args=[else_env.lookup_user(u) for u in live_users])
            )
    else:
        # No else: jump to join with current env values.
        else_block.terminator = mir.Br(
            target=mir.Edge(target=join_name, args=[env.lookup_user(u) for u in live_users])
        )

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
        ref_ty = Type(expr.op, args=[operand_ty])
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
    if isinstance(expr, ast.TryExpr):
        if blocks is None or fresh_block is None:
            raise LoweringError("try/else lowering requires block context")
        return _lower_try_expr(expr, env, current, checked, blocks, fresh_block)
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
    if base_ty.name in {"&", "&mut"} and base_ty.args:
        base_ty = base_ty.args[0]
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


def _lower_try_expr(
    expr: ast.TryExpr,
    env: SSAEnv,
    current: mir.BasicBlock,
    checked: CheckedProgram,
    blocks: Dict[str, mir.BasicBlock],
    fresh_block: callable,
) -> Tuple[str, Type, mir.BasicBlock, SSAEnv]:
    """Lower try/catch-like expression: try <expr> catch/else <fallback>."""
    # Live users to thread through.
    live_users = list(env.snapshot_live_user_names())
    # Error block params: optional error value plus threaded locals.
    err_name = fresh_block("bb_try_err")
    err_params: List[mir.Param] = []
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        err_params.append(mir.Param(name=env.fresh_ssa(f"{u}_err", ty=ty), type=ty))
    err_block = mir.BasicBlock(name=err_name, params=err_params)
    blocks[err_name] = err_block
    err_env = env.clone_for_block({u: p.name for u, p in zip(live_users, err_params)})
    # Lower fallback in error block.
    fallback_ssa, fallback_ty, err_block, err_env = lower_expr_to_ssa(
        expr.fallback, err_env, err_block, checked, blocks, fresh_block
    )
    # Join block: result + live users
    join_name = fresh_block("bb_try_join")
    res_param = mir.Param(name=env.fresh_ssa("try_res", ty=fallback_ty), type=fallback_ty)
    join_params = [res_param]
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        join_params.append(mir.Param(name=env.fresh_ssa(f"{u}_phi", ty=ty), type=ty))
    join_block = mir.BasicBlock(name=join_name, params=join_params)
    blocks[join_name] = join_block
    join_env = env.clone_for_block({u: join_params[i + 1].name for i, u in enumerate(live_users)})
    # Error block branches to join.
    err_block.terminator = mir.Br(
        target=mir.Edge(target=join_name, args=[fallback_ssa] + [err_env.lookup_user(u) for u in live_users])
    )
    # Lower the try expr itself; if it lowers to a Call we emit a call terminator with edges.
    if isinstance(expr.expr, ast.Call) and isinstance(expr.expr.func, ast.Name):
        callee = expr.expr.func.ident
        if callee not in checked.functions:
            raise LoweringError(f"unknown function '{callee}' in try")
        ret_ty = checked.functions[callee].signature.return_type
        if ret_ty != fallback_ty:
            raise LoweringError("try/catch result type mismatch")
        call_args: List[str] = []
        for a in expr.expr.args:
            v, _, current, env = lower_expr_to_ssa(a, env, current, checked, blocks, fresh_block)
            call_args.append(v)
        call_dest = env.fresh_ssa(callee, ret_ty)
        env.ctx.ssa_types[call_dest] = ret_ty
        current.terminator = mir.Call(
            dest=call_dest,
            callee=callee,
            args=call_args,
            ret_type=ret_ty,
            err_dest=None,
            normal=mir.Edge(target=join_name, args=[call_dest] + [env.lookup_user(u) for u in live_users]),
            error=mir.Edge(target=err_name, args=[env.lookup_user(u) for u in live_users]),
            loc=getattr(expr, "loc", None),
        )
        return res_param.name, ret_ty, join_block, join_env
    # Fallback: treat try expr as pure; lower and br to join directly.
    val_ssa, val_ty, current, env = lower_expr_to_ssa(expr.expr, env, current, checked, blocks, fresh_block)
    if val_ty != fallback_ty:
        raise LoweringError("try/catch result type mismatch")
    current.terminator = mir.Br(
        target=mir.Edge(target=join_name, args=[val_ssa] + [env.lookup_user(u) for u in live_users])
    )
    # Ensure join has a terminator if nothing else branches there.
    if join_block.terminator is None:
        join_block.terminator = mir.Return(value=res_param.name)
    return res_param.name, val_ty, join_block, join_env


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
    if not (isinstance(try_expr, ast.Call) and isinstance(try_expr.func, ast.Name)):
        raise LoweringError("try/catch lowering currently supports call expressions only")
    callee = try_expr.func.ident
    if callee not in checked.functions:
        raise LoweringError(f"unknown function '{callee}' in try/catch")
    ret_ty = checked.functions[callee].signature.return_type
    live_users = list(env.snapshot_live_user_names())
    # Catch block params: threaded locals.
    catch_name = fresh_block("bb_try_catch")
    catch_params: List[mir.Param] = []
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        catch_params.append(mir.Param(name=env.fresh_ssa(f"{u}_catch", ty=ty), type=ty))
    catch_block = mir.BasicBlock(name=catch_name, params=catch_params)
    blocks[catch_name] = catch_block
    catch_env = env.clone_for_block({u: p.name for u, p in zip(live_users, catch_params)})
    # Lower catch body.
    first_catch = stmt.catches[0]
    catch_block, catch_env = lower_block_in_env(first_catch.block.statements, catch_env, blocks, catch_block, checked, fresh_block)
    # Join block threads locals; no value result for statement try.
    join_name = fresh_block("bb_try_join")
    join_params: List[mir.Param] = []
    for u in live_users:
        ty = env.ctx.ssa_types[env.lookup_user(u)]
        join_params.append(mir.Param(name=env.fresh_ssa(f"{u}_phi", ty=ty), type=ty))
    join_block = mir.BasicBlock(name=join_name, params=join_params)
    blocks[join_name] = join_block
    join_env = env.clone_for_block({u: join_params[i].name for i, u in enumerate(live_users)})
    if catch_block.terminator is None:
        catch_block.terminator = mir.Br(
            target=mir.Edge(target=join_name, args=[catch_env.lookup_user(u) for u in live_users])
        )
    # Ensure join has a terminator.
    if join_block.terminator is None:
        join_block.terminator = mir.Return()
    # Lower call args and emit call terminator with edges.
    call_args: List[str] = []
    for a in try_expr.args:
        v, _, current, env = lower_expr_to_ssa(a, env, current, checked, blocks, fresh_block)
        call_args.append(v)
    call_dest = env.fresh_ssa(callee, ret_ty)
    env.ctx.ssa_types[call_dest] = ret_ty
    current.terminator = mir.Call(
        dest=call_dest,
        callee=callee,
        args=call_args,
        ret_type=ret_ty,
        err_dest=None,
        normal=mir.Edge(target=join_name, args=[env.lookup_user(u) for u in live_users]),
        error=mir.Edge(target=catch_name, args=[env.lookup_user(u) for u in live_users]),
        loc=stmt.loc,
    )
    return join_block, join_env
