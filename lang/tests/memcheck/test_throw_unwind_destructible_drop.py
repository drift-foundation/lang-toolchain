# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG #102 regression: a Destructible local in scope at a
`throw` site whose surrounding try has a non-matching typed catch
must be dropped along the throw-unwind path.

Surfaced 2026-05-22 on Singular self-test 0.32.10 memcheck.  The
leaking shape is `singular.gateway::SingularImpl::{complete,fail}`
(see /home/sl/src/pushcoin/singular/packages/singular/src/gateway.drift
around line 651):

	try {
		var lease = _acquire_lease(self);              // Destructible
		val conn: &mut rpc.RpcConnection = lease.conn().or_throw();
		...
		if row.result_code == 1 { return ... }
		else if row.result_code == 2 { return ... }
		else {
			throw SingularException(...);              // not caught below
		}
	} catch managed:ManagedError(e) { ... }            // doesn't match

The local typed catch only matches `managed:ManagedError`, so the
thrown `SingularException` routes through `_visit_stmt_HTry`'s
dispatch-block "no event-arm matches" branch and `_propagate_error`s
outward.  By the time that else branch emits
`_emit_function_exit_cleanup_hook()`, the try-body scope has already
been popped by `lower_block(stmt.body)` — so `lease` is no longer a
CleanupHook candidate.  No drop is emitted on the throw-unwind edge
and the Destructible's owned heap members (here: the inner String
payload) leak.

This regression isolates the bug from singular/mariadb so a memcheck
on the toolchain alone shows the leak, independent of any pool/RPC
runtime.

Fix site: `lang/driftc/stage2/hir_to_mir.py::_visit_stmt_HThrow` (or
the matching site in `_visit_stmt_HTry`'s dispatch-else) must emit a
CleanupHook covering all locals registered in scopes from the
throw-site down to (but not including) the scope at try entry, before
the Goto(dispatch_block).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

# Destructible struct whose `destroy()` implementation has a no-op
# body; the epilogue must drop the owned-String field `label`.  The
# String literal payload is long enough to be a real heap allocation
# (not a small-string-optimisation inline) so memcheck observes it.
#
# `inner()` is can-throw (no `nothrow`); `main()` is nothrow and
# catches at top-level so the binary exits cleanly even when the bug
# is present — only the leak signal differs.
SOURCE = """\
module main;

import std.core as core;
import std.console as console;

pub error CaughtKind {}
pub error UncaughtKind { code: Int }

pub struct Conn {
\tpub seq: Int,
}

// Heap-allocated payload owned by the Lease.  If the Lease's destroy
// is not run on the throw-unwind path, this Array<Byte> is leaked.
pub struct Lease {
\tpub label: String,
\tpub payload: Array<Byte>,
\tpub inner: Conn,
}

implement core.Destructible for Lease {
\tpub fn destroy(var self: Lease) nothrow -> Void {
\t\tconsole.print("LEASE_DESTROYED\\n");
\t\treturn;
\t}
}

implement Lease {
\t// Mirrors `lease.conn()` in singular: returns a borrow into self.
\tpub fn conn(self: &Lease) nothrow -> &Conn {
\t\treturn &self.inner;
\t}
}

pub struct ArgsBag {
\tpub items: Array<String>,
}

fn _make_payload() nothrow -> Array<Byte> {
\tvar a: Array<Byte> = [];
\tvar i = 0;
\twhile i < 64 {
\t\ta.push(cast<Byte>(i));
\t\ti = i + 1;
\t}
\treturn move a;
}

pub error AcquireKind { tag: String }

// Can-throw acquire — mirrors singular's `_acquire_lease`, which is
// can-throw (throws `SingularException(PoolAcquire)` on pool error
// inside an inner match's Err arm).
fn _acquire(flag: Int) -> Lease {
\tif flag == -1 {
\t\tthrow AcquireKind(tag = "pool_error");
\t}
\treturn Lease(
\t\tlabel = "ACQUIRED_LABEL_PAYLOAD_LONG_ENOUGH_TO_HEAP_ALLOC",
\t\tpayload = _make_payload(),
\t\tinner = Conn(seq = 0)
\t);
}

fn _new_args() nothrow -> ArgsBag {
\tvar v: Array<String> = [];
\treturn ArgsBag(items = move v);
}

// Can-throw — mirrors `_call_operation_sp` in singular.
fn _do_call(conn: &Conn, sp: &String, args: ArgsBag, flag: Int) -> Int {
\tif flag == -2 {
\t\tthrow AcquireKind(tag = "rpc_error");
\t}
\treturn conn.seq + args.items.len + sp.byte_length();
}

// Shape mirrors singular.gateway::SingularImpl::complete more
// closely than the simple variant: a Destructible local `lease`, a
// borrow `conn` taken into the lease via `lease.conn()`, an owned
// `args` consumed by `move`, and a throw nested inside if/else-if/else.
fn inner(code: Int) -> Int {
\ttry {
\t\tvar lease = _acquire(code);
\t\tval conn: &Conn = lease.conn();
\t\tvar args = _new_args();
\t\targs.items.push("a");
\t\targs.items.push("b");
\t\tval sp: String = "sp_singular_complete";
\t\tval row = _do_call(conn, sp, move args, code);
\t\tif code == 1 {
\t\t\treturn 1 + row;
\t\t} else if code == 2 {
\t\t\treturn 2 + row;
\t\t} else {
\t\t\tthrow UncaughtKind(code = code);
\t\t}
\t} catch CaughtKind(_e) {
\t}
\treturn 0;
}

pub fn main() nothrow -> Int {
\ttry {
\t\tval _r = inner(99);
\t} catch {
\t}
\treturn 0;
}
"""


def test_throw_unwind_destructible_drop_no_leak(tmp_path: Path) -> None:
	"""Pin LANGUAGE_BUG #102: throw routed past a non-matching typed
	catch must still drop in-scope Destructible locals."""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	out_bin = tmp_path / "test_bin"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:600]}"

	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=120,
	)
	# Independent of memcheck: assert Destructible::destroy() actually
	# ran along the throw-unwind path.  If the compiler skips drop
	# dispatch, the observable print is absent.
	assert "LEASE_DESTROYED" in vg.stdout, (
		f"Destructible::destroy() did not run on the throw-unwind "
		f"path through non-matching typed catch.\n"
		f"stdout: {vg.stdout!r}\nstderr: {vg.stderr[-300:]!r}"
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	indir_match = re.search(r"indirectly lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	indirectly_lost = int(indir_match.group(1).replace(",", "")) if indir_match else 0

	assert vg.returncode != 97, (
		f"Valgrind detected leaks — Destructible `lease` local was not "
		f"dropped on throw-unwind through non-matching typed catch.\n"
		f"definitely lost: {definitely_lost} bytes; "
		f"indirectly lost: {indirectly_lost} bytes\n"
		f"valgrind log (tail):\n{vg_output[-1200:]}"
	)
	assert definitely_lost == 0, (
		f"definitely lost: {definitely_lost} bytes "
		f"(indirectly lost: {indirectly_lost})"
	)
	assert indirectly_lost == 0, (
		f"indirectly lost: {indirectly_lost} bytes "
		f"(definitely lost: {definitely_lost})"
	)
