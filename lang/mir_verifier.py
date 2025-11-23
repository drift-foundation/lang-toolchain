from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from . import mir
from .types import ERROR


@dataclass
class VerificationError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


class State:
    def __init__(self) -> None:
        self.defined: Set[str] = set()
        self.moved: Set[str] = set()
        self.dropped: Set[str] = set()

    def define(self, name: str) -> None:
        self.defined.add(name)

    def is_defined(self, name: str) -> bool:
        return name in self.defined

    def move(self, name: str) -> None:
        self.moved.add(name)

    def is_moved(self, name: str) -> bool:
        return name in self.moved

    def drop(self, name: str) -> None:
        self.dropped.add(name)

    def is_dropped(self, name: str) -> bool:
        return name in self.dropped


def verify_program(program: mir.Program) -> None:
    for fn in program.functions.values():
        verify_function(fn)


def verify_function(fn: mir.Function) -> None:
    if fn.entry not in fn.blocks:
        raise VerificationError(f"{fn.name}: entry block '{fn.entry}' missing")
    for name, block in fn.blocks.items():
        if block.terminator is None:
            raise VerificationError(f"{fn.name}:{name}: missing terminator")
    for block in fn.blocks.values():
        _verify_block(fn, block)


def _verify_block(fn: mir.Function, block: mir.BasicBlock) -> None:
    state = State()
    for param in block.params:
        state.define(param.name)
    for instr in block.instructions:
        if isinstance(instr, mir.Const):
            _ensure_not_defined(state, instr.dest, block, "const")
            state.define(instr.dest)
        elif isinstance(instr, mir.Move):
            _ensure_defined(state, instr.source, block, "move")
            _ensure_not_moved_or_dropped(state, instr.source, block, "move")
            _ensure_not_defined(state, instr.dest, block, "move")
            state.define(instr.dest)
            state.move(instr.source)
        elif isinstance(instr, mir.Copy):
            _ensure_defined(state, instr.source, block, "copy")
            _ensure_not_moved_or_dropped(state, instr.source, block, "copy")
            _ensure_not_defined(state, instr.dest, block, "copy")
            state.define(instr.dest)
        elif isinstance(instr, mir.Call):
            for arg in instr.args:
                _ensure_defined(state, arg, block, "call")
                _ensure_not_moved_or_dropped(state, arg, block, "call")
            _ensure_not_defined(state, instr.dest, block, "call")
            state.define(instr.dest)
            _ensure_edge(fn, instr.normal, block, error=False)
            _ensure_edge(fn, instr.error, block, error=True)
        elif isinstance(instr, mir.StructInit):
            for arg in instr.args:
                _ensure_defined(state, arg, block, "struct_init")
                _ensure_not_moved_or_dropped(state, arg, block, "struct_init")
            _ensure_not_defined(state, instr.dest, block, "struct_init")
            state.define(instr.dest)
        elif isinstance(instr, mir.FieldGet):
            _ensure_defined(state, instr.base, block, "field_get")
            _ensure_not_moved_or_dropped(state, instr.base, block, "field_get")
            _ensure_not_defined(state, instr.dest, block, "field_get")
            state.define(instr.dest)
        elif isinstance(instr, mir.ArrayInit):
            for elem in instr.elements:
                _ensure_defined(state, elem, block, "array_init")
                _ensure_not_moved_or_dropped(state, elem, block, "array_init")
            _ensure_not_defined(state, instr.dest, block, "array_init")
            state.define(instr.dest)
        elif isinstance(instr, mir.ArrayGet):
            _ensure_defined(state, instr.base, block, "array_get")
            _ensure_not_moved_or_dropped(state, instr.base, block, "array_get")
            _ensure_defined(state, instr.index, block, "array_get")
            _ensure_not_moved_or_dropped(state, instr.index, block, "array_get")
            _ensure_not_defined(state, instr.dest, block, "array_get")
            state.define(instr.dest)
        elif isinstance(instr, mir.ArraySet):
            _ensure_defined(state, instr.base, block, "array_set")
            _ensure_not_moved_or_dropped(state, instr.base, block, "array_set")
            _ensure_defined(state, instr.index, block, "array_set")
            _ensure_not_moved_or_dropped(state, instr.index, block, "array_set")
            _ensure_defined(state, instr.value, block, "array_set")
            _ensure_not_moved_or_dropped(state, instr.value, block, "array_set")
        elif isinstance(instr, mir.Unary):
            _ensure_defined(state, instr.operand, block, "unary")
            _ensure_not_moved_or_dropped(state, instr.operand, block, "unary")
            _ensure_not_defined(state, instr.dest, block, "unary")
            state.define(instr.dest)
        elif isinstance(instr, mir.Binary):
            _ensure_defined(state, instr.left, block, "binary")
            _ensure_not_moved_or_dropped(state, instr.left, block, "binary")
            _ensure_defined(state, instr.right, block, "binary")
            _ensure_not_moved_or_dropped(state, instr.right, block, "binary")
            _ensure_not_defined(state, instr.dest, block, "binary")
            state.define(instr.dest)
        elif isinstance(instr, mir.Drop):
            _ensure_defined(state, instr.value, block, "drop")
            _ensure_not_moved_or_dropped(state, instr.value, block, "drop")
            state.drop(instr.value)
        else:
            raise VerificationError(f"{fn.name}:{block.name}: unsupported instruction {instr}")

    term = block.terminator
    if isinstance(term, mir.Br):
        _ensure_edge(fn, term.target, block)
    elif isinstance(term, mir.CondBr):
        _ensure_defined(state, term.cond, block, "condbr", term.loc)
        _ensure_not_moved_or_dropped(state, term.cond, block, "condbr", term.loc)
        _ensure_edge(fn, term.then, block)
        _ensure_edge(fn, term.els, block)
    elif isinstance(term, mir.Return):
        if term.value is not None:
            _ensure_defined(state, term.value, block, "return", term.loc)
            _ensure_not_moved_or_dropped(state, term.value, block, "return", term.loc)
    elif isinstance(term, mir.Raise):
        _ensure_defined(state, term.error, block, "raise", term.loc)
        _ensure_not_moved_or_dropped(state, term.error, block, "raise", term.loc)
    else:
        raise VerificationError(f"{fn.name}:{block.name}: missing or unsupported terminator")


