# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pending-lambda finalization is ONE total outcome across all consumers.

A stored lambda (`val f = |...| => ...`) registers as PENDING and is
finalized by exactly one classifier (`_classify_and_type_pending`) from
whichever consumer reaches it first: an ordinary HVar VALUE USE (alias,
return, argument, move/borrow subject), a direct HCall callee, a direct
HInvoke callee, or the end-of-function drain.  The outcome contract:

- capturing: the ONE approved v1 primary (bare-storage / borrowed
  non-escaping) — the lambda is never typed, no capture effects begin;
- unconstrained with no contextual shape: one clean cannot-infer primary;
- inferable: typed exactly once, concrete thin `Fn` type installed and
  the static fnptr const recorded for later Callback wraps;
- residual Unknown component: POISONED Unknown binding, and the
  `LambdaFnSpec`/fnptr publication is RETRACTED — an Unknown-ABI
  contract must never stay lowering-consumable
  (review-2026-08-05T03-42-22Z P1-1).

Pre-fix, EVERY value read of a pending binding cascaded E-COPY-UNKNOWN —
including fully valid captureless shapes (aliasing a stored inferable
lambda was impossible), and the direct-call consumers had their own
typing path with different primaries and an Unknown-ABI publication hole.
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


def _build_run(tmp_path: Path, source: str) -> None:
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


# ---------------------------------------------------------------------------
# Value-use finalization: accepted shapes compile AND run.
# ---------------------------------------------------------------------------

def test_captureless_inferable_alias_compiles_and_runs(tmp_path: Path) -> None:
	# The candidate LANGUAGE_BUG child: pre-fix the alias read cascaded
	# E-COPY-UNKNOWN and this fully valid program was rejected.
	_build_run(
		tmp_path,
		"""
module repro;
pub fn main() nothrow -> Int {
	val f = | x: Int | => { x + 1 };
	val g = f;
	return g(6) - 7;
}
""",
	)


def test_resolve_after_alias_compiles_and_runs(tmp_path: Path) -> None:
	# The alias read finalizes FIRST; the later direct call of the
	# original binding uses the already-installed concrete type.
	_build_run(
		tmp_path,
		"""
module repro;
pub fn main() nothrow -> Int {
	val f = | x: Int | => { x + 1 };
	val g = f;
	return f(6) - 7;
}
""",
	)


def test_unused_annotated_stored_lambda_drains_clean(tmp_path: Path) -> None:
	# End-of-function drain: a never-used inferable lambda still types
	# once and publishes a concrete spec (no diagnostics).
	_build_run(
		tmp_path,
		"""
module repro;
pub fn main() nothrow -> Int {
	val f = | x: Int | => { x + 1 };
	return 0;
}
""",
	)


def test_shadowed_name_resolves_to_inner_concrete(tmp_path: Path) -> None:
	# Shadowing: the inner concrete binding wins for inner reads; the
	# outer pending lambda still drains clean.
	_build_run(
		tmp_path,
		"""
module repro;
pub fn main() nothrow -> Int {
	val f = | x: Int | => { x + 1 };
	{
		val f = 7;
		return f - 7;
	}
}
""",
	)


# ---------------------------------------------------------------------------
# Rejection shapes: one clean primary each, no cascades.
# ---------------------------------------------------------------------------

def test_unconstrained_alias_one_cannot_infer_primary(tmp_path: Path, capsys) -> None:
	msgs = _diag_compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val f = | x | => { x };
	val g = f;
	return 0;
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "cannot infer type for lambda parameter(s)" in msgs[0], msgs


def test_explicit_value_capture_alias_one_bare_storage_primary(tmp_path: Path, capsys) -> None:
	# Value captures may escape only through a SUPPORTED representation:
	# the bare stored form gets the one approved primary, and the alias
	# read of the poisoned binding must not add E-COPY-UNKNOWN.
	msgs = _diag_compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val x = 1;
	val f = | | captures(copy x) => { x };
	val g = f;
	return 0;
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "bare capturing lambdas cannot be stored in v1" in msgs[0], msgs


# ---------------------------------------------------------------------------
# Direct-call consumers (P1-1 pins): same classifier, same primaries.
# ---------------------------------------------------------------------------

def test_capturing_direct_hcall_one_primary(tmp_path: Path, capsys) -> None:
	# A stored borrow-capturing lambda called directly: the classifier's
	# approved primary, and the call's Unknown callee is causally
	# suppressed (no "call target is not a function value" cascade).
	msgs = _diag_compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	var x = 1;
	val f = | | => { x };
	return f();
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "closures with borrowed captures are non-escaping" in msgs[0], msgs


def test_capturing_direct_hinvoke_one_primary(tmp_path: Path, capsys) -> None:
	# Parenthesized callee routes through HInvoke: identical outcome.
	msgs = _diag_compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	var x = 1;
	val f = | | => { x };
	return (f)();
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "closures with borrowed captures are non-escaping" in msgs[0], msgs


def test_residual_unknown_direct_hcall_one_primary(tmp_path: Path, capsys) -> None:
	# The callsite context for the unannotated param is Unknown BECAUSE
	# the arg is a diagnosed unknown name: the classifier poisons the
	# binding WITHOUT typing (no Unknown-ABI LambdaFnSpec, no body-check
	# cascade against Unknown params) and stays silent — the arg's
	# primary already explains everything.
	msgs = _diag_compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val f = | x | => { x };
	return f(missing_name);
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "unknown name 'missing_name'" in msgs[0], msgs


def test_residual_unknown_direct_hinvoke_one_primary(tmp_path: Path, capsys) -> None:
	msgs = _diag_compile(
		tmp_path,
		capsys,
		"""
module m_main;
pub fn main() nothrow -> Int {
	val f = | x | => { x };
	return (f)(missing_name);
}
""",
	)
	assert len(msgs) == 1, msgs
	assert "unknown name 'missing_name'" in msgs[0], msgs
