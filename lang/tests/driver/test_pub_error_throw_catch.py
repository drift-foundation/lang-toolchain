# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: throw/catch surface for `pub error` types.

Pins:

  1. `throw E(...)` over a `pub error` type lands in a typed
     `catch E(e) { ... }` arm and the typed catch arm claims
     coverage of narrow declared throws (no `nothrow` violation
     in the outer scope).  Probe bodies use literal returns.
  2. Multiple typed catch arms over distinct `pub error` types
     compile and route by event identity (verified at e2e level).
  3. Bare `catch` (catch-all-no-binder) fallback covers any
     thrown `pub error` not matched by a preceding typed arm.
  4. Slice 3 first proof — typed catch binder is the Error
     envelope viewed through the matched event schema.  The
     same binder supports both:
       * declared schema field access (`e.offset`) — typed
         projection from the envelope's params, with the type
         taken from the Path-A co-registered struct's field type;
       * Error envelope methods (`e.encode_compact()`, etc.) —
         direct dispatch on the same Error handle.
     There is no separate native `ParseError` local.  Per K's
     clarified model (spec §24): there is no native exception
     object after throw.
  4-neg. Field access on the typed catch binder whose name is
     neither in the schema NOR an Error method/field is rejected
     with `E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA` — closes the
     permissive-Unknown false-positive route.
  5-6. Typed throws validation + visibility coherence (slice 2B).

**Out of scope:** `Result.or_throw()` (test_pub_error_or_throw.py),
manual `Diagnostic` impls (test_pub_error_manual_diagnostic.py),
re-throw event-identity preservation (deferred to
implementation-phase tests).

History note: an earlier slice 2B draft asserted typed-binder
field access via `e.offset` and via fn-arg pass-through to a
function taking `ParseError`.  Both were based on the wrong
model and gave false confidence (the former via a permissive
HField → Unknown fallback; the latter requiring a re-materialized
native struct that doesn't exist after throw).  Probes have been
revised to match the now-clarified envelope-plus-typed-projection
model.

Spec: `work/exception-diagnostics-context/slice5-spec.md` §4-§5,
§24 (slice 3 typed catch binder).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main

from lang.codegen.llvm.test_utils import sanitizer_timeout


_SLICE_5_PENDING = pytest.mark.xfail(
	strict=True,
	reason=(
		"Slice 5 (pub error language migration) not yet implemented; "
		"spec locked at work/exception-diagnostics-context/slice5-spec.md"
	),
)


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[dict]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


def _ok(rc: int, errs: list[dict], label: str) -> None:
	assert rc == 0, (
		f"{label}: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def _fails_with_code(rc: int, errs: list[dict], code: str, label: str) -> None:
	codes = [e.get('code') for e in errs]
	assert rc != 0, f"{label}: expected compile failure but rc=0"
	assert code in codes, (
		f"{label}: expected diagnostic {code} not in {codes}\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


_PRE = """
module main;

import std.core as core;
"""


# ── Probe 1 ─ throw + typed catch coverage ────────────────────────


def test_throw_pub_error_typed_catch_field_access(tmp_path, capsys):
	"""`throw ParseError(...)` lands in `catch ParseError(e)`; the
	typed catch arm claims coverage of the narrow declared throws
	so the outer `nothrow` scope is satisfied.

	Probe scope: coverage only.  Typed-binder field access
	(`e.offset`) and same-binder envelope-method access
	(`e.encode_compact()`) are pinned by
	`test_typed_catch_binder_same_envelope_typed_projection`
	below."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\tmessage: String,
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(message = "bad", offset = 12);
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\treturn 12;
\t}
}
""")
	_ok(rc, errs, "throw + typed catch coverage")


# ── Probe 2 ─ two typed catch arms compile ─────────────────────────


def test_two_typed_catch_arms_compile(tmp_path, capsys):
	"""Two typed catch arms over distinct `pub error` types both
	compile.  Runtime routing by event identity is verified at the
	e2e level; this probe pins the static surface."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

pub error CodecError {
\tkind: String,
}

fn risky(which: Int) throws ParseError, CodecError -> Int {
\tif which == 0 {
\t\tthrow ParseError(offset = 1);
\t}
\tthrow CodecError(kind = "utf8");
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky(0);
\t} catch ParseError(e) {
\t\treturn 12;
\t} catch CodecError(e) {
\t\treturn 99;
\t}
}
""")
	_ok(rc, errs, "two typed catch arms compile")


# ── Probe 3 ─ catch-all fallback after typed arms ──────────────────


def test_catch_wildcard_after_typed_arm(tmp_path, capsys):
	"""Bare `catch` (catch-all) fallback after a typed arm compiles;
	pins that catch-all remains available alongside typed catch.
	(Drift spells the catch-all as bare `catch` or `catch e`, not
	`catch *`.)"""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

pub error OtherError {}

fn risky() throws OtherError -> Int {
\tthrow OtherError();
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\treturn 12;
\t} catch {
\t\treturn -1;
\t}
}
""")
	_ok(rc, errs, "catch-all fallback after typed arm")


# ── Probe 4 ─ typed catch binder same-envelope projection ──────────


def test_typed_catch_binder_same_envelope_typed_projection(tmp_path, capsys):
	"""Slice 3 first proof: the typed catch binder is the Error
	envelope viewed through the matched event schema.  The SAME
	binder supports BOTH:

	  * declared schema field access (`e.offset`) — typed projection
	    from the envelope's params, with the type taken from the
	    Path-A co-registered struct's field type;
	  * Error envelope methods (`e.encode_compact()`) — direct
	    dispatch on the same Error handle.

	Per K's clarified model (spec §24): there is no native exception
	object after throw — `pub error` values are encoded into the
	generic Error envelope at throw time; the binder IS the envelope.
	The typed projection on `e.offset` does NOT recover a separate
	`ParseError` struct value, and there is NO second native local.

	The probe replaces the earlier `test_typed_catch_binder_is_struct_xfail`
	(slice 2B), which was designed for the wrong model — it asserted
	fn-arg pass-through expecting a re-materialized native struct.
	"""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(offset = 12);
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\tval n: Int = e.offset;
\t\tval _s: String = e.encode_compact();
\t\treturn n;
\t}
}
""")
	_ok(rc, errs, "typed catch binder same-envelope typed projection")


