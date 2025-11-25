from __future__ import annotations

from llvmlite import binding as llvm  # type: ignore
from llvmlite import ir  # type: ignore

from . import mir
from .types import BOOL, ERROR, F64, I64, STR, Type


def lower_function(fn: mir.Function, func_map: dict[str, ir.Function] | None = None) -> tuple[str, bytes]:
    """
    MIR → LLVM lowering (supports branches/phi via block params; calls with normal/error edges lower to conditional branches; no real error payload lowering yet).
    """
    llvm.initialize()
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()
    target = llvm.Target.from_default_triple()
    tm = target.create_target_machine(reloc="pic", codemodel="small")

    llvm_module = ir.Module(name=f"{fn.name}_module")
    llvm_module.triple = llvm.get_default_triple()
    llvm_module.data_layout = tm.target_data

    param_types = [_llvm_type(p.type) for p in fn.params]
    ret_ty = _llvm_type(fn.return_type)
    func_ty = ir.FunctionType(ir.LiteralStructType([ret_ty, _llvm_type(ERROR)]), param_types)
    if func_map is not None and fn.name in func_map:
        llvm_fn = func_map[fn.name]
    else:
        llvm_fn = ir.Function(llvm_module, func_ty, name=fn.name)

    llvm_blocks = {name: llvm_fn.append_basic_block(name=name) for name in fn.blocks}
    phi_nodes: dict[str, dict[str, ir.PhiInstr]] = {}
    entry_name = fn.entry

    # Create phi nodes for block params
    for name, block in fn.blocks.items():
        builder = ir.IRBuilder(llvm_blocks[name])
        phi_nodes[name] = {}
        if name == entry_name:
            continue  # entry params come from function args directly
        for param in block.params:
            phi = builder.phi(_llvm_type(param.type), name=param.name)
            phi_nodes[name][param.name] = phi

    base_env: dict[str, ir.Value] = {p.name: llvm_fn.args[idx] for idx, p in enumerate(fn.params)}
    envs: dict[str, dict[str, ir.Value]] = {fn.entry: dict(base_env)}
    worklist = [fn.entry]
    visited = set()

    while worklist:
        bname = worklist.pop()
        if bname in visited:
            continue
        visited.add(bname)
        block = fn.blocks[bname]
        builder = ir.IRBuilder(llvm_blocks[bname])
        # Seed with any incoming env (or the base params if unseen).
        env: dict[str, ir.Value] = dict(envs.get(bname, base_env))
        # params (phi nodes) override if present for this block.
        if bname != entry_name:
            for param in block.params:
                env[param.name] = phi_nodes[bname][param.name]
        call_terminator = None
        for instr in block.instructions:
            if isinstance(instr, mir.Const):
                env[instr.dest] = _const(builder, instr.type, instr.value)
            elif isinstance(instr, mir.Move):
                env[instr.dest] = env[instr.source]
            elif isinstance(instr, mir.Copy):
                env[instr.dest] = env[instr.source]
            elif isinstance(instr, mir.Binary):
                env[instr.dest] = _lower_binary(builder, instr, env)
            elif isinstance(instr, mir.ArrayInit):
                if instr.element_type == STR:
                    elements = [env[e] for e in instr.elements]
                    arr_ty = ir.ArrayType(ir.IntType(8).as_pointer(), len(elements))
                    gv_ty = ir.IntType(8).as_pointer()
                elif instr.element_type == I64:
                    elements = [env[e] for e in instr.elements]
                    arr_ty = ir.ArrayType(ir.IntType(64), len(elements))
                    gv_ty = ir.IntType(64)
                else:
                    raise NotImplementedError("ArrayInit currently supports String or Int64 elements only")
                unique_id = len(builder.module.globals)
                gv = ir.GlobalVariable(builder.module, arr_ty, name=f".arr{unique_id}")
                gv.linkage = "internal"
                gv.global_constant = True
                gv.initializer = ir.Constant(arr_ty, elements)
                env[instr.dest] = gv.bitcast(gv_ty.as_pointer())
            elif isinstance(instr, mir.Call):
                arg_vals = [env[a] for a in instr.args]
                if instr.normal or instr.error:
                    callee = llvm_module.globals.get(instr.callee)
                    if callee is None or not isinstance(callee, ir.Function):
                        if func_map and instr.callee in func_map:
                            callee = func_map[instr.callee]
                        else:
                            ret_ty = _llvm_type(fn.return_type)
                            pair_ty = ir.LiteralStructType([ret_ty, _llvm_type(ERROR)])
                            arg_tys = [val.type for val in arg_vals]
                            callee_ty = ir.FunctionType(pair_ty, arg_tys)
                            callee = ir.Function(llvm_module, callee_ty, name=instr.callee)
                    call_val = builder.call(callee, arg_vals, name=instr.dest)
                    if not isinstance(call_val.type, ir.LiteralStructType):
                        raise NotImplementedError("expected pair return for call with error edges")
                    val = builder.extract_value(call_val, 0, name=instr.dest)
                    err = builder.extract_value(call_val, 1, name=f"{instr.dest}_err")
                    env[instr.dest] = val
                    if instr.err_dest:
                        env[instr.err_dest] = err
                    is_ok = builder.icmp_signed("==", err, ir.Constant(err.type, None))
                    if instr.normal:
                        _add_phi_incoming(phi_nodes, instr.normal, env, llvm_blocks[bname])
                    if instr.error:
                        _add_phi_incoming(phi_nodes, instr.error, env, llvm_blocks[bname])
                    then_bb = llvm_blocks[instr.normal.target] if instr.normal else llvm_blocks[bname]
                    else_bb = llvm_blocks[instr.error.target] if instr.error else llvm_blocks[bname]
                    builder.cbranch(is_ok, then_bb, else_bb)
                    if instr.normal:
                        envs.setdefault(instr.normal.target, dict(env))
                    if instr.error:
                        envs.setdefault(instr.error.target, dict(env))
                    if instr.normal:
                        worklist.append(instr.normal.target)
                    if instr.error:
                        worklist.append(instr.error.target)
                    call_terminator = instr
                    break  # terminates this block
                else:
                    callee = llvm_module.globals.get(instr.callee)
                    if callee is None or not isinstance(callee, ir.Function):
                        if func_map and instr.callee in func_map:
                            callee = func_map[instr.callee]
                        else:
                            ret_ty = _llvm_type(ERROR if instr.callee in {"error_new", "drift_error_new", "error"} else fn.return_type)
                            arg_tys = [val.type for val in arg_vals]
                            callee_ty = ir.FunctionType(ret_ty, arg_tys)
                            callee = ir.Function(llvm_module, callee_ty, name=instr.callee)
                    call_val = builder.call(callee, arg_vals, name=instr.dest)
                    env[instr.dest] = call_val
            else:
                raise NotImplementedError(f"unsupported instruction: {instr}")
        if call_terminator:
            envs[bname] = env
            continue
        term = block.terminator
        if isinstance(term, mir.Br):
            _add_phi_incoming(phi_nodes, term.target, env, llvm_blocks[bname])
            builder.branch(llvm_blocks[term.target.target])
            envs.setdefault(term.target.target, dict(env))
            worklist.append(term.target.target)
        elif isinstance(term, mir.CondBr):
            _add_phi_incoming(phi_nodes, term.then, env, llvm_blocks[bname])
            _add_phi_incoming(phi_nodes, term.els, env, llvm_blocks[bname])
            builder.cbranch(env[term.cond], llvm_blocks[term.then.target], llvm_blocks[term.els.target])
            envs.setdefault(term.then.target, dict(env))
            envs.setdefault(term.els.target, dict(env))
            worklist.extend([term.then.target, term.els.target])
        elif isinstance(term, mir.Return):
            val = env[term.value] if term.value else ir.Constant(_llvm_type(fn.return_type), None)
            zero_err = ir.Constant(_llvm_type(ERROR), None)
            pair_ty = ir.LiteralStructType([_llvm_type(fn.return_type), _llvm_type(ERROR)])
            agg = ir.Constant(pair_ty, ir.Undefined)
            agg = builder.insert_value(agg, val, 0)
            agg = builder.insert_value(agg, zero_err, 1)
            builder.ret(agg)
        elif isinstance(term, mir.Raise):
            err_val = env[term.error]
            # For Error return type, return err directly; otherwise return {undef, err}
            pair_ty = ir.LiteralStructType([_llvm_type(fn.return_type), _llvm_type(ERROR)])
            undef_val = ir.Constant(_llvm_type(fn.return_type), None)
            agg = builder.insert_value(ir.Constant(pair_ty, ir.Undefined), undef_val, 0)
            agg = builder.insert_value(agg, err_val, 1)
            builder.ret(agg)
        else:
            raise NotImplementedError("missing terminator")
        envs[bname] = env

    llvm_mod = llvm.parse_assembly(str(llvm_module))
    llvm_mod.verify()
    obj = tm.emit_object(llvm_mod)
    return str(llvm_module), obj


