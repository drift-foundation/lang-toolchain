from __future__ import annotations

from typing import Iterable

from . import mir


def format_edge(edge: mir.Edge) -> str:
    args = ", ".join(edge.args)
    return f"{edge.target}({args})" if args else edge.target


def format_term(term: mir.Terminator) -> str:
    if isinstance(term, mir.Br):
        return f"  br {format_edge(term.target)}"
    if isinstance(term, mir.CondBr):
        return f"  condbr {term.cond}, then {format_edge(term.then)}, else {format_edge(term.els)}"
    if isinstance(term, mir.Return):
        if term.value is None:
            return "  return"
        return f"  return {term.value}"
    if isinstance(term, mir.Raise):
        return f"  raise {term.error}"
    return "  <invalid terminator>"


def format_instr(instr: mir.Instruction) -> str:
    if isinstance(instr, mir.Const):
        return f"  {instr.dest} = const {instr.type} {instr.value}"
    if isinstance(instr, mir.Move):
        return f"  {instr.dest} = move {instr.source}"
    if isinstance(instr, mir.Copy):
        return f"  {instr.dest} = copy {instr.source}"
    if isinstance(instr, (mir.Call, mir.CallWithCtx)):
        args = ", ".join(instr.args)
        suffix = ""
        if instr.normal or instr.error:
            n = format_edge(instr.normal) if instr.normal else "<none>"
            e = format_edge(instr.error) if instr.error else "<none>"
            suffix = f" normal {n} error {e}"
        return f"  {instr.dest} = call {instr.callee}({args}){suffix}"
    if isinstance(instr, mir.StructInit):
        args = ", ".join(instr.args)
        return f"  {instr.dest} = struct_init {instr.type}({args})"
    if isinstance(instr, mir.FieldGet):
        return f"  {instr.dest} = field_get {instr.base}.{instr.field}"
    if isinstance(instr, mir.ArrayInit):
        elems = ", ".join(instr.elements)
        return f"  {instr.dest} = array_init [{elems}]"
    if isinstance(instr, mir.ArrayGet):
        return f"  {instr.dest} = array_get {instr.base}[{instr.index}]"
    if isinstance(instr, mir.ArraySet):
        return f"  array_set {instr.base}[{instr.index}] = {instr.value}"
    if isinstance(instr, mir.Unary):
        return f"  {instr.dest} = {instr.op} {instr.operand}"
    if isinstance(instr, mir.Binary):
        return f"  {instr.dest} = {instr.left} {instr.op} {instr.right}"
    if isinstance(instr, mir.ConsoleWrite):
        return f"  console_write {instr.value}"
    if isinstance(instr, mir.ConsoleWriteln):
        return f"  console_writeln {instr.value}"
    if isinstance(instr, mir.Drop):
        return f"  drop {instr.value}"
    return "  <invalid instr>"


def format_block(block: mir.BasicBlock) -> str:
    params = ", ".join(f"{p.name}: {p.type}" for p in block.params)
    lines = [f"{block.name}({params}):" if params else f"{block.name}:"]
    for instr in block.instructions:
        lines.append(format_instr(instr))
    if block.terminator:
        lines.append(format_term(block.terminator))
    return "\n".join(lines)


def format_function(fn: mir.Function) -> str:
    params = ", ".join(f"{p.name}: {p.type}" for p in fn.params)
    header = f"fn {fn.name}({params}) -> {fn.return_type} {{ entry = {fn.entry} }}"
    blocks = "\n".join(format_block(fn.blocks[name]) for name in sorted(fn.blocks.keys()))
    return f"{header}\n{blocks}"


def format_program(prog: mir.Program) -> str:
    return "\n\n".join(format_function(fn) for fn in prog.functions.values())
