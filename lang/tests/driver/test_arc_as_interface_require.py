# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Stage 1 regression: `Arc<T>.as_interface<I>()` is declared as an
`@intrinsic` method with `require T is I`.  In Stage 1 its lowering
is not yet implemented — the purpose of this test is to pin the
compile-time soundness gate so Stage 3 can safely add the runtime
without losing the negative-case diagnostic.

Positive calls intentionally omitted at this stage: they would
typecheck and then fail at MIR/LLVM lowering with an unhelpful
"missing intrinsic lowering" message.  Those land in Stage 3.

See `docs/history.md` 2026-04-18 (fat `Arc<Interface>` 0.28.0,
ABI 10) for the full cutover context.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(
	tmp_path: Path, source: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict]:
	mod_root = tmp_path / "mods"
	_write_file(mod_root / "main" / "main.drift", source)
	root = stdlib_root()
	args = [
		"-M", str(mod_root),
		str(mod_root / "main" / "main.drift"),
		"--dev",
		"--json",
	]
	if root:
		args += ["--stdlib-root", str(root)]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


_NEGATIVE_AS_INTERFACE_UNSATISFIED = """
module main;

import std.concurrent as conc;

pub interface Speaker {
	fn speak(self: &Self) nothrow -> Int;
}

pub struct Quiet {
	pub n: Int
}

// Quiet does NOT implement Speaker.

fn run() nothrow -> Int {
	val a = conc.arc(Quiet(n = 7));
	val _ = a.as_interface<type Speaker>();
	return 0;
}

fn main() nothrow -> Int {
	return try run() catch { 99 };
}
""".lstrip()


def test_arc_as_interface_rejects_unimplemented_interface(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Compile-time gate: `arc.as_interface<type I>()` on an `Arc<T>`
	where T does not implement I must be rejected via the standard
	`require T is I` diagnostic path (`E_REQUIREMENT_NOT_SATISFIED`),
	not as 'unknown method' or 'missing intrinsic' or any other
	shape.  This pins the soundness gate regardless of whether the
	runtime lowering is implemented yet.

	The diagnostic must name the substituted target interface
	(Speaker), not the declaration-local method-type-param `I` — the
	method-level substitution fix makes the require solver see
	Speaker at the call site.  If the message still says `...I`
	somewhere, method-level substitution regressed.
	"""
	rc, payload = _run_driftc_json(tmp_path, _NEGATIVE_AS_INTERFACE_UNSATISFIED, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc != 0, (
		f"expected compile failure — Quiet does not implement Speaker, "
		f"so `arc.as_interface<type Speaker>()` must be rejected.  "
		f"Got rc={rc}, diagnostics={diagnostics}"
	)
	req_errors = [
		e for e in errors
		if e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "Quiet" in (e.get("message") or "")
		and "Speaker" in (e.get("message") or "")
		and "as_interface" in (e.get("message") or "")
	]
	assert req_errors, (
		f"expected E_REQUIREMENT_NOT_SATISFIED naming Quiet + Speaker "
		f"(substituted target) + as_interface origin; got: {errors}"
	)
	for e in req_errors:
		msg = e.get("message") or ""
		assert not msg.rstrip().endswith(" is I"), (
			f"diagnostic still ends with bare 'is I' — method-level type "
			f"param not substituted: {msg}"
		)
