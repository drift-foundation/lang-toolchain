# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: a same-name method overload split across an
`implement Interface for T` block (canonical, 1-arg) and an
`implement T` block (convenience, no-arg) must survive the published-package
boundary — both overloads stay in one candidate set on the CONSUME side.

The no-arg convenience body calls the 1-arg canonical form.  In a whole-source
compile the resolver sees both overloads; before the fix, a consumer compiled
against the published `.dmp` failed with:

    error: no matching method 'acquire' for receiver pkg::pkg.Pool
    error: no matching method 'acquire' for receiver Ref<pkg::pkg.Pool>

Root cause (CORE_BUG, toolchain 0.33.13): on the CONSUME side the interface-impl
method's fn_id is tagged by `trait_impl_index`, so `resolve_method_call` routed
it into the segregated `trait_candidates` bucket; the inherent-wins selection
(`if inherent_candidates: candidates = inherent_candidates`) then DISCARDED it,
leaving only the no-arg inherent overload — so a 2-arg call could not resolve.
In a whole-source compile the same method is not trait-tagged
(`trait_key_for_fn_id` returns None), stays inherent, and both overloads coexist.

Fix: interface-impl candidates are routed to a peer `iface_candidates` list and
unioned with `inherent_candidates` in the final selection, so inherent +
interface-impl methods of the same name on a concrete type form ONE overload
set (whole-source parity).  `pub trait` candidates keep their use-trait scoping
fallback.  See `call_resolver.resolve_method_call`.

This is the exact `effective-drift.md` idiom ("Interfaces can't overload —
canonical method plus concrete-type sugar").  Reported by mariadb-rpc
(2026-05-31): `ConnectionSource.acquire(wait)` canonical + no-arg `acquire()`
sugar blocked `drift deploy` (baseline smoke recompiles against the published
interface) while `just test` (whole-source) was green.

Sibling of the earlier (fixed) cross-package interface-dispatch bug:
test_cross_pkg_interface_trait_metadata.py (that dropped interface trait
metadata entirely; this one carried it but failed to keep the two same-name
methods in one overload set at resolution time).
"""
from __future__ import annotations

import io
import contextlib
import subprocess
from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _run(argv: list[str]) -> tuple[int, str]:
	buf = io.StringIO()
	with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
		try:
			rc = driftc_main(argv)
		except SystemExit as e:
			rc = int(e.code) if e.code is not None else 0
	return rc, buf.getvalue()


def _emit_consume_run(tmp_path: Path, name: str, lib_src: str, app_src: str) -> int:
	"""Emit `name` as a package, compile a consumer against the PUBLISHED
	artifact (the `drift deploy` recompile path), link, run, return exit code.
	Each compile step is asserted, so a compile failure surfaces as the test
	failure with the diagnostic."""
	d = tmp_path
	(d / "lib").mkdir(parents=True, exist_ok=True)
	(d / "lib" / f"{name}.drift").write_text(lib_src, encoding="utf-8")
	pkg = d / f"{name}.dmp"
	rc, out = _run([
		"--target-word-bits", "64",
		"-M", str(d), str(d / "lib" / f"{name}.drift"),
		"--emit-package", str(pkg),
		"--package-id", name, "--package-version", "0.1.0",
		"--package-target", "test-target",
	])
	assert rc == 0, f"emit-package failed:\n{out}"

	(d / "main.drift").write_text(app_src, encoding="utf-8")
	out_bin = d / "consumer"
	rc, out = _run([
		"--target-word-bits", "64",
		"-M", str(d),
		"--package-root", str(d),
		"--dep", f"{name}@0.1.0",
		"--allow-unsigned-from", str(d),
		str(d / "main.drift"),
		"--entry", "main::main",
		"-o", str(out_bin),
	])
	assert rc == 0, f"consumer compile against published package failed:\n{out}"
	assert out_bin.exists(), f"no consumer binary produced:\n{out}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=30)
	return run.returncode


_PLAIN_LIB = """
module plov;
export { Pool, Wait, make_pool };
pub variant Wait { UseDefault, Forever }
pub struct Pool { v: Int }
implement Pool {
	pub fn acquire(self: &Pool, wait: Wait) nothrow -> Int {
		match wait { Wait::UseDefault => { return self.v + 1; }, Wait::Forever => { return self.v + 2; } }
	}
	pub fn acquire(self: &Pool) nothrow -> Int { return self.acquire(Wait::UseDefault()); }
}
pub fn make_pool(v: Int) nothrow -> Pool { return Pool(v = v); }
"""

_PLAIN_APP = """
module main;
import plov as plov;
fn main() nothrow -> Int {
	val p = plov.make_pool(10);
	return p.acquire() + p.acquire(plov.Wait::Forever());
}
"""

_IFACE_LIB = """
module ifov;
export { Pool, Wait, Source, make_pool };
pub variant Wait { UseDefault, Forever }
pub interface Source { fn acquire(self: &Self, wait: Wait) nothrow -> Int; }
pub struct Pool { v: Int }
implement Source for Pool {
	pub fn acquire(self: &Pool, wait: Wait) nothrow -> Int {
		match wait { Wait::UseDefault => { return self.v + 1; }, Wait::Forever => { return self.v + 2; } }
	}
}
implement Pool {
	pub fn acquire(self: &Pool) nothrow -> Int { return self.acquire(Wait::UseDefault()); }
}
pub fn make_pool(v: Int) nothrow -> Pool { return Pool(v = v); }
"""

_IFACE_APP = """
module main;
import ifov as ifov;
fn main() nothrow -> Int {
	val p = ifov.make_pool(10);
	return p.acquire() + p.acquire(ifov.Wait::Forever());
}
"""

# p.acquire() == v+1 == 11; p.acquire(Forever) == v+2 == 12; sum 23.
_EXPECT = 23


def test_plain_inherent_overload_survives_publish(tmp_path: Path) -> None:
	"""Control: a same-name overload entirely within one `implement Pool {}`
	block survives the publish boundary (compiles, links, runs to 23)."""
	assert _emit_consume_run(tmp_path, "plov", _PLAIN_LIB, _PLAIN_APP) == _EXPECT


def test_iface_inherent_overload_survives_publish(tmp_path: Path) -> None:
	"""mariadb idiom: canonical method on `implement Source for Pool`, no-arg
	sugar on `implement Pool`.  The consumer compiled against the published
	package must resolve both overloads, link, and run to 23.  This was the
	publish-boundary overload-merge CORE_BUG."""
	assert _emit_consume_run(tmp_path, "ifov", _IFACE_LIB, _IFACE_APP) == _EXPECT
