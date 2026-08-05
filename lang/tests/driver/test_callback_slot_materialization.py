# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Callback slots construct the canonical wrapper BEFORE typing the lambda.

Typing a bare `HLambda` against a Callback interface expectation first
was unsound across re-check passes: the callback context stamped
`allow_capture_invoke` on the shared node, a later pass short-circuited
to an interface LABEL with no wrapper, label equality skipped the
post-typing wrap branch, and MIR received a raw HLambda under an
interface-typed binding — an ICE ("raw HLambda reached HLet lowering").
The fix pre-wraps at the slot sites (typed-let and return position): the
canonical `core.callbackN(...)` call is constructed and spliced FIRST,
and the lambda is only ever typed INSIDE that construction.

MIR's callback construction is static-only in v1 (fnptr const or lambda
literal).  For fn-typed HVar wrap args, the checker records the finalized
static `(fn_ref, call_sig)` per binding and `_implicit_callback_wrap`
splices the CONSTANT — including across transparent immutable alias hops
(`val g = f`; review-2026-08-05T03-42-22Z P1-3).  The `__lambda_fn_`
symbol in the emitted IR is the structural witness that the wrapper holds
the static fnptr, not a runtime fn-typed variable read.

A REJECTED pre-wrap (explicit borrowed captures) binds the slot as a
poisoned Unknown WITH the rejection as its recorded cause, so later uses
add no cascade (P1-4).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _diag_compile(tmp_path: Path, capsys, source: str) -> list[str]:
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	main_path.parent.mkdir(parents=True, exist_ok=True)
	main_path.write_text(source)
	driftc_main(["-M", str(root), str(main_path), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return [d.get("message", "") for d in payload.get("diagnostics", [])]


def _build_run(tmp_path: Path, source: str) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = tmp_path / "repro"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc", str(src),
		"--entry", "repro::main", "--target-word-bits", "64", "-o", str(out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	build = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240))
	err = build.stdout + build.stderr
	assert build.returncode == 0, err
	assert "Traceback" not in err, err
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, (run.returncode, run.stderr)
	return out


def test_typed_let_callback_slot_wraps_bare_lambda(tmp_path: Path) -> None:
	# The ICE shape: pre-fix a re-check pass bypassed the wrap and MIR
	# aborted on the raw HLambda.
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
pub fn main() nothrow -> Int {
	val cb: core.Callback1<Int, Int> = | x: Int | => { x + 1 };
	return cb.call(6) - 7;
}
""",
	)


def test_return_position_callback_slot_wraps_bare_lambda(tmp_path: Path) -> None:
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
fn make() nothrow -> core.Callback0<Int> {
	return | | => { 7 };
}
pub fn main() nothrow -> Int {
	val cb = make();
	return cb.call() - 7;
}
""",
	)


def test_pending_alias_into_callback_slot(tmp_path: Path) -> None:
	# Finalized fn-typed HVar in a Callback slot: the wrap splices the
	# recorded static fnptr const (MIR static-only boundary holds).
	out = _build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
pub fn main() nothrow -> Int {
	val f = | x: Int | => { x + 1 };
	val cb: core.Callback1<Int, Int> = f;
	return cb.call(6) - 7;
}
""",
	)
	ll = (out.parent / (out.name + ".ll")).read_text()
	assert "__lambda_fn_" in ll


def test_alias_hop_preserves_static_fnptr(tmp_path: Path) -> None:
	# P1-3 pin: `val g = f` must carry f's finalized (fn_ref, call_sig)
	# so the Callback consumer of g still splices the static constant.
	out = _build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
pub fn main() nothrow -> Int {
	val f = | x: Int | => { x + 1 };
	val g = f;
	val cb: core.Callback1<Int, Int> = g;
	return cb.call(6) - 7;
}
""",
	)
	ll = (out.parent / (out.name + ".ll")).read_text()
	assert "__lambda_fn_" in ll


def test_alias_chain_preserves_static_fnptr(tmp_path: Path) -> None:
	# Longer transparent chain: provenance propagates binding-to-binding.
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
pub fn main() nothrow -> Int {
	val f = | x: Int | => { x + 1 };
	val g = f;
	val h = g;
	val cb: core.Callback1<Int, Int> = h;
	return cb.call(6) - 7;
}
""",
	)


def test_bare_fn_arg_to_callback_param(tmp_path: Path) -> None:
	# Argument-position wrap of a finalized fn-typed binding.
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
	return cb.call(6);
}
pub fn main() nothrow -> Int {
	val f = | x: Int | => { x + 1 };
	return take_cb(f) - 7;
}
""",
	)


def test_rejected_wrap_poisons_binding_no_cascade(tmp_path: Path, capsys) -> None:
	# P1-4 pin: the borrowed-capture rejection is the binding's recorded
	# cause; a later copy use AND a later method-call use add nothing.
	msgs = _diag_compile(
		tmp_path,
		capsys,
		"""
module m_main;
import std.core as core;
pub fn main() nothrow -> Int {
	var y = 1;
	val cb: core.Callback0<Int> = | | captures(&y) => { y };
	val z = cb;
	cb.call();
	return 0;
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "closures with borrowed captures are non-escaping" in msgs[0], msgs
