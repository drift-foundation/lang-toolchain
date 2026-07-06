# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: `--emit-package` must reject ANY projected lambda capture
(`p.field`, not a whole local) with a real diagnostic, rather than silently
serializing a package whose consumers cannot correctly rebuild the closure
env (0.33.70, `fix/projected-capture-lowering`).

Root cause this pins: `--emit-package` serializes `_pre_typecheck_hirs`, a
deep-copy of the normalized HIR taken BEFORE type-checking runs. The only
capture-discovery pass that has run by that point
(`validate_lambdas_non_retaining`, called early in `driftc.py`'s
package-emit branch so `HLambda.captures` is populated before the
snapshot) is necessarily untyped — no `binding_types`/`type_table` exist
yet, AND for a lambda that will become a boxed callback, `capture_as_move`
isn't set yet either (`call_resolver.py` sets it during type-checking,
which hasn't run). So that early pass cannot make the correct decision for
a projected capture: it cannot supply the `is_copy_projected_field`
resolver the LATER, typed pass (post-typecheck) uses to downgrade a
Copy-typed projected capture from the unsupported MOVE path to a plain
COPY read, and for a not-yet-`capture_as_move` lambda it defaults an
implicit read to REF instead of MOVE regardless of what it will actually
need to be. Whatever `HLambda.captures` ends up holding at the early,
untyped pass is exactly what gets serialized into `_pre_typecheck_hirs` —
independent of whatever the later, correctly-typed pass computes on the
live (in-process) HIR afterward. A package consumer loading this HIR only
re-discovers EXPLICIT captures on load (`_pkg_hir_loaded` handling in
`driftc.py`), not implicit ones, and has no typed resolver of its own
either.

