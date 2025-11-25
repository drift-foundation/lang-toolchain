from __future__ import annotations

from typing import Dict, Tuple, List, Optional

from . import ast, mir
from .checker import CheckedProgram
from .types import BOOL, F64, I64, STR, ERROR, Type


class LoweringError(Exception):
    pass


def lower_straightline(checked: CheckedProgram, source_name: str | None = None) -> mir.Program:
    """
    Minimal lowering supporting:
    - params/literals/names/binary ops
    - return
    - ternary expressions
    - if/else conditionals
    - raise (assumes value is already an Error)
    """
    if not checked.program.functions:
        raise LoweringError("no functions to lower")

    functions: Dict[str, mir.Function] = {}

    for fn_def in checked.program.functions:
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
                ty = _lookup_type(expr.ident, block_params, temp_types, checked)
                return expr.ident, ty, current_block
            if isinstance(expr, ast.Binary):
                lhs, lhs_ty, current_block = lower_expr(expr.left, current_block, temp_types)
                rhs, rhs_ty, current_block = lower_expr(expr.right, current_block, temp_types)
                dest = fresh_val()
                current_block.instructions.append(mir.Binary(dest=dest, op=expr.op, left=lhs, right=rhs))
                temp_types[dest] = lhs_ty
                return dest, lhs_ty, current_block
            if isinstance(expr, ast.Call):
                if not isinstance(expr.func, ast.Name):
                    raise LoweringError("only simple name calls supported in minimal lowering")
                dest = fresh_val()
                err_dest = fresh_val()
                arg_vals: List[str] = []
                for a in expr.args:
                    v, _, current_block = lower_expr(a, current_block, temp_types)
                    arg_vals.append(v)
                current_block.instructions.append(mir.Call(dest=dest, err_dest=err_dest, callee=expr.func.ident, args=arg_vals))
                call_ty = _lookup_type(expr.func.ident, block_params, temp_types, checked)
                temp_types[dest] = call_ty
                temp_types[err_dest] = ERROR
                return dest, call_ty, current_block
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
            if isinstance(expr, ast.TryExpr):
                if not isinstance(expr.expr, ast.Call):
                    raise LoweringError("try/else lowering currently supports call attempts only")
                if not isinstance(expr.expr.func, ast.Name):
                    raise LoweringError("try/else lowering supports simple name callees only")
                call_args: List[str] = []
                for a in expr.expr.args:
                    v, _, current_block = lower_expr(a, current_block, temp_types)
                    call_args.append(v)
                norm_name = fresh_block("bb_norm")
                err_name = fresh_block("bb_err")
                join_name = fresh_block("bb_join")
                norm_param = mir.Param(name=fresh_val(), type=_type_of_literal(0))
                norm_block = mir.BasicBlock(name=norm_name, params=[norm_param])
                err_param = mir.Param(name=fresh_val(), type=ERROR)
                err_block = mir.BasicBlock(name=err_name, params=[err_param])
                join_param = mir.Param(name=f"phi{temp_counter}", type=_type_of_literal(0))
                join_block = mir.BasicBlock(name=join_name, params=[join_param])
                blocks[norm_name] = norm_block
                blocks[err_name] = err_block
                blocks[join_name] = join_block
                call_dest = fresh_val()
                call_err = fresh_val()
                current_block.instructions.append(
                    mir.Call(
                        dest=call_dest,
                        err_dest=call_err,
                        callee=expr.expr.func.ident,
                        args=call_args,
                        normal=mir.Edge(target=norm_name, args=[call_dest]),
                        error=mir.Edge(target=err_name, args=[call_err]),
                    )
                )
                call_type = _lookup_type(expr.expr.func.ident, block_params, temp_types, checked)
                temp_types[call_dest] = call_type
                temp_types[call_err] = ERROR
                norm_block.params[0] = mir.Param(name=norm_param.name, type=call_type)
                norm_block.terminator = mir.Br(target=mir.Edge(target=join_name, args=[norm_param.name]))
                join_block.params[0] = mir.Param(name=join_param.name, type=call_type)
                temp_types[join_param.name] = call_type
                fb_val, fb_ty, err_block = lower_expr(expr.fallback, err_block, temp_types.copy())
                err_block.terminator = mir.Br(target=mir.Edge(target=join_name, args=[fb_val]))
                return join_param.name, temp_types[call_dest], join_block
            raise LoweringError(f"unsupported expression: {expr}")

        def lower_stmt(
            stmt: ast.Stmt,
            current_block: mir.BasicBlock,
            temp_types: Dict[str, Type],
            err_target: str | None = None,
        ) -> Optional[mir.BasicBlock]:
            if isinstance(stmt, ast.ReturnStmt):
                if stmt.value is None:
                    current_block.terminator = mir.Return()
                else:
                    val, _, current_block = lower_expr(stmt.value, current_block, temp_types)
                    current_block.terminator = mir.Return(value=val)
                return None
            if isinstance(stmt, ast.IfStmt):
                return _lower_if(stmt, current_block, temp_types, err_target)
            if isinstance(stmt, ast.TryStmt):
                return _lower_try(stmt, current_block, temp_types, err_target)
            if isinstance(stmt, ast.RaiseStmt):
                # Special-case raising an exception constructor: build an Error* via drift_error_new.
                if (
                    isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.ident in checked.exceptions
                ):
                    exc_name = stmt.value.func.ident
                    # Build attrs array from kwargs/positional (pos mapped to declared args).
                    msg_expr = None
                    domain_expr = None
                    # Add current function name and line for frames
                    frame_file = fresh_val()
                    frame_func = fresh_val()
                    frame_line = fresh_val()
                    from pathlib import Path
                    file_label = Path(source_name).name if source_name else "<unknown>"
                    current_block.instructions.append(mir.Const(dest=frame_file, type=STR, value=file_label))
                    current_block.instructions.append(mir.Const(dest=frame_func, type=STR, value=fn_def.name))
                    current_block.instructions.append(mir.Const(dest=frame_line, type=I64, value=stmt.loc.line))
                    temp_types[frame_file] = STR
                    temp_types[frame_func] = STR
                    temp_types[frame_line] = I64
                    for kw in stmt.value.kwargs:
                        if kw.name == "msg" and msg_expr is None:
                            msg_expr = kw.value
                        if kw.name == "domain" and domain_expr is None:
                            domain_expr = kw.value
                    if msg_expr is None and stmt.value.args:
                        msg_expr = stmt.value.args[0]
                    if msg_expr is None:
                        msg_expr = ast.Literal(loc=stmt.loc, value=exc_name)
                    msg_val, _, current_block = lower_expr(msg_expr, current_block, temp_types)
                    # Build deterministic attrs (keys/values) and count
                    attrs: Dict[str, ast.Expr] = {}
                    for kw in stmt.value.kwargs:
                        if kw.name == "domain":
                            continue
                        attrs[kw.name] = kw.value
                    if "msg" not in attrs:
                        attrs["msg"] = msg_expr
                    # deterministically ordered by key
                    sorted_items = sorted(attrs.items(), key=lambda kv: kv[0])
                    key_vals: List[str] = []
                    val_vals: List[str] = []
                    for k, vexpr in sorted_items:
                        key_name = fresh_val()
                        current_block.instructions.append(mir.Const(dest=key_name, type=STR, value=k))
                        temp_types[key_name] = STR
                        v_name, _, current_block = lower_expr(vexpr, current_block, temp_types)
                        key_vals.append(key_name)
                        val_vals.append(v_name)
                    keys_arr = fresh_val()
                    vals_arr = fresh_val()
                    current_block.instructions.append(mir.ArrayInit(dest=keys_arr, elements=key_vals, element_type=STR))
                    current_block.instructions.append(mir.ArrayInit(dest=vals_arr, elements=val_vals, element_type=STR))
                    temp_types[keys_arr] = STR
                    temp_types[vals_arr] = STR
                    attr_count_val = fresh_val()
                    current_block.instructions.append(mir.Const(dest=attr_count_val, type=I64, value=len(sorted_items)))
                    temp_types[attr_count_val] = I64
                    # frame arrays
                    frame_files_arr = fresh_val()
                    frame_funcs_arr = fresh_val()
                    frame_lines_arr = fresh_val()
                    frame_count = fresh_val()
                    current_block.instructions.append(mir.ArrayInit(dest=frame_files_arr, elements=[frame_file], element_type=STR))
                    current_block.instructions.append(mir.ArrayInit(dest=frame_funcs_arr, elements=[frame_func], element_type=STR))
                    current_block.instructions.append(mir.ArrayInit(dest=frame_lines_arr, elements=[frame_line], element_type=I64))
                    current_block.instructions.append(mir.Const(dest=frame_count, type=I64, value=1))
                    temp_types[frame_files_arr] = STR
                    temp_types[frame_funcs_arr] = STR
                    temp_types[frame_lines_arr] = I64
                    temp_types[frame_count] = I64
                    evt_val = msg_val
                    dom_val = fresh_val()
                    exc_domain = checked.exceptions[exc_name].domain if exc_name in checked.exceptions else None
                    if domain_expr is not None:
                        dom_val_res, _, current_block = lower_expr(domain_expr, current_block, temp_types)
                        dom_val = dom_val_res
                    else:
                        domain_val = exc_domain if exc_domain is not None else "main"
                        current_block.instructions.append(mir.Const(dest=dom_val, type=STR, value=domain_val))
                    temp_types[dom_val] = STR
                    err_tmp = fresh_val()
                    current_block.instructions.append(
                        mir.Call(
                            dest=err_tmp,
                            callee="drift_error_new",
                            args=[keys_arr, vals_arr, attr_count_val, evt_val, dom_val, frame_files_arr, frame_funcs_arr, frame_lines_arr, frame_count],
                        )
                    )
                    temp_types[err_tmp] = ERROR
                    if err_target:
                        current_block.terminator = mir.Br(target=mir.Edge(target=err_target, args=[err_tmp]))
                    else:
                        current_block.terminator = mir.Raise(error=err_tmp)
                    return None
                val, _, current_block = lower_expr(stmt.value, current_block, temp_types)
                if err_target:
                    current_block.terminator = mir.Br(target=mir.Edge(target=err_target, args=[val]))
                else:
                    current_block.terminator = mir.Raise(error=val)
                return None
            raise LoweringError(f"unsupported statement: {stmt}")

        def lower_block(
            stmts: List[ast.Stmt],
            current_block: mir.BasicBlock,
            temp_types: Dict[str, Type],
            err_target: str | None = None,
        ) -> Optional[mir.BasicBlock]:
            block = current_block
            for stmt in stmts:
                block = lower_stmt(stmt, block, temp_types, err_target=err_target)
                if block is None:
                    return None
            return block

        def _lower_if(
            stmt: ast.IfStmt,
            current_block: mir.BasicBlock,
            temp_types: Dict[str, Type],
            err_target: str | None = None,
        ) -> Optional[mir.BasicBlock]:
            cond_val, _, current_block = lower_expr(stmt.condition, current_block, temp_types)
            then_name = fresh_block("bb_then")
            else_name = fresh_block("bb_else")
            then_block = mir.BasicBlock(name=then_name)
            else_block = mir.BasicBlock(name=else_name)
            blocks[then_name] = then_block
            blocks[else_name] = else_block
            current_block.terminator = mir.CondBr(cond=cond_val, then=mir.Edge(target=then_name), els=mir.Edge(target=else_name))

            end_then = lower_block(stmt.then_block.statements, then_block, temp_types.copy(), err_target=err_target)
            end_else = lower_block(stmt.else_block.statements, else_block, temp_types.copy(), err_target=err_target) if stmt.else_block else else_block

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

        def _lower_try(
            stmt: ast.TryStmt,
            current_block: mir.BasicBlock,
            temp_types: Dict[str, Type],
            outer_err_target: str | None = None,
        ) -> Optional[mir.BasicBlock]:
            body_name = fresh_block("bb_try_body")
            body_block = mir.BasicBlock(name=body_name)
            blocks[body_name] = body_block
            current_block.terminator = mir.Br(target=mir.Edge(target=body_name))

            # Build catch blocks; route errors to the first catch (no pattern matching yet).
            catch_blocks: List[tuple[ast.CatchClause, mir.BasicBlock]] = []
            for clause in stmt.catches:
                cb_name = fresh_block("bb_catch")
                binder = clause.binder or fresh_val()
                params = [mir.Param(name=binder, type=ERROR)]
                cb = mir.BasicBlock(name=cb_name, params=params)
                blocks[cb_name] = cb
                catch_blocks.append((clause, cb))

            catch_entry = catch_blocks[0][1].name if catch_blocks else None
            end_body = lower_block(stmt.body.statements, body_block, temp_types.copy(), err_target=catch_entry)

            fallthroughs: List[mir.BasicBlock] = []
            if end_body is not None:
                fallthroughs.append(end_body)
            for clause, cb in catch_blocks:
                ct_types = temp_types.copy()
                if clause.binder:
                    ct_types[clause.binder] = ERROR
                end_cb = lower_block(clause.block.statements, cb, ct_types, err_target=outer_err_target)
                if end_cb is not None:
                    fallthroughs.append(end_cb)

            if not fallthroughs:
                return None
            join_name = fresh_block("bb_after_try")
            join_block = mir.BasicBlock(name=join_name)
            blocks[join_name] = join_block
            for fb in fallthroughs:
                if fb.terminator is None:
                    fb.terminator = mir.Br(target=mir.Edge(target=join_name))
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
        functions[fn.name] = fn

    return mir.Program(functions=functions)

def _lookup_type(name: str, params, temps: Dict[str, Type], checked: CheckedProgram | None = None) -> Type:
    for p in params:
        if p.name == name:
            return p.type
    if name in temps:
        return temps[name]
    if checked:
        if name in checked.functions:
            return checked.functions[name].signature.return_type
        if name in checked.exceptions:
            return ERROR
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