# ── Probe 4-neg ─ unknown field on typed binder rejected ───────────


def test_typed_catch_binder_unknown_field_rejected(tmp_path, capsys):
	"""Slice 3 negative: a field access on the typed catch binder
	whose name is neither in the matched schema NOR an Error envelope
	method/field MUST fail with `E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA`
	— not silently fall through to Unknown.  This closes the
	false-positive route the slice 2B caveat warned about."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(offset = 12);
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\tval _x = e.not_a_declared_field;
\t\treturn 0;
\t}
}
""")
	_fails_with_code(rc, errs, 'E_TYPED_CATCH_FIELD_NOT_IN_SCHEMA',
		"typed catch binder unknown field rejected")


# ── Probe 5 ─ throws clause rejects non-error types ────────────────


def test_throws_clause_rejects_non_error_type(tmp_path, capsys):
	"""`fn f() throws E -> T` where `E` is not a `pub error` /
	`error` kind is rejected with `E_THROWS_NOT_ERROR_TYPE` at the
	throws-clause site.  Pins the typed-throws validation that
	landed in slice 2B (`_resolve_declared_throws_types`)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn risky() throws Int -> Int {
\treturn 0;
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_THROWS_NOT_ERROR_TYPE',
		"throws clause rejects non-error type")


# ── Probe 6 ─ public function leaks private error in throws ────────


def test_pub_fn_throws_private_error_rejected(tmp_path, capsys):
	"""`pub fn f() throws PrivateError` is rejected with
	`E_PRIVATE_ERROR_LEAKED_VIA_PUB` — slice 2B visibility coherence
	(spec §2.3.1).  A module-private `error E { ... }` declaration
	cannot appear in the throws clause of a `pub fn` because that
	would leak the private type through the API surface."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
