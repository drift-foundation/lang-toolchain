"""SSA-to-LLVM codegen (minimal)."""

from __future__ import annotations

from pathlib import Path

from llvmlite import ir, binding as llvm  # type: ignore

from . import mir
from .types import I64, Type


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


def _llvm_int(ty: Type) -> ir.IntType:
    # Treat Int/Int64 as i32 for now.
    if ty.name in {"Int", "Int64"}:
        return ir.IntType(32)
    raise NotImplementedError(f"unsupported type {ty}")


def emit_simple_main_object(fn: mir.Function, out_path: Path) -> None:
    """Lower a small SSA function with ints + branches into LLVM main.

    Supported subset:
    - integer const/move/binary ops
    - multiple blocks with block params
    - Br / CondBr / Return terminators
    - no calls/arrays/structs yet
    """
    llvm.initialize()
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()

    mod = ir.Module(name="ssa_main")
    ret_ir_ty = _llvm_int(fn.return_type)
    llvm_main = ir.Function(mod, ir.FunctionType(ret_ir_ty, []), name="main")

    # Pre-create LLVM basic blocks for each SSA block.
    llvm_blocks: dict[str, ir.Block] = {}
    for name in fn.blocks:
        llvm_blocks[name] = llvm_main.append_basic_block(name)

    # PHIs for block params.
    phis: dict[tuple[str, str], ir.Instruction] = {}
    values: dict[str, ir.Value] = {}

    for bname, block in fn.blocks.items():
        builder = ir.IRBuilder(llvm_blocks[bname])
        for param in block.params:
            phi = builder.phi(_llvm_int(param.type), name=param.name)
            phis[(bname, param.name)] = phi
            values[param.name] = phi

    # Emit instructions per block.
    for bname, block in fn.blocks.items():
        builder = ir.IRBuilder(llvm_blocks[bname])
        for instr in block.instructions:
            if isinstance(instr, mir.Const):
                if not isinstance(instr.value, int):
                    raise RuntimeError("simple backend supports int const only")
                ir_ty = _llvm_int(instr.type)
                values[instr.dest] = ir_ty(instr.value)
            elif isinstance(instr, mir.Move):
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
                elif instr.op in {"==", "!="}:
                    cmp = builder.icmp_unsigned("==", lhs, rhs, name=f"cmp_{instr.dest}")
                    if instr.op == "!=":
                        cmp = builder.not_(cmp, name=instr.dest)
                    values[instr.dest] = cmp
                else:
                    raise RuntimeError(f"unsupported binary op {instr.op}")
            else:
                raise RuntimeError(f"unsupported instruction {instr}")

        # Terminators
        term = block.terminator
        if isinstance(term, mir.Return):
            if term.value is None:
                builder.ret(ret_ir_ty(0))
            else:
                builder.ret(values[term.value])
        elif isinstance(term, mir.Br):
            tgt = term.target.target
            tblock = fn.blocks[tgt]
            for param, arg in zip(tblock.params, term.target.args):
                phis[(tgt, param.name)].add_incoming(values[arg], llvm_blocks[bname])
            builder.branch(llvm_blocks[tgt])
        elif isinstance(term, mir.CondBr):
            cond = values[term.cond]
            for edge in (term.then, term.els):
                tgt = edge.target
                tblock = fn.blocks[tgt]
                for param, arg in zip(tblock.params, edge.args):
                    phis[(tgt, param.name)].add_incoming(values[arg], llvm_blocks[bname])
            builder.cbranch(cond, llvm_blocks[term.then.target], llvm_blocks[term.els.target])
        else:
            raise RuntimeError(f"unsupported terminator {term}")

    target = llvm.Target.from_default_triple()
    tm = target.create_target_machine()
    obj = tm.emit_object(llvm.parse_assembly(str(mod)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(obj)
