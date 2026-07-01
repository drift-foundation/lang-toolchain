# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Arc relocation — `Arc<T>` and `arc()` moved from `std.concurrent`
to `std.core` at ABI 11.

This file pins both endpoints of the relocation:

  - **New canonical spelling**: `core.Arc<T>` / `core.arc(value)`
    via `import std.core as core` resolves to the type/function
    declared in `std.core.arc`.
  - **Source-compat spelling**: `conc.Arc<T>` / `conc.arc(value)`
    via `import std.concurrent as conc` continues to compile and
    refer to the SAME type identity (the submodule is re-exported
    from `std.concurrent` via `export { std.core.arc.* };`).

Why the move.  Arc is an ownership primitive — its semantic
contract is shared ownership, not concurrency.  The current
implementation uses atomic refcounts, but that is a runtime
mechanism, not the contract.  Hosting Arc in `std.core` keeps the
public ownership surface stable across future implementation
shifts (single-thread non-atomic refcounts, hybrid retain
strategies, compiler-owned retains, platform-specific atomics).
See `feedback_module_classification_by_contract.md`.

If any test in this file fails, the relocation has regressed at
the source layer.  The compiler-side recognizers (intrinsic
dispatch in `lang/driftc/parser/__init__.py` and the fat-Arc helper
lookups in `lang/driftc/stage2/hir_to_mir.py` /
`lang/driftc/driftc.py`) all key on `std.core.arc` post-ABI-11;
old `std.concurrent`-keyed recognition was deleted in the same
commit that ships this test file.

Out of scope for this file: package-mode binary compatibility.
ABI 11 forces consumers to rebuild against the new compiler;
`.dmp` files produced under ABI 10 are rejected by the link-time
ABI guard rather than silently mapped.  See the ABI-mismatch
regression in `lang/tests/packages/`."""
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


# ── New canonical spelling: `core.Arc` / `core.arc` ──────────────


def test_arc_via_std_core_construct_clone_get(tmp_path, capsys):
	"""The new canonical access path: `import std.core as core;
	core.arc(value)` produces an `Arc<T>` whose `clone` and `get`
	intrinsic methods resolve correctly.

	If this fails, the `std.core.arc.*` re-export from
	`stdlib/std/core/core.drift` regressed, OR the parser's
	intrinsic-recognition gate (which now expects
	`module_id == \"std.core.arc\"`) regressed."""
	rc, errs = _compile(tmp_path, capsys, """
module main;
import std.core as core;
pub fn main() nothrow -> Int {
\tvar a = core.arc<type Int>(42);
\tvar b = a.clone();
\tval v: Int = *a.get();
\tval w: Int = *b.get();
\treturn v + w;
}
""")
	assert rc == 0, f"core.Arc<Int> construct + clone + get must compile: rc={rc}, errs={errs}"


def test_arc_via_std_core_with_string(tmp_path, capsys):
	"""Heap-bearing payload through the new spelling — the per-T
	drop thunk resolves via the in-place declarations of
	`_arc_drop_thunk_for<T>` and `drop_value` in `std.core.arc`."""
	rc, errs = _compile(tmp_path, capsys, """
module main;
import std.core as core;
pub fn main() nothrow -> Int {
\tvar a = core.arc<type String>("hello");
\tvar b = a.clone();
\tval r = a.get();
\treturn r.byte_length();
}
""")
	assert rc == 0, f"core.Arc<String> must compile: rc={rc}, errs={errs}"


# ── Source-compat: old spelling still compiles ──────────────────


def test_arc_via_std_concurrent_compat(tmp_path, capsys):
	"""Pre-existing user code that imports `std.concurrent` and
	writes `conc.Arc<T>` / `conc.arc(value)` must keep compiling.
	The re-export `export { std.core.arc.* };` in
	`stdlib/std/concurrent/concurrent.drift` is what preserves
	source-level compat; type identity is unchanged because
	re-export does not produce a new nominal type."""
	rc, errs = _compile(tmp_path, capsys, """
module main;
import std.concurrent as conc;
pub fn main() nothrow -> Int {
\tvar a = conc.arc<type Int>(42);
\tvar b = a.clone();
\tval v: Int = *a.get();
\tval w: Int = *b.get();
\treturn v + w;
}
""")
	assert rc == 0, f"`conc.arc(...)` source-compat must hold: rc={rc}, errs={errs}"


def test_arc_old_and_new_spelling_share_type_identity(tmp_path, capsys):
	"""Cross-spelling assignment must type-check: an `Arc<T>` built
	via `core.arc(...)` is assignable to a binding declared as
	`conc.Arc<T>` (and vice-versa).  This proves both names refer
	to the same `NominalKey(package_id=\"std\", module=\"std.core.arc\",
	name=\"Arc\")`."""
	rc, errs = _compile(tmp_path, capsys, """
module main;
import std.core as core;
import std.concurrent as conc;
pub fn main() nothrow -> Int {
\tval new_form: core.Arc<Int> = core.arc<type Int>(1);
\tval old_form: conc.Arc<Int> = conc.arc<type Int>(2);
\tval cross_a: conc.Arc<Int> = core.arc<type Int>(3);
\tval cross_b: core.Arc<Int> = conc.arc<type Int>(4);
\treturn *new_form.get() + *old_form.get() + *cross_a.get() + *cross_b.get();
}
""")
	assert rc == 0, (
		f"`core.Arc<T>` and `conc.Arc<T>` must be the same nominal "
		f"type (re-export, not alias): rc={rc}, errs={errs}"
	)


# ── Stdlib internal callers continue working ─────────────────────


def test_log_resolver_uses_arc(tmp_path, capsys):
	"""`std.log.LoggerConfigBuilder.resolver` has a
	`conc.Arc<ContextResolver>` field — relocation must not break
	this stdlib internal."""
	rc, errs = _compile(tmp_path, capsys, """
module main;
import std.log as log;
pub fn main() nothrow -> Int {
\tval cfg = log.config_builder();
\treturn 0;
}
""")
	assert rc == 0, f"std.log Arc usage must keep compiling: rc={rc}, errs={errs}"
