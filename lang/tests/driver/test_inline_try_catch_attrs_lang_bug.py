# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression — inline expression-form `try expr
catch <Pattern>(e) { ... e.attrs[k].as_*() ... }` returns wrong
attribute values.

**Symptom.**  In the inline expression-form try/catch, attribute
lookup against the catch-bound `e` does not return the value
that was set at the throw site.  Specifically,
`dv.as_int()` returns `None` even when the throw declared the
field as `idx = 99`.

**Status.**  Pinned as a strict xfail.  Discovered while
scaffolding Phase 2 (DV→JSON diagnostics-context migration)
tests.  NOT in scope for that migration — the inline-form
catch-binder is structurally orthogonal to the throw-side params
projection.  The statement-form try/catch (`try { ... } catch
<Pattern>(e) { return ...; }`) returns correct attribute values,
so Phase 2 tests use that form throughout.

**Acceptance.**  When the underlying bug is fixed (likely a
catch-binder ownership/aliasing issue in the inline-expression
HIR→MIR lowering or an HExpressionTryCatch-specific narrowing
pass), this xfail flips to passing automatically and the strict
flag enforces removal of the decorator.

**Owner.**  Separate from the diagnostics-context branch.  Track
under `inline-try-catch-attrs-binder` in the bug ledger.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _build_run(tmp_path: Path, source: str) -> tuple[int, str, str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "test_bin"
	env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
	env["PYTHONPATH"] = str(ROOT)
	build = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang.driftc.driftc",
			"--stdlib-root",
			str(ROOT / "stdlib"),
			str(src),
			"--entry",
			"main::main",
			"-o",
			str(out_bin),
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=120,
		env=env,
	)
	if build.returncode != 0:
		return (build.returncode, build.stdout, build.stderr)
	run = subprocess.run(
		[str(out_bin)],
		capture_output=True,
		text=True,
		timeout=30,
	)
	return (run.returncode, run.stdout, run.stderr)


@pytest.mark.xfail(
	strict=True,
	reason=(
		"LANGUAGE_BUG: inline expression-form `try expr catch "
		"<Pattern>(e) { e.attrs[k].as_*() }` returns None for the "
		"field even though the throw site set it.  Statement-form "
		"try/catch on the same shape returns the correct value, so "
		"the bug is local to the inline-expression catch-binder "
		"path.  When fixed, this xfail flips to passing automatically."
	),
)
def test_inline_try_catch_attrs_lookup_returns_correct_value(tmp_path):
	"""Minimal repro of the inline-form catch-binder attrs bug.
	Same shape as the proven-working
	`exception_string_attr_concat_double_catch_no_corruption` e2e
	test, but expressed as an inline `try expr catch P(e) {
	e.attrs[k] }` — which returns the wrong value."""
	source = """
module main;

import std.core as core;
import std.console as console;
import std.format as format;

pub exception PathErr(payload: String, idx: Int);

fn _do_throw() throws -> Int {
\tthrow PathErr(payload = "tag.value", idx = 99);
}

pub fn main() nothrow -> Int {
\tval result: Int = try _do_throw() catch PathErr(e) {
\t\tval dv = e.attrs["idx"];
\t\tmatch dv.as_int() {
\t\t\tSome(v) => { v },
\t\t\tNone => { -1 }
\t\t}
\t} catch e {
\t\t-2
\t};
\tconsole.println(format.format_int(result));
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	assert rc == 0, (
		f"compile/run failure: rc={rc}\n"
		f"stdout:\n{stdout[:1500]}\n"
		f"stderr:\n{stderr[:1500]}"
	)
	# Expected (when fix lands): 99.  Currently returns -1 (None
	# match) due to the inline-form catch-binder bug.
	assert stdout.strip() == "99", (
		f"inline-form catch-binder returned wrong value: got {stdout!r}, "
		f"expected '99'.\n"
		f"stderr:\n{stderr[:1500]}"
	)