error PrivateE {}

pub fn f() throws PrivateE -> Int {
\tthrow PrivateE();
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PRIVATE_ERROR_LEAKED_VIA_PUB',
		"public function leaks private error in throws clause")


# ── Probe 4-neg2 ─ unsupported field type rejected ────────────────


def test_typed_catch_binder_unsupported_field_type_rejected(tmp_path, capsys):
	"""Slice 3 negative (K-blocker fix, 2026-05-04), tightened by Slice 5
	projectability rule (K, 2026-05-04): a `pub error` field whose
	type is NOT projectable (collections, raw pointers, plain
	structs without a manual `Diagnostic`) is now rejected at the
	declaration site with `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`.

	Originally pinned the typed-catch binder rejection
	(`E_TYPED_CATCH_FIELD_UNSUPPORTED_TYPE`).  Under the Slice 5
	rule the decl fails closed BEFORE typed-catch lookup runs —
	same fall-through-to-garbage protection at an earlier and
	stricter site."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error Bad {
\txs: Array<Int>,
}

fn risky() throws Bad -> Int {
\tthrow Bad(xs = []);
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch Bad(e) {
\t\tval xs = e.xs;
\t\treturn 0;
\t}
}
""")
	_fails_with_code(rc, errs, 'E_PUB_ERROR_FIELD_NOT_PROJECTABLE',
		"typed catch binder unsupported field type rejected")


# ── Probe 4-neg3 ─ aliased Error binder field access rejected ─────


def test_typed_catch_binder_alias_field_access_rejected(tmp_path, capsys):
	"""Slice 3 negative (K alias guard, 2026-05-04): an aliased
	binding of the typed catch binder (e.g. `val ref = e; ref.offset`)
	must NOT silently compile via the permissive Error HField
	fallback.  The slice 3 first-pass binder detection is HVar-only;
	aliased shapes fail clearly with `E_UNKNOWN_FIELD_ON_ERROR`
	rather than lowering to garbage.  Future work may widen the
	detection to track alias propagation; until then this probe
	pins the rejection."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(offset = 12);
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\tval ref = e;
\t\treturn ref.offset;
\t}
}
""")
	_fails_with_code(rc, errs, 'E_UNKNOWN_FIELD_ON_ERROR',
		"aliased typed catch binder field access rejected")


# ── Probe 4-neg4 ─ throw arg type must match declared field type ──


def test_throw_pub_error_field_type_mismatch_rejected(tmp_path, capsys):
	"""Slice 7b regression (K, 2026-05-06, LANGUAGE_BUG): `throw E(...)`
	argument types must match the `pub error E` declared field types.

	Pre-fix bug shape: when Slice 7b retired the per-field
	`is_diagnostic` walk + DV auto-promotion at HExceptionInit
	(Site A), the field-type check was lost too.  The lowering
	then constructed the Path-A struct with mismatched MIR values
	and LLVM codegen crashed with an internal `struct field type
	mismatch (have %DriftString, expected drift.int)` —
	a checker/lowering contract failure, not a user diagnostic.

	Post-fix: each resolved field value is compared against the
	Path-A co-registered struct's declared field type at type-
	check time; mismatches surface as
	`E_THROW_FIELD_TYPE_MISMATCH` user diagnostics."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error Bad { x: Int }

fn boom() -> Int {
\tthrow Bad(x = "not-int");
}

pub fn main() nothrow -> Int {
\ttry { return boom(); } catch { return 0; }
}
""")
	_fails_with_code(rc, errs, 'E_THROW_FIELD_TYPE_MISMATCH',
		"throw arg type mismatch with declared pub error field type")


# ── Probe 4-neg5 ─ &Declared NOT accepted where Declared expected ──


