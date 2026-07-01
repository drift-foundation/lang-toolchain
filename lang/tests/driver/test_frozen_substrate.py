# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 1a milestone — `Frozen` auto-derive + enforcement.

Per `work/constshare-substrate/phase1a-dispositions.md` (revised
2026-04-30): `Frozen` is the soundness boundary for `ConstArc<T>`
and (later) `ConstShare`-implementing types.  This file pins:

  - **Positive**: stdlib-baked Frozen impls (primitives, String,
    Optional<T:Frozen>, Result<T:Frozen, E:Frozen>) and user
    struct/variant auto-derive when all owned fields are Frozen.
  - **Negative**: types that must NOT be Frozen — Array, HashMap,
    Mutex, Arc, atomic-shaped, `&T`, `&mut T`, and user types
    containing any of those.
  - **Rejection**: user-side `implement Frozen for X` outside
    stdlib-baked impls is rejected.

This milestone delivers ONLY the Frozen layer — no ConstArc, no
ConstShare auto-derive, no synthesis.  Each subsequent layer gets
its own milestone and tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[str]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d.get("message", "") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


_PRE = """
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.containers as containers;
import std.concurrent as conc;

use trait shareable.Frozen;

// Test-only witness: requires the type-arg to satisfy `T: Frozen`.
// If the constraint fails, the call site fails to type-check with a
// trait-bound diagnostic.
fn assert_frozen<T>() nothrow -> Void require T is shareable.Frozen { }
"""


# ── Positive — stdlib-baked Frozen impls ────────────────────────


def test_int_is_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type Int>();
\treturn 0;
}
""")
	assert rc == 0, f"Int must be Frozen: rc={rc}, errs={errs}"
	assert not errs


def test_string_is_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type String>();
\treturn 0;
}
""")
	assert rc == 0, f"String must be Frozen: rc={rc}, errs={errs}"
	assert not errs


def test_bool_float_byte_uint_void_dv_are_frozen(tmp_path, capsys):
	# Slice 7a (0.31.62, 2026-05-05): `DiagnosticValue` retired from
	# the user-source surface (see test_dv_public_removed.py); the
	# Frozen impl on it is now compiler-internal only.  This probe
	# pins Frozen for the remaining primitives.
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type Bool>();
\tassert_frozen<type Float>();
\tassert_frozen<type Byte>();
\tassert_frozen<type Uint>();
\tassert_frozen<type Uint64>();
\tassert_frozen<type Void>();
\treturn 0;
}
""")
	assert rc == 0, f"primitives must be Frozen: rc={rc}, errs={errs}"
	assert not errs


# ── Positive — generic recursive: Optional<T> / Result<T, E> ─────


def test_optional_int_is_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type Optional<Int>>();
\treturn 0;
}
""")
	assert rc == 0, f"Optional<Int> must be Frozen: rc={rc}, errs={errs}"
	assert not errs


def test_result_int_string_is_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type core.Result<Int, String>>();
\treturn 0;
}
""")
	assert rc == 0, f"Result<Int, String> must be Frozen: rc={rc}, errs={errs}"
	assert not errs


def test_optional_array_is_not_frozen(tmp_path, capsys):
	"""Array<Int> is NOT Frozen → Optional<Array<Int>> NOT Frozen."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type Optional<Array<Int>>>();
\treturn 0;
}
""")
	assert rc != 0, f"Optional<Array<Int>> must NOT be Frozen: rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


# ── Positive — user struct/variant auto-derive ───────────────────


def test_user_struct_all_frozen_fields_is_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct Config {
\tpub name: String,
\tpub port: Int,
\tpub enabled: Bool
}

pub fn main() nothrow -> Int {
\tassert_frozen<type Config>();
\treturn 0;
}
""")
	assert rc == 0, f"struct of all-Frozen fields must auto-derive Frozen: rc={rc}, errs={errs}"
	assert not errs


def test_user_variant_all_frozen_fields_is_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub variant Status {
\tIdle,
\tRunning(pid: Int),
\tDone(msg: String)
}

pub fn main() nothrow -> Int {
\tassert_frozen<type Status>();
\treturn 0;
}
""")
	assert rc == 0, f"variant of all-Frozen fields must auto-derive Frozen: rc={rc}, errs={errs}"
	assert not errs


def test_nested_user_struct_all_frozen_is_frozen(tmp_path, capsys):
	"""Recursive: struct containing another auto-derived Frozen struct."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct Endpoint {
