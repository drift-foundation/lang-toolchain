# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 5 ConstShare implicit duplication — source-mode driver tests.

Goal: any value-consumption of a `T: ConstShare` binding that is
NOT under explicit `move` and NOT a borrow must auto-synthesize a
real `const_share()` HCall through the normal call-resolution
path.  This makes ConstShare types behave value-like at value-flow
sites without `.const_share()` ceremony.

Phase 5 v1 contract (per directive — semantics pass, NOT a
liveness optimization):
  - `val b = a`             → duplicates (a remains usable)
  - `takes_owned(a)`        → duplicates at call site
  - `return a`              → duplicates at return
  - `move a`                → moves (no duplication)
  - `&a` / `&mut a`         → borrows (no duplication)
  - non-ConstShare types    → existing "use move" error preserved

Trigger condition is purely TYPE-based: if the source type proves
`shareable.ConstShare` AND the use isn't `move`/`copy`/`borrow`,
synthesize.  No liveness/last-use analysis in v1 — extra
retain/release is acceptable; explicit `move` is the escape hatch.

Implementation seam: hook at the typechecker/value-use site where
`_require_copy_value` (`type_checker.py:2743`) currently rejects
non-Copy bindings.  When the type proves ConstShare, route the
HVar read through the normal call-resolver path
(`HMethodCall(receiver=..., method_name="const_share")` or the
trait-qualified call shape `Share::share` already uses) so the
call is fully typed/resolved before borrow-check and HIR→MIR
see it.

This file starts with the SINGLE smallest failing test: ConstArc
let-binding duplication.  Additional cases (owned arg, owned
return, synthesized struct/variant, generic struct, negative
move-then-use, negative non-ConstShare, borrow non-synth) and
package roundtrip + memcheck land after the core path is green.
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


# ── Smallest possible failing case ───────────────────────────────


def _ok(rc: int, errs: list[dict], label: str) -> None:
	assert rc == 0, (
		f"{label}: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def _err(rc: int, errs: list[dict], label: str, *, code_or_msg_substr: str | None = None) -> None:
	assert rc != 0, f"{label}: expected error but compile succeeded"
	if code_or_msg_substr is not None:
		matched = any(
			code_or_msg_substr in (e.get("code", "") or "")
			or code_or_msg_substr in (e.get("message", "") or "")
			for e in errs
		)
		assert matched, (
			f"{label}: expected diagnostic containing {code_or_msg_substr!r}, got:\n"
			+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
		)


# ── Positive — implicit duplication at value-flow sites ──────────


def test_phase5_const_arc_let_binding_duplicates(tmp_path, capsys):
	"""`val b = a` for `ConstArc<String>` auto-synthesizes
	`a.const_share()`; both bindings are usable afterwards."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;

pub fn main() nothrow -> Int {
\tval a = core.const_arc<type String>("hi");
\tval b = a;
\tval r1: &String = a.get();
\tval r2: &String = b.get();
\treturn r1.byte_length() + r2.byte_length();
}
""")
	_ok(rc, errs, "let-binding ConstArc duplication")


def test_phase5_const_arc_owned_arg_duplicates(tmp_path, capsys):
	"""Owned-arg passing of a ConstArc binding (`takes_owned(a)`)
	auto-shares; the source `a` remains usable after."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;

fn takes_owned(x: core.ConstArc<String>) nothrow -> Int {
\tval r: &String = x.get();
\treturn r.byte_length();
}

pub fn main() nothrow -> Int {
\tval a = core.const_arc<type String>("hi");
\tval n1 = takes_owned(a);
\tval n2 = takes_owned(a);
\treturn n1 + n2;
}
""")
	_ok(rc, errs, "owned-arg ConstArc duplication")