def _loc_for(block: mir.BasicBlock, loc: Optional[mir.Location]) -> str:
    src = loc.file if loc else "<unknown>"
    line = loc.line if loc else 0
    return f"{src}:{line}"


def _ensure_defined(state: State, name: str, block: mir.BasicBlock, ctx: str, loc: Optional[mir.Location] = None) -> None:
    if not state.is_defined(name):
        raise VerificationError(f"{block.name}: {ctx}: '{name}' is undefined at {_loc_for(block, loc)}")


def _ensure_not_defined(state: State, name: str, block: mir.BasicBlock, ctx: str, loc: Optional[mir.Location] = None) -> None:
    if state.is_defined(name):
        raise VerificationError(f"{block.name}: {ctx}: '{name}' already defined at {_loc_for(block, loc)}")


def _ensure_not_moved_or_dropped(state: State, name: str, block: mir.BasicBlock, ctx: str, loc: Optional[mir.Location] = None) -> None:
    if state.is_moved(name):
        raise VerificationError(f"{block.name}: {ctx}: '{name}' was moved at {_loc_for(block, loc)}")
    if state.is_dropped(name):
        raise VerificationError(f"{block.name}: {ctx}: '{name}' was dropped at {_loc_for(block, loc)}")


def _ensure_edge(fn: mir.Function, edge: mir.Edge, block: mir.BasicBlock, error: bool = False) -> None:
    if edge.target not in fn.blocks:
        raise VerificationError(f"{fn.name}:{block.name}: edge to unknown block '{edge.target}'")
    dest_params = fn.blocks[edge.target].params
    if len(edge.args) != len(dest_params):
        raise VerificationError(
            f"{fn.name}:{block.name}: edge to '{edge.target}' expects {len(dest_params)} args, got {len(edge.args)}"
        )
    # Type checks are deferred; verifier can be extended to check types when available.
    if error:
        # Error edges must carry an Error value in the first arg if present.
        if edge.args and dest_params:
            if dest_params[0].type != ERROR:
                raise VerificationError(
                    f"{fn.name}:{block.name}: error edge '{edge.target}' first param must be Error"
                )