Until package serialization/loading is taught to carry the typed capture
decision through this boundary, `driftc.py` rejects ANY projected lambda
capture (`cap.key.proj` non-empty) found in `--emit-package` mode, checked
post-typecheck once the correct decision is known (so both a Copy-typed
implicit MOVE-downgraded-to-COPY capture AND a pre-existing REF-kind
projected capture are covered — the boundary problem applies to both, not
just the new 0.33.70 capability). See `doc/history.md` (0.33.70 entry) and
`work/callback-env-uaf-ref-args/REPORT-0.33.70-projected-capture-lowering.md`
§9/§10 for the full writeup, including why an earlier attempt at this fix
(making the untyped early pass's own diagnostics fatal) turned out to be
unreachable for the boxed-callback shape specifically, since
`capture_as_move` isn't known that early.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _emit(tmp_path: Path, source: str, *, module_name: str = "lib") -> tuple[int, Path]:
	_write_file(tmp_path / module_name / f"{module_name}.drift", source)
	pkg = tmp_path / f"{module_name}.dmp"
	exit_code = driftc_main(
		[
			"-M",
			str(tmp_path),
			str(tmp_path / module_name / f"{module_name}.drift"),
			"--emit-package",
			str(pkg),
			"--package-id",
			module_name,
			"--package-version",
			"0.1.0",
			"--package-target",
			"test-target",
		]
	)
	return exit_code, pkg


# Same shape as the non-package positive test
# (test_boxed_callback_projected_move_capture_rejected.py::
# test_copy_typed_projected_field_now_compiles_and_runs), which DOES
# compile in regular (non-package) mode as of 0.33.70.
_COPY_FIELD_IMPLICIT_CAPTURE_SOURCE = """\
module lib;
import std.core as core;
import std.concurrent as conc;

export { driver_handle };

struct Prepared {
\tcount: Int,
}

pub fn driver_handle(prepare: core.CallbackThrow2<Int, String, Prepared>, tk: Int, payload: String) throws -> Int {
\tvar p = prepare.call(tk, payload);
\tvar vt = conc.spawn<type Int>(core.callback0(| | => {
\t\treturn p.count + 1;
\t}));
\tmatch vt.join() {
\t\tOk(v) => { return v; },
\t\tErr(_) => { return -1; },
\t\tdefault => { return -2; }
\t}
}
"""

# Same shape as the immediate-lambda REF-projected lowering-mechanism test
# (test_projected_lambda_capture_lowering.py::
# test_ref_projected_capture_immediate_lambda_compiles_and_runs) — not a
# boxed callback, no MOVE/Copy-downgrade involved at all. Confirms the
# package-emit rejection is about the projected KEY, not specific to the
# 0.33.70 Copy-downgrade capability.
_REF_PROJECTED_IMMEDIATE_SOURCE = """\
module lib2;

export { use_it };

struct Prepared {
\tcount: Int,
}

pub fn use_it(p: Prepared) -> Int {
\treturn (| | => { return p.count + 1; })();
}
"""

# Control: a whole-local (non-projected) implicit capture must still
# compile and package cleanly — this branch's package-emit restriction is
# scoped to projected captures specifically, not implicit captures in
# general.
_WHOLE_LOCAL_IMPLICIT_CAPTURE_SOURCE = """\
module lib3;
import std.core as core;
import std.concurrent as conc;

export { driver_handle };

pub fn driver_handle(tk: Int) throws -> Int {
\tvar vt = conc.spawn<type Int>(core.callback0(| | => {
\t\treturn tk + 1;
\t}));
\tmatch vt.join() {
\t\tOk(v) => { return v; },
\t\tErr(_) => { return -1; },
\t\tdefault => { return -2; }
\t}
}
"""


def test_emit_package_rejects_implicit_copy_projected_capture(tmp_path: Path, capsys) -> None:
	"""`--emit-package` must fail with a diagnostic for an implicit
	Copy-typed projected capture inside a boxed callback, rather than
	silently serializing a package whose HIR is missing the capture."""
	exit_code, pkg = _emit(tmp_path, _COPY_FIELD_IMPLICIT_CAPTURE_SOURCE, module_name="lib")
	captured = capsys.readouterr()
	assert exit_code != 0, (
		"expected --emit-package to fail for an implicit projected capture; "
		f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
	)
	assert not pkg.exists(), "no package artifact should be written on failure"
	assert "projected lambda captures are not yet supported across package serialization" in captured.err, (
		f"expected the package-emit projected-capture rejection diagnostic, got:\n{captured.err}"
	)


def test_emit_package_rejects_ref_projected_capture_non_boxed(tmp_path: Path, capsys) -> None:
	"""The same rejection applies to a pre-existing REF-kind projected
	capture in a plain (non-boxed-callback) immediate-invoked lambda — the
	package-serialization boundary problem is about the projected KEY, not
	specific to the new Copy-downgrade capability."""
	exit_code, pkg = _emit(tmp_path, _REF_PROJECTED_IMMEDIATE_SOURCE, module_name="lib2")
	captured = capsys.readouterr()
	assert exit_code != 0, (
		"expected --emit-package to fail for a REF-projected capture; "
		f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
	)
	assert not pkg.exists(), "no package artifact should be written on failure"
	assert "projected lambda captures are not yet supported across package serialization" in captured.err, (
		f"expected the package-emit projected-capture rejection diagnostic, got:\n{captured.err}"
	)


def test_emit_package_whole_local_capture_still_compiles(tmp_path: Path, capsys) -> None:
	"""Control: a whole-local (non-projected) implicit capture must still
	compile and package successfully — this restriction is scoped to
	projected captures, not implicit captures in general."""
	exit_code, pkg = _emit(tmp_path, _WHOLE_LOCAL_IMPLICIT_CAPTURE_SOURCE, module_name="lib3")
	captured = capsys.readouterr()
	assert exit_code == 0, (
		f"expected --emit-package to succeed for a whole-local capture; "
		f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
	)
	assert pkg.exists(), "package artifact should be written on success"
