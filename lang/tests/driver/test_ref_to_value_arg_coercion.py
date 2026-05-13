# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Symmetric `&T → T` / `&mut T → T` argument coercion.

Mirrors the existing borrowed field-projection auto-dup
(`type_checker.py:8645-8646`).  When a function parameter type
is `T` and the argument type is `&T` or `&mut T`, and `T` is
`Copy` or proves `ConstShare`, the type-checker inserts an
explicit deref HIR node (and, for non-Copy ConstShare, a
`.const_share()` wrap on top of the deref) so HIR→MIR lowering
sees the right ABI shape.

These tests are regression-first: they should fail before the
fix lands and pass after.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root
from lang.codegen.llvm.test_utils import sanitizer_timeout


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	root = stdlib_root()
	args = list(argv)
	if root:
		args += ["--stdlib-root", str(root)]
	args += ["--dev"]
	args += ["--json"]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile_main(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, dict]:
	mod_root = tmp_path / "mods"
	_write_file(mod_root / "main" / "main.drift", source)
	paths = sorted(mod_root.rglob("*.drift"))
	return _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)


def test_ref_string_to_string_param_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""Passing `&String` where `String` is expected should auto-dup
	(String is Copy + ConstShare).  Compiler today rejects this with
	a type mismatch; after fix, it must accept and emit `*s_ref`.
	"""
	rc, payload = _compile_main(
		tmp_path, capsys,
		"""
module main;

fn take_s(s: String) nothrow -> Int { return s.byte_length(); }

fn caller(s_ref: &String) nothrow -> Int {
	return take_s(s_ref);
}

fn main() nothrow -> Int {
	val s: String = "hello";
	return caller(&s);
}
""".lstrip(),
	)
	assert rc == 0, payload


def test_ref_int_to_int_param_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""Plain-Copy scalar case — Int.  `&Int → Int` must auto-dup
	to a register copy (no ConstShare wrap needed)."""
	rc, payload = _compile_main(
		tmp_path, capsys,
		"""
module main;

fn take_i(n: Int) nothrow -> Int { return n + 1; }

fn caller(n_ref: &Int) nothrow -> Int {
	return take_i(n_ref);
}

fn main() nothrow -> Int {
	val n: Int = 41;
	return caller(&n);
}
""".lstrip(),
	)
	assert rc == 0, payload


def test_ref_mut_string_to_string_param_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""`&mut T → T` is strictly more permissive than `&T → T`.
	A mutable borrow should also coerce to a duplicated owned value
	without moving out of the referent.
	"""
	rc, payload = _compile_main(
		tmp_path, capsys,
		"""
module main;

fn take_s(s: String) nothrow -> Int { return s.byte_length(); }

fn caller(s_mref: &mut String) nothrow -> Int {
	return take_s(s_mref);
}

fn main() nothrow -> Int {
	var s: String = "abc";
	return caller(&mut s);
}
""".lstrip(),
	)
	assert rc == 0, payload


def test_ref_mut_alias_pinned_after_arg_dup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""Aliasing regression for `&mut T → T`: the duplicated value
	must NOT move out of the referent.  After the call, the borrow
	is still live and the original local is still owned.
	"""
	rc, payload = _compile_main(
		tmp_path, capsys,
		"""
module main;

fn read_s(s: String) nothrow -> Int { return s.byte_length(); }

fn caller(s_mref: &mut String) nothrow -> Int {
	val n1 = read_s(s_mref);
	val n2 = read_s(s_mref);
	return n1 + n2;
}

fn main() nothrow -> Int {
	var s: String = "xy";
	val total = caller(&mut s);
	return total + s.byte_length();
}
""".lstrip(),
	)
	assert rc == 0, payload


def test_ref_to_var_param_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""`var x: T` parameter (mutable binding inside callee) still
	auto-dups from `&T` at the call boundary — `var` is about the
	binding inside the function, not ownership at the call site.
	"""
	rc, payload = _compile_main(
		tmp_path, capsys,
		"""
module main;

fn take_var(var s: String) nothrow -> Int {
	s = "replaced";
	return s.byte_length();
}

fn caller(s_ref: &String) nothrow -> Int {
	return take_var(s_ref);
}

fn main() nothrow -> Int {
	val s: String = "original";
	return caller(&s);
}
""".lstrip(),
	)
	assert rc == 0, payload


def test_ref_to_value_negative_destructible_rejects(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	"""Negative: a struct with a user `Destructible` impl is not
	Copy and does not prove ConstShare (default).  `&T → T` must
	be rejected, not silently auto-duped.
	"""
	rc, payload = _compile_main(
		tmp_path, capsys,
		"""
