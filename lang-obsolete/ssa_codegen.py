"""SSA-to-LLVM codegen (minimal)."""

from __future__ import annotations

from pathlib import Path

import sys
from llvmlite import ir, binding as llvm  # type: ignore

from . import mir
from .ir_layout import StructLayout
from .types import BOOL, ERROR, I64, INT, STR, UNIT, Type, ReferenceType, array_element_type, array_of

# Architecture word size: target x86_64 for now.
WORD_BITS = 64
WORD_INT = ir.IntType(WORD_BITS)
I64_TY = ir.IntType(64)
I32_TY = ir.IntType(32)
I1_TY = ir.IntType(1)
ERROR_PTR_TY = ir.IntType(8).as_pointer()
DRIFT_ERROR_ARG_PTR_TY = None  # legacy args; initialized lazily after _drift_string_type is defined
DV_TY = None  # initialized lazily
I8P = ir.IntType(8).as_pointer()


def _drift_string_type() -> ir.LiteralStructType:
    # { len: i64, ptr: i8* }
    return ir.LiteralStructType([I64_TY, I8P])


# initialize the arg pointer type after string type is defined
DRIFT_ERROR_ARG_PTR_TY = ir.LiteralStructType([_drift_string_type(), _drift_string_type()]).as_pointer()
# DiagnosticValue ABI (tag + union). Union is modeled as a byte array big enough for the largest primitive/string case.
# Model DriftDiagnosticValue layout: tag (u8) + padding + 16-byte union (align 8).
DV_TY = ir.LiteralStructType(
    [
        ir.IntType(8),                         # tag
        ir.ArrayType(ir.IntType(8), 7),        # padding to 8-byte alignment
        ir.ArrayType(I64_TY, 2),               # 16-byte union storage (aligned)
    ]
)


