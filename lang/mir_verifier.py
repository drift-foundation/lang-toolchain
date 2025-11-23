from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from . import mir
from .types import ERROR, array_of, Type


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
        self.types: Dict[str, Type] = {}

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

    def set_type(self, name: str, typ: Type) -> None:
        self.types[name] = typ

    def get_type(self, name: str) -> Optional[Type]:
        return self.types.get(name)


def verify_program(program: mir.Program) -> None:
    for fn in program.functions.values():
        verify_function(fn, program)


def verify_function(fn: mir.Function, program: mir.Program | None = None) -> None:
    if fn.entry not in fn.blocks:
        raise VerificationError(f"{fn.name}: entry block '{fn.entry}' missing")
    for name, block in fn.blocks.items():
        if block.terminator is None:
            raise VerificationError(f"{fn.name}:{name}: missing terminator")
    defs, types = _compute_defs_and_types(fn, program)
    incoming = _compute_incoming_args(fn, defs, types)
    _verify_cfg(fn, defs, types, incoming)
    for block in fn.blocks.values():
        _verify_block(fn, block, program, incoming)


def _compute_defs_and_types(fn: mir.Function, program: mir.Program | None) -> tuple[Dict[str, Set[str]], Dict[str, Dict[str, Type]]]:
    defs: Dict[str, Set[str]] = {}
    types: Dict[str, Dict[str, Type]] = {}
    for block in fn.blocks.values():
        defined: Set[str] = set()
        type_map: Dict[str, Type] = {}
        for param in block.params:
            defined.add(param.name)
            type_map[param.name] = param.type
        for instr in block.instructions:
            if isinstance(instr, mir.Const):
                defined.add(instr.dest)
                type_map[instr.dest] = instr.type
            elif isinstance(instr, (mir.Move, mir.Copy)):
                defined.add(instr.dest)
                src_ty = type_map.get(instr.source)
                if src_ty:
                    type_map[instr.dest] = src_ty
            elif isinstance(instr, mir.Call):
                defined.add(instr.dest)
                if program and instr.callee in program.functions:
                    type_map[instr.dest] = program.functions[instr.callee].return_type
            elif isinstance(instr, mir.StructInit):
                defined.add(instr.dest)
                type_map[instr.dest] = instr.type
            elif isinstance(instr, mir.FieldGet):
                defined.add(instr.dest)
            elif isinstance(instr, mir.ArrayInit):
                defined.add(instr.dest)
                type_map[instr.dest] = array_of(instr.element_type)
            elif isinstance(instr, mir.ArrayGet):
                defined.add(instr.dest)
            elif isinstance(instr, mir.Unary):
                defined.add(instr.dest)
            elif isinstance(instr, mir.Binary):
                defined.add(instr.dest)
            # ArraySet/Drop produce no new defs
        defs[block.name] = defined
        types[block.name] = type_map
    return defs, types


def _compute_incoming_args(
    fn: mir.Function, defs: Dict[str, Set[str]], types: Dict[str, Dict[str, Type]]
) -> Dict[str, List[tuple[List[str], List[Type]]]]:
    incoming: Dict[str, List[tuple[List[str], List[Type]]]] = {name: [] for name in fn.blocks}
    for source_name, block in fn.blocks.items():
        term = block.terminator
        if isinstance(term, mir.Br):
            args = term.target.args
            arg_types = [types.get(source_name, {}).get(arg) for arg in args]
            incoming[term.target.target].append((args, arg_types))
        elif isinstance(term, mir.CondBr):
            args_then = term.then.args
            arg_types_then = [types.get(source_name, {}).get(arg) for arg in args_then]
            incoming[term.then.target].append((args_then, arg_types_then))
            args_else = term.els.args
            arg_types_else = [types.get(source_name, {}).get(arg) for arg in args_else]
            incoming[term.els.target].append((args_else, arg_types_else))
    return incoming


