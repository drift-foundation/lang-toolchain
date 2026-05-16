# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pin: qualified variant constructor `Foo::Ctor()` fails to
resolve when called from inside a function with a type parameter,
even though identical syntax resolves cleanly in non-generic
functions in the same module.

## The bug

Discovered while writing the `std.concurrent.Condvar` e2e suite
(2026-05-15).  `stdlib/std/concurrent/concurrent.drift`'s
`fn _wait_inner<T>(...)` calls
`atomic.MemoryOrder::Acquire()` / `::Release()` / `::AcqRel()`.
The compiler rejects these with:

    error: no matching overload for function 'Acquire' with args []
    [E-AUTO-3e8a49cf]

The very same `atomic.MemoryOrder::Acquire()` syntax resolves
cleanly in `Condvar.signal_all()` / `Condvar.close()` /
`_claim_one()` / `_drain_active()` / `_prune_inactive()` — all of
which are **non-generic** functions in the same file.

Confirmed by manual bisection:

  - non-generic fn → ok
  - same expression in `fn _wait_inner<T>(...)` → fails
  - removing the local `pub type MemoryOrder = atomic.MemoryOrder;`
    re-export does NOT fix the generic-fn case
  - using `sync.MemoryOrder::Acquire()` (via `import std.sync`) also
    fails in the same generic-fn context, even though identical
    `sync.MemoryOrder::AcqRel()` works in `std.log::_drain_locked`
    (a non-generic fn)

## Minimal reproduction

This pin builds a synthetic two-function module:

  - `non_generic()` — calls `atomic.MemoryOrder::Acquire()` and
    compiles cleanly.
  - `generic<T>()` — identical body and call, fails with
    `E-AUTO-3e8a49cf no matching overload`.

If both functions resolve, the bug is fixed.  If only the generic
form fails, the bug reproduces.

## Why it matters

Blocks `std.concurrent.Condvar` (every wait/signal path needs
explicit MemoryOrder).  More generally: ANY generic function in
ANY user module that uses a qualified variant constructor on a
re-exported variant type will hit this.  Common shapes:
`Optional::None()`, `Result::Err()`, user-defined sum types.

## Expected after fix

Resolver should treat `atomic.MemoryOrder::Acquire()` identically
inside generic and non-generic functions.  Both arms should
resolve to the same variant constructor.  When fixed, this test
flips from "expects E-AUTO-3e8a49cf error" to "compiles cleanly
and exits 0."
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


_REPRO = """\
module m;
import lang.atomic as atomic;

fn non_generic() nothrow -> Bool {
	val ab = atomic.atomic_bool(false);
	return ab.load(atomic.MemoryOrder::Acquire());
}

fn generic<T>() nothrow -> Bool {
	val ab = atomic.atomic_bool(false);
	return ab.load(atomic.MemoryOrder::Acquire());
}

pub fn main() nothrow -> Int {
	val a = non_generic();
	val b = generic<type Int>();
	if a { return 1; }
	if b { return 2; }
	return 0;
}
"""


# K-spec'd variant-via-alias minimal repro (separate fixture).
# Covers the case where a `pub type` alias lives in the SAME module
# as the variant and qualified-member ctor resolution must go
# through the alias.  Decoupled from the lang.atomic-import shape
# so a fix can be tested orthogonally.
_REPRO_LOCAL_ALIAS = """\
module m;

pub variant E {
	A,
	B
}

pub type Alias = E;

fn via_original() nothrow -> E {
	return E::A();
}

fn via_alias_same_module() nothrow -> Alias {
	return Alias::A();
}

pub fn main() nothrow -> Int {
	val x = via_alias_same_module();
	val y = via_original();
	return 0;
}
"""


# K-spec'd imported-alias minimal repro (separate fixture).
# Covers the case where the variant is in module A, a `pub type`
# alias is in module B, and a user file imports B and calls
# `Alias::Ctor()`.  Decoupled from the generic-fn shape so we can
# isolate the alias-only failure mode.
_REPRO_GENERIC_IMPORTED_ALIAS = """\
module m;
import std.sync as sync;

fn use_in_generic<T>() nothrow -> sync.MemoryOrder {
	return sync.MemoryOrder::Acquire();
}

pub fn main() nothrow -> Int {
	val mo = use_in_generic<type Int>();
	return 0;
}
"""


def _compile_subprocess(src_text: str, tmp_path: Path) -> subprocess.CompletedProcess:
	"""Run driftc as a subprocess with `-o` so the full pipeline
	(checker → MIR lowering → codegen) runs and any generic-fn
	instantiation surfaces.  `--json` mode short-circuits before
	generic instantiation and hides the bug — see notes in the
	module docstring.

	Returns the CompletedProcess for assertion against returncode
	and stdout+stderr."""
	src = tmp_path / "main.drift"
	src.write_text(src_text, encoding="utf-8")
	out_bin = tmp_path / "out"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--dev",
		"--entry", "m::main",
		str(src),
		"-o", str(out_bin),
	]
	root = stdlib_root()
	if root:
		cmd.insert(-2, "--stdlib-root")
		cmd.insert(-2, str(root))
	return subprocess.run(
		cmd,
		cwd=Path(__file__).parents[3],
		capture_output=True,
		text=True,
		timeout=120,
	)


