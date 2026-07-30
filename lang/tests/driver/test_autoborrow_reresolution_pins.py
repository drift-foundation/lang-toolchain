# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pins for two subtle reject-redundant-call-borrows corpus fixes
(review round 4, item 4).

1. STALE-ARG-TYPES IDEMPOTENCY in `_apply_autoborrow_args`
   (type_checker.py): re-resolution passes can hand the helper stale
   `arg_types` against the already-mutated `args` list.  Two coercion
   branches must skip when the slot's node already types to the formal:

   - the symmetric `&T→T` branch — regressed as "deref requires a
     reference value" on the second pass wrapping a second deref
     around its own first (for-in over a Copy by-value iterable is
     the natural trigger: the desugar resolves `Iterable::iter`
     through the assoc/trait-qualified path, which re-enters);
   - the nested `&&T→&T` branch — regressed as "cannot copy value of
     type 'Array<…>'" (the second deref reached INTO the pointee; a
     for-in over a `&Array<NonCopy>` binding is the natural trigger).

   Designated e2e run-carriers: `for_in_byvalue_copy_local_reuse` (and
   siblings) for the symmetric branch; `ref_array_jsonnode_usage_matrix`
   / `for_iter_json_expect_array` for the nested branch.  The driver
   rows here are the MINIMIZED compile+run forms so the regression is
   caught close to the mechanism, without std.json in the loop.

2. BORROW-INFERENCE HEAD SELECTION in `_borrow_infer_arg_types`
   (checker/call_resolver.py): for a typevar-bearing declared-ref
   formal and a bare non-ref argument,

   - SAME head constructor  → plain parameter-directed auto-borrow
     (`&mut MutexGuard<T>` ← `MutexGuard<Counter>`-style; regression
     direction: E-INFER-CONFLICT if the Borrow-trait view wins);
   - MISMATCHED head        → the Borrow-TRAIT view must be retained
     (`lock<T>(m: &Mutex<T>)` ← `Arc<Mutex<Counter>>`; regression
     direction: E-INFER-CONFLICT if plain auto-borrow wins — the
     corpus flip class fixed in round 4).

   Designated e2e run-carriers: `callback_move_capture_replace_state`
   and siblings (mismatched head via `conc.lock(captured)`).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _build_and_run(tmp_path: Path, src_text: str) -> None:
	src = tmp_path / "main.drift"
	src.write_text(src_text)
	out = tmp_path / "x.bin"
	comp = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert comp.returncode == 0, (comp.stderr + comp.stdout)[-1500:]
	run = subprocess.run(
		[str(out)], capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	assert run.returncode == 0, f"rc={run.returncode}\n{run.stderr[-800:]}"


def test_symmetric_ref_to_value_coercion_idempotent_under_reresolution(
	tmp_path: Path,
) -> None:
	"""for-in over a Copy by-value Iterable, twice: `Iterable::iter`
	resolves through the assoc path whose re-entry carries stale
	arg_types.  Pre-fix: "deref requires a reference value"."""
	_build_and_run(tmp_path, """\
module main;
import std.iter as iter;
use trait iter.SinglePassIterator;

pub variant V { Done, Count(n: Int) }
implement iter.SinglePassIterator<Int> for V {
	pub fn next(self: &mut V) nothrow -> Optional<Int> {
		match self {
			V::Count(n) => {
				if *n <= 0 { return Optional<Int>::None(); }
				val c = *n;
				*n = *n - 1;
				return Optional::Some(c);
			},
			default => { return Optional<Int>::None(); }
		}
	}
}
implement iter.Iterable<V, Int, V> for V {
	pub fn iter(var self: V) nothrow -> V { return move self; }
}

pub fn main() nothrow -> Int {
	val v = V::Count(3);
	var s1 = 0;
	for x in v { s1 = s1 + x; }
	var s2 = 0;
	for y in v { s2 = s2 + y; }
	if s1 + s2 != 12 { return 1; }
	return 0;
}
""")


def test_nested_double_ref_coercion_idempotent_under_reresolution(
	tmp_path: Path,
) -> None:
	"""for-in over a `&Array<NonCopy>` param plus a `&&Array` argument
	both drive the `&&T→&T` deref coercion through re-resolution.
	Pre-fix: "cannot copy value of type 'Array<Handle>'"."""
	_build_and_run(tmp_path, """\
module main;
import std.core as core;

pub struct Handle { pub raw: Int }
implement core.Destructible for Handle {
	pub fn destroy(var self: Handle) nothrow -> Void { return; }
}

fn count(xs: &Array<Handle>) nothrow -> Int {
	var n = 0;
	for val item : xs {
		val _ = item;
		n = n + 1;
	}
	return n;
}

pub fn main() nothrow -> Int {
	var a: Array<Handle> = [];
	a.push(Handle(raw = 1));
	a.push(Handle(raw = 2));
	// for-in inside `count` derefs the &&Array iter receiver.
	if count(a) != 2 { return 1; }
	// Nested-ref argument: &&Array<Handle> at a &Array<Handle> formal.
	val r: &Array<Handle> = &a;
	val rr = &r;
	if count(rr) != 2 { return 2; }
	return 0;
}
""")


def test_borrow_infer_same_head_prefers_plain_autoborrow(
	tmp_path: Path,
) -> None:
	"""`inspect<T>(a: &Arc<T>)` with a bare `Arc<Int>` argument: same
	head constructor, so plain auto-borrow must serve inference
	(T := Int via `&Arc<Int>`).  LOAD-BEARING because Arc<T> also has a
	COMPETING `Borrow<T>` view (`Arc<Int>.borrow() → &Int`): if the
	same-head preference is lost, the Borrow-trait rewrite offers
	`&Int` against `&Arc<T>` and inference fails (round-5 finding: a
	Borrow-less carrier type could pass through the independent
	declared-ref peel in `_infer` even with the preference broken)."""
	_build_and_run(tmp_path, """\
module main;
import std.concurrent as conc;
import std.core as core;

fn inspect<T>(a: &core.Arc<T>) nothrow -> Int { return 1; }

pub fn main() nothrow -> Int {
	var a = conc.arc(7);
	if inspect(a) != 1 { return 1; }
	return 0;
}
""")


def test_borrow_infer_mismatched_head_retains_borrow_trait_view(
	tmp_path: Path,
) -> None:
	"""`conc.lock<T>(m: &Mutex<T>)` with a bare `Arc<Mutex<Int>>`
	argument: mismatched head, so the Borrow-TRAIT view must be
	retained (`Arc.borrow() → &Mutex<Int>`, T := Int).  Regression
	direction: E-INFER-CONFLICT if plain auto-borrow wraps the raw
	Arc (the round-4 corpus flip class)."""
	_build_and_run(tmp_path, """\
module main;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
	var a = conc.arc(conc.mutex(41));
	{
		var g = conc.lock(a);
		if *g.get_mut() != 41 { return 1; }
	}
	var g2 = conc.lock(a);
	if *g2.get_mut() != 41 { return 2; }
	return 0;
}
""")