module main;

import std.core as core;

pub struct Resource { pub tag: Int }

implement core.Destructible for Resource {
	pub fn destroy(var self: Resource) nothrow -> Void { return; }
}

fn take(r: Resource) nothrow -> Int { return r.tag; }

fn caller(r_ref: &Resource) nothrow -> Int {
	return take(r_ref);
}

fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
	)
	# Compile must fail; the &Resource → Resource boundary is illegal.
	# Resource is non-Copy (has Destructible) and does not prove ConstShare.
	# The rejection must occur at the `take(r_ref)` boundary, not earlier
	# in the parser — verify the diagnostic points at the call site.
	assert rc != 0, payload
	diags = payload.get("diagnostics", [])
	assert diags, payload
	# Diagnostic must be a coercion / call-resolution rejection, not a
	# parser shape error — the destructible impl itself is well-formed.
	call_site_diag = any(
		"take" in str(d.get("message", ""))
		or "Resource" in str(d.get("message", ""))
		or d.get("line") == 13
		for d in diags
	)
	parser_shape_diag = any("Expected ARROW" in str(d.get("message", "")) for d in diags)
	assert not parser_shape_diag, f"unexpected parser error: {payload}"
	assert call_site_diag, f"expected call-site rejection, got: {payload}"


def test_ref_to_value_coercion_loses_to_exact_ref_overload(tmp_path: Path) -> None:
	"""Overload disambiguation: when both `fn pick(s: &T)` and
	`fn pick(s: T)` exist and the caller passes `&t`, the exact
	`&T → &T` match must win.  The new `&T → T` coercion is a
	strict fallback — only considered when no exact / borrow-coerce
	candidate exists.

	Compile-and-run: the two overload bodies return different
	values (`byte_length()` vs `byte_length() + 1000`), so the exit
	code proves WHICH overload was selected.  `rc == 0` alone would
	pass even if the wrong overload was picked.
	"""
	root = Path(__file__).resolve().parents[3]
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

fn pick(s: &String) nothrow -> Int { return s.byte_length(); }
fn pick(s: String) nothrow -> Int { return s.byte_length() + 100; }

fn main() nothrow -> Int {
	val s: String = "hello";
	return pick(&s);
}
""".lstrip(),
		encoding="utf-8",
	)
	out_bin = tmp_path / "bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(root / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=root, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:1500]}"
	assert out_bin.exists(), "binary not produced"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))
	# "hello".byte_length() == 5.  Exact `&String` overload returns 5.
	# The `String` (coerced) overload would return 105.
	assert run.returncode == 5, (
		f"wrong overload chosen: exit={run.returncode}; expected 5 "
		f"(&String overload), 105 would indicate the coerced "
		f"String overload was selected instead"
	)


def test_ref_to_value_method_overload_prefers_exact_ref(tmp_path: Path) -> None:
	"""Method-call sibling of the free-function overload pin: when
	`Box.pick(self, &String)` and `Box.pick(self, String)` both
	exist and the caller passes `&s`, the exact `&String` overload
	must win.

	The method-call resolver routes through `_apply_autoborrow_args`
	for HIR rewriting, but candidate selection happens before the
	rewrite; this test pins that the rewrite cannot silently
	upgrade a coerced match to win against an exact one.
	"""
	root = Path(__file__).resolve().parents[3]
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

pub struct Box { v: Int }

implement Box {
	pub fn pick(self: &Box, s: &String) nothrow -> Int { return self.v + s.byte_length(); }
	pub fn pick(self: &Box, s: String) nothrow -> Int { return self.v + s.byte_length() + 100; }
}

fn main() nothrow -> Int {
	val b = Box(v = 10);
	val s: String = "hello";
	return b.pick(&s);
}
""".lstrip(),
		encoding="utf-8",
	)
	out_bin = tmp_path / "bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(root / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=root, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:1500]}"
	assert out_bin.exists(), "binary not produced"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))
	# v=10 + "hello".byte_length()=5 → exact `&String` overload returns 15.
	# The `String` (coerced) overload would return 115.
	assert run.returncode == 15, (
		f"wrong method overload chosen: exit={run.returncode}; "
		f"expected 15 (Box.pick(&String) overload); 115 would "
		f"indicate the coerced String overload was selected"
	)