\tpub host: String,
\tpub port: Int
}

struct Server {
\tpub primary: Endpoint,
\tpub backup_count: Int
}

pub fn main() nothrow -> Int {
\tassert_frozen<type Endpoint>();
\tassert_frozen<type Server>();
\treturn 0;
}
""")
	assert rc == 0, f"nested all-Frozen structs must auto-derive: rc={rc}, errs={errs}"
	assert not errs


# ── Negative — types that must NOT be Frozen ─────────────────────


def test_array_int_is_not_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type Array<Int>>();
\treturn 0;
}
""")
	assert rc != 0, f"Array<Int> must NOT be Frozen (mutable storage): rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


def test_hashmap_is_not_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type containers.HashMap<String, Int>>();
\treturn 0;
}
""")
	assert rc != 0, f"HashMap must NOT be Frozen: rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


def test_mutex_int_is_not_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type conc.Mutex<Int>>();
\treturn 0;
}
""")
	assert rc != 0, f"Mutex<Int> must NOT be Frozen (mutation through &Mutex): rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


def test_arc_int_is_not_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type conc.Arc<Int>>();
\treturn 0;
}
""")
	assert rc != 0, f"Arc<Int> must NOT be Frozen (referent may be mutable): rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


def test_ref_int_is_not_frozen(tmp_path, capsys):
	"""References hold to potentially-mutable referents.  Per the
	revised disposition (§3a), `&T` is NOT Frozen by default in v1."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type &Int>();
\treturn 0;
}
""")
	assert rc != 0, f"&Int must NOT be Frozen in v1: rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


def test_ref_mut_int_is_not_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type &mut Int>();
\treturn 0;
}
""")
	assert rc != 0, f"&mut Int must NOT be Frozen: rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


# ── Negative — user types with non-Frozen fields ─────────────────


def test_user_struct_with_array_field_is_not_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct WithArray {
\tpub name: String,
\tpub values: Array<Int>
}

pub fn main() nothrow -> Int {
\tassert_frozen<type WithArray>();
\treturn 0;
}
""")
	assert rc != 0, f"struct with Array<T> field must NOT be Frozen: rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


def test_user_struct_with_mutex_field_is_not_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct WithMutex {
\tpub name: String,
\tpub state: conc.Mutex<Int>
}

pub fn main() nothrow -> Int {
\tassert_frozen<type WithMutex>();
\treturn 0;
}
""")
	assert rc != 0, f"struct with Mutex field must NOT be Frozen: rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


def test_user_struct_with_arc_field_is_not_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct WithArc {
\tpub name: String,
\tpub shared: conc.Arc<Int>
}

pub fn main() nothrow -> Int {
\tassert_frozen<type WithArc>();
\treturn 0;
}
""")
	assert rc != 0, f"struct with Arc field must NOT be Frozen: rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


def test_user_struct_with_ref_field_is_not_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct WithRef {
\tpub name: String,
\tpub r: &Int
}

pub fn main() nothrow -> Int {
\tassert_frozen<type WithRef>();
\treturn 0;
}
""")
	assert rc != 0, f"struct with &T field must NOT be Frozen in v1: rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


def test_user_variant_with_non_frozen_arm_is_not_frozen(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub variant Mixed {
\tEmpty,
\tWithArray(arr: Array<Int>)
}

pub fn main() nothrow -> Int {
\tassert_frozen<type Mixed>();
\treturn 0;
}
""")
	assert rc != 0, f"variant with non-Frozen arm must NOT be Frozen: rc={rc}, errs={errs}"
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"


# ── Rejection — user-side `implement Frozen for X` ─────────────


def test_user_implement_frozen_rejected(tmp_path, capsys):
	"""Per the revised disposition (§3a, "v1 implementability rules"):
	user code does NOT write `implement Frozen for X { }` blocks.
	The compiler enforces this.  Direct user impls are rejected with a
	diagnostic citing the contract."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct MyType {
\tpub mtx: conc.Mutex<Int>
}

implement shareable.Frozen for MyType { }

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	assert rc != 0, f"user-side `implement Frozen` must be rejected: rc={rc}, errs={errs}"
	# Diagnostic must mention Frozen and that direct impl isn't allowed.
	assert any("Frozen" in m for m in errs), f"diagnostic should mention Frozen: {errs}"
