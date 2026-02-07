from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

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


def _call_terminator(block: mir.BasicBlock) -> Optional[mir.Call]:
    if block.instructions:
        last = block.instructions[-1]
        if isinstance(last, (mir.Call, mir.CallWithCtx)) and (getattr(last, "normal", None) or getattr(last, "error", None)):
            return last  # type: ignore[return-value]
    return None


def verify_program(program: mir.Program) -> None:
    for fn in program.functions.values():
        verify_function(fn, program)


def verify_function(fn: mir.Function, program: mir.Program | None = None) -> None:
    if fn.entry not in fn.blocks:
        raise VerificationError(f"{fn.name}: entry block '{fn.entry}' missing")
    for name, block in fn.blocks.items():
        if block.terminator is None and not _call_terminator(block):
            raise VerificationError(f"{fn.name}:{name}: missing terminator")
        if isinstance(block.terminator, mir.Raise) and fn.return_type != ERROR:
            # Functions that can raise should use pair return; warning here until ABI is uniform.
            pass
    def_blocks: Dict[str, Set[str]] = {}
    for name, block in fn.blocks.items():
        for p in block.params:
            def_blocks.setdefault(p.name, set()).add(name)
        for instr in block.instructions:
            if isinstance(instr, (mir.Const, mir.Move, mir.Copy, mir.Call, mir.CallWithCtx, mir.StructInit, mir.FieldGet, mir.ArrayInit, mir.ArrayLiteral, mir.ArrayGet, mir.Unary, mir.Binary, mir.ConsoleWrite, mir.ConsoleWriteln)):
                def_blocks.setdefault(getattr(instr, "dest", None), set()).add(name) if getattr(instr, "dest", None) else None
    in_state, out_state = _dataflow_defs_types(fn, program)
    incoming = _compute_incoming_args(fn, out_state)
    _verify_cfg(fn, out_state, incoming)
    dominators = _compute_dominators(fn)
    for block in fn.blocks.values():
        _verify_block(fn, block, program, incoming, in_state, out_state, dominators, def_blocks)


def _compute_incoming_args(
    fn: mir.Function, out_state: Dict[str, Tuple[Set[str], Dict[str, Type]]]
) -> Dict[str, List[tuple[List[str], List[Type]]]]:
    incoming: Dict[str, List[tuple[List[str], List[Type]]]] = {name: [] for name in fn.blocks}
    for source_name, block in fn.blocks.items():
        term = block.terminator
        if isinstance(term, mir.Br):
            args = term.target.args
            arg_types = [out_state.get(source_name, (set(), {}))[1].get(arg) for arg in args]
            if term.target.target in incoming:
                incoming[term.target.target].append((args, arg_types))
        elif isinstance(term, mir.CondBr):
            args_then = term.then.args
            arg_types_then = [out_state.get(source_name, (set(), {}))[1].get(arg) for arg in args_then]
            if term.then.target in incoming:
                incoming[term.then.target].append((args_then, arg_types_then))
            args_else = term.els.args
            arg_types_else = [out_state.get(source_name, (set(), {}))[1].get(arg) for arg in args_else]
            if term.els.target in incoming:
                incoming[term.els.target].append((args_else, arg_types_else))
        # call edges as terminators
        for instr in block.instructions:
            if isinstance(instr, mir.Call) and (instr.normal or instr.error):
                if instr.normal:
                    args = instr.normal.args
                    arg_types = [out_state.get(source_name, (set(), {}))[1].get(arg) for arg in args]
                    if instr.normal.target in incoming:
                        incoming[instr.normal.target].append((args, arg_types))
                if instr.error:
                    args = instr.error.args
                    arg_types = [out_state.get(source_name, (set(), {}))[1].get(arg) for arg in args]
                    if instr.error.target in incoming:
                        incoming[instr.error.target].append((args, arg_types))
    return incoming


