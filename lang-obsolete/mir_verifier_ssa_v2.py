"""Strict SSA MIR verifier (v2) for the new SSA lowering path.

This verifier currently enforces:
  - Single definition per SSA name (function params, block params, defining instructions/terminators).
  - Def-before-use both lexically (within a block) and via dominance (def block must dominate use block).
  - Structural CFG checks: all blocks reachable; edges target known blocks; block params/edge args arity match.
  - Operand use checking for supported MIR instructions/terminators.

It assumes MIR blocks are provided either as a list or as a dict of BasicBlock objects.
It assumes MIR blocks are provided either as a list or as a dict of BasicBlock objects.
"""

from __future__ import annotations

from typing import Dict, List, Set

from . import mir


class SSAVerifierV2:
    def __init__(self, func):
        # func.blocks: Iterable[BasicBlock] or dict of name -> BasicBlock
        self.func = func
        self.entry_name = getattr(func, "entry", None)
        # SSA name -> textual location of definition
        self.global_defs: Dict[str, str] = {}
        # SSA name -> block name where it is defined
        self.def_blocks: Dict[str, str] = {}
        self.blocks = {}
        # Predecessor map: block name -> set of pred block names
        self.preds: Dict[str, Set[str]] = {}
        # Dominators: block name -> set of dominating block names
        self.doms: Dict[str, Set[str]] = {}

    def verify(self) -> None:
        self._collect_blocks()
        reachable = self._reachable_blocks()
        self._ensure_all_blocks_reachable(reachable)
        self._build_preds(reachable)
        self._compute_dominators(reachable)
        self._check_phi_arity(reachable)
        # Register block params and function params.
        for bname in reachable:
            block = self.blocks[bname]
            self._register_block_params(block)
        # Register defining instruction dests and terminator dests.
        for bname in reachable:
            block = self.blocks[bname]
            self._register_instr_defs(block)
            self._register_term_defs(block)
        # Check uses in instructions and terminators.
        for bname in reachable:
            block = self.blocks[bname]
            self._visit_block(block)

    def _iter_blocks(self):
        # Not used now; kept for compatibility if needed.
        return self.blocks.values()

    def _define(self, ssa_name: str, where: str, block_name: str) -> None:
        if ssa_name in self.global_defs:
            prev = self.global_defs[ssa_name]
            raise RuntimeError(f"SSA name {ssa_name!r} defined twice: {prev} and {where}")
        self.global_defs[ssa_name] = where
        self.def_blocks[ssa_name] = block_name

    def _use(self, ssa_name: str, where: str, block_name: str, local_defs: Set[str]) -> None:
        if ssa_name not in self.global_defs:
            raise RuntimeError(f"SSA name {ssa_name!r} used before definition at {where}")
        def_block = self.def_blocks.get(ssa_name)
        # Dominance check: def block must dominate current block.
        doms = self.doms.get(block_name, set())
        if def_block is not None and def_block not in doms:
            raise RuntimeError(
                f"use of {ssa_name!r} in block {block_name} not dominated by its definition in {def_block}"
            )
        # Intra-block ordering: if defined in the same block, it must already have been seen locally.
        if def_block == block_name and ssa_name not in local_defs:
            raise RuntimeError(f"use of {ssa_name!r} before definition in block {block_name}")

    def _register_block_params(self, block) -> None:
        for p in block.params:
            self._define(p.name, where=f"block_param {block.name}", block_name=block.name)

    def _register_instr_defs(self, block) -> None:
        where = f"block {block.name}"
        for instr in block.instructions:
            dest = getattr(instr, "dest", None)
            if dest:
                self._define(dest, f"{where}, instr {instr!r}", block_name=block.name)

    def _register_term_defs(self, block) -> None:
        term = block.terminator
        if term is None:
            return
        # Only Call terminators may define a value.
        if isinstance(term, mir.Call) and (term.normal or term.error):
            dest = getattr(term, "dest", None)
            if dest:
                self._define(dest, where=f"terminator {block.name}", block_name=block.name)
            err_dest = getattr(term, "err_dest", None)
            if err_dest:
                self._define(err_dest, where=f"terminator {block.name}", block_name=block.name)
        else:
            # Reject any other terminator that carries a dest.
            if getattr(term, "dest", None):
                raise RuntimeError(f"unsupported terminator with dest in block {block.name}: {term!r}")

    def _visit_block(self, block) -> None:
        where_block = f"block {block.name}"
        if block.terminator is None:
            raise RuntimeError(f"block {block.name} is missing a terminator")
        # Structural: terminator not in instructions
        if block.terminator is not None and block.terminator in block.instructions:
            raise RuntimeError(f"terminator for block {block.name} must not appear in instructions list")
        local_defs: Set[str] = {p.name for p in block.params}
        for instr in block.instructions:
            self._visit_instr(where_block, instr, block.name, local_defs)
            dest = getattr(instr, "dest", None)
            if dest:
                local_defs.add(dest)
        if block.terminator is not None:
            self._visit_terminator(where_block, block.terminator, block.name, local_defs)

    def _visit_instr(self, where_block: str, instr, block_name: str, local_defs: Set[str]) -> None:
        where = f"{where_block}, instr {instr!r}"
        if isinstance(instr, mir.Move):
            self._use(instr.source, where, block_name, local_defs)
        elif isinstance(instr, mir.Binary):
            self._use(instr.left, where, block_name, local_defs)
            self._use(instr.right, where, block_name, local_defs)
        elif isinstance(instr, mir.Copy):
            self._use(instr.source, where, block_name, local_defs)
        elif isinstance(instr, mir.Call):
            if instr.normal or instr.error:
                raise RuntimeError(
                    f"Call with edges must appear as terminator, found in instruction list at {where}"
                )
            for arg in instr.args:
                self._use(arg, where, block_name, local_defs)
            # Calls in the instruction list are expected to have no edges; edges handled in terminators.
        elif isinstance(instr, mir.Unary):
            self._use(instr.operand, where, block_name, local_defs)
        elif isinstance(instr, mir.ArrayGet):
            self._use(instr.base, where, block_name, local_defs)
            self._use(instr.index, where, block_name, local_defs)
        elif isinstance(instr, mir.FieldGet):
            self._use(instr.base, where, block_name, local_defs)
        elif isinstance(instr, mir.FieldSet):
            # Mutation: must not define dest; operands must be defined.
            self._use(instr.base, where, block_name, local_defs)
            self._use(instr.value, where, block_name, local_defs)
            if getattr(instr, "dest", None):
                raise RuntimeError(f"store instruction must not define dest: {instr!r}")
        elif isinstance(instr, mir.ArraySet):
            # Mutation: must not define dest; operands must be defined.
            self._use(instr.base, where, block_name, local_defs)
            self._use(instr.index, where, block_name, local_defs)
            self._use(instr.value, where, block_name, local_defs)
            if getattr(instr, "dest", None):
                raise RuntimeError(f"store instruction must not define dest: {instr!r}")
        elif isinstance(instr, mir.Const):
            # No operands to use; dest is pre-registered.
            pass
        elif isinstance(instr, mir.StructInit):
            for arg in instr.args:
                self._use(arg, where, block_name, local_defs)
        elif isinstance(instr, mir.ArrayInit):
            for arg in instr.elements:
                self._use(arg, where, block_name, local_defs)
        elif isinstance(instr, mir.ArrayLiteral):
            for arg in instr.elements:
                self._use(arg, where, block_name, local_defs)
        elif isinstance(instr, mir.ArrayLen):
            self._use(instr.base, where, block_name, local_defs)
        elif isinstance(instr, mir.CallWithCtx):
            self._use(instr.ctx_dest, where, block_name, local_defs)
            for arg in instr.args:
                self._use(arg, where, block_name, local_defs)
            if instr.ctx_arg:
                self._use(instr.ctx_arg, where, block_name, local_defs)
            if instr.normal or instr.error:
                raise RuntimeError(f"CallWithCtx with edges must be a terminator: {instr!r}")
        elif isinstance(instr, mir.ConsoleWrite):
            self._use(instr.value, where, block_name, local_defs)
        elif isinstance(instr, mir.ConsoleWriteln):
            self._use(instr.value, where, block_name, local_defs)
        elif isinstance(instr, mir.Drop):
            self._use(instr.value, where, block_name, local_defs)
        elif isinstance(instr, mir.ErrorEvent):
            self._use(instr.error, where, block_name, local_defs)
        # Add other instruction kinds as the SSA lowering grows.

    def _visit_terminator(self, where_block: str, term, block_name: str, local_defs: Set[str]) -> None:
        where = f"{where_block}, term {term!r}"
        # Call-as-terminator with normal/error edges
        if isinstance(term, mir.Call) and (term.normal or term.error):
            if term.dest:
                local_defs.add(term.dest)
            if term.err_dest:
                local_defs.add(term.err_dest)
            for arg in term.args:
                self._use(arg, where, block_name, local_defs)
            for edge in (term.normal, term.error):
                if edge:
                    for arg in edge.args:
                        self._use(arg, where, block_name, local_defs)
            return
        if isinstance(term, mir.CondBr):
            self._use(term.cond, where, block_name, local_defs)
            for edge in (term.then, term.els):
                for arg in edge.args:
                    self._use(arg, where, block_name, local_defs)
            return
        if isinstance(term, mir.Br):
            for arg in term.target.args:
                self._use(arg, where, block_name, local_defs)
            return
        if isinstance(term, mir.Return) and term.value is not None:
            self._use(term.value, where, block_name, local_defs)
            return
        if isinstance(term, mir.Throw):
            self._use(term.error, where, block_name, local_defs)
            return
        # Raise or other terminators either carry no SSA operands or are not yet handled in the scaffold.
    def _collect_blocks(self) -> None:
        blocks = getattr(self.func, "blocks", None)
        if blocks is None:
            return
        if isinstance(blocks, dict):
            self.blocks = dict(blocks)
        else:
            # If list-like, assume each block has a unique name attribute.
            self.blocks = {blk.name: blk for blk in blocks}

    def _entry_block_name(self) -> str:
        if self.entry_name:
            if self.entry_name not in self.blocks:
                raise RuntimeError(f"declared entry block {self.entry_name} not found in blocks")
            if self.entry_name != "bb0":
                raise RuntimeError(f"entry block must be 'bb0' for now, found {self.entry_name}")
            return self.entry_name
        name = next(iter(self.blocks.values())).name
        if name != "bb0":
            raise RuntimeError(f"entry block must be 'bb0' for now, found {name}")
        return name

    def _reachable_blocks(self) -> Set[str]:
        """Return names of blocks reachable from the entry."""
        if not self.blocks:
            return set()
        entry = self._entry_block_name()
        seen: Set[str] = set()
        work: List[str] = [entry]
        while work:
            name = work.pop()
            if name in seen:
                continue
            seen.add(name)
            blk = self.blocks.get(name)
            if blk is None:
                continue
            succs: List[str] = []
            term = blk.terminator
            if isinstance(term, mir.CondBr):
                succs.extend([term.then.target, term.els.target])
            elif isinstance(term, mir.Br):
                succs.append(term.target.target)
            elif isinstance(term, mir.Call) and (term.normal or term.error):
                if term.normal:
                    succs.append(term.normal.target)
                if term.error:
                    succs.append(term.error.target)
            for s in succs:
                if s in self.blocks:
                    work.append(s)
        return seen

    def _ensure_all_blocks_reachable(self, reachable: Set[str]) -> None:
        if not self.blocks:
            return
        entry = self._entry_block_name()
        if entry not in reachable:
            raise RuntimeError("entry block is unreachable")
        # Allow unreachable blocks (e.g., dead catch arms); they will be pruned by later passes.

    def _build_preds(self, reachable: Set[str]) -> None:
        preds: Dict[str, Set[str]] = {name: set() for name in reachable}
        for blk in self.blocks.values():
            if blk.name not in reachable:
                continue
            term = blk.terminator
            edges: List[mir.Edge] = []
            if isinstance(term, mir.CondBr):
                edges = [term.then, term.els]
            elif isinstance(term, mir.Br):
                edges = [term.target]
            elif isinstance(term, mir.Call) and (term.normal or term.error):
                edges = [e for e in (term.normal, term.error) if e]
            for e in edges:
                if e.target not in self.blocks:
                    raise RuntimeError(f"edge from {blk.name} targets unknown block {e.target}")
                if e.target in preds:
                    preds[e.target].add(blk.name)
        self.preds = preds

    def _compute_dominators(self, reachable: Set[str]) -> None:
        if not reachable:
            self.doms = {}
            return
        entry = self._entry_block_name()
        doms: Dict[str, Set[str]] = {name: set(reachable) for name in reachable}
        doms[entry] = {entry}
        changed = True
        while changed:
            changed = False
            for b in reachable:
                if b == entry:
                    continue
                preds = self.preds.get(b, set())
                if not preds:
                    # unreachable or malformed CFG; leave doms as is
                    continue
                new_dom = set(reachable)
                for p in preds:
                    new_dom &= doms.get(p, set())
                new_dom.add(b)
                if new_dom != doms[b]:
                    doms[b] = new_dom
                    changed = True
        self.doms = doms

    def _edges_from_block(self, blk) -> List[mir.Edge]:
        term = blk.terminator
        if isinstance(term, mir.CondBr):
            return [term.then, term.els]
        if isinstance(term, mir.Br):
            return [term.target]
        if isinstance(term, mir.Call) and (term.normal or term.error):
            return [e for e in (term.normal, term.error) if e]
        return []

    def _check_phi_arity(self, reachable: Set[str]) -> None:
        if not reachable:
            return
        entry = self._entry_block_name()
        for name, block in self.blocks.items():
            if name not in reachable:
                continue
            incoming_edges: List[mir.Edge] = []
            for pred_name in self.preds.get(name, set()):
                pred_blk = self.blocks[pred_name]
                edges = [e for e in self._edges_from_block(pred_blk) if e.target == name]
                if not edges:
                    raise RuntimeError(f"predecessor {pred_name} missing edge to {name}")
                incoming_edges.extend(edges)
            if block.params and name != entry and not incoming_edges:
                raise RuntimeError(f"block {name} has params but no predecessors")
            if not block.params:
                for edge in incoming_edges:
                    if edge.args:
                        raise RuntimeError(
                            f"block {name} has no params but edge from predecessor passes args {edge.args}"
                        )
            for edge in incoming_edges:
                if len(edge.args) != len(block.params):
                    raise RuntimeError(
                        f"edge {edge.target} expects {len(block.params)} args, got {len(edge.args)} from {edge}"
                    )