def test_phase5_const_arc_owned_return_duplicates(tmp_path, capsys):
	"""Returning a ConstArc binding by value auto-shares so the
	caller gets a fresh owner; the function-local source binding
	is dropped at scope end."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;

fn dup(a: core.ConstArc<String>) nothrow -> core.ConstArc<String> {
\treturn a;
}

pub fn main() nothrow -> Int {
\tval a = core.const_arc<type String>("hi");
\tval b = dup(a);
\tval r1: &String = a.get();
\tval r2: &String = b.get();
\treturn r1.byte_length() + r2.byte_length();
}
""")
	_ok(rc, errs, "owned-return ConstArc duplication")


def test_phase5_synthesized_struct_let_binding_duplicates(tmp_path, capsys):
	"""Phase 1 synthesized ConstShare struct (`Holder { handle:
	ConstArc<String> }`) participates in implicit duplication —
	`val b = a` works without `.const_share()` ceremony."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;

pub struct Holder {
\tpub handle: core.ConstArc<String>
}

pub fn main() nothrow -> Int {
\tval a = Holder(handle = core.const_arc<type String>("hi"));
\tval b = a;
\tval r1: &String = a.handle.get();
\tval r2: &String = b.handle.get();
\treturn r1.byte_length() + r2.byte_length();
}
""")
	_ok(rc, errs, "synthesized struct duplication")


def test_phase5_synthesized_variant_let_binding_duplicates(tmp_path, capsys):
	"""Phase 4 synthesized ConstShare variant participates."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;

pub variant Carrier {
\tEmpty,
\tWrap(handle: core.ConstArc<String>)
}

pub fn main() nothrow -> Int {
\tval a = Carrier::Wrap(core.const_arc<type String>("hi"));
\tval b = a;
\treturn 0;
}
""")
	_ok(rc, errs, "synthesized variant duplication")


def test_phase5_generic_struct_with_const_share_require_duplicates(tmp_path, capsys):
	"""Phase 3 generic struct that proves ConstShare via its
	require clause participates.  `Box<T> require T is ConstShare`
	instantiated with a ConstShare type duplicates implicitly."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;
import std.core.shareable as shareable;

use trait shareable.ConstShare;

pub struct Box<T> require T is shareable.ConstShare {
\tpub value: T
}

pub fn main() nothrow -> Int {
\tval inner = core.const_arc<type String>("hi");
\tval a = Box<type core.ConstArc<String>>(value = inner);
\tval b = a;
\treturn 0;
}
""")
	_ok(rc, errs, "generic ConstShare struct duplication")


# ── Negative — explicit move and non-ConstShare types ─────────────


def test_phase5_explicit_move_then_reuse_still_errors(tmp_path, capsys):
	"""Explicit `move a` is the user's escape hatch — it stays a
	plain move and reuse-after-move is rejected with the existing
	borrow-check diagnostic.  Implicit ConstShare must not weaken
	this."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;

fn takes_owned(x: core.ConstArc<String>) nothrow -> Int {
\treturn 0;
}

pub fn main() nothrow -> Int {
\tvar a = core.const_arc<type String>("hi");
\tval n = takes_owned(move a);
\tval r: &String = a.get();
\treturn r.byte_length();
}
""")
	_err(rc, errs, "explicit move + reuse", code_or_msg_substr="moved")


def test_phase5_non_const_share_owned_still_requires_move(tmp_path, capsys):
	"""Non-ConstShare non-Copy owned values still require explicit
	`move` — the existing 'cannot copy ... use move' diagnostic
	must continue to fire for types that don't prove ConstShare."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

pub struct OwnedToken {
\tpub fd: Int
}

pub fn main() nothrow -> Int {
\tval a = OwnedToken(fd = 7);
\tval b = a;
\treturn 0;
}
""")
	# OwnedToken is non-Copy AND doesn't prove ConstShare (no
	# auto-derive — Int is Copy+Frozen so derivation could fire,
	# but plain Int field actually means it WOULD derive).
	# Use a type that genuinely doesn't qualify: a struct holding
	# an Array (non-Copy non-ConstShare).
	if rc == 0:
		# OwnedToken with only Int field actually derives
		# ConstShare via the Copy+Frozen path — adjust to a
		# clearly non-CS shape.
		rc, errs = _compile(tmp_path, capsys, """
module main;

pub struct OwnedBag {
\tpub items: Array<Int>
}

pub fn main() nothrow -> Int {
\tval a = OwnedBag(items = [1, 2, 3]);
\tval b = a;
\treturn 0;
}
""")
	_err(rc, errs, "non-ConstShare requires explicit move",
	     code_or_msg_substr="use move")