def _llvm_type(ty: Type) -> ir.Type:
    if ty == I64:
        return ir.IntType(64)
    if ty == BOOL:
        return ir.IntType(1)
    if ty == F64:
        return ir.DoubleType()
    if ty == STR:
        return ir.IntType(8).as_pointer()
    if ty == ERROR:
        return ir.IntType(8).as_pointer()
    return ir.IntType(8).as_pointer()


def _const(builder: ir.IRBuilder, ty: Type, val: object) -> ir.Value:
    if ty == I64:
        return ir.Constant(ir.IntType(64), int(val))
    if ty == BOOL:
        return ir.Constant(ir.IntType(1), int(bool(val)))
    if ty == F64:
        return ir.Constant(ir.DoubleType(), float(val))
    if ty == STR:
        if val is None:
            return ir.Constant(ir.IntType(8).as_pointer(), None)
        data = bytearray(str(val).encode("utf-8"))
        data.append(0)
        unique_id = len(builder.module.globals)
        gv = ir.GlobalVariable(builder.module, ir.ArrayType(ir.IntType(8), len(data)), name=f".str{unique_id}")
        gv.linkage = "internal"
        gv.global_constant = True
        gv.initializer = ir.Constant(gv.type.pointee, data)
        return gv.bitcast(ir.IntType(8).as_pointer())
    raise NotImplementedError(f"const of type {ty}")


def _lower_binary(builder: ir.IRBuilder, instr: mir.Binary, env: dict[str, ir.Value]) -> ir.Value:
    lhs = env[instr.left]
    rhs = env[instr.right]
    op = instr.op
    if op == "+":
        return builder.add(lhs, rhs, name=instr.dest)
    if op == "-":
        return builder.sub(lhs, rhs, name=instr.dest)
    if op == "*":
        return builder.mul(lhs, rhs, name=instr.dest)
    if op == "/":
        return builder.sdiv(lhs, rhs, name=instr.dest)
    if op == "==":
        return builder.icmp_signed("==", lhs, rhs, name=instr.dest)
    if op == "!=":
        return builder.icmp_signed("!=", lhs, rhs, name=instr.dest)
    if op == "<":
        return builder.icmp_signed("<", lhs, rhs, name=instr.dest)
    if op == "<=":
        return builder.icmp_signed("<=", lhs, rhs, name=instr.dest)
    if op == ">":
        return builder.icmp_signed(">", lhs, rhs, name=instr.dest)
    if op == ">=":
        return builder.icmp_signed(">=", lhs, rhs, name=instr.dest)
    raise NotImplementedError(f"binary op {op}")


def _add_phi_incoming(phi_nodes: dict[str, dict[str, ir.PhiInstr]], edge: mir.Edge, env: dict[str, ir.Value], pred_block: ir.Block) -> None:
    if not edge.args:
        return
    target = edge.target
    for arg_val, (param_name, phi) in zip(edge.args, phi_nodes[target].items()):
        phi.add_incoming(env[arg_val], pred_block)
