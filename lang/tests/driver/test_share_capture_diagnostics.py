# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Checker-level diagnostics for `captures(share x)`.

The `Share` capability is checker-enforced: `captures(share x)` requires
`type(x): Share`.  When that constraint is violated, the type checker
emits an `E-CAPTURE-SHARE-NOT-SHARE` diagnostic that distinguishes the
two mistake-classes:

  - `x: Copy` — user wanted value-like duplication; suggest `copy x`.
  - `x: !Share` — user must implement `Share` for this type, or use
    `move x` to transfer ownership.

The HIR→MIR layer carries a defensive assertion that enforces the same
precondition; this test pins the user-facing diagnostic at the checker.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile_with_stdlib(
	tmp_path: Path,
	capsys: pytest.CaptureFixture[str],
	source: str,
) -> tuple[int, dict]:
	main_path = tmp_path / "main.drift"
	_write_file(main_path, source)
	argv = ["--stdlib-root", "stdlib", "--test-build-only", str(main_path)]
	return _run_driftc_json(argv, capsys)


def test_share_capture_of_copy_type_suggests_copy(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	# `Int` is `Copy`, not `Share`.  The user-facing fix is `captures(copy x)`.
	source = """
module main;

pub fn main() nothrow -> Int {
	val x: Int = 5;
	return (| | captures(share x) => { return x; })();
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc != 0, "compile should reject share-capture of a Copy type"
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("E-CAPTURE-SHARE-NOT-SHARE" in m for m in msgs), f"diagnostics: {msgs}"
	assert any("is `Copy`, not `Share`" in m for m in msgs), f"diagnostics: {msgs}"
	assert any("captures(copy x)" in m for m in msgs), f"diagnostics: {msgs}"


def test_share_capture_of_non_share_non_copy_type_suggests_move(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	# `Token` is non-Copy and has no `Share` impl.  The diagnostic
	# directs the user at `move` (transfer ownership) or implementing
	# `Share`.  The `Destructible` impl on `Token` makes it non-Copy.
	source = """
module main;

import std.core as core;

struct Token {
	v: Int
}

implement core.Destructible for Token {
	pub fn destroy(self: Token) nothrow -> Void {
		return;
	}
}

pub fn main() nothrow -> Int {
	val tok = Token(v = 42);
	return (| | captures(share tok) => { return 0; })();
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc != 0, "compile should reject share-capture of a non-Share, non-Copy type"
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("E-CAPTURE-SHARE-NOT-SHARE" in m for m in msgs), f"diagnostics: {msgs}"
	assert any("does not implement `std.core.shareable.Share`" in m for m in msgs), f"diagnostics: {msgs}"
	assert any("captures(move tok)" in m for m in msgs), f"diagnostics: {msgs}"


def test_share_capture_inherent_share_method_does_not_satisfy_trait(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	# A user-defined struct with an inherent `.share()` method that does
	# NOT implement `std.core.shareable.Share` MUST NOT satisfy
	# `captures(share x)`.  The desugar resolves through the explicit
	# trait `HQualifiedMember(Share-trait, "share")` — not through
	# ordinary method-name lookup — so the inherent method is irrelevant
	# to the capture form.  This is the trait-discipline guardrail: any
	# future change that lets `captures(share x)` fall through to method
	# lookup would silently bind to this inherent method and produce
	# semantically wrong behavior.
	source = """
module main;

import std.core as core;

struct Counter {
	v: Int
}

implement core.Destructible for Counter {
	pub fn destroy(self: Counter) nothrow -> Void {
		return;
	}
}

implement Counter {
	// Inherent `.share()` on a non-Share type — must NOT satisfy
	// `captures(share x)`.  The desugar resolves the Share trait
	// specifically, not method-name lookup.
	pub fn share(self: &Counter) nothrow -> Counter {
		return Counter(v = self.v);
	}
}

pub fn main() nothrow -> Int {
	val c = Counter(v = 42);
	return (| | captures(share c) => { return 0; })();
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	assert rc != 0, (
		"compile must reject `captures(share c)` for a type that has an inherent "
		".share() method but does NOT implement std.core.shareable.Share"
	)
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	# Focused, trait-aware diagnostic — NOT a "method not found" fallthrough.
	assert any("E-CAPTURE-SHARE-NOT-SHARE" in m for m in msgs), f"diagnostics: {msgs}"
	assert any("does not implement `std.core.shareable.Share`" in m for m in msgs), f"diagnostics: {msgs}"
	# The diagnostic must explicitly mention that an inherent method does NOT satisfy.
	assert any("inherent" in m.lower() for m in msgs), f"diagnostics must mention inherent: {msgs}"