def _verify_cfg(
    fn: mir.Function,
    defs: Dict[str, Set[str]],
    types: Dict[str, Dict[str, Type]],
    incoming: Dict[str, List[tuple[List[str], List[Type]]]],
) -> None:
    blocks = fn.blocks
    entry = fn.entry
    seen: Set[str] = set()

    def walk(name: str) -> None:
        if name in seen:
            return
        if name not in blocks:
            raise VerificationError(f"{fn.name}: edge to unknown block '{name}'")
        seen.add(name)
        term = blocks[name].terminator
        if isinstance(term, mir.Br):
            _ensure_edge(fn, term.target, blocks[name], defs, types, name)
            walk(term.target.target)
        elif isinstance(term, mir.CondBr):
            _ensure_edge(fn, term.then, blocks[name], defs, types, name)
            _ensure_edge(fn, term.els, blocks[name], defs, types, name)
            walk(term.then.target)
            walk(term.els.target)
        elif isinstance(term, (mir.Return, mir.Raise)):
            return
        else:
            raise VerificationError(f"{fn.name}:{name}: unsupported terminator")

    walk(entry)
    if len(seen) != len(blocks):
        missing = set(blocks.keys()) - seen
        raise VerificationError(f"{fn.name}: unreachable blocks: {', '.join(sorted(missing))}")

    # Validate incoming args vs block params
    for block_name, block in blocks.items():
        param_types = [p.type for p in block.params]
        for args, arg_types in incoming.get(block_name, []):
            if len(args) != len(param_types):
                raise VerificationError(
                    f"{fn.name}:{block_name}: predecessor passed {len(args)} args, expected {len(param_types)}"
                )
            for idx, (a_ty, p_ty) in enumerate(zip(arg_types, param_types)):
                if a_ty is not None and a_ty != p_ty:
                    raise VerificationError(
                        f"{fn.name}:{block_name}: arg {idx} type mismatch from predecessor"
                    )
        # Ensure block params are defined before use in the block
        for param in block.params:
            if param.name not in defs.get(block_name, set()):
                raise VerificationError(f"{fn.name}:{block_name}: param '{param.name}' not available at block entry")
    for block in fn.blocks.values():
        if isinstance(block.terminator, mir.Br):
            _ensure_edge(fn, block.terminator.target, block)
        elif isinstance(block.terminator, mir.CondBr):
            _ensure_edge(fn, block.terminator.then, block)
            _ensure_edge(fn, block.terminator.els, block)


def _verify_block(fn: mir.Function, block: mir.BasicBlock, program: mir.Program | None = None, incoming: Dict[str, List[tuple[List[str], List[Type]]]] | None = None) -> None:
    state = State()
    for param in block.params:
        state.define(param.name)
        state.set_type(param.name, param.type)
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
            if instr.normal:
                _ensure_edge(fn, instr.normal, block, {}, {}, block.name, error=False)
            if instr.error:
                _ensure_edge(fn, instr.error, block, {}, {}, block.name, error=True)
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
        _ensure_edge(fn, term.target, block, {}, {}, block.name)
    elif isinstance(term, mir.CondBr):
        _ensure_defined(state, term.cond, block, "condbr", term.loc)
        _ensure_not_moved_or_dropped(state, term.cond, block, "condbr", term.loc)
        _ensure_edge(fn, term.then, block, {}, {}, block.name)
        _ensure_edge(fn, term.els, block, {}, {}, block.name)
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


def _ensure_edge(
    fn: mir.Function,
    edge: mir.Edge,
    block: mir.BasicBlock,
    defs: Dict[str, Set[str]],
    types: Dict[str, Dict[str, Type]],
    source_block: str,
    error: bool = False,
) -> None:
    if edge.target not in fn.blocks:
        raise VerificationError(f"{fn.name}:{block.name}: edge to unknown block '{edge.target}'")
    dest_params = fn.blocks[edge.target].params
    if len(edge.args) != len(dest_params):
        raise VerificationError(
            f"{fn.name}:{block.name}: edge to '{edge.target}' expects {len(dest_params)} args, got {len(edge.args)}"
        )
    # Ensure args are defined in the source block
    defined = defs.get(source_block, set())
    for arg in edge.args:
        if arg not in defined:
            raise VerificationError(f"{fn.name}:{block.name}: edge to '{edge.target}' references undefined '{arg}'")
    # Type checks when available
    src_types = types.get(source_block, {})
    for arg, param in zip(edge.args, dest_params):
        arg_ty = src_types.get(arg)
        if arg_ty and arg_ty != param.type:
            raise VerificationError(
                f"{fn.name}:{block.name}: edge to '{edge.target}' param '{param.name}' type mismatch"
            )
    if error:
        # Error edges must carry an Error value in the first arg if present.
        if edge.args and dest_params:
            if dest_params[0].type != ERROR:
                raise VerificationError(
                    f"{fn.name}:{block.name}: error edge '{edge.target}' first param must be Error"
                )