def test_phase5_ternary_branches_duplicate(tmp_path, capsys):
	"""Pin: `cond ? a : b` for ConstShare bindings duplicates each
	value branch.  Without the HTernary slot handler this raised
	the discipline AssertionError at typecheck (the user-found
	gap); with the handler both branches install a wrap and the
	source bindings stay usable after.
	"""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;

fn pick(cond: Bool, a: core.ConstArc<String>, b: core.ConstArc<String>) nothrow -> core.ConstArc<String> {
\tval r = cond ? a : b;
\tval n: &String = a.get();
\tval m: &String = b.get();
\treturn r;
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_ok(rc, errs, "HTernary branches implicit duplication")


def test_phase5_public_result_ctor_payload_duplicates(tmp_path, capsys):
	"""Pin: `Ok(a)` resolved as the PUBLIC `core.Result` constructor (an
	ordinary contextual variant-constructor call) treats its payload as a
	value-position owned slot: a ConstShare binding `a` receives the real
	implicit `const_share()` HIR wrapper and stays usable afterwards.

	History: this test previously targeted the payload slot of the legacy
	internal result-ok HIR node.  That node and the unqualified `Ok(...)`
	source seam feeding it were DELETED (Slawomir-approved 2026-08-03; see
	the doc/history.md supersession note) because the seam hijacked the
	public Result constructor.  The surviving contract is the ordinary
	variant constructor's owned payload slot, pinned here at BOTH levels:
	structural (the rewritten HIR carries the implicit const_share wrap; no
	bare `HVar(a)` remains as the ctor argument) and full compile/run (the
	annotated Result is constructed, `a` is read afterwards, and the Ok
	payload is matched — semantic exit code).
	"""
	import contextlib
	import io
	from lang.driftc import type_checker as _tc_mod
	from lang.driftc import stage1 as H

	source = """
module main;

import std.core as core;

fn use_arc(a: core.ConstArc<String>) nothrow -> Int {
	return 0;
}

fn make_ok(a: core.ConstArc<String>) nothrow -> Int {
	val r: core.Result<core.ConstArc<String>, Int> = Ok(a);
	val n = use_arc(a);
	val m = match r {
		Ok(v) => { 1 },
		Err(e) => { 0 },
	};
	return (n + m);
}

pub fn main() nothrow -> Int {
	return 0;
}
"""
	# Structural half: capture make_ok's post-typecheck HIR (mutated in place
	# by the checker, so it reflects the Phase-5 ConstShare rewrite) — same
	# capture pattern as test_constshare_generic_field_frontend.py.
	captured: dict[str, object] = {}
	orig = _tc_mod.TypeChecker.check_function

	def _patched(self, fn_id, hir, *a, **k):
		res = orig(self, fn_id, hir, *a, **k)
		captured[str(fn_id)] = hir
		return res

	_tc_mod.TypeChecker.check_function = _patched
	try:
		src = tmp_path / "main.drift"
		src.write_text(source, encoding="utf-8")
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
			rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
		out = buf.getvalue()
		payload = json.loads(out) if out.strip() else {}
	finally:
		_tc_mod.TypeChecker.check_function = orig
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	_ok(rc, errs, "public Result ctor payload implicit duplication")
	hir = next((h for fid, h in captured.items() if "make_ok" in fid), None)
	assert hir is not None, "failed to capture make_ok HIR"

	wrap_receivers: list[str] = []
	bare_ctor_args: list[str] = []

	def _walk(node):
		if isinstance(node, H.HCall) and isinstance(node.fn, H.HVar) and node.fn.name == "Ok":
			for arg in node.args:
				if (
					isinstance(arg, H.HMethodCall)
					and arg.method_name == "const_share"
					and getattr(arg, "origin", None) == "implicit_const_share"
				):
					# `const_share` takes `&self`: the receiver is the
					# auto-borrowed canonical place of the source binding.
					recv = arg.receiver
					if isinstance(recv, H.HVar):
						wrap_receivers.append(recv.name)
					elif isinstance(recv, H.HBorrow):
						subj = recv.subject
						base = getattr(subj, "base", None)
						name = getattr(base, "name", None) or getattr(subj, "name", None)
						if name is not None:
							wrap_receivers.append(name)
				elif isinstance(arg, H.HVar):
					bare_ctor_args.append(arg.name)
		fields = getattr(node, "__dataclass_fields__", None)
		if fields is None:
			return
		for name in fields:
			val = getattr(node, name, None)
			if isinstance(val, (list, tuple)):
				for item in val:
					_walk(item)
			elif val is not None and hasattr(val, "__dataclass_fields__"):
				_walk(val)

	for stmt in hir.statements:
		_walk(stmt)
	assert "a" in wrap_receivers, (
		f"expected the Ok ctor payload to carry an implicit const_share() "
		f"wrap of `a`; wrap receivers={wrap_receivers} (rc==0 alone could be "
		f"satisfied by a wrong Copy classification)"
	)
	assert bare_ctor_args == [], f"bare HVar ctor arg(s) not rewritten: {bare_ctor_args}"


def test_phase5_public_result_ctor_payload_runs(tmp_path):
	"""Full compile/run half (mandatory under the checker/lowering contract:
	constructor routing and ownership rewriting are lowering-visible): the
	annotated Result is constructed from a ConstShare payload, the SOURCE
	binding is dereferenced afterwards (`a.get()`), the Ok payload is
	dereferenced in the match arm (`v.get()`), and the exit code derives from
	BOTH strings' byte lengths (7 + 7 - 14 = 0) — proving both owners are
	valid through lowering/runtime, not merely bound and dropped."""
	import subprocess
	import sys as _sys

	source = """
module repro;

import std.core as core;

fn use_arc(a: core.ConstArc<String>) nothrow -> Int {
	val s: &String = a.get();
	return s.byte_length();
}

fn make_ok(a: core.ConstArc<String>) nothrow -> Int {
	val r: core.Result<core.ConstArc<String>, Int> = Ok(a);
	val n = use_arc(a);
	val m = match r {
		Ok(v) => {
			val sv: &String = v.get();
			sv.byte_length()
		},
		Err(e) => { 0 },
	};
	return (n + m);
}

pub fn main() nothrow -> Int {
	val a = core.const_arc("payload");
	return (make_ok(a) - 14);
}
"""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = tmp_path / "okrun"
	r = subprocess.run(
		[_sys.executable, "-m", "lang.driftc.driftc", str(src), "--entry", "repro::main", "--target-word-bits", "64", "--stdlib-root", "stdlib", "-o", str(out)],
		capture_output=True, text=True, timeout=600,
	)
	assert r.returncode == 0, r.stderr
	rr = subprocess.run([str(out)], capture_output=True, timeout=60)
	assert rr.returncode == 0, rr.stderr


def test_phase5_borrow_does_not_synthesize(tmp_path, capsys):
	"""Borrows (`&a`) must not trigger duplication — they yield a
	reference, not a fresh owner."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;

fn read_ref(r: &core.ConstArc<String>) nothrow -> Int {
\tval inner: &String = r.get();
\treturn inner.byte_length();
}

pub fn main() nothrow -> Int {
\tval a = core.const_arc<type String>("hi");
\tval n = read_ref(a);
\tval r: &String = a.get();
\treturn n + r.byte_length();
}
""")
	_ok(rc, errs, "borrow does not synthesize")