def _dataflow_defs_types(
    fn: mir.Function,
    program: mir.Program | None = None,
) -> tuple[Dict[str, Tuple[Set[str], Dict[str, Type]]], Dict[str, Tuple[Set[str], Dict[str, Type]]]]:
    in_state: Dict[str, Tuple[Set[str], Dict[str, Type]]] = {}
    out_state: Dict[str, Tuple[Set[str], Dict[str, Type]]] = {}
    worklist: List[str] = list(fn.blocks.keys())
    for name, block in fn.blocks.items():
        in_state[name] = (set(p.name for p in block.params), {p.name: p.type for p in block.params})
        out_state[name] = (set(), {})

    changed = True
    while changed:
        changed = False
        for name, block in fn.blocks.items():
            # merge predecessors into in_state
            merged_defs: Set[str] = set(p.name for p in block.params)
            merged_types: Dict[str, Type] = {p.name: p.type for p in block.params}
            for pred_name, pred_block in fn.blocks.items():
                term = pred_block.terminator
                edges: List[mir.Edge] = []
                if isinstance(term, mir.Br):
                    edges = [term.target]
                elif isinstance(term, mir.CondBr):
                    edges = [term.then, term.els]
                for instr in pred_block.instructions:
                    if isinstance(instr, mir.Call) and instr.normal:
                        edges.append(instr.normal)
                    if isinstance(instr, mir.Call) and instr.error:
                        edges.append(instr.error)
                for edge in edges:
                    if edge.target == name:
                        pred_out_defs, pred_out_types = out_state.get(pred_name, (set(), {}))
                        merged_defs.update(pred_out_defs)
                        if isinstance(pred_out_types, dict):
                            merged_types.update(pred_out_types)
            if (merged_defs, merged_types) != in_state[name]:
                in_state[name] = (merged_defs, merged_types)
                changed = True

            in_defs, in_types = in_state[name]
            cur_defs = set(in_defs)
            cur_types = dict(in_types)
            for instr in block.instructions:
                if isinstance(instr, mir.Const):
                    cur_defs.add(instr.dest)
                    cur_types[instr.dest] = instr.type
                elif isinstance(instr, (mir.Move, mir.Copy)):
                    cur_defs.add(instr.dest)
                    src_ty = cur_types.get(instr.source)
                    if src_ty:
                        cur_types[instr.dest] = src_ty
                elif isinstance(instr, (mir.Call, mir.CallWithCtx)):
                    cur_defs.add(instr.dest)
                    if program and instr.callee in program.functions:
                        cur_types[instr.dest] = program.functions[instr.callee].return_type
                    if instr.err_dest:
                        cur_defs.add(instr.err_dest)
                        cur_types[instr.err_dest] = ERROR
                elif isinstance(instr, mir.StructInit):
                    cur_defs.add(instr.dest)
                    cur_types[instr.dest] = instr.type
                elif isinstance(instr, mir.FieldGet):
                    cur_defs.add(instr.dest)
                elif isinstance(instr, mir.ArrayInit):
                    cur_defs.add(instr.dest)
                    cur_types[instr.dest] = array_of(instr.element_type)
                elif isinstance(instr, mir.ArrayLiteral):
                    cur_defs.add(instr.dest)
                    cur_types[instr.dest] = array_of(instr.elem_type)
                elif isinstance(instr, mir.ArrayGet):
                    cur_defs.add(instr.dest)
                elif isinstance(instr, mir.Unary):
                    cur_defs.add(instr.dest)
                elif isinstance(instr, mir.Binary):
                    cur_defs.add(instr.dest)
                # ArraySet/Drop produce no new defs
            if out_state[name] != (cur_defs, cur_types):
                out_state[name] = (cur_defs, cur_types)
                changed = True
            # propagate to successors
            succs: List[str] = []
            term = block.terminator
            if isinstance(term, mir.Br):
                succs = [term.target.target]
            elif isinstance(term, mir.CondBr):
                succs = [term.then.target, term.els.target]
            for instr in block.instructions:
                if isinstance(instr, mir.Call) and instr.normal:
                    succs.append(instr.normal.target)
                if isinstance(instr, mir.Call) and instr.error:
                    succs.append(instr.error.target)
            for succ in succs:
                if succ not in worklist:
                    worklist.append(succ)
    return in_state, out_state


