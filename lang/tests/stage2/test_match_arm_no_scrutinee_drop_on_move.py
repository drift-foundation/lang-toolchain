# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: match-arm lowering must not emit a scrutinee DropValue when
the Ok payload was moved out of a Result<OwnedStruct, Error>.

Exercises the integrated lowering path through compile_stubbed_funcs:
  - Result variant with a non-Copy Ok payload containing a Destructible field
  - match Ok(x) extracts x
  - the scrutinee must NOT be dropped (arm_scrut_payload_moved must be True)
  - if the scrutinee WERE dropped, the Destructible field destructor would run
    on the already-moved payload (use-after-move)

This pins the actual bug from the PEX cert failure where copy_status returned
True for RunningServer, causing arm_scrut_payload_moved to stay False and
emitting a DropValue that cancelled the VirtualThread inside.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable, TypeKind
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.stage2 import mir_nodes as M


SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.sync as sync;

struct Handle {
\tpub flag: conc.Arc<sync.AtomicBool>,
\tpub value: Int
}

struct Server {
\tpub handle: Handle,
\tpub port: Int,
\tpub vt: conc.VirtualThread<Int>
}

fn make_server() nothrow -> core.Result<Server, String> {
\tval a = conc.arc(sync.atomic_bool(false));
\tvar handle = Handle(flag = move a, value = 42);
\tvar vt = conc.spawn_cb(core.callback0(|| captures(move handle) nothrow => {
\t\treturn handle.value;
\t}));
\tvar s = Server(handle = Handle(flag = conc.arc(sync.atomic_bool(false)), value = 0), port = 8080, vt = move vt);
\treturn core.Result::Ok(move s);
}

pub fn main() nothrow -> Int {
\tmatch make_server() {
\t\tcore.Result::Err(_) => { return 1; },
\t\tcore.Result::Ok(srv) => {
\t\t\t// srv must be live here — no scrutinee drop should have
\t\t\t// destroyed the VirtualThread inside.
\t\t\tvar server = move srv;
\t\t\treturn server.port;
\t\t}
\t}
}
"""


def test_match_ok_arm_no_scrutinee_drop_for_destructible_payload() -> None:
	"""Integrated: match on Result<Server, String> must not emit DropValue for
	the scrutinee in the Ok arm when the payload (Server) is non-Copy."""
	stdlib = stdlib_root()
	if stdlib is None:
		import pytest
		pytest.skip("stdlib not available")

	from lang.test_support.drift_tmp import drift_mkdtemp
	import shutil
	tmp = Path(drift_mkdtemp(prefix="match-arm-drop-"))
	try:
		src = tmp / "main.drift"
		src.write_text(SOURCE)

		modules, type_table, exc_catalog, module_exports, deps, diags = parse_drift_workspace_to_hir(
			paths=[src],
			stdlib_root=stdlib,
		)
		assert not any(d.severity == "error" for d in diags), \
			f"parse errors: {[d.message for d in diags if d.severity == 'error']}"

		hirs = {}
		sigs = {}
		for mid, mod in modules.items():
			hirs.update(mod.func_hirs)
			sigs.update(mod.signatures_by_id)

		mir_funcs, checked = compile_stubbed_funcs(
			func_hirs=hirs,
			signatures=sigs,
			exc_env=exc_catalog,
			module_exports=module_exports,
			module_deps=deps,
			return_checked=True,
			type_table=type_table,
			prelude_enabled=True,
			entry_module="main",
			entry_name="main",
		)

		# Find the main function's MIR.
		main_fn_id = FunctionId(module="main", name="main", ordinal=0)
		main_func = mir_funcs.get(main_fn_id)
		assert main_func is not None, "main function not found in MIR"

		# The Ok arm must use MoveOut (not CopyValue) to extract the Server
		# payload from the Result scrutinee.  MoveOut means arm_scrut_payload_moved
		# is True, which prevents the hir_to_mir match-arm scrutinee drop (line
		# 1117-1124 in hir_to_mir.py).  CopyValue means the payload was treated
		# as Copy — the bug.
		#
		# Note: string_arc may emit its own DropValue for the scrutinee local at
		# return blocks — this is correct because the variant drop checks the tag
		# and only drops the active arm's fields (which were zeroed for the moved
		# Ok payload).  The bug is specifically when CopyValue is used instead of
		# MoveOut, leaving the Ok payload un-zeroed in the scrutinee.
		has_moveout_for_server = False
		has_copyvalue_for_server = False
		server_tid = None
		for tid in range(1, type_table._next_id):
			try:
				td = type_table.get(tid)
				if td.kind is TypeKind.STRUCT and td.name == "Server" and td.module_id == "main":
					server_tid = tid
					break
			except Exception:
				continue
		assert server_tid is not None, "Server type not found"

		for block in main_func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, M.MoveOut) and getattr(instr, "ty", None) == server_tid:
					has_moveout_for_server = True
				if isinstance(instr, M.CopyValue) and getattr(instr, "ty", None) == server_tid:
					has_copyvalue_for_server = True
				# string_arc expands MoveOut to LoadLocal — check for the
				# __match_field_move pattern which is the MoveOut expansion.
				if isinstance(instr, M.StoreLocal):
					local = getattr(instr, "local", "")
					if "__match_field_move" in local:
						# This indicates the non-Copy MoveOut path was taken.
						local_ty = main_func.local_types.get(local)
						if local_ty == server_tid:
							has_moveout_for_server = True

		assert has_moveout_for_server, (
			f"Server payload must be extracted via MoveOut (not CopyValue). "
			f"MoveOut={has_moveout_for_server}, CopyValue={has_copyvalue_for_server}. "
			f"If CopyValue was used, copy_status(Server) returned True (bug)."
		)
		assert not has_copyvalue_for_server, (
			f"Server payload must NOT be extracted via CopyValue — Server is "
			f"non-Copy (contains Arc/Destructible). CopyValue causes the "
			f"scrutinee drop to destroy the un-moved payload."
		)

		# Verify Server is non-Copy (sanity check for the test setup).
		server_tid = None
		for tid in range(1, type_table._next_id):
			try:
				td = type_table.get(tid)
				if td.kind is TypeKind.STRUCT and td.name == "Server" and td.module_id == "main":
					server_tid = tid
					break
			except Exception:
				continue
		assert server_tid is not None, "Server type not found in type table"
		assert type_table.copy_status(server_tid) is not True, (
			f"Server must not be Copy — it contains Arc (Destructible). "
			f"copy_status returned {type_table.copy_status(server_tid)}"
		)
	finally:
		shutil.rmtree(tmp, ignore_errors=True)
