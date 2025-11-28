from __future__ import annotations

from pathlib import Path

from llvmlite import binding as llvm  # type: ignore
from llvmlite import ir  # type: ignore

from . import mir
from .types import BOOL, ERROR, F64, I64, STR, UNIT, Type, array_element_type

# Initialized per TargetMachine in lower_function
STRING_LLVM_TYPE: ir.Type | None = None
SIZE_T: ir.IntType | None = None


def lower_function(fn: mir.Function, func_map: dict[str, ir.Function] | None = None) -> tuple[str, bytes]:
    """
    MIR → LLVM lowering (supports branches/phi via block params; calls with normal/error edges lower to conditional branches; no real error payload lowering yet).
    """
    llvm.initialize()
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()
    target = llvm.Target.from_default_triple()
    tm = target.create_target_machine(reloc="pic", codemodel="small")
    global SIZE_T, STRING_LLVM_TYPE
    SIZE_T = ir.IntType(_pointer_size_bits(str(tm.target_data)))
    STRING_LLVM_TYPE = ir.LiteralStructType([SIZE_T, ir.IntType(8).as_pointer()])

    llvm_module = ir.Module(name=f"{fn.name}_module")
    llvm_module.triple = llvm.get_default_triple()
    llvm_module.data_layout = tm.target_data
    module_label = fn.module or "main"
    file_label = Path(fn.source).name if fn.source else "<unknown>"

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
            elif isinstance(instr, mir.ConsoleWrite):
                _lower_console_write(builder, env[instr.value])
            elif isinstance(instr, mir.ConsoleWriteln):
                _lower_console_writeln(builder, env[instr.value])
            elif isinstance(instr, mir.ArrayInit):
                if instr.element_type == STR:
                    assert STRING_LLVM_TYPE is not None
                    elements = [env[e] for e in instr.elements]
                    if not elements:
                        env[instr.dest] = ir.Constant(STRING_LLVM_TYPE.as_pointer(), None)
                        continue
                    arr_ty = ir.ArrayType(STRING_LLVM_TYPE, len(elements))
                elif instr.element_type == I64:
                    elements = [env[e] for e in instr.elements]
                    if not elements:
                        env[instr.dest] = ir.Constant(ir.IntType(64).as_pointer(), None)
                        continue
                    arr_ty = ir.ArrayType(ir.IntType(64), len(elements))
                else:
                    raise NotImplementedError("ArrayInit currently supports String or Int64 elements only")
                # Allocate on the stack and store elements (cannot be a global if elements are dynamic).
                arr_alloca = builder.alloca(arr_ty, name=f"arr{len(elements)}")
                zero32 = ir.Constant(ir.IntType(32), 0)
                for idx, elem in enumerate(elements):
                    idx_val = ir.Constant(ir.IntType(32), idx)
                    ptr = builder.gep(arr_alloca, [zero32, idx_val], inbounds=True)
                    builder.store(elem, ptr)
                env[instr.dest] = builder.gep(arr_alloca, [zero32, zero32], inbounds=True)
            elif isinstance(instr, mir.ArrayLiteral):
                assert SIZE_T is not None
                elem_ty = _llvm_type(instr.elem_type)
                if not isinstance(elem_ty, ir.IntType):
                    raise NotImplementedError("array literals currently support Int64 elements only")
                elem_size = ir.Constant(SIZE_T, elem_ty.width // 8)
                elem_align = elem_size
                arr_ty = ir.LiteralStructType([SIZE_T, SIZE_T, elem_ty.as_pointer()])
                length = ir.Constant(SIZE_T, len(instr.elements))
                cap = length
                raw = builder.call(_alloc_array_decl(llvm_module), [elem_size, elem_align, length, cap])
                data_ptr = builder.bitcast(raw, elem_ty.as_pointer())
                for idx, val_name in enumerate(instr.elements):
                    val = env[val_name]
                    idx_const = ir.Constant(SIZE_T, idx)
                    ptr = builder.gep(data_ptr, [idx_const], inbounds=True)
                    builder.store(val, ptr)
                tmp0 = builder.insert_value(ir.Constant(arr_ty, None), length, 0)
                tmp1 = builder.insert_value(tmp0, cap, 1)
                arr_val = builder.insert_value(tmp1, data_ptr, 2)
                env[instr.dest] = arr_val
            elif isinstance(instr, mir.ArrayGet):
                assert SIZE_T is not None
                arr_val = env[instr.base]
                idx_val = env[instr.index]
                len_val = builder.extract_value(arr_val, 0, name=f"{instr.dest}_len")
                data_ptr = builder.extract_value(arr_val, 2, name=f"{instr.dest}_data")
                is_neg = builder.icmp_signed("<", idx_val, ir.Constant(idx_val.type, 0), name=f"{instr.dest}_isneg")
                idx_size = builder.zext(idx_val, SIZE_T, name=f"{instr.dest}_idx_size")
                too_big = builder.icmp_unsigned(">=", idx_size, len_val, name=f"{instr.dest}_toobig")
                oob = builder.or_(is_neg, too_big, name=f"{instr.dest}_oob")
                ok_bb = llvm_fn.append_basic_block(name=f"{bname}_ok_{instr.dest}")
                oob_bb = llvm_fn.append_basic_block(name=f"{bname}_oob_{instr.dest}")
                builder.cbranch(oob, oob_bb, ok_bb)
                with builder.goto_block(oob_bb):
                    builder.call(_bounds_check_fail_decl(llvm_module), [idx_size, len_val])
                    builder.unreachable()
                builder.position_at_end(ok_bb)
                elem_ptr = builder.gep(data_ptr, [idx_size], inbounds=True)
                env[instr.dest] = builder.load(elem_ptr, name=instr.dest)
            elif isinstance(instr, mir.Call):
                arg_vals = [env[a] for a in instr.args]
                if instr.normal or instr.error:
                    callee = llvm_module.globals.get(instr.callee)
                    if callee is None or not isinstance(callee, ir.Function):
                        if func_map and instr.callee in func_map:
                            callee = func_map[instr.callee]
                        else:
                            ret_ty = _llvm_type(instr.ret_type)
                            pair_ty = ir.LiteralStructType([ret_ty, _llvm_type(ERROR)])
                            arg_tys = [val.type for val in arg_vals]
                            callee_ty = ir.FunctionType(pair_ty, arg_tys)
                            callee = ir.Function(llvm_module, callee_ty, name=instr.callee)
                    # Special-case drift_error_new/error_push_frame to pass string pointers.
                    if instr.callee == "drift_error_new":
                        callee = _error_new_decl(llvm_module)
                        arg_vals = list(arg_vals)
                        arg_vals[3] = _as_string_ptr(builder, arg_vals[3])
                        arg_vals[4] = _as_string_ptr(builder, arg_vals[4])
                    elif instr.callee == "error_push_frame":
                        callee = _error_push_frame_decl(llvm_module)
                        arg_vals = list(arg_vals)
                        arg_vals[1] = _as_string_ptr(builder, arg_vals[1])
                        arg_vals[2] = _as_string_ptr(builder, arg_vals[2])
                        arg_vals[3] = _as_string_ptr(builder, arg_vals[3])
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
                    # Force the ABI for error helpers even if already declared.
                    if instr.callee == "drift_error_new":
                        callee = _error_new_decl(llvm_module)
                        arg_vals = list(arg_vals)
                        arg_vals[3] = _as_string_ptr(builder, arg_vals[3])
                        arg_vals[4] = _as_string_ptr(builder, arg_vals[4])
                    elif instr.callee == "error_push_frame":
                        callee = _error_push_frame_decl(llvm_module)
                        arg_vals = list(arg_vals)
                        arg_vals[1] = _as_string_ptr(builder, arg_vals[1])
                        arg_vals[2] = _as_string_ptr(builder, arg_vals[2])
                        arg_vals[3] = _as_string_ptr(builder, arg_vals[3])
                    elif callee is None or not isinstance(callee, ir.Function):
                        if func_map and instr.callee in func_map:
                            callee = func_map[instr.callee]
                        else:
                            ret_ty = _llvm_type(ERROR if instr.callee in {"error_new", "error"} else instr.ret_type)
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
        assert STRING_LLVM_TYPE is not None
        return STRING_LLVM_TYPE
    if ty == ERROR:
        return ir.IntType(8).as_pointer()
    inner = array_element_type(ty)
    if inner is not None:
        assert SIZE_T is not None
        elem_ty = _llvm_type(inner)
        return ir.LiteralStructType([SIZE_T, SIZE_T, elem_ty.as_pointer()])
    return ir.IntType(8).as_pointer()


def _const(builder: ir.IRBuilder, ty: Type, val: object) -> ir.Value:
    if ty == I64:
        return ir.Constant(ir.IntType(64), int(val))
    if ty == BOOL:
        return ir.Constant(ir.IntType(1), int(bool(val)))
    if ty == F64:
        return ir.Constant(ir.DoubleType(), float(val))
    if ty == STR:
        assert STRING_LLVM_TYPE is not None and SIZE_T is not None
        if val is None:
            # Explicit empty literal {0, null}
            return ir.Constant.literal_struct([ir.Constant(SIZE_T, 0), ir.Constant(ir.IntType(8).as_pointer(), None)])
        data = bytearray(str(val).encode("utf-8"))
        data.append(0)
        unique_id = len(builder.module.globals)
        gv = ir.GlobalVariable(
            builder.module, ir.ArrayType(ir.IntType(8), len(data)), name=f".str{unique_id}"
        )
        gv.linkage = "internal"
        gv.global_constant = True
        gv.initializer = ir.Constant(gv.type.pointee, data)
        zero = ir.Constant(SIZE_T, 0)
        ptr = builder.gep(gv, [zero, zero], inbounds=True)
        # build struct {len, ptr} via inserts so we can include non-constant GEP
        s = ir.Constant(STRING_LLVM_TYPE, None)
        s = builder.insert_value(s, ir.Constant(SIZE_T, len(data) - 1), 0)
        s = builder.insert_value(s, ptr, 1)
        return s
    raise NotImplementedError(f"const of type {ty}")


def _pointer_size_bits(data_layout: str) -> int:
    """
    Derive pointer size from the target data layout string (e.g., ...-p272:64:64-...).
    Falls back to 64 if no pointer fragment is found.
    """
    last_bits = None
    for frag in data_layout.split("-"):
        if frag.startswith("p"):
            parts = frag.split(":")
            if len(parts) >= 2 and parts[1].isdigit():
                last_bits = int(parts[1])
    return last_bits or 64


def _lower_binary(builder: ir.IRBuilder, instr: mir.Binary, env: dict[str, ir.Value]) -> ir.Value:
    lhs = env[instr.left]
    rhs = env[instr.right]
    op = instr.op
    if op == "+":
        # String concatenation: if both operands are the String struct, call runtime concat.
        if STRING_LLVM_TYPE is not None and lhs.type == STRING_LLVM_TYPE and rhs.type == STRING_LLVM_TYPE:
            return builder.call(_string_concat_decl(builder.module), [lhs, rhs], name=instr.dest)
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


def _lower_console_write(builder: ir.IRBuilder, value: ir.Value) -> None:
    assert STRING_LLVM_TYPE is not None
    if value.type != STRING_LLVM_TYPE:
        value = _coerce_to_string(builder, value)
    fn = _console_write_decl(builder.module)
    builder.call(fn, [value])


def _lower_console_writeln(builder: ir.IRBuilder, value: ir.Value) -> None:
    assert STRING_LLVM_TYPE is not None
    if value.type != STRING_LLVM_TYPE:
        value = _coerce_to_string(builder, value)
    fn = _console_writeln_decl(builder.module)
    builder.call(fn, [value])


def _add_phi_incoming(phi_nodes: dict[str, dict[str, ir.PhiInstr]], edge: mir.Edge, env: dict[str, ir.Value], pred_block: ir.Block) -> None:
    if not edge.args:
        return
    target = edge.target
    for arg_val, (param_name, phi) in zip(edge.args, phi_nodes[target].items()):
        phi.add_incoming(env[arg_val], pred_block)


def _string_concat_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_string_concat")
    if isinstance(fn, ir.Function):
        return fn
    assert STRING_LLVM_TYPE is not None
    fn_ty = ir.FunctionType(STRING_LLVM_TYPE, (STRING_LLVM_TYPE, STRING_LLVM_TYPE))
    fn = ir.Function(module, fn_ty, name="drift_string_concat")
    return fn


def _error_new_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_error_new")
    if isinstance(fn, ir.Function):
        return fn
    assert STRING_LLVM_TYPE is not None and SIZE_T is not None
    str_ptr = STRING_LLVM_TYPE.as_pointer()
    fn_ty = ir.FunctionType(
        ir.IntType(8).as_pointer(),  # Error*
        (
            str_ptr,  # keys
            str_ptr,  # values
            SIZE_T,   # attr_count
            str_ptr,  # event*
            str_ptr,  # domain*
            str_ptr,  # frame_modules
            str_ptr,  # frame_files
            str_ptr,  # frame_funcs
            SIZE_T.as_pointer(),  # frame_lines
            SIZE_T,  # frame_count
            str_ptr,  # cap_keys
            str_ptr,  # cap_values
            SIZE_T.as_pointer(),  # cap_counts
            SIZE_T,  # cap_total
        ),
    )
    return ir.Function(module, fn_ty, name="drift_error_new")


def _error_push_frame_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("error_push_frame")
    if isinstance(fn, ir.Function):
        return fn
    assert STRING_LLVM_TYPE is not None and SIZE_T is not None
    str_ptr = STRING_LLVM_TYPE.as_pointer()
    fn_ty = ir.FunctionType(
        ir.IntType(8).as_pointer(),  # Error*
        (
            ir.IntType(8).as_pointer(),  # Error* incoming
            str_ptr,  # module*
            str_ptr,  # file*
            str_ptr,  # func*
            ir.IntType(64),    # line (still i64 in MIR)
            str_ptr,           # cap_keys
            str_ptr,           # cap_values
            SIZE_T,            # cap_count
        ),
    )
    return ir.Function(module, fn_ty, name="error_push_frame")


def _console_write_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_console_write")
    if isinstance(fn, ir.Function):
        return fn
    assert STRING_LLVM_TYPE is not None
    fn_ty = ir.FunctionType(ir.VoidType(), (STRING_LLVM_TYPE,))
    fn = ir.Function(module, fn_ty, name="drift_console_write")
    return fn


def _console_writeln_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_console_writeln")
    if isinstance(fn, ir.Function):
        return fn
    assert STRING_LLVM_TYPE is not None
    fn_ty = ir.FunctionType(ir.VoidType(), (STRING_LLVM_TYPE,))
    fn = ir.Function(module, fn_ty, name="drift_console_writeln")
    return fn


def _alloc_array_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_alloc_array")
    if isinstance(fn, ir.Function):
        return fn
    assert SIZE_T is not None
    fn_ty = ir.FunctionType(ir.IntType(8).as_pointer(), (SIZE_T, SIZE_T, SIZE_T, SIZE_T))
    return ir.Function(module, fn_ty, name="drift_alloc_array")


def _bounds_check_fail_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_bounds_check_fail")
    if isinstance(fn, ir.Function):
        return fn
    assert SIZE_T is not None
    fn_ty = ir.FunctionType(ir.VoidType(), (SIZE_T, SIZE_T))
    return ir.Function(module, fn_ty, name="drift_bounds_check_fail")


def _string_from_int64_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_string_from_int64")
    if isinstance(fn, ir.Function):
        return fn
    assert STRING_LLVM_TYPE is not None
    fn_ty = ir.FunctionType(STRING_LLVM_TYPE, (ir.IntType(64),))
    fn = ir.Function(module, fn_ty, name="drift_string_from_int64")
    return fn


def _string_from_bool_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_string_from_bool")
    if isinstance(fn, ir.Function):
        return fn
    assert STRING_LLVM_TYPE is not None
    fn_ty = ir.FunctionType(STRING_LLVM_TYPE, (ir.IntType(1),))
    fn = ir.Function(module, fn_ty, name="drift_string_from_bool")
    return fn


def _as_string_ptr(builder: ir.IRBuilder, value: ir.Value) -> ir.Value:
    assert STRING_LLVM_TYPE is not None
    if isinstance(value.type, ir.PointerType) and value.type.pointee == STRING_LLVM_TYPE:
        return value
    # Allocate a slot and store the struct to get a pointer.
    ptr = builder.alloca(STRING_LLVM_TYPE)
    builder.store(value, ptr)
    return ptr


def _coerce_to_string(builder: ir.IRBuilder, value: ir.Value) -> ir.Value:
    if isinstance(value.type, ir.IntType):
        if value.type.width == 1:
            return builder.call(_string_from_bool_decl(builder.module), [value])
        if value.type.width == 64:
            return builder.call(_string_from_int64_decl(builder.module), [value])
    raise NotImplementedError("console write expects String; automatic coercions implemented for bool and Int64")


def _string_from_cstr_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_string_from_cstr")
    if isinstance(fn, ir.Function):
        return fn
    assert STRING_LLVM_TYPE is not None
    fn_ty = ir.FunctionType(STRING_LLVM_TYPE, (ir.IntType(8).as_pointer(),))
    fn = ir.Function(module, fn_ty, name="drift_string_from_cstr")
    return fn


def _string_from_utf8_bytes_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_string_from_utf8_bytes")
    if isinstance(fn, ir.Function):
        return fn
    assert STRING_LLVM_TYPE is not None and SIZE_T is not None
    fn_ty = ir.FunctionType(STRING_LLVM_TYPE, (ir.IntType(8).as_pointer(), SIZE_T))
    fn = ir.Function(module, fn_ty, name="drift_string_from_utf8_bytes")
    return fn


def _string_free_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_string_free")
    if isinstance(fn, ir.Function):
        return fn
    assert STRING_LLVM_TYPE is not None
    fn_ty = ir.FunctionType(ir.VoidType(), (STRING_LLVM_TYPE,))
    fn = ir.Function(module, fn_ty, name="drift_string_free")
    return fn


def _string_to_cstr_decl(module: ir.Module) -> ir.Function:
    fn = module.globals.get("drift_string_to_cstr")
    if isinstance(fn, ir.Function):
        return fn
    i8p = ir.IntType(8).as_pointer()
    assert STRING_LLVM_TYPE is not None
    fn_ty = ir.FunctionType(i8p, (STRING_LLVM_TYPE,))
    fn = ir.Function(module, fn_ty, name="drift_string_to_cstr")
    return fn