def _declare_dv_sret(module: ir.Module, name: str, arg_types: list[ir.Type]) -> ir.Function:
    """Declare a DriftDiagnosticValue-returning helper using an explicit sret out-param."""
    fn = ir.Function(module, ir.FunctionType(ir.VoidType(), [DV_TY.as_pointer(), *arg_types]), name=name)
    # Mark the first argument as sret/noalias to match C ABI for aggregates >16 bytes.
    fn.args[0].attributes.add("sret")
    fn.args[0].attributes.add("noalias")
    # Tag so the call-site logic knows to use an out-slot.
    setattr(fn, "_dv_sret", True)
    return fn


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
    - Float64   → double
    - Bool      → i1
    - Void      → void
    """
    if ty.name == "Int":
        return WORD_INT
    if ty.name == "Int64":
        return I64_TY
    if ty.name == "Int32":
        return I32_TY
    if ty.name == "Float64" or ty.name == "Float":
        return ir.DoubleType()
    if ty == BOOL or ty.name == "Bool":
        return I1_TY
    if ty == ERROR or ty.name == "Error":
        return ERROR_PTR_TY
    if ty == UNIT or ty.name == "Void":
        return ir.VoidType()
    if ty == STR or ty.name == "String":
        return _drift_string_type()
    if ty.name == "DiagnosticValue":
        return DV_TY
    raise NotImplementedError(f"unsupported type {ty}")


def emit_simple_main_object(fn: mir.Function, out_path: Path) -> None:
    """Legacy helper kept for compatibility; emit a single function as main."""
    emit_module_object([fn], fn.name, out_path)


def emit_module_object(
    funcs: list[mir.Function],
    struct_layouts: dict[str, StructLayout],
    entry: str,
    out_path: Path,
    exception_names: Optional[set[str]] = None,
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
    # Track which functions are allowed to produce errors; MIR should already mark them.
    can_error_funcs: set[str] = {f.name for f in funcs if getattr(f, "can_error", False)}
    # Cached runtime decls
    rt_error_dummy: Optional[ir.Function] = None
    rt_error_add_attr_dv: Optional[ir.Function] = None
    rt_error_add_local_dv: Optional[ir.Function] = None
    rt_exc_attrs_get_dv: Optional[ir.Function] = None
    rt_dv_int: Optional[ir.Function] = None
    rt_dv_string: Optional[ir.Function] = None
    rt_dv_bool: Optional[ir.Function] = None
    rt_dv_null: Optional[ir.Function] = None
    rt_dv_get: Optional[ir.Function] = None
    rt_dv_index: Optional[ir.Function] = None
    rt_dv_as_int: Optional[ir.Function] = None
    rt_dv_as_bool: Optional[ir.Function] = None
    rt_dv_as_float: Optional[ir.Function] = None
    rt_dv_as_string: Optional[ir.Function] = None

    def _reachable(f: mir.Function) -> set[str]:
        seen: set[str] = set()
        work = [f.entry]
        while work:
            b = work.pop()
            if b in seen or b not in f.blocks:
                continue
            seen.add(b)
            term = f.blocks[b].terminator
            if isinstance(term, mir.Br):
                work.append(term.target.target)
            elif isinstance(term, mir.CondBr):
                work.append(term.then.target)
                work.append(term.els.target)
            elif isinstance(term, mir.Call) and term.normal and term.error:
                work.append(term.normal.target)
                work.append(term.error.target)
        return seen

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

    def llvm_ret_with_error(ret_ty: Type) -> ir.Type:
        """{T, Error*} for can-error functions; Error* for void-like return."""
        val_ll_ty = _llvm_type_with_structs(ret_ty)
        if isinstance(val_ll_ty, ir.VoidType):
            return ERROR_PTR_TY
        return ir.LiteralStructType([val_ll_ty, ERROR_PTR_TY])

    exception_names = exception_names or set()

    def _llvm_type_with_structs(ty: Type) -> ir.Type:
        try:
            return _llvm_type(ty)
        except NotImplementedError:
            if ty.name in exception_names:
                return ERROR_PTR_TY
            if ty.name == "Optional" and ty.args:
                inner = _llvm_type_with_structs(ty.args[0])
                # Optional<Bool> runtime uses i8 tag + i8 value; coerce accordingly.
                if ty.args[0] == BOOL:
                    inner = ir.IntType(8)
                return ir.LiteralStructType([ir.IntType(8), inner])
            if isinstance(ty, ReferenceType):
                return _llvm_type_with_structs(ty.args[0]).as_pointer()
            if ty.name in struct_layouts:
                return llvm_struct_type(ty.name)
            elem_ty = array_element_type(ty)
            if elem_ty is not None:
                return ir.LiteralStructType([WORD_INT, _llvm_type_with_structs(elem_ty).as_pointer()])
            raise

    opt_string_ty = _llvm_type_with_structs(Type("Optional", (STR,)))
    # Assert MIR can_error markings cover all throw/call-with-edges sites.
    for f in funcs:
        for block in f.blocks.values():
            term = block.terminator
            if isinstance(term, mir.Call) and term.normal and term.error:
                if term.callee not in can_error_funcs:
                    raise RuntimeError(f"callee {term.callee} used with error edges but not marked can_error")
            elif isinstance(term, mir.Throw):
                if f.name not in can_error_funcs:
                    raise RuntimeError(f"function {f.name} contains Throw but is not marked can_error")

    for f in funcs:
        reachable = _reachable(f)
        if f.name in can_error_funcs:
            ret_ty = llvm_ret_with_error(f.return_type)
        else:
            ret_ty = _llvm_type_with_structs(f.return_type)
        param_tys = [_llvm_type_with_structs(p.type) for p in f.params]
        llvm_fn = ir.Function(mod, ir.FunctionType(ret_ty, param_tys), name=f.name)
        fn_map[f.name] = llvm_fn
        for bname in f.blocks:
            if bname in reachable:
                blocks_map[(f.name, bname)] = llvm_fn.append_basic_block(bname)

    for f in funcs:
        reachable = _reachable(f)
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

        # Pre-allocate struct slots for any struct init/call dests to ensure allocas live in entry.
        prealloc_builder = ir.IRBuilder(entry_block)
        prealloc_builder.position_at_start(entry_block)
        for blk in f.blocks.values():
            for instr in blk.instructions:
                struct_name: Optional[str] = None
                if isinstance(instr, mir.StructInit):
                    struct_name = instr.type.name
                    dest_name = instr.dest
                elif isinstance(instr, mir.Call) and not (instr.normal or instr.error) and instr.callee in struct_layouts:
                    struct_name = instr.callee
                    dest_name = instr.dest
                if struct_name and dest_name not in struct_slots:
                    prealloc_builder.position_at_start(entry_block)
                    struct_slots[dest_name] = prealloc_builder.alloca(
                        _llvm_type_with_structs(Type(struct_name)), name=f"{dest_name}.slot"
                    )

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

        # Pre-emit allocas for explicit Alloc instructions so they are always
        # defined in the entry block before any use.
        for block in f.blocks.values():
            for instr in block.instructions:
                if isinstance(instr, mir.Alloc):
                    ll_ty = _llvm_type_with_structs(instr.type)
                    entry_builder.position_at_start(entry_block)
                    slot = entry_builder.alloca(ll_ty, name=f"{instr.dest}.slot")
                    values[instr.dest] = slot
                    ssa_types[instr.dest] = ReferenceType(instr.type)

        # Block params: entry params map to function args; others get PHIs.
        for bname, block in f.blocks.items():
            if bname not in reachable:
                continue
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
            if bname not in reachable:
                continue
            builder = ir.IRBuilder(blocks_map[(f.name, bname)])
            for instr in block.instructions:
                if isinstance(instr, mir.Const):
                    if instr.type == ERROR or instr.type.name == "Error":
                        if instr.value not in (None, 0):
                            raise RuntimeError("Error const supports only null")
                        values[instr.dest] = ir.Constant(ERROR_PTR_TY, None)
                        ssa_types[instr.dest] = instr.type
                    elif isinstance(instr.value, (int, bool)):
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
                elif isinstance(instr, mir.Alloc):
                    # Already emitted in the pre-pass above.
                    continue
                elif isinstance(instr, mir.Store):
                    base_val = values.get(instr.base)
                    if base_val is None:
                        raise RuntimeError(f"store base {instr.base} undefined")
                    val = values.get(instr.value)
                    if val is None:
                        raise RuntimeError(f"store value {instr.value} undefined; have {list(values.keys())}")
                    # Align pointee types if needed.
                    if isinstance(base_val.type, ir.PointerType) and isinstance(val.type, ir.PointerType):
                        if base_val.type.pointee != val.type.pointee:
                            val = builder.bitcast(val, base_val.type)
                    if not isinstance(base_val.type, ir.PointerType):
                        raise RuntimeError(f"store base {instr.base} is not a pointer")
                    # If storing a struct value into a pointer, bitcast the destination as needed.
                    if isinstance(val.type, ir.LiteralStructType) and isinstance(base_val.type, ir.PointerType):
                        if base_val.type.pointee != val.type:
                            base_val = builder.bitcast(base_val, val.type.as_pointer())
                    # If the source is a pointer to a struct value, load it before storing.
                    expected_ty = base_val.type.pointee if isinstance(base_val.type, ir.PointerType) else None
                    val_ssa_ty = ssa_types.get(instr.value)
                    if (
                        isinstance(val.type, ir.PointerType)
                        and isinstance(expected_ty, ir.LiteralStructType)
                        and (val_ssa_ty and not isinstance(val_ssa_ty, ReferenceType))
                    ):
                        val = builder.load(val)
                    builder.store(val, base_val)
                elif isinstance(instr, mir.Binary):
                    def _load_if_ref(name: str):
                        val = values[name]
                        ty = ssa_types.get(name)
                        if isinstance(ty, ReferenceType):
                            val = builder.load(val)
                            ty = ty.args[0]
                        return val, ty
                    lhs, lhs_ty = _load_if_ref(instr.left)
                    rhs, rhs_ty = _load_if_ref(instr.right)
                    if instr.op == "+":
                        values[instr.dest] = builder.add(lhs, rhs, name=instr.dest)
                        ssa_types[instr.dest] = lhs_ty or rhs_ty
                    elif instr.op == "-":
                        values[instr.dest] = builder.sub(lhs, rhs, name=instr.dest)
                        ssa_types[instr.dest] = lhs_ty or rhs_ty
                    elif instr.op == "*":
                        values[instr.dest] = builder.mul(lhs, rhs, name=instr.dest)
                        ssa_types[instr.dest] = lhs_ty or rhs_ty
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
                        ssa_types[instr.dest] = BOOL
                    else:
                        raise RuntimeError(f"unsupported binary op {instr.op}")
                elif isinstance(instr, mir.Call):
                    callee: Optional[ir.Function] = None
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
                            values[instr.dest] = ir.Constant(_llvm_type(instr.ret_type), None)
                        continue
                    elif instr.callee in struct_layouts:
                        layout = struct_layouts[instr.callee]
                        if instr.args and len(instr.args) != len(layout.field_names):
                            raise RuntimeError(f"struct ctor {instr.callee} arity mismatch")
                        # Allocate slot if needed.
                        if instr.dest not in struct_slots:
                            eb = ir.IRBuilder(entry_block)
                            eb.position_at_start(entry_block)
                            struct_slots[instr.dest] = eb.alloca(
                                _llvm_type_with_structs(Type(instr.callee)), name=f"{instr.dest}.slot"
                            )
                        slot = struct_slots[instr.dest]
                        for idx, arg in enumerate(instr.args):
                            field_ptr = builder.gep(slot, [I32_TY(0), I32_TY(idx)], inbounds=True)
                            builder.store(values[arg], field_ptr)
                        values[instr.dest] = slot
                        ssa_types[instr.dest] = Type(instr.callee)
                    else:
                        callee = fn_map.get(instr.callee)
                        if callee is not None and callee.name != instr.callee:
                            # Force runtime helper resolution for known helpers if the name is mismatched.
                            callee = None
                        if callee is None:
                            mod_fn = module.globals.get(instr.callee)
                            if isinstance(mod_fn, ir.Function):
                                callee = mod_fn
                        # If we picked up a non-sret declaration for DV helpers, force redeclaration.
                        if callee is not None and instr.callee.startswith("drift_dv_") and not getattr(
                            callee, "_dv_sret", False
                        ):
                            callee = None
                        dv_sret_decl = False
                        if callee is None:
                            # Try runtime helpers.
                            if instr.callee == "drift_error_new_dummy":
                                if rt_error_dummy is None:
                                    rt_error_dummy = ir.Function(
                                        module,
                                        ir.FunctionType(
                                            ERROR_PTR_TY, [WORD_INT, _drift_string_type(), _drift_string_type()]
                                        ),
                                        name="drift_error_new_dummy",
                                    )
                                callee = rt_error_dummy
                            elif instr.callee == "drift_error_add_attr_dv":
                                if rt_error_add_attr_dv is None:
                                    rt_error_add_attr_dv = ir.Function(
                                        module,
                                        ir.FunctionType(ir.VoidType(), [ERROR_PTR_TY, _drift_string_type(), DV_TY.as_pointer()]),
                                        name="drift_error_add_attr_dv",
                                    )
                                callee = rt_error_add_attr_dv
                            elif instr.callee == "drift_error_add_local_dv":
                                if rt_error_add_local_dv is None:
                                    rt_error_add_local_dv = ir.Function(
                                        module,
                                        ir.FunctionType(
                                            ir.VoidType(),
                                            [ERROR_PTR_TY, _drift_string_type(), _drift_string_type(), DV_TY],
                                        ),
                                        name="drift_error_add_local_dv",
                                    )
                                callee = rt_error_add_local_dv
                            elif instr.callee == "drift_dv_int":
                                if rt_dv_int is None:
                                    rt_dv_int = _declare_dv_sret(module, "drift_dv_int", [I64_TY])
                                callee = rt_dv_int
                            elif instr.callee == "drift_dv_string":
                                if rt_dv_string is None:
                                    rt_dv_string = _declare_dv_sret(module, "drift_dv_string", [_drift_string_type()])
                                callee = rt_dv_string
                            elif instr.callee == "drift_dv_bool":
                                if rt_dv_bool is None:
                                    rt_dv_bool = _declare_dv_sret(module, "drift_dv_bool", [ir.IntType(8)])
                                callee = rt_dv_bool
                            elif instr.callee == "drift_dv_float":
                                callee = module.globals.get("drift_dv_float")
                                if callee is None:
                                    callee = _declare_dv_sret(module, "drift_dv_float", [ir.DoubleType()])
                                else:
                                    setattr(callee, "_dv_sret", True)
                            elif instr.callee == "drift_dv_null":
                                if rt_dv_null is None:
                                    rt_dv_null = _declare_dv_sret(module, "drift_dv_null", [])
                                callee = rt_dv_null
                            elif instr.callee == "drift_dv_missing":
                                callee = module.globals.get("drift_dv_missing")
                                if callee is None:
                                    callee = _declare_dv_sret(module, "drift_dv_missing", [])
                                else:
                                    setattr(callee, "_dv_sret", True)
                            elif instr.callee == "drift_dv_get":
                                if rt_dv_get is None:
                                    rt_dv_get = _declare_dv_sret(module, "drift_dv_get", [DV_TY, _drift_string_type()])
                                callee = rt_dv_get
                            elif instr.callee == "drift_dv_index":
                                if rt_dv_index is None:
                                    rt_dv_index = _declare_dv_sret(module, "drift_dv_index", [DV_TY, I64_TY])
                                callee = rt_dv_index
                            elif instr.callee == "drift_diag_from_bool":
                                callee = module.globals.get("drift_diag_from_bool")
                                if callee is None:
                                    callee = _declare_dv_sret(module, "drift_diag_from_bool", [ir.IntType(8)])
                                else:
                                    setattr(callee, "_dv_sret", True)
                            elif instr.callee == "drift_diag_from_int":
                                callee = module.globals.get("drift_diag_from_int")
                                if callee is None:
                                    callee = _declare_dv_sret(module, "drift_diag_from_int", [I64_TY])
                                else:
                                    setattr(callee, "_dv_sret", True)
                            elif instr.callee == "drift_diag_from_float":
                                callee = module.globals.get("drift_diag_from_float")
                                if callee is None:
                                    callee = _declare_dv_sret(module, "drift_diag_from_float", [ir.DoubleType()])
                                else:
                                    setattr(callee, "_dv_sret", True)
                            elif instr.callee == "drift_diag_from_string":
                                callee = module.globals.get("drift_diag_from_string")
                                if callee is None:
                                    callee = _declare_dv_sret(module, "drift_diag_from_string", [_drift_string_type()])
                                else:
                                    setattr(callee, "_dv_sret", True)
                            elif instr.callee == "drift_diag_from_optional_int":
                                callee = module.globals.get("drift_diag_from_optional_int")
                                if callee is None:
                                    opt_int_ty = ir.LiteralStructType([ir.IntType(8), WORD_INT])
                                    callee = _declare_dv_sret(module, "drift_diag_from_optional_int", [opt_int_ty])
                                else:
                                    setattr(callee, "_dv_sret", True)
                            elif instr.callee == "drift_diag_from_optional_string":
                                callee = module.globals.get("drift_diag_from_optional_string")
                                if callee is None:
                                    opt_str_ty = _llvm_type_with_structs(Type("Optional", (STR,)))
                                    callee = _declare_dv_sret(module, "drift_diag_from_optional_string", [opt_str_ty])
                                else:
                                    setattr(callee, "_dv_sret", True)
                            elif instr.callee == "drift_diag_from_optional_bool":
                                callee = module.globals.get("drift_diag_from_optional_bool")
                                if callee is None:
                                    opt_bool_ty = ir.LiteralStructType([ir.IntType(8), ir.IntType(8)])
                                    callee = _declare_dv_sret(module, "drift_diag_from_optional_bool", [opt_bool_ty])
                                else:
                                    setattr(callee, "_dv_sret", True)
                            elif instr.callee == "drift_diag_from_optional_float":
                                callee = module.globals.get("drift_diag_from_optional_float")
                                if callee is None:
                                    opt_f_ty = _llvm_type_with_structs(Type("Optional", (Type("Float64"),)))
                                    callee = _declare_dv_sret(module, "drift_diag_from_optional_float", [opt_f_ty])
                                else:
                                    setattr(callee, "_dv_sret", True)
                            elif instr.callee == "drift_dv_as_int":
                                if rt_dv_as_int is None:
                                    rt_dv_as_int = ir.Function(
                                        module,
                                        ir.FunctionType(_llvm_type_with_structs(Type("Optional", (INT,))), [DV_TY.as_pointer()]),
                                        name="drift_dv_as_int",
                                    )
                                callee = rt_dv_as_int
                            elif instr.callee == "drift_dv_as_bool":
                                if rt_dv_as_bool is None:
                                    opt_bool_ty = ir.LiteralStructType([ir.IntType(8), ir.IntType(8)])
                                    rt_dv_as_bool = ir.Function(
                                        module,
                                        ir.FunctionType(opt_bool_ty, [DV_TY.as_pointer()]),
                                        name="drift_dv_as_bool",
                                    )
                                callee = rt_dv_as_bool
                            elif instr.callee == "drift_dv_as_float":
                                if rt_dv_as_float is None:
                                    opt_f_ty = _llvm_type_with_structs(Type("Optional", (Type("Float64"),)))
                                    rt_dv_as_float = ir.Function(
                                        module,
                                        ir.FunctionType(opt_f_ty, [DV_TY.as_pointer()]),
                                        name="drift_dv_as_float",
                                    )
                                callee = rt_dv_as_float
                            elif instr.callee == "drift_dv_as_string":
                                if rt_dv_as_string is None:
                                    opt_s_ty = _llvm_type_with_structs(Type("Optional", (STR,)))
                                    rt_dv_as_string = ir.Function(
                                        module,
                                        ir.FunctionType(
                                            ir.VoidType(), [opt_s_ty.as_pointer(), DV_TY.as_pointer()]
                                        ),
                                        name="drift_dv_as_string",
                                    )
                                    rt_dv_as_string.args[0].attributes.add("sret")
                                    rt_dv_as_string.args[0].attributes.add("noalias")
                                    setattr(rt_dv_as_string, "_opt_sret_ty", opt_s_ty)
                                callee = rt_dv_as_string
                            elif instr.callee == "drift_string_eq":
                                callee = ir.Function(
                                    module,
                                    ir.FunctionType(WORD_INT, [_drift_string_type(), _drift_string_type()]),
                                    name="drift_string_eq",
                                )
                            elif instr.callee == "__exc_attrs_get_dv":
                                if rt_exc_attrs_get_dv is None:
                                    rt_exc_attrs_get_dv = ir.Function(
                                        module,
                                        ir.FunctionType(ir.VoidType(), [DV_TY.as_pointer(), ERROR_PTR_TY, _drift_string_type()]),
                                        name="__exc_attrs_get_dv",
                                    )
                                callee = rt_exc_attrs_get_dv
                            elif instr.callee == "drift_optional_int_some":
                                callee = module.globals.get("drift_optional_int_some")
                                if callee is None:
                                    callee = ir.Function(
                                        module,
                                        ir.FunctionType(
                                            ir.LiteralStructType([ir.IntType(8), WORD_INT]), [WORD_INT]
                                        ),
                                        name="drift_optional_int_some",
                                    )
                            elif instr.callee == "drift_optional_int_none":
                                callee = module.globals.get("drift_optional_int_none")
                                if callee is None:
                                    callee = ir.Function(
                                        module,
                                        ir.FunctionType(ir.LiteralStructType([ir.IntType(8), WORD_INT]), []),
                                        name="drift_optional_int_none",
                                    )
                            # If still unknown, declare an external with a best-effort signature.
                            if callee is None:
                                ret_ty = _llvm_type_with_structs(instr.ret_type)
                                param_tys = [_llvm_type_with_structs(ssa_types[a]) for a in instr.args]
                                if instr.ret_type.name == "DiagnosticValue":
                                    callee = _declare_dv_sret(module, instr.callee, param_tys)
                                else:
                                    callee = ir.Function(module, ir.FunctionType(ret_ty, param_tys), name=instr.callee)
                                module.globals[callee.name] = callee
                                fn_map[instr.callee] = callee
                    if callee is None:
                        raise RuntimeError(f"unknown callee {instr.callee}")
                    if callee.name in can_error_funcs:
                        raise RuntimeError(f"call to can-error function {callee.name} without error edges")
                    args = [values[a] for a in instr.args]
                    # Adjust bool arg to match runtime signature (i8).
                    if callee is rt_dv_bool and args and isinstance(args[0].type, ir.IntType) and args[0].type.width == 1:
                        args[0] = builder.zext(args[0], ir.IntType(8), name=f"{instr.dest}_bext")
                    dv_sret_call = getattr(callee, "_dv_sret", False)
                    if callee is rt_exc_attrs_get_dv:
                        out_slot = builder.alloca(DV_TY, name=f"{instr.dest}.dv")
                        out_slot.align = 8
                        builder.call(callee, [out_slot] + args)
                        call_val = builder.load(out_slot, name=instr.dest)
                        values[instr.dest] = call_val
                    elif dv_sret_call:
                        out_slot = builder.alloca(DV_TY, name=f"{instr.dest}.dv")
                        out_slot.align = 8
                        builder.call(callee, [out_slot] + args)
                        call_val = builder.load(out_slot, name=instr.dest)
                        values[instr.dest] = call_val
                    elif instr.callee in ("drift_optional_int_some", "drift_optional_int_none"):
                        call_val = builder.call(callee, args, name=instr.dest)
                        values[instr.dest] = call_val
                    elif callee is rt_error_add_attr_dv:
                        # ensure DiagnosticValue is passed by pointer
                        dv_arg = args[2]
                        if not isinstance(dv_arg.type, ir.PointerType):
                            slot = builder.alloca(DV_TY, name=f"{instr.dest}.dvslot")
                            slot.align = 8
                            builder.store(dv_arg, slot)
                            dv_arg = slot
                        builder.call(callee, [args[0], args[1], dv_arg])
                        # void return; no SSA value
                    elif callee in (rt_dv_as_int, rt_dv_as_bool, rt_dv_as_float):
                        dv_arg = args[0]
                        if not isinstance(dv_arg.type, ir.PointerType):
                            slot = builder.alloca(DV_TY, name=f"{instr.dest}.dvslot")
                            slot.align = 8
                            builder.store(dv_arg, slot)
                            dv_arg = slot
                        call_val = builder.call(callee, [dv_arg], name=instr.dest)
                        values[instr.dest] = call_val
                    elif callee is rt_dv_as_string:
                        dv_arg = args[0]
                        if not isinstance(dv_arg.type, ir.PointerType):
                            slot = builder.alloca(DV_TY, name=f"{instr.dest}.dvslot")
                            slot.align = 8
                            builder.store(dv_arg, slot)
                            dv_arg = slot
                        ret_ty = getattr(callee, "_opt_sret_ty")
                        out_slot = builder.alloca(ret_ty, name=f"{instr.dest}.opt_str")
                        out_slot.align = 8
                        builder.call(callee, [out_slot, dv_arg])
                        call_val = builder.load(out_slot, name=instr.dest)
                        values[instr.dest] = call_val
                    else:
                        adj_args = []
                        for idx, arg in enumerate(args):
                            expected = callee.function_type.args[idx]
                            if isinstance(expected, ir.LiteralStructType) and isinstance(arg.type, ir.PointerType):
                                if arg.type.pointee == expected:
                                    arg = builder.load(arg)
                            elif isinstance(expected, ir.PointerType):
                                if isinstance(arg.type, ir.LiteralStructType):
                                    slot = builder.alloca(expected.pointee, name=f"{instr.dest}.arg{idx}")
                                    builder.store(arg, slot)
                                    arg = slot
                                elif not isinstance(arg.type, ir.PointerType):
                                    slot = builder.alloca(expected.pointee, name=f"{instr.dest}.arg{idx}")
                                    # If scalar, bitcast/store as needed.
                                    if expected.pointee == arg.type:
                                        builder.store(arg, slot)
                                    else:
                                        cast_slot = builder.bitcast(slot, arg.type.as_pointer()) if hasattr(arg.type, "as_pointer") else slot
                                        builder.store(arg, cast_slot)
                                    arg = slot
                            adj_args.append(arg)
                        try:
                            call_val = builder.call(callee, adj_args, name=instr.dest)
                        except TypeError as exc:
                            arg_ssa_types = [ssa_types.get(a) for a in instr.args]
                            raise TypeError(
                                f"{instr.callee} call arg types {[a.type for a in adj_args]} "
                                f"expected {callee.function_type} "
                                f"ssa_arg_types={arg_ssa_types}"
                            ) from exc
                        values[instr.dest] = call_val
                    ssa_types[instr.dest] = instr.ret_type
                elif isinstance(instr, mir.StructInit):
                    if instr.type.name not in struct_layouts:
                        raise RuntimeError(f"unknown struct type {instr.type}")
                    if instr.dest not in struct_slots:
                        eb = ir.IRBuilder(entry_block)
                        eb.position_at_start(entry_block)
                        slot = eb.alloca(_llvm_type_with_structs(instr.type), name=f"{instr.dest}.slot")
                        struct_slots[instr.dest] = slot
                    slot = struct_slots[instr.dest]
                    for idx, arg in enumerate(instr.args):
                        field_ptr = builder.gep(slot, [I32_TY(0), I32_TY(idx)], inbounds=True)
                        arg_val = values[arg]
                        arg_ty = ssa_types.get(arg)
                        if isinstance(arg_ty, ReferenceType):
                            arg_val = builder.load(arg_val)
                        builder.store(arg_val, field_ptr)
                    values[instr.dest] = slot
                    ssa_types[instr.dest] = instr.type
                elif isinstance(instr, mir.FieldSet):
                    base_ty = ssa_types.get(instr.base)
                    inner_ty = base_ty.args[0] if isinstance(base_ty, ReferenceType) else base_ty
                    if inner_ty is None or inner_ty.name not in struct_layouts:
                        raise RuntimeError(f"base {instr.base} is not a struct for field set")
                    layout = struct_layouts[inner_ty.name]
                    if instr.field not in layout.index_by_name:
                        raise RuntimeError(f"struct {inner_ty.name} has no field {instr.field}")
                    base_ptr = values.get(instr.base) if isinstance(base_ty, ReferenceType) else struct_slots.get(instr.base)
                    if base_ptr is None:
                        base_ptr = values.get(instr.base)
                    if base_ptr is None:
                        raise RuntimeError(f"no struct slot for {instr.base}")
                    idx = layout.index_by_name[instr.field]
                    field_ptr = builder.gep(base_ptr, [I32_TY(0), I32_TY(idx)], inbounds=True)
                    builder.store(values[instr.value], field_ptr)
                elif isinstance(instr, mir.FieldGet):
                    base_ty = ssa_types.get(instr.base)
                    if base_ty == ERROR:
                        base_ptr = values[instr.base]
                        err_ty = ir.LiteralStructType([ir.IntType(64)])
                        cast_ptr = builder.bitcast(base_ptr, err_ty.as_pointer())
                        if instr.field == "code":
                            field_ptr = builder.gep(cast_ptr, [I32_TY(0), I32_TY(0)], inbounds=True)
                            loaded = builder.load(field_ptr, name=instr.dest)
                            values[instr.dest] = loaded
                            ssa_types[instr.dest] = I64
                            continue
                        raise RuntimeError(f"Error has no field {instr.field}")
                    if base_ty and base_ty.name == "Optional" and base_ty.args:
                        inner_ty = base_ty.args[0]
                        base_val = values[instr.base]
                        if instr.field == "is_some":
                            loaded = builder.extract_value(base_val, 0, name=instr.dest)
                            values[instr.dest] = loaded
                            ssa_types[instr.dest] = BOOL
                            continue
                        if instr.field == "value":
                            loaded = builder.extract_value(base_val, 1, name=instr.dest)
                            values[instr.dest] = loaded
                            ssa_types[instr.dest] = inner_ty
                            continue
                        raise RuntimeError(f"Optional has no field {instr.field}")
                    inner_ty = base_ty.args[0] if isinstance(base_ty, ReferenceType) else base_ty
                    if inner_ty is None or inner_ty.name not in struct_layouts:
                        raise RuntimeError(f"base {instr.base} is not a struct for field get")
                    layout = struct_layouts[inner_ty.name]
                    if instr.field not in layout.index_by_name:
                        raise RuntimeError(f"struct {inner_ty.name} has no field {instr.field}")
                    base_ptr = values.get(instr.base) if isinstance(base_ty, ReferenceType) else struct_slots.get(instr.base)
                    if base_ptr is None:
                        base_ptr = values.get(instr.base)
                    if base_ptr is None:
                        raise RuntimeError(f"no struct slot for {instr.base}")
                    idx = layout.index_by_name[instr.field]
                    if isinstance(base_ptr.type, ir.LiteralStructType):
                        loaded = builder.extract_value(base_ptr, idx, name=instr.dest)
                        values[instr.dest] = loaded
                        ssa_types[instr.dest] = layout.field_types[idx]
                        continue
                    field_ptr = builder.gep(base_ptr, [I32_TY(0), I32_TY(idx)], inbounds=True)
                    loaded = builder.load(field_ptr, name=instr.dest)
                    values[instr.dest] = loaded
                    ssa_types[instr.dest] = layout.field_types[idx]
                elif isinstance(instr, mir.Unary):
                    operand = values[instr.operand]
                    if instr.op == "not":
                        if not isinstance(operand.type, ir.IntType) or operand.type.width != 1:
                            operand = builder.icmp_unsigned("!=", operand, operand.type(0), name=f"{instr.dest}_bool")
                        result = builder.icmp_unsigned("==", operand, ir.IntType(1)(0), name=instr.dest)
                        values[instr.dest] = result
                        ssa_types[instr.dest] = BOOL
                    elif instr.op == "-":
                        if isinstance(operand.type, ir.DoubleType):
                            result = builder.fneg(operand, name=instr.dest)
                        else:
                            result = builder.neg(operand, name=instr.dest)
                        values[instr.dest] = result
                        ssa_types[instr.dest] = ssa_types.get(instr.operand, BOOL)
                    else:
                        raise RuntimeError(f"unsupported unary op {instr.op}")
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
                elif isinstance(instr, mir.ErrorEvent):
                    # Projection of error event: for the dummy runtime, call helper to get code as Int.
                    if instr.error not in values:
                        raise RuntimeError(f"error value {instr.error} undefined")
                    err_val = values[instr.error]
                    err_evt_fn = module.globals.get("drift_error_get_code")
                    if not isinstance(err_evt_fn, ir.Function):
                        err_evt_fn = ir.Function(
                            module, ir.FunctionType(ir.IntType(64), [ERROR_PTR_TY]), name="drift_error_get_code"
                        )
                    call_val = builder.call(err_evt_fn, [err_val], name=instr.dest)
                    values[instr.dest] = call_val
                    ssa_types[instr.dest] = I64
                else:
                    raise RuntimeError(f"unsupported instruction {instr}")

            term = block.terminator
            if isinstance(term, mir.Return):
                if f.name in can_error_funcs:
                    ret_ll_ty = _llvm_type_with_structs(f.return_type)
                    if term.error is None:
                        err_val = ir.Constant(ERROR_PTR_TY, None)
                    else:
                        if term.error not in values:
                            raise RuntimeError(f"error value {term.error} undefined in {f.name}")
                        err_val = values[term.error]
                    if isinstance(ret_ll_ty, ir.VoidType):
                        builder.ret(err_val)
                    else:
                        if term.value is None:
                            raise RuntimeError(f"missing return value for non-void function {f.name}")
                        if term.value not in values:
                            raise RuntimeError(f"return value {term.value} undefined")
                        val_ll = values[term.value]
                        # Unwrap references for return values.
                        if isinstance(ssa_types.get(term.value), ReferenceType) and isinstance(val_ll.type, ir.PointerType):
                            val_ll = builder.load(val_ll)
                        pair_ty = llvm_ret_with_error(f.return_type)
                        pair_ptr = builder.alloca(pair_ty, name=f"{f.name}_ret_pair")
                        val_ptr = builder.gep(pair_ptr, [I32_TY(0), I32_TY(0)], inbounds=True)
                        builder.store(val_ll, val_ptr)
                        err_ptr = builder.gep(pair_ptr, [I32_TY(0), I32_TY(1)], inbounds=True)
                        builder.store(err_val, err_ptr)
                        pair_loaded = builder.load(pair_ptr)
                        builder.ret(pair_loaded)
                else:
                    if isinstance(_llvm_type_with_structs(f.return_type), ir.VoidType):
                        builder.ret_void()
                    else:
                        if term.value is None:
                            raise RuntimeError(f"missing return value for non-void function {f.name}")
                        if term.value not in values:
                            raise RuntimeError(f"return value {term.value} undefined")
                        ret_val = values[term.value]
                        if isinstance(ssa_types.get(term.value), ReferenceType) and isinstance(ret_val.type, ir.PointerType):
                            ret_val = builder.load(ret_val)
                        ret_ll_ty = _llvm_type_with_structs(f.return_type)
                        if isinstance(ret_ll_ty, ir.LiteralStructType) and isinstance(ret_val.type, ir.PointerType):
                            if ret_val.type.pointee == ret_ll_ty:
                                ret_val = builder.load(ret_val)
                        builder.ret(ret_val)
            elif isinstance(term, mir.Call):
                if not (term.normal and term.error):
                    raise RuntimeError("call terminator without normal/error edges not supported")
                callee = fn_map.get(term.callee)
                if callee is None:
                    raise RuntimeError(f"unknown callee {term.callee}")
                if callee.name not in can_error_funcs:
                    raise RuntimeError(f"call with edges to non-error function {callee.name}")
                call_args = [values[a] for a in term.args]
                adj_args = []
                for idx, arg in enumerate(call_args):
                    expected = callee.function_type.args[idx]
                    if isinstance(expected, ir.LiteralStructType) and isinstance(arg.type, ir.PointerType):
                        if arg.type.pointee == expected:
                            arg = builder.load(arg)
                    elif isinstance(expected, ir.PointerType):
                        if isinstance(arg.type, ir.LiteralStructType):
                            slot = builder.alloca(expected.pointee, name=f"{term.dest or 'call_pair'}.arg{idx}")
                            builder.store(arg, slot)
                            arg = slot
                        elif not isinstance(arg.type, ir.PointerType):
                            slot = builder.alloca(expected.pointee, name=f"{term.dest or 'call_pair'}.arg{idx}")
                            if expected.pointee == arg.type:
                                builder.store(arg, slot)
                            else:
                                cast_slot = builder.bitcast(slot, arg.type.as_pointer()) if hasattr(arg.type, "as_pointer") else slot
                                builder.store(arg, cast_slot)
                            arg = slot
                    adj_args.append(arg)
                call_pair = builder.call(callee, adj_args, name=term.dest or "call_pair")
                ret_ty = callee.function_type.return_type
                if isinstance(ret_ty, ir.LiteralStructType):
                    val_component = builder.extract_value(call_pair, 0)
                    err_component = builder.extract_value(call_pair, 1)
                    if term.dest is not None:
                        values[term.dest] = val_component
                        ssa_types[term.dest] = term.ret_type
                    if term.err_dest is not None:
                        values[term.err_dest] = err_component
                        ssa_types[term.err_dest] = ERROR
                else:
                    # void-with-error* case
                    val_component = None
                    err_component = call_pair
                    if term.err_dest is not None:
                        values[term.err_dest] = err_component
                        ssa_types[term.err_dest] = ERROR
                cond_val = builder.icmp_unsigned(
                    "!=", err_component, ir.Constant(ERROR_PTR_TY, None), name=f"errchk_{bname}"
                )
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
                    incoming_val = values[arg]
                    phi = phis[(norm_tgt, param.name)]
                    expected_ty = phi.type.pointee if isinstance(phi.type, ir.PointerType) else phi.type
                    if isinstance(expected_ty, ir.LiteralStructType) and isinstance(incoming_val.type, ir.PointerType):
                        incoming_val = builder.load(incoming_val)
                    phis[(norm_tgt, param.name)].add_incoming(incoming_val, blocks_map[(f.name, bname)])
                for param, arg in zip(err_block.params, term.error.args):
                    incoming_val = values[arg]
                    phi = phis[(err_tgt, param.name)]
                    expected_ty = phi.type.pointee if isinstance(phi.type, ir.PointerType) else phi.type
                    if isinstance(expected_ty, ir.LiteralStructType) and isinstance(incoming_val.type, ir.PointerType):
                        incoming_val = builder.load(incoming_val)
                    phis[(err_tgt, param.name)].add_incoming(incoming_val, blocks_map[(f.name, bname)])
                builder.cbranch(cond_val, blocks_map[(f.name, err_tgt)], blocks_map[(f.name, norm_tgt)])
            elif isinstance(term, mir.Br):
                tgt = term.target.target
                tblock = f.blocks[tgt]
                if len(term.target.args) != len(tblock.params):
                    raise RuntimeError(f"edge to {tgt} has arity {len(term.target.args)} expected {len(tblock.params)}")
                for param, arg in zip(tblock.params, term.target.args):
                    incoming_val = values[arg]
                    phi = phis[(tgt, param.name)]
                    expected_ty = phi.type.pointee if isinstance(phi.type, ir.PointerType) else phi.type
                    if isinstance(expected_ty, ir.LiteralStructType) and isinstance(incoming_val.type, ir.PointerType):
                        incoming_val = builder.load(incoming_val)
                    phis[(tgt, param.name)].add_incoming(incoming_val, blocks_map[(f.name, bname)])
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
                        incoming_val = values[arg]
                        phi = phis[(tgt, param.name)]
                        expected_ty = phi.type.pointee if isinstance(phi.type, ir.PointerType) else phi.type
                        # Only load pointers for value params; reference params expect the pointer.
                        if not isinstance(param.type, ReferenceType) and isinstance(expected_ty, ir.LiteralStructType) and isinstance(incoming_val.type, ir.PointerType):
                            incoming_val = builder.load(incoming_val)
                        phis[(tgt, param.name)].add_incoming(incoming_val, blocks_map[(f.name, bname)])
                builder.cbranch(cond_val, blocks_map[(f.name, term.then.target)], blocks_map[(f.name, term.els.target)])
            elif isinstance(term, mir.Throw):
                if f.name not in can_error_funcs:
                    raise RuntimeError(f"throw in non-error function {f.name} not supported")
                if term.error not in values:
                    raise RuntimeError(f"throw error value {term.error} undefined in {f.name}")
                err_val = values[term.error]
                ret_ll_ty = _llvm_type_with_structs(f.return_type)
                if isinstance(ret_ll_ty, ir.VoidType):
                    builder.ret(err_val)
                else:
                    # Value is ignored on error; return a zeroed pair with the error.
                    pair_ty = llvm_ret_with_error(f.return_type)
                    if isinstance(ret_ll_ty, ir.IntType):
                        val_zero = ret_ll_ty(0)
                    elif isinstance(ret_ll_ty, ir.PointerType):
                        val_zero = ir.Constant(ret_ll_ty, None)
                    elif isinstance(ret_ll_ty, ir.LiteralStructType):
                        elems = []
                        for t in ret_ll_ty.elements:
                            if isinstance(t, ir.IntType):
                                elems.append(t(0))
                            elif isinstance(t, ir.PointerType):
                                elems.append(ir.Constant(t, None))
                            else:
                                elems.append(ir.Constant(t, None))
                        val_zero = ir.Constant.literal_struct(elems)
                    elif isinstance(ret_ll_ty, ir.ArrayType):
                        val_zero = ir.Constant(ret_ll_ty, None)
                    else:
                        val_zero = ir.Constant(ret_ll_ty, None)
                    tmp_pair = ir.Constant(pair_ty, None)
                    tmp_pair = builder.insert_value(tmp_pair, val_zero, 0)
                    tmp_pair = builder.insert_value(tmp_pair, err_val, 1)
                    builder.ret(tmp_pair)
            elif term is None:
                builder.unreachable()
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
