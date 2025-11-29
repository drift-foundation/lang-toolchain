"""SSA-to-LLVM codegen (minimal)."""

from __future__ import annotations

from pathlib import Path

import sys
from llvmlite import ir, binding as llvm  # type: ignore

from . import mir
from .ir_layout import StructLayout
from .types import BOOL, I64, STR, UNIT, Type, array_element_type, array_of

# Architecture word size: target x86_64 for now.
WORD_BITS = 64
WORD_INT = ir.IntType(WORD_BITS)
I64_TY = ir.IntType(64)
I32_TY = ir.IntType(32)
I1_TY = ir.IntType(1)
ERROR_PTR_TY = ir.IntType(8).as_pointer()
I8P = ir.IntType(8).as_pointer()


def _drift_string_type() -> ir.LiteralStructType:
    # { len: i64, ptr: i8* }
    return ir.LiteralStructType([I64_TY, I8P])


def emit_dummy_main_object(out_path: Path) -> None:
    """Emit a trivial main that returns 0."""
    llvm.initialize()
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()

    mod = ir.Module(name="ssa_dummy")
    int32 = ir.IntType(32)
    fn_ty = ir.FunctionType(int32, [])
    main_fn = ir.Function(mod, fn_ty, name="main")
    entry_bb = main_fn.append_basic_block(name="entry")
    builder = ir.IRBuilder(entry_bb)
    builder.ret(int32(0))

    target = llvm.Target.from_default_triple()
    tm = target.create_target_machine()
    obj = tm.emit_object(llvm.parse_assembly(str(mod)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(obj)


def _llvm_type(ty: Type) -> ir.Type:
    """Map Drift types to LLVM types (minimal surface).

    - Int       → word-sized int (currently i64)
    - Int64     → i64
    - Int32     → i32
    - Bool      → i1
    - Void      → void
    """
    if ty.name == "Int":
        return WORD_INT
    if ty.name == "Int64":
        return I64_TY
    if ty.name == "Int32":
        return I32_TY
    if ty == BOOL or ty.name == "Bool":
        return I1_TY
    if ty == UNIT or ty.name == "Void":
        return ir.VoidType()
    if ty == STR or ty.name == "String":
        return _drift_string_type()
    raise NotImplementedError(f"unsupported type {ty}")


def emit_simple_main_object(fn: mir.Function, out_path: Path) -> None:
    """Legacy helper kept for compatibility; emit a single function as main."""
    emit_module_object([fn], fn.name, out_path)


def emit_module_object(
    funcs: list[mir.Function],
    struct_layouts: dict[str, StructLayout],
    entry: str,
    out_path: Path,
) -> None:
    """Lower a small set of SSA functions (ints + branches + calls) into LLVM."""
    llvm.initialize()
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()

    mod = ir.Module(name="ssa_main")
    # First pass: create LLVM functions and basic blocks.
    fn_map: dict[str, ir.Function] = {}
    blocks_map: dict[tuple[str, str], ir.Block] = {}
    struct_type_cache: dict[str, ir.Type] = {}

    def llvm_struct_type(name: str) -> ir.Type:
        if name in struct_type_cache:
            return struct_type_cache[name]
        if name not in struct_layouts:
            raise RuntimeError(f"unknown struct layout for {name}")
        layout = struct_layouts[name]
        field_ll_tys = [_llvm_type(t) for t in layout.field_types]
        ty = ir.LiteralStructType(field_ll_tys)
        struct_type_cache[name] = ty
        return ty

    def _llvm_type_with_structs(ty: Type) -> ir.Type:
        try:
            return _llvm_type(ty)
        except NotImplementedError:
            if ty.name in struct_layouts:
                return llvm_struct_type(ty.name)
            elem_ty = array_element_type(ty)
            if elem_ty is not None:
                return ir.LiteralStructType([WORD_INT, _llvm_type_with_structs(elem_ty).as_pointer()])
            raise
    for f in funcs:
        ret_ty = _llvm_type_with_structs(f.return_type)
        param_tys = [_llvm_type_with_structs(p.type) for p in f.params]
        llvm_fn = ir.Function(mod, ir.FunctionType(ret_ty, param_tys), name=f.name)
        fn_map[f.name] = llvm_fn
        for bname in f.blocks:
            blocks_map[(f.name, bname)] = llvm_fn.append_basic_block(bname)

    # Second pass: PHIs for params + body emission.
    for f in funcs:
        llvm_fn = fn_map[f.name]
        phis: dict[tuple[str, str], ir.Instruction] = {}
        values: dict[str, ir.Value] = {}
        module = llvm_fn.module
        # Map SSA names to types for struct lookups.
        ssa_types: dict[str, Type] = {}
        # Allocate struct slots in entry for any struct-typed SSA values.
        struct_slots: dict[str, ir.Instruction] = {}
        entry_block = blocks_map[(f.name, f.entry)]
        entry_builder = ir.IRBuilder(entry_block)
        entry_builder.position_at_start(entry_block)

        # Map function params.
        for param, llvm_param in zip(f.params, llvm_fn.args):
            llvm_param.name = param.name
            values[param.name] = llvm_param
            ssa_types[param.name] = param.type
            if param.type.name in struct_layouts:
                # Param is a struct passed by value; store to a slot.
                slot = entry_builder.alloca(_llvm_type_with_structs(param.type), name=f"{param.name}.slot")
                entry_builder.store(llvm_param, slot)
                struct_slots[param.name] = slot
                values[param.name] = slot  # treat struct SSA as pointer to its slot

        # Block params: entry params map to function args; others get PHIs.
        for bname, block in f.blocks.items():
            builder = ir.IRBuilder(blocks_map[(f.name, bname)])
            if bname == f.entry and block.params:
                if len(block.params) != len(f.params):
                    raise RuntimeError(f"entry block params arity mismatch in {f.name}")
                for idx, param in enumerate(block.params):
                    values[param.name] = values[f.params[idx].name]
                    ssa_types[param.name] = param.type
                    if param.type.name in struct_layouts and param.name in struct_slots:
                        values[param.name] = struct_slots[param.name]
            else:
                for param in block.params:
                    phi = builder.phi(_llvm_type_with_structs(param.type), name=param.name)
                    phis[(bname, param.name)] = phi
                    values[param.name] = phi
                    ssa_types[param.name] = param.type

        # Emit instructions and terminators.
        for bname, block in f.blocks.items():
            builder = ir.IRBuilder(blocks_map[(f.name, bname)])
            for instr in block.instructions:
                if isinstance(instr, mir.Const):
                    if isinstance(instr.value, (int, bool)):
                        ir_ty = _llvm_type_with_structs(instr.type)
                        values[instr.dest] = ir_ty(int(instr.value))
                        ssa_types[instr.dest] = instr.type
                    elif isinstance(instr.value, str):
                        # Materialize a global string constant.
                        data = bytearray(instr.value.encode("utf-8"))
                        data.append(0)
                        gv = ir.GlobalVariable(
                            module, ir.ArrayType(ir.IntType(8), len(data)), name=f".str{len(module.globals)}"
                        )
                        gv.linkage = "internal"
                        gv.global_constant = True
                        gv.initializer = ir.Constant(gv.type.pointee, data)
                        ptr = builder.gep(
                            gv,
                            [ir.Constant(I32_TY, 0), ir.Constant(I32_TY, 0)],
                            inbounds=True,
                            name=f"strptr{len(module.globals)}",
                        )
                        strlen = ir.Constant(I64_TY, len(data) - 1)
                        str_ty = _drift_string_type()
                        zero_struct = ir.Constant.literal_struct([ir.Constant(I64_TY, 0), ir.Constant(I8P, None)])
                        tmp = builder.insert_value(zero_struct, strlen, 0)
                        str_val = builder.insert_value(tmp, ptr, 1)
                        values[instr.dest] = str_val
                        ssa_types[instr.dest] = instr.type
                    else:
                        raise RuntimeError("simple backend supports int/bool/string const only")
                elif isinstance(instr, mir.Move):
                    values[instr.dest] = values[instr.source]
                    ssa_types[instr.dest] = ssa_types.get(instr.source, ssa_types.get(instr.dest))
                    if instr.dest in struct_slots and instr.source in values:
                        # propagate pointer mapping if applicable
                        values[instr.dest] = values[instr.source]
                elif isinstance(instr, mir.Binary):
                    lhs = values[instr.left]
                    rhs = values[instr.right]
                    if instr.op == "+":
                        values[instr.dest] = builder.add(lhs, rhs, name=instr.dest)
                    elif instr.op == "-":
                        values[instr.dest] = builder.sub(lhs, rhs, name=instr.dest)
                    elif instr.op == "*":
                        values[instr.dest] = builder.mul(lhs, rhs, name=instr.dest)
                    elif instr.op in {"==", "!=", "<", "<=", ">", ">="}:
                        pred_map = {
                            "==": "==",
                            "!=": "!=",
                            "<": "<",
                            "<=": "<=",
                            ">": ">",
                            ">=": ">=",
                        }
                        pred = pred_map[instr.op]
                        cmp = builder.icmp_signed(pred, lhs, rhs, name=f"cmp_{instr.dest}")
                        values[instr.dest] = cmp
                    else:
                        raise RuntimeError(f"unsupported binary op {instr.op}")
                elif isinstance(instr, mir.Call):
                    if instr.normal or instr.error:
                        raise RuntimeError("call with edges not yet supported in SSA backend")
                    # Console builtin: special-case out.writeln
                    if instr.callee == "out.writeln":
                        if len(instr.args) != 1:
                            raise RuntimeError("out.writeln expects one arg")
                        arg_val = values[instr.args[0]]
                        console_fn = module.globals.get("drift_console_writeln")
                        if not isinstance(console_fn, ir.Function):
                            console_fn = ir.Function(
                                module, ir.FunctionType(ir.VoidType(), (_drift_string_type(),)), name="drift_console_writeln"
                            )
                        builder.call(console_fn, [arg_val])
                        if not isinstance(_llvm_type(instr.ret_type), ir.VoidType):
                            # map dest to undef to keep SSA map consistent
                            values[instr.dest] = ir.Constant.undef(_llvm_type(instr.ret_type))
                    else:
                        callee = fn_map.get(instr.callee)
                        if callee is None:
                            raise RuntimeError(f"unknown callee {instr.callee}")
                        args = [values[a] for a in instr.args]
                        call_val = builder.call(callee, args, name=instr.dest)
                        values[instr.dest] = call_val
                        ssa_types[instr.dest] = instr.ret_type
                elif isinstance(instr, mir.StructInit):
                    if instr.type.name not in struct_layouts:
                        raise RuntimeError(f"unknown struct type {instr.type}")
                    if instr.dest not in struct_slots:
                        slot = entry_builder.alloca(_llvm_type_with_structs(instr.type), name=f"{instr.dest}.slot")
                        struct_slots[instr.dest] = slot
                    slot = struct_slots[instr.dest]
                    for idx, arg in enumerate(instr.args):
                        field_ptr = builder.gep(slot, [I32_TY(0), I32_TY(idx)], inbounds=True)
                        builder.store(values[arg], field_ptr)
                    values[instr.dest] = slot
                    ssa_types[instr.dest] = instr.type
                elif isinstance(instr, mir.FieldSet):
                    base_slot = struct_slots.get(instr.base)
                    if base_slot is None:
                        base_slot = values.get(instr.base)
                    if base_slot is None:
                        raise RuntimeError(f"no struct slot for {instr.base}")
                    base_ty = ssa_types.get(instr.base)
                    if base_ty is None or base_ty.name not in struct_layouts:
                        raise RuntimeError(f"base {instr.base} is not a struct for field set")
                    layout = struct_layouts[base_ty.name]
                    if instr.field not in layout.index_by_name:
                        raise RuntimeError(f"struct {base_ty.name} has no field {instr.field}")
                    idx = layout.index_by_name[instr.field]
                    field_ptr = builder.gep(base_slot, [I32_TY(0), I32_TY(idx)], inbounds=True)
                    builder.store(values[instr.value], field_ptr)
                elif isinstance(instr, mir.FieldGet):
                    base_slot = struct_slots.get(instr.base)
                    if base_slot is None:
                        base_slot = values.get(instr.base)
                    if base_slot is None:
                        raise RuntimeError(f"no struct slot for {instr.base}")
                    base_ty = ssa_types.get(instr.base)
                    if base_ty is None or base_ty.name not in struct_layouts:
                        raise RuntimeError(f"base {instr.base} is not a struct for field get")
                    layout = struct_layouts[base_ty.name]
                    if instr.field not in layout.index_by_name:
                        raise RuntimeError(f"struct {base_ty.name} has no field {instr.field}")
                    idx = layout.index_by_name[instr.field]
                    field_ptr = builder.gep(base_slot, [I32_TY(0), I32_TY(idx)], inbounds=True)
                    loaded = builder.load(field_ptr, name=instr.dest)
                    values[instr.dest] = loaded
                    ssa_types[instr.dest] = layout.field_types[idx]
                elif isinstance(instr, mir.ArrayLen):
                    arr_ty = ssa_types.get(instr.base)
                    if arr_ty is None or array_element_type(arr_ty) is None:
                        raise RuntimeError(f"base {instr.base} is not an array for len")
                    arr_val = values[instr.base]
                    # If array is a value struct {len, data*}
                    if isinstance(arr_val.type, ir.LiteralStructType):
                        len_val = builder.extract_value(arr_val, 0, name=instr.dest)
                    else:
                        # assume pointer to struct
                        field_ptr = builder.gep(arr_val, [I32_TY(0), I32_TY(0)], inbounds=True)
                        len_val = builder.load(field_ptr, name=instr.dest)
                    values[instr.dest] = len_val
                    ssa_types[instr.dest] = WORD_INT
                elif isinstance(instr, mir.ArrayGet):
                    arr_ty = ssa_types.get(instr.base)
                    elem_ty = array_element_type(arr_ty) if arr_ty else None
                    if elem_ty is None:
                        raise RuntimeError(f"base {instr.base} is not an array for get")
                    arr_val = values[instr.base]
                    # obtain data pointer
                    if isinstance(arr_val.type, ir.LiteralStructType):
                        data_ptr = builder.extract_value(arr_val, 1)
                    else:
                        field_ptr = builder.gep(arr_val, [I32_TY(0), I32_TY(1)], inbounds=True)
                        data_ptr = builder.load(field_ptr)
                    idx_val = values[instr.index]
                    elem_ptr = builder.gep(data_ptr, [idx_val], inbounds=True)
                    loaded = builder.load(elem_ptr, name=instr.dest)
                    values[instr.dest] = loaded
                    ssa_types[instr.dest] = elem_ty
                elif isinstance(instr, mir.ArraySet):
                    arr_ty = ssa_types.get(instr.base)
                    elem_ty = array_element_type(arr_ty) if arr_ty else None
                    if elem_ty is None:
                        raise RuntimeError(f"base {instr.base} is not an array for set")
                    arr_val = values[instr.base]
                    if isinstance(arr_val.type, ir.LiteralStructType):
                        data_ptr = builder.extract_value(arr_val, 1)
                    else:
                        field_ptr = builder.gep(arr_val, [I32_TY(0), I32_TY(1)], inbounds=True)
                        data_ptr = builder.load(field_ptr)
                    idx_val = values[instr.index]
                    elem_ptr = builder.gep(data_ptr, [idx_val], inbounds=True)
                    builder.store(values[instr.value], elem_ptr)
                elif isinstance(instr, mir.ArrayLiteral):
                    # Stack-allocate array elements and build a {len, data*} struct.
                    elem_ty = instr.elem_type
                    try:
                        elem_ir_ty = _llvm_type_with_structs(elem_ty)
                    except NotImplementedError:
                        raise RuntimeError(f"array literal element type {elem_ty} not supported")
                    count = len(instr.elements)
                    data_buf = builder.alloca(elem_ir_ty, ir.Constant(I32_TY, count), name=f"{instr.dest}.data")
                    for idx, arg in enumerate(instr.elements):
                        builder.store(values[arg], builder.gep(data_buf, [ir.Constant(I32_TY, idx)], inbounds=True))
                    arr_ty = _llvm_type_with_structs(array_of(elem_ty))
                    arr_zero = ir.Constant.literal_struct([ir.Constant(WORD_INT, 0), ir.Constant(elem_ir_ty.as_pointer(), None)])
                    arr_tmp = builder.insert_value(arr_zero, ir.Constant(WORD_INT, count), 0)
                    arr_val = builder.insert_value(arr_tmp, data_buf, 1)
                    values[instr.dest] = arr_val
                    ssa_types[instr.dest] = array_of(elem_ty)
                else:
                    raise RuntimeError(f"unsupported instruction {instr}")

            term = block.terminator
            if isinstance(term, mir.Return):
                if isinstance(_llvm_type(f.return_type), ir.VoidType):
                    builder.ret_void()
                else:
                    if term.value is None:
                        raise RuntimeError(f"missing return value for non-void function {f.name}")
                    if term.value not in values:
                        raise RuntimeError(f"return value {term.value} undefined")
                    builder.ret(values[term.value])
            elif isinstance(term, mir.Call):
                if not (term.normal and term.error):
                    raise RuntimeError("call terminator without normal/error edges not supported")
                callee = fn_map.get(term.callee)
                if callee is None:
                    raise RuntimeError(f"unknown callee {term.callee}")
                if isinstance(callee.function_type.return_type, ir.VoidType):
                    if term.dest is not None:
                        # SSA lowering should not attach a value dest for void returns.
                        raise RuntimeError("call with edges to void callee is not supported yet")
                    builder.call(callee, [values[a] for a in term.args])
                    call_val = None
                else:
                    call_val = builder.call(callee, [values[a] for a in term.args], name=term.dest)
                    values[term.dest] = call_val
                    ssa_types[term.dest] = term.ret_type
                # For now assume normal path only; branch on a constant false (err == null).
                cond_val = ir.Constant(I1_TY, 0)
                # Wire incoming args for successors.
                norm_tgt = term.normal.target
                err_tgt = term.error.target
                if norm_tgt not in f.blocks or err_tgt not in f.blocks:
                    raise RuntimeError("call edge targets unknown block")
                norm_block = f.blocks[norm_tgt]
                err_block = f.blocks[err_tgt]
                if len(term.normal.args) != len(norm_block.params):
                    raise RuntimeError(f"edge to {norm_tgt} has arity {len(term.normal.args)} expected {len(norm_block.params)}")
                if len(term.error.args) != len(err_block.params):
                    raise RuntimeError(f"edge to {err_tgt} has arity {len(term.error.args)} expected {len(err_block.params)}")
                for param, arg in zip(norm_block.params, term.normal.args):
                    phis[(norm_tgt, param.name)].add_incoming(values[arg], blocks_map[(f.name, bname)])
                for param, arg in zip(err_block.params, term.error.args):
                    phis[(err_tgt, param.name)].add_incoming(values[arg], blocks_map[(f.name, bname)])
                builder.cbranch(cond_val, blocks_map[(f.name, err_tgt)], blocks_map[(f.name, norm_tgt)])
            elif isinstance(term, mir.Br):
                tgt = term.target.target
                tblock = f.blocks[tgt]
                if len(term.target.args) != len(tblock.params):
                    raise RuntimeError(f"edge to {tgt} has arity {len(term.target.args)} expected {len(tblock.params)}")
                for param, arg in zip(tblock.params, term.target.args):
                    phis[(tgt, param.name)].add_incoming(values[arg], blocks_map[(f.name, bname)])
                builder.branch(blocks_map[(f.name, tgt)])
            elif isinstance(term, mir.CondBr):
                if term.cond not in values:
                    raise RuntimeError(f"cond value {term.cond} undefined")
                cond_val = values[term.cond]
                if cond_val.type != ir.IntType(1):
                    cond_val = builder.icmp_unsigned("!=", cond_val, cond_val.type(0), name=f"cond_{bname}")
                for edge in (term.then, term.els):
                    tgt = edge.target
                    tblock = f.blocks[tgt]
                    if len(edge.args) != len(tblock.params):
                        raise RuntimeError(
                            f"edge to {tgt} has arity {len(edge.args)} expected {len(tblock.params)}"
                        )
                    for param, arg in zip(tblock.params, edge.args):
                        phis[(tgt, param.name)].add_incoming(values[arg], blocks_map[(f.name, bname)])
                builder.cbranch(cond_val, blocks_map[(f.name, term.then.target)], blocks_map[(f.name, term.els.target)])
            else:
                raise RuntimeError(f"unsupported terminator {term}")

    # Debugging aid: print module if LLVM rejects it.
    target = llvm.Target.from_default_triple()
    tm = target.create_target_machine()
    try:
        llvm_mod = llvm.parse_assembly(str(mod))
    except RuntimeError as e:
        # Debug aid: dump module on parse failure.
        print(mod, file=sys.stderr)
        raise
    obj = tm.emit_object(llvm_mod)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(obj)