def test_throw_pub_error_field_ref_for_owned_rejected(tmp_path, capsys):
	"""Slice 7b regression follow-up (K, 2026-05-06, LANGUAGE_BUG):
	a `&Int` value is NOT accepted where the `pub error` field
	declares an owned `Int`.

	Pre-fix bug shape: an earlier draft of the field-type check
	allowed `&Declared` where `Declared` was expected (intended as
	a reborrow convenience).  But HIR→MIR's `ConstructStruct`
	hands the original expression directly to the Path-A struct
	constructor — there is no implicit deref / reborrow at that
	site — so a `&Int` value reached LLVM codegen and crashed
	with `struct field type mismatch (have ptr, expected
	drift.int)`.  Same class of contract failure as the plain
	String-for-Int case, just routed through a borrow.

	Post-fix: rejected at type-check time with
	`E_THROW_FIELD_TYPE_MISMATCH` and a help note pointing at the
	owned-value expectation.  Users who want to throw a borrow
	declare the field as `&T` explicitly."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error Bad { x: Int }

fn boom() -> Int {
\tval n = 7;
\tthrow Bad(x = &n);
}

pub fn main() nothrow -> Int {
\ttry { return boom(); } catch { return 0; }
}
""")
	_fails_with_code(rc, errs, 'E_THROW_FIELD_TYPE_MISMATCH',
		"&Int rejected where Int field expected")
	# Also pin the help-note shape so the user-facing guidance
	# doesn't silently regress.
	hits = [e for e in errs if e.get('code') == 'E_THROW_FIELD_TYPE_MISMATCH']
	assert hits, "expected E_THROW_FIELD_TYPE_MISMATCH"
	notes = hits[0].get('notes') or []
	assert any('not a reference' in n for n in notes), (
		f"expected help note pointing at the owned-value expectation; got notes={notes}"
	)


# ── Probe 4-neg6 ─ private error fields project through Diagnostic ──


def test_throw_private_error_projects_fields_through_synthesized_diagnostic(
	tmp_path, capsys
):
	"""Slice 7b regression follow-up (K, 2026-05-06, LANGUAGE_BUG):
	a `throw` of a non-pub `error E { ... }` with fields must NOT
	silently drop the field values from the params envelope.

	Pre-fix bug shape: synthesis of `implement core.Diagnostic for E`
	fired only for `pub error` — non-pub `error E { msg: String }`
	had no Diagnostic impl, so the unified throw lowering's
	`to_json_text` lookup missed and emission fell through to the
	empty-envelope `ConstructError(payload=None)` shape.  At runtime
	`e.params.get("msg")` returned `Missing` even though `"boom"` was
	the value passed at the throw site.  Silent data loss across the
	throw boundary.

	Post-fix: synthesis fires for ALL `error E` decls (pub or
	non-pub) once they have at least one field — the module-internal
	thrower gets the same canonical params projection as a pub
	error.  Visibility AS A FIELD TYPE in another `pub error` stays
	pub-only (private types do not leak through public surfaces).

	This is a build-and-run probe (not just compile-only) to catch
	the runtime data-loss class — a compile-only assertion would
	pass even pre-fix because the empty-envelope shape compiles
	cleanly."""
	from lang.driftc.driftc import main as driftc_main
	import subprocess
	src = tmp_path / "main.drift"
	out_bin = tmp_path / "main_bin"
	src.write_text("""\
module main;

import std.core as core;

error E { msg: String }

fn _run() nothrow -> Int {
\ttry {
\t\tthrow E(msg = "boom");
\t} catch E(e) {
\t\tmatch e.params.get("msg").as_string() {
\t\t\tSome(v) => { if v == "boom" { return 0; } return 2; },
\t\t\tNone => { return 3; }
\t\t}
\t} catch e { return 5; }
\treturn 4;
}

pub fn main() nothrow -> Int {
\treturn _run();
}
""", encoding="utf-8")
	rc = driftc_main([
		"--stdlib-root", "stdlib",
		str(src),
		"-o", str(out_bin),
	])
	out = capsys.readouterr().out
	assert rc == 0, f"compile failed: rc={rc} out={out[:300]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, (
		f"non-pub `error E` field 'msg' was silently dropped from params "
		f"(exit {run.returncode}); expected 0 = `{{\"msg\":\"boom\"}}` round-trip"
	)