def test_qualified_variant_ctor_resolves_in_generic_function(tmp_path: Path) -> None:
	"""Regression pin (post-fix, 2026-05-15): qualified variant ctor
	calls (`atomic.MemoryOrder::Acquire()`) MUST resolve cleanly
	inside generic function bodies, EXACTLY as they do in non-generic
	siblings.

	## History (pre-fix bug)

	The qmem-branch in `call_resolver.py::resolve_call_expr`
	resolved variant ctor calls during template typecheck AND
	rewrote `expr.fn` from `HQualifiedMember` to `HVar(name, module_id)`
	"to satisfy typed-mode invariants."  That rewrite lost the
	syntactic-category information needed for re-resolution.

	On generic instantiation, stage1 `normalize_hir` rebuilds HCall
	nodes (BorrowMaterializeRewriter / PlaceCanonicalizeRewriter
	construct new `H.HCall(...)` objects with copied named fields,
	dropping dynamic resolved-cache attrs).  The instantiation pass
	then saw `expr.fn = HVar` with no cache → the HVar branch fell
	through to free-function lookup → `Acquire` isn't a free function
	→ `E-AUTO-3e8a49cf no matching overload for function 'Acquire'`.

	The bug fired ONLY at method-call ARGUMENT positions inside
	generic-fn bodies (not return position, not free-call arg).

	## Fix

	`call_resolver.py:5256` removed the `expr.fn = H.HVar(...)`
	rewrite.  `expr.fn` stays `HQualifiedMember` so re-resolution
	during instantiation re-enters the qmem branch and resolves
	cleanly.  Resolved-cache attrs (`_resolved_ctor_info` /
	`_resolved_ctor_return`) are still set for in-pass downstream
	consumers but are no longer the load-bearing fast-path for
	ctor calls across instantiation.

	## What this pin proves

	`generic<T>()` calls `atomic.MemoryOrder::Acquire()` in the
	exact bug shape (method-call arg position inside a generic-fn
	body).  Compile succeeds + binary returns 0.
	"""
	res = _compile_subprocess(_REPRO, tmp_path)
	assert res.returncode == 0, (
		"post-fix: qualified variant ctor resolution inside generic-fn "
		"bodies must compile cleanly.\n"
		+ (res.stdout + res.stderr)[-2000:]
	)
	# Find the compiled binary alongside the source.
	out_bin = tmp_path / "out"
	if not out_bin.exists():
		# `out` may have an extension stripped by driftc; locate it.
		out_bins = list(tmp_path.glob("out*"))
		out_bin = next((p for p in out_bins if p.is_file() and p.stat().st_mode & 0o111), out_bin)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=30)
	assert run.returncode == 0, (
		f"binary should exit 0 (both call sites return false); got {run.returncode}\n"
		f"stdout: {run.stdout}\nstderr: {run.stderr}"
	)


def test_qualified_variant_ctor_resolves_in_non_generic_function(tmp_path: Path) -> None:
	"""Positive sibling: identical `atomic.MemoryOrder::Acquire()`
	syntax in a NON-generic function compiles cleanly.  Pins the
	bisected finding that the bug is genuinely generic-fn-context
	scoped, not a global resolver issue.

	If this test starts failing, the bug has spread beyond the
	originally-bisected scope and the primary pin's assumption
	'`non_generic()` works' is wrong."""
	src_only_non_generic = """\
module m;
import lang.atomic as atomic;

fn non_generic() nothrow -> Bool {
	val ab = atomic.atomic_bool(false);
	return ab.load(atomic.MemoryOrder::Acquire());
}

pub fn main() nothrow -> Int {
	val a = non_generic();
	if a { return 1; }
	return 0;
}
"""
	res = _compile_subprocess(src_only_non_generic, tmp_path)
	assert res.returncode == 0, (
		"non-generic-fn call site should compile cleanly.  "
		"If this fails, the resolver bug has spread beyond the "
		"originally-bisected generic-context-only scope.\n"
		+ (res.stdout + res.stderr)[-2000:]
	)


@pytest.mark.xfail(
	strict=False,
	reason=(
		"K-spec'd imported-alias arm.  In a STANDALONE user module, "
		"`sync.MemoryOrder::Acquire()` in a generic fn currently "
		"compiles cleanly — the bug surfaces only inside "
		"stdlib/std/concurrent/concurrent.drift::_wait_inner<T>.  "
		"The in-stdlib-context trigger is not yet isolated.  Soft "
		"xfail tracks the imported-alias dimension without gating."
	),
)
def test_qualified_variant_ctor_via_imported_alias_in_generic_fn(tmp_path: Path) -> None:
	res = _compile_subprocess(_REPRO_GENERIC_IMPORTED_ALIAS, tmp_path)
	assert res.returncode != 0, "imported-alias generic-fn case is expected to fail per K-spec"


@pytest.mark.xfail(
	strict=False,
	reason=(
		"K-spec'd same-module-alias arm.  Variant + `pub type Alias "
		"= E;` in the SAME module + `Alias::A()`.  Standalone repro "
		"currently compiles cleanly; the in-stdlib trigger is not "
		"yet isolated.  Soft xfail tracks this dimension."
	),
)
def test_qualified_variant_ctor_via_same_module_alias(tmp_path: Path) -> None:
	res = _compile_subprocess(_REPRO_LOCAL_ALIAS, tmp_path)
	assert res.returncode != 0, "same-module-alias case is expected to fail per K-spec"
