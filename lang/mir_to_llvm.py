from __future__ import annotations

from llvmlite import binding as llvm  # type: ignore
from llvmlite import ir  # type: ignore

from . import mir
from .types import BOOL, ERROR, F64, I64, STR, Type


def lower_function(fn: mir.Function) -> tuple[str, bytes]:
    """
    MIR → LLVM lowering (supports branches/phi via block params; no raise/try yet).
    """
    llvm.initialize()
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()
    target = llvm.Target.from_default_triple()
    tm = target.create_target_machine()

    llvm_module = ir.Module(name=f"{fn.name}_module")
    llvm_module.triple = llvm.get_default_triple()
    llvm_module.data_layout = tm.target_data

    param_types = [_llvm_type(p.type) for p in fn.params]
    func_ty = ir.FunctionType(_llvm_type(fn.return_type), param_types)
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

    envs: dict[str, dict[str, ir.Value]] = {}
    worklist = [fn.entry]
    visited = set()

    while worklist:
        bname = worklist.pop()
        if bname in visited:
            continue
        visited.add(bname)
        block = fn.blocks[bname]
        builder = ir.IRBuilder(llvm_blocks[bname])
        env: dict[str, ir.Value] = {}
        # params
        if bname == entry_name:
            for p, arg in zip(fn.params, llvm_fn.args):
                env[p.name] = arg
        else:
            for param in block.params:
                env[param.name] = phi_nodes[bname][param.name]
        for instr in block.instructions:
            if isinstance(instr, mir.Const):
                env[instr.dest] = _const(builder, instr.type, instr.value)
            elif isinstance(instr, mir.Move):
                env[instr.dest] = env[instr.source]
            elif isinstance(instr, mir.Copy):
                env[instr.dest] = env[instr.source]
            elif isinstance(instr, mir.Binary):
                env[instr.dest] = _lower_binary(builder, instr, env)
            else:
                raise NotImplementedError(f"unsupported instruction: {instr}")
        term = block.terminator
        if isinstance(term, mir.Br):
            _add_phi_incoming(phi_nodes, term.target, env, llvm_blocks[bname])
            builder.branch(llvm_blocks[term.target.target])
            worklist.append(term.target.target)
        elif isinstance(term, mir.CondBr):
            _add_phi_incoming(phi_nodes, term.then, env, llvm_blocks[bname])
            _add_phi_incoming(phi_nodes, term.els, env, llvm_blocks[bname])
            builder.cbranch(env[term.cond], llvm_blocks[term.then.target], llvm_blocks[term.els.target])
            worklist.extend([term.then.target, term.els.target])
        elif isinstance(term, mir.Return):
            retval = env[term.value] if term.value else None
            builder.ret(retval)
        elif isinstance(term, mir.Raise):
            raise NotImplementedError("raise not supported in MIR→LLVM yet")
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
        data = bytearray(str(val).encode("utf-8"))
        data.append(0)
        gv = ir.GlobalVariable(builder.module, ir.ArrayType(ir.IntType(8), len(data)), name=f".str{len(data)}")
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
    raise NotImplementedError(f"binary op {op}")


def _add_phi_incoming(phi_nodes: dict[str, dict[str, ir.PhiInstr]], edge: mir.Edge, env: dict[str, ir.Value], pred_block: ir.Block) -> None:
    if not edge.args:
        return
    target = edge.target
    for arg_val, (param_name, phi) in zip(edge.args, phi_nodes[target].items()):
        phi.add_incoming(env[arg_val], pred_block)
