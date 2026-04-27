# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression — stale ledger after `drop_flags` insertion.

Pinned bug (introduced by 0.31.9 Phase 3B step-1, commit 94a9c44d
"step 3 done"):

`compile_stubbed_funcs` builds the ownership ledger after
`cleanup_authoring`, then runs `drop_flags` which INSERTS new
instructions at block heads (drop-flag init `ConstBool` + `StoreLocal`
of `__drop_flag_*`).  The insertions shift instruction indices.  Then
`string_arc` consults the pre-drop_flags ledger via
`_ledger.verdict_at((block, idx), local, needs_drop=...)` using
POST-drop_flags indices — reading state from the wrong index.

For a function that introduces a destructible local with a fresh
`var t = move x;` AFTER prior locals that picked up drop flags, the
shift makes the ledger return `MUST_DROP` at the first `StoreLocal(t)`
even though `t` is UNINIT there.  String_arc then emits the
drop-before-overwrite pattern (`LoadLocal(t) → ZeroValue → StoreLocal
→ DropValue`), reading uninitialised memory.  SSA later catches it as
`load before store for local 't'`.

Discovered live in the orch certification cycle for drift-web —
`web.client.session::_finish_tls_new` shape.  Test below extracts the
minimal triggering source: a struct with `Destructible` impl, two
`mem.replace` calls extracting fields, and a destructible local
moved-out on both arms of an `if`.  The cascade-relevant prior
locals (which receive drop flags from `drop_flags`) shift indices
enough for the bug to fire on the destructible local that follows.

Acceptance: this test fails pre-fix with `RuntimeError: SSA: load
before store for local 't' …` and passes after the ledger is rebuilt
between `drop_flags` and `string_arc`.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _compile(tmp_path: Path, source: str, *, entry: str = "m::main"):
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == [], parse_diags
	func_hirs, signatures, _ = flatten_modules(modules)
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry=entry,
	)
	return checked


def test_destructible_local_moved_on_both_arms_no_join_drop(tmp_path: Path) -> None:
	"""Function shape from drift-web `_finish_tls_new`: a destructible
	local is moved on BOTH arms of an `if`; the cleanup author and
	`drop_before_overwrite` site MUST agree the local is uniformly
	moved — no drop emitted at the join, no drop-before-overwrite at
	the var declaration.

	Pre-fix: ledger queries with shifted indices return MUST_DROP at
	the first `StoreLocal(t)`, string_arc emits drop-before-overwrite
	reading uninit memory, SSA crashes with `load before store for
	local 't'`.

	Post-fix: ledger rebuilt after `drop_flags`; verdict at the first
	`StoreLocal(t)` is MUST_NOT_DROP (UNINIT pre-state), no drop
	emitted, SSA passes.
	"""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;
import std.mem as mem;

struct DestrThing { tag: String, closed: Bool }

implement core.Destructible for DestrThing {
	pub fn destroy(var self: DestrThing) nothrow -> Void {
		if self.closed { return; }
		val _ = move self;
	}
}

fn drop_thing(t: DestrThing) nothrow -> Void { val _ = move t; }

struct PoolEntry { stream: DestrThing, read_buf: Array<Byte> }
struct PooledResult { response: String, remainder: Array<Byte>, reusable: Bool }

fn _empty_bytes() nothrow -> Array<Byte> { val a: Array<Byte> = []; return move a; }
fn _empty_str() nothrow -> String { return ""; }

fn put_entry(e: PoolEntry) nothrow -> Void { val _ = move e; }

fn handle(reusable: Bool, result: PooledResult, conn: DestrThing) nothrow -> Int {
	var r = move result;
	var t = move conn;
	val response = mem.replace<type String>(&mut r.response, _empty_str());
	val remainder = mem.replace<type Array<Byte>>(&mut r.remainder, _empty_bytes());
	if r.reusable == true {
		val new_entry = PoolEntry(stream = move t, read_buf = move remainder);
		put_entry(move new_entry);
	} else {
		val _ = drop_thing(move t);
	}
	val _ = response;
	return 0;
}

fn main() nothrow -> Int {
	val empty: Array<Byte> = [];
	val r = PooledResult(response = "", remainder = move empty, reusable = true);
	return handle(true, move r, DestrThing(tag = "", closed = false));
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors
