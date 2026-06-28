# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: generic destructor dropped inside an interface-impl method
reached ONLY via interface dispatch must be EMITTED in package-consumer mode.

Root cause (driftc package-consumer reachability):
`_deploy`/consumer codegen seeds destroyer fns for every `DropValue` in the
reachable set, then separately seeds interface-impl methods for
`ConstructIfaceValue` / `ArcAsInterface` boxing.  The destroyer pass ran
BEFORE the interface-impl seeding, so a `MutexGuard<T>::destroy` (or any
generic destructor) dropped inside an `implement Iface for S` method — whose
body is reachable only via the vtable, not a call edge — was referenced by a
bare `call` with no `define`, link-failing with
`use of undefined value @...destroy__inst__<hash>` in package mode.
Source-mode masked it (the whole stdlib is monomorphized).

`destroy` lives in the std PACKAGE (`std.concurrent::MutexGuard<T>`), so this
only reproduces when stdlib is consumed as a signed `.dmp` (package mode),
which is why a signed-stdlib consumer build is required.

Surfaced by drift-query M7.1b (`dqc.storage` MemStorage interior `Mutex`,
12 undefined `MutexGuard<MemState>::destroy` refs across the `Storage` impl
methods).  Reduced here to a synthetic in-repo consumer (no FFI, no LMDB,
no external repo dependency).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _build_and_run_pkg_consumer(tmp_path: Path, source: str) -> tuple[int, str, str]:
	"""Build signed stdlib, compile consumer in package mode, run binary.

	Returns (exit_code, compile_stderr, run_stderr).
	"""
	from lang.tests.driver.pkg_test_helpers import _build_signed_stdlib, STD_VERSION
	pkg_root, trust_path, core_trust_path, empty_stdlib = _build_signed_stdlib(tmp_path)
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir(exist_ok=True)
	(consumer_dir / "consumer.drift").write_text(source)
	out_bin = tmp_path / "consumer_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(empty_stdlib),
		 "--package-root", str(pkg_root),
		 "--dep", f"std@{STD_VERSION}",
		 "--trust-store", str(trust_path),
		 "--dev-core-trust-store", str(core_trust_path),
		 "--target-word-bits", "64",
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180),
	)
	if res.returncode != 0:
		return res.returncode, res.stderr, ""
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(10))
	return run.returncode, res.stderr, run.stderr


# Shared fixture: an interface whose impl method holds a `Mutex<T>` field
# (interior mutability) and frees the `MutexGuard<T>` when the method returns.
_COMMON = """\
module consumer;
import std.core as core;
import std.concurrent as conc;
struct MemState { n: Int }
interface Store { fn bump(self: &Self) nothrow -> Int; }
struct Mem { state: conc.Mutex<MemState> }
implement Store for Mem {
\tpub fn bump(self: &Mem) nothrow -> Int {
\t\tvar g = conc.lock(&self.state);
\t\tval m = g.get_mut();
\t\tm.n = m.n + 1;
\t\treturn m.n;
\t}
}
fn new_mem() nothrow -> Mem { return Mem(state = conc.mutex(MemState(n = 0))); }
"""


def test_destructor_in_iface_dispatched_method_is_emitted(tmp_path: Path) -> None:
	"""THE regression: `Mem::bump` is reached only via interface dispatch
	(`m: Store = new_mem(); m.bump()`).  Its `MutexGuard<MemState>` drop must
	be emitted, not left as an undefined symbol at link.
	"""
	source = _COMMON + """\
pub fn main() nothrow -> Int {
\tval m: Store = new_mem();
\treturn m.bump() - 1;
}
"""
	rc, compile_stderr, run_stderr = _build_and_run_pkg_consumer(tmp_path, source)
	assert "use of undefined value" not in compile_stderr, (
		f"generic destructor in iface-dispatched impl method not emitted: "
		f"{compile_stderr[:600]}"
	)
	assert rc == 0, f"exit {rc}, expected 0. compile: {compile_stderr[:400]}"


def test_destructor_in_direct_called_method_is_emitted(tmp_path: Path) -> None:
	"""Control: the SAME impl method reached via a direct (inherent) call
	already worked (call-edge BFS seeds it before the destroyer pass).  Pins
	that the fix did not regress the direct path.
	"""
	source = _COMMON + """\
pub fn main() nothrow -> Int {
\tvar mm = new_mem();
\treturn mm.bump() - 1;
}
"""
	rc, compile_stderr, run_stderr = _build_and_run_pkg_consumer(tmp_path, source)
	assert "use of undefined value" not in compile_stderr, compile_stderr[:600]
	assert rc == 0, f"exit {rc}, expected 0. compile: {compile_stderr[:400]}"