def _verify_cfg(
    fn: mir.Function,
    out_state: Dict[str, Tuple[Set[str], Dict[str, Type]]],
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
        call_term = _call_terminator(blocks[name])
        if call_term:
            if call_term.normal:
                _ensure_edge(fn, call_term.normal, blocks[name], source_block=name, out_state=out_state)
                walk(call_term.normal.target)
            if call_term.error:
                _ensure_edge(fn, call_term.error, blocks[name], source_block=name, out_state=out_state, error=True)
                walk(call_term.error.target)
            return
        term = blocks[name].terminator
        if isinstance(term, mir.Br):
            _ensure_edge(fn, term.target, blocks[name], source_block=name, out_state=out_state)
            walk(term.target.target)
        elif isinstance(term, mir.CondBr):
            _ensure_edge(fn, term.then, blocks[name], source_block=name, out_state=out_state)
            _ensure_edge(fn, term.els, blocks[name], source_block=name, out_state=out_state)
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
        # Ensure block params are defined before use in the block (in_state seeds)
        in_defs = out_state.get(block_name, (set(), {}))[0] if out_state else set()
        for param in block.params:
            if param.name not in in_defs:
                raise VerificationError(f"{fn.name}:{block_name}: param '{param.name}' not available at block entry")
    for block in fn.blocks.values():
        if isinstance(block.terminator, mir.Br):
            _ensure_edge(fn, block.terminator.target, block, source_block=block.name, out_state=out_state)
        elif isinstance(block.terminator, mir.CondBr):
            _ensure_edge(fn, block.terminator.then, block, source_block=block.name, out_state=out_state)
            _ensure_edge(fn, block.terminator.els, block, source_block=block.name, out_state=out_state)


def _verify_block(
    fn: mir.Function,
    block: mir.BasicBlock,
    program: mir.Program | None = None,
    incoming: Dict[str, List[tuple[List[str], List[Type]]]] | None = None,
    in_state: Dict[str, Tuple[Set[str], Dict[str, Type]]] | None = None,
    out_state: Dict[str, Tuple[Set[str], Dict[str, Type]]] | None = None,
    dominators: Dict[str, Set[str]] | None = None,
    def_blocks: Dict[str, Set[str]] | None = None,
) -> None:
    state = State()
    if in_state and block.name in in_state:
        defs_in, types_in = in_state[block.name]
        state.defined = set(defs_in)
        state.types = dict(types_in)
    else:
        for param in block.params:
            state.define(param.name)
            state.set_type(param.name, param.type)
    for instr in block.instructions:
        if isinstance(instr, mir.Const):
            _ensure_not_defined(state, instr.dest, block, "const")
            state.define(instr.dest)
        elif isinstance(instr, mir.Move):
            _ensure_defined(state, instr.source, block, "move", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.source, block, "move")
            # Allow reassignments (mutations) by permitting dest to already be defined.
            state.define(instr.dest)
            state.move(instr.source)
        elif isinstance(instr, mir.Copy):
            _ensure_defined(state, instr.source, block, "copy", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.source, block, "copy")
            _ensure_not_defined(state, instr.dest, block, "copy")
            state.define(instr.dest)
        elif isinstance(instr, (mir.Call, mir.CallWithCtx)):
            for arg in instr.args:
                _ensure_defined(state, arg, block, "call", None, dominators, def_blocks)
                _ensure_not_moved_or_dropped(state, arg, block, "call")
            _ensure_not_defined(state, instr.dest, block, "call")
            state.define(instr.dest)
            if instr.err_dest:
                _ensure_not_defined(state, instr.err_dest, block, "call")
                state.define(instr.err_dest)
                state.set_type(instr.err_dest, ERROR)
            if instr.normal or instr.error:
                if instr.normal:
                    _ensure_edge(fn, instr.normal, block, source_block=block.name, out_state=out_state, error=False)
                if instr.error:
                    _ensure_edge(fn, instr.error, block, source_block=block.name, out_state=out_state, error=True)
                return  # call with edges acts as terminator
        elif isinstance(instr, mir.StructInit):
            for arg in instr.args:
                _ensure_defined(state, arg, block, "struct_init", None, dominators, def_blocks)
                _ensure_not_moved_or_dropped(state, arg, block, "struct_init")
            _ensure_not_defined(state, instr.dest, block, "struct_init")
            state.define(instr.dest)
        elif isinstance(instr, mir.FieldGet):
            _ensure_defined(state, instr.base, block, "field_get", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.base, block, "field_get")
            _ensure_not_defined(state, instr.dest, block, "field_get")
            state.define(instr.dest)
        elif isinstance(instr, mir.ArrayInit):
            for elem in instr.elements:
                _ensure_defined(state, elem, block, "array_init", None, dominators, def_blocks)
                _ensure_not_moved_or_dropped(state, elem, block, "array_init")
            _ensure_not_defined(state, instr.dest, block, "array_init")
            state.define(instr.dest)
        elif isinstance(instr, mir.ArrayLiteral):
            for elem in instr.elements:
                _ensure_defined(state, elem, block, "array_literal", None, dominators, def_blocks)
                _ensure_not_moved_or_dropped(state, elem, block, "array_literal")
            _ensure_not_defined(state, instr.dest, block, "array_literal")
            state.define(instr.dest)
        elif isinstance(instr, mir.ArrayGet):
            _ensure_defined(state, instr.base, block, "array_get", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.base, block, "array_get")
            _ensure_defined(state, instr.index, block, "array_get", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.index, block, "array_get")
            _ensure_not_defined(state, instr.dest, block, "array_get")
            state.define(instr.dest)
        elif isinstance(instr, mir.ArraySet):
            _ensure_defined(state, instr.base, block, "array_set", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.base, block, "array_set")
            _ensure_defined(state, instr.index, block, "array_set", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.index, block, "array_set")
            _ensure_defined(state, instr.value, block, "array_set", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.value, block, "array_set")
        elif isinstance(instr, mir.Unary):
            _ensure_defined(state, instr.operand, block, "unary", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.operand, block, "unary")
            _ensure_not_defined(state, instr.dest, block, "unary")
            state.define(instr.dest)
        elif isinstance(instr, mir.Binary):
            _ensure_defined(state, instr.left, block, "binary", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.left, block, "binary")
            _ensure_defined(state, instr.right, block, "binary", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.right, block, "binary")
            _ensure_not_defined(state, instr.dest, block, "binary")
            state.define(instr.dest)
        elif isinstance(instr, mir.ConsoleWrite):
            _ensure_defined(state, instr.value, block, "console_write", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.value, block, "console_write")
        elif isinstance(instr, mir.ConsoleWriteln):
            _ensure_defined(state, instr.value, block, "console_writeln", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.value, block, "console_writeln")
        elif isinstance(instr, mir.Drop):
            _ensure_defined(state, instr.value, block, "drop", None, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, instr.value, block, "drop")
            state.drop(instr.value)
        else:
            raise VerificationError(f"{fn.name}:{block.name}: unsupported instruction {instr}")

    term = block.terminator
    if isinstance(term, mir.Br):
        _ensure_edge(fn, term.target, block, source_block=block.name, out_state=out_state)
    elif isinstance(term, mir.CondBr):
        _ensure_defined(state, term.cond, block, "condbr", term.loc, dominators, def_blocks)
        _ensure_not_moved_or_dropped(state, term.cond, block, "condbr", term.loc)
        _ensure_edge(fn, term.then, block, source_block=block.name, out_state=out_state)
        _ensure_edge(fn, term.els, block, source_block=block.name, out_state=out_state)
    elif isinstance(term, mir.Return):
        if term.value is not None:
            _ensure_defined(state, term.value, block, "return", term.loc, dominators, def_blocks)
            _ensure_not_moved_or_dropped(state, term.value, block, "return", term.loc)
            # type check
            val_ty = out_state.get(block.name, (set(), {}))[1].get(term.value) if out_state else None
            if val_ty and val_ty != fn.return_type:
                raise VerificationError(
                    f"{fn.name}:{block.name}: return type mismatch, expected {fn.return_type}, got {val_ty}"
                )
    elif isinstance(term, mir.Raise):
        _ensure_defined(state, term.error, block, "raise", term.loc, dominators, def_blocks)
        _ensure_not_moved_or_dropped(state, term.error, block, "raise", term.loc)
        err_ty = out_state.get(block.name, (set(), {}))[1].get(term.error) if out_state else None
        if err_ty and err_ty != ERROR:
            raise VerificationError(f"{fn.name}:{block.name}: raise expects Error, got {err_ty}")
    else:
        raise VerificationError(f"{fn.name}:{block.name}: missing or unsupported terminator")


def _loc_for(block: mir.BasicBlock, loc: Optional[mir.Location]) -> str:
    src = loc.file if loc else "<unknown>"
    line = loc.line if loc else 0
    return f"{src}:{line}"


def _compute_dominators(fn: mir.Function) -> Dict[str, Set[str]]:
    blocks = fn.blocks
    dom: Dict[str, Set[str]] = {name: set(blocks.keys()) for name in blocks}
    dom[fn.entry] = {fn.entry}
    preds: Dict[str, Set[str]] = {name: set() for name in blocks}
    for name, block in blocks.items():
        term = block.terminator
        if isinstance(term, mir.Br):
            preds[term.target.target].add(name)
        elif isinstance(term, mir.CondBr):
            preds[term.then.target].add(name)
            preds[term.els.target].add(name)

    changed = True
    while changed:
        changed = False
        for name in blocks:
            if name == fn.entry:
                continue
            pred_sets = [dom[p] for p in preds[name]] or [set(blocks.keys())]
            new_dom = set([name]).union(set.intersection(*pred_sets))
            if new_dom != dom[name]:
                dom[name] = new_dom
                changed = True
    return dom


def _ensure_defined(
    state: State,
    name: str,
    block: mir.BasicBlock,
    ctx: str,
    loc: Optional[mir.Location] = None,
    dominators: Dict[str, Set[str]] | None = None,
    def_blocks: Dict[str, Set[str]] | None = None,
) -> None:
    if state.is_defined(name):
        return
    if dominators and def_blocks and name in def_blocks:
        defs = def_blocks[name]
        doms = dominators.get(block.name, set())
        if defs & doms:
            return
    raise VerificationError(f"{block.name}: {ctx}: '{name}' is undefined at {_loc_for(block, loc)}")


def _ensure_not_defined(state: State, name: str, block: mir.BasicBlock, ctx: str, loc: Optional[mir.Location] = None) -> None:
    if state.is_defined(name):
        # Allow compiler-generated temporaries (_t*) to be redefined in cyclic CFGs produced by the minimal lowering.
        if name.startswith("_t"):
            return
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
    source_block: str,
    out_state: Dict[str, Tuple[Set[str], Dict[str, Type]]] | None = None,
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
    defined = out_state.get(source_block, (set(), {}))[0] if out_state else set()
    for arg in edge.args:
        if arg not in defined:
            # Debug trace for unexpected misses
            # (can be removed once verifier is stable)
            # print(f"DEBUG edge from {source_block} to {edge.target}: defined={defined}, missing={arg}")
            raise VerificationError(f"{fn.name}:{block.name}: edge to '{edge.target}' references undefined '{arg}'")
    # Type checks when available
    src_types = out_state.get(source_block, (set(), {}))[1] if out_state else {}
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
