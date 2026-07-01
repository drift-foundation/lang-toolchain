# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Stage2 structural regression for the for-in match-binder MIR-identity fix.

Bug: the match-binder lowering stored the binder under the raw `arm.binders`
name while binder READS used the binding-id canonical local
(`_canonical_local`).  When a second for-in loop reused a binder NAME, the second
binder stored `x` but was read as `x__b<id>` — an undefined local that the SSA
pass correctly rejected ("load before store").

Fix: the binder loop now pairs `arm.binders` with `arm.binder_ids` and
canonicalizes the destination (`binder_local = self._canonical_local(id, name)`)
before every `ensure_local` / `_local_types` / `_register_drop_local` /
`StoreLocal`, so stores and reads agree.

This test captures the pre-SSA MIR of a two-loop name-reuse function and asserts
the structural invariant the fix restores: **every local that is read
(`LoadLocal`/`MoveOut`) is also stored (`StoreLocal`) or is a parameter** — i.e.
no read of an undefined local.  Before the fix this fails for the second loop's
canonical binder local; the run-level + memcheck coverage lives in the e2e case
`for_in_loop_var_reuse`.
"""
from __future__ import annotations

import contextlib
import io

from lang.driftc.driftc import main as driftc_main
from lang.driftc.stage4 import ssa as _ssa_mod
from lang.driftc.stage2 import mir_nodes as M


_TWO_LOOP_REUSE = """
module main;

import std.iter as iter;
use trait iter.SinglePassIterator;

pub fn main() nothrow -> Int {
	var a: Array<Int> = [];
	a.push(1); a.push(2); a.push(3);
	var s = 0;
	for x in a { s = s + *x; }
	for x in a { s = s + *x; }
	return s;
}
""".lstrip()


def test_two_loop_binder_reuse_has_no_undefined_local_reads(tmp_path, monkeypatch) -> None:
	captured = []
	orig = _ssa_mod.MirToSSA.run

	def _spy(self, func):
		captured.append(func)
		return orig(self, func)

	monkeypatch.setattr(_ssa_mod.MirToSSA, "run", _spy)

	src = tmp_path / "main.drift"
	src.write_text(_TWO_LOOP_REUSE, encoding="utf-8")
	with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
		rc = driftc_main(["--stdlib-root", "stdlib", "--entry", "main", "-o", str(tmp_path / "out.bin"), str(src)])

	# The whole point: it must now COMPILE (pre-fix this raised in SSA).
	assert rc == 0, "two-loop binder-name reuse failed to compile (SSA load-before-store regression)"

	main_func = next((f for f in captured if getattr(f.fn_id, "name", None) == "main"), None)
	assert main_func is not None, "did not capture main()'s MIR"

	defs: set[str] = set(main_func.params or [])
	reads: set[str] = set()
	_blocks = main_func.blocks
	_block_iter = _blocks.values() if isinstance(_blocks, dict) else _blocks
	for block in _block_iter:
		for instr in block.instructions:
			if isinstance(instr, M.StoreLocal):
				defs.add(instr.local)
			elif isinstance(instr, M.LoadLocal):
				reads.add(instr.local)
			elif isinstance(instr, M.MoveOut):
				reads.add(instr.local)

	undefined = reads - defs
	assert not undefined, (
		f"MIR reads locals with no store (binder-identity mismatch): {sorted(undefined)}. "
		f"The match-binder store must use the same canonical local as binder reads."
	)
