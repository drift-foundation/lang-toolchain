# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression: type_checker.py:5656 — `_resolve_struct_field_type`
crashes with NameError on generic-impl method bodies that call a method
on a typevar-typed field.

**Root cause.**  `_resolve_struct_field_type` is nested inside
`type_expr` (around `lang/driftc/type_checker.py:5624`), which is itself
nested inside `check_function`.  `type_expr` rebinds `sig` at multiple
points (e.g. `:6816 sig = None`, `:6818 sig = signatures_by_id.get(...)`,
`:8266 sig = signatures_by_id.get(fn_id_local)`) without declaring
`nonlocal sig`.  Python's scoping rule: any assignment to `sig` inside
`type_expr` makes `sig` a local of `type_expr`.  The free-variable
reference at `:5656` then resolves through the `type_expr` cell, which
is unbound until the FIRST rebind executes.

The path through `:5656` is gated by:
  - the prior `struct_field` lookup either returned None or a
    field with kind=UNKNOWN, AND
  - `schema.type_params` is non-empty (the struct is generic).

Triggered by user source: a generic struct's typevar-typed field
read in method-call position (`self.value.<trait_method>()`) inside a
generic impl method.  The method-resolution path
(`lang/driftc/checker/call_resolver.py:resolve_method_call` →
`type_expr` → `_resolve_struct_field_type`) hits this branch BEFORE
any of the `sig = ...` rebindings inside `type_expr` have run on this
typecheck visit, so the free-variable cell is unbound and Python
raises NameError.

**Fix (companion patch).**  In `_resolve_struct_field_type`, use the
closure-stable `fn_sig` (assigned at `check_function:1629` and never
rebound inside `type_expr`) instead of the locally-shadowed `sig`.
`fn_sig` carries the same value (the function being checked) and
isn't subject to type_expr's local-rebind shadowing.  Do NOT add
`nonlocal sig` to type_expr — that would let nested call-resolution
assignments mutate the current-function signature view.

This regression is standalone — no ConstShare / synthesis involvement.
The same path is also reached by ConstShare structural synthesis
(Phase 3 generic structs); the Phase 3 positives in
`test_const_share_phase3_generics.py` provide a second carrier.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[dict]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


def test_generic_impl_typevar_field_method_call_typechecks(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Pin: a generic-impl method that calls a trait method on a
	typevar-typed field type-checks cleanly.

	The source is well-formed Drift: `Box<T>` requires `T is
	ConstShare`, and `self.value.const_share()` resolves through
	the require constraint to the trait method.  Returns Void.

	Before the LANGUAGE_BUG fix, this raised an uncaught Python
	NameError ("cannot access free variable 'sig' where it is not
	associated with a value in enclosing scope") at
	`lang/driftc/type_checker.py:5656` during method-call
	resolution on the typevar field — pytest surfaced it as ERROR,
	not FAILED.

	After the fix, the source compiles to `rc == 0` with no
	diagnostics.  Asserting `rc == 0` is the stronger pin: it
	catches both the original NameError AND any future regression
	that re-breaks generic-impl typevar-field method-call type
	checking.
	"""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;
import std.core.shareable as shareable;

use trait shareable.ConstShare;

pub struct Box<T> require T is shareable.ConstShare {
\tpub value: T
}

implement<T> Box<T> require T is shareable.ConstShare {
\tpub fn touch(self: &Box<T>) nothrow -> Void {
\t\tval _ = self.value.const_share();
\t\treturn ;
\t}
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	assert rc == 0, (
		"generic-impl typevar-field method call must type-check "
		f"cleanly: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)
