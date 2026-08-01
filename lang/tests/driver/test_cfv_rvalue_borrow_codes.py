# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Diagnostic-CODE pins for value-control-flow (ternary/match) rvalue
borrow rejection (work/bare-temp-field-projection-uaf, review round 7).

A shared borrow of a projection off — or the whole of — a value-control-
flow rvalue whose OWNER type has drop work is rejected bind-first with
`E_PROJECTED_RVALUE_ARG_BINDING_REQUIRED`. The e2e runner asserts only a
diagnostic's message + phase, so the exact CODE is pinned here through
the real `driftc` CLI (stdout+stderr), including the two cases the e2e
message pins cannot distinguish:

  * a droppable-ROOT ternary projecting a BITCOPY field — the guard keys
    on the root's `has_drop`, not the leaf's bitcopy-ness, so this still
    rejects (else the root's owned payload double-frees); and
  * the SOURCE-written `&(cond ? a : b).root` — which must give this same
    bind-first CODE, NOT `E_REDUNDANT_ARG_BORROW` (whose "pass directly"
    fix-it would name the also-rejected bare form).

A bitcopy-ROOT ternary (`read(cond ? 1 : 2)` at `&Int`) has no drop work
to double and must COMPILE + RUN — the over-rejection boundary control.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_REJECT_CODE = "E_PROJECTED_RVALUE_ARG_BINDING_REQUIRED"

# Bare field projection of a ternary; droppable root (owns String + Array).
_BARE_FIELD = """\
module m;
struct Node { text: String, children: Array<Node>, }
struct PR { root: Node, }
fn mk_a() nothrow -> PR { var k: Array<Node> = []; return PR(root = Node(text = "a" + "", children = move k)); }
fn mk_b() nothrow -> PR { var k: Array<Node> = []; return PR(root = Node(text = "b" + "", children = move k)); }
fn peek(n: &Node) nothrow -> Int { return n.children.len(); }
pub fn main() nothrow -> Int { val cond = true; return peek((cond ? mk_a() : mk_b()).root); }
"""

# Whole-value ternary borrow; droppable root.
_BARE_WHOLE = """\
module m;
struct Node { text: String, children: Array<Node>, }
fn na() nothrow -> Node { var k: Array<Node> = []; return Node(text = "a" + "", children = move k); }
fn nb() nothrow -> Node { var k: Array<Node> = []; return Node(text = "b" + "", children = move k); }
fn peek(n: &Node) nothrow -> Int { return n.children.len(); }
pub fn main() nothrow -> Int { val cond = true; return peek(cond ? na() : nb()); }
"""

# Droppable ROOT (PR owns a String), BITCOPY projected field (.count: Int).
_BITCOPY_FIELD_DROPPABLE_ROOT = """\
module m;
struct PR { count: Int, text: String, }
fn mk_a() nothrow -> PR { return PR(count = 1, text = "a" + ""); }
fn mk_b() nothrow -> PR { return PR(count = 2, text = "b" + ""); }
fn peek_int(n: &Int) nothrow -> Int { val x = n; return 0; }
pub fn main() nothrow -> Int { val cond = true; return peek_int((cond ? mk_a() : mk_b()).count); }
"""

# SOURCE-written `&` of a ternary projection.
_EXPLICIT_FIELD = """\
module m;
struct Node { text: String, }
struct PR { root: Node, }
fn mk_a() nothrow -> PR { return PR(root = Node(text = "a" + "")); }
fn mk_b() nothrow -> PR { return PR(root = Node(text = "b" + "")); }
fn peek(n: &Node) nothrow -> Int { return n.text.byte_length(); }
pub fn main() nothrow -> Int { val cond = true; return peek(&(cond ? mk_a() : mk_b()).root); }
"""

# Bitcopy ROOT (Int) — no drop work, must compile + run.
_BITCOPY_SCALAR_ROOT = """\
module m;
fn read(n: &Int) nothrow -> Int { val x = n; return 7; }
pub fn main() nothrow -> Int { val cond = true; val c = read(cond ? 1 : 2); return c - 7; }
"""

# Generic CFV borrow: the root type is a type variable `T`. `has_drop(T)`
# fails open (caches False), so this MUST be caught by the fail-closed
# `has_typevar` guard at the generic-body check — regardless of the
# instantiation type. Instantiated with a droppable `String` here (the
# case that would double-free if it slipped through).
_GENERIC_DROPPABLE = """\
module m;
fn use_ref<T>(v: &T) nothrow -> Int { return 0; }
fn pick<T>(c: Bool, x: T, y: T) nothrow -> Int { return use_ref(c ? move x : move y); }
pub fn main() nothrow -> Int { return pick<type String>(true, "a" + "", "b" + ""); }
"""

# Same generic shape instantiated with a BITCOPY `Int`: the guard is at
# the generic-body check (T is a typevar there), so this is rejected too —
# conservative fail-closed, the ratified v1 contract.
_GENERIC_BITCOPY = """\
module m;
fn use_ref<T>(v: &T) nothrow -> Int { return 0; }
fn pick<T>(c: Bool, x: T, y: T) nothrow -> Int { return use_ref(c ? move x : move y); }
pub fn main() nothrow -> Int { return pick<type Int>(true, 1, 2); }
"""


def _compile(src_text: str, tmp_path: Path, name: str) -> subprocess.CompletedProcess:
	src = tmp_path / f"{name}.drift"
	src.write_text(src_text)
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "m::main", "-o", str(tmp_path / f"{name}.bin")],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)


import pytest


@pytest.mark.parametrize("name,src", [
	("bare_field", _BARE_FIELD),
	("bare_whole", _BARE_WHOLE),
	("bitcopy_field_droppable_root", _BITCOPY_FIELD_DROPPABLE_ROOT),
	("explicit_field", _EXPLICIT_FIELD),
	("generic_droppable", _GENERIC_DROPPABLE),
	("generic_bitcopy_fail_closed", _GENERIC_BITCOPY),
])
def test_cfv_rvalue_borrow_rejected_with_code(tmp_path: Path, name, src) -> None:
	res = _compile(src, tmp_path, name)
	out = res.stderr + res.stdout
	assert res.returncode != 0, f"{name} unexpectedly compiled:\n{out[-1000:]}"
	assert _REJECT_CODE in out, f"{name}: expected {_REJECT_CODE}, got:\n{out[-1000:]}"


def test_explicit_ternary_is_not_classified_redundant(tmp_path: Path) -> None:
	"""The source `&` spelling must NOT get the redundancy diagnostic — its
	"pass directly" fix-it would name the rejected bare form."""
	res = _compile(_EXPLICIT_FIELD, tmp_path, "explicit_not_redundant")
	out = res.stderr + res.stdout
	assert _REJECT_CODE in out, out[-1000:]
	assert "E_REDUNDANT_ARG_BORROW" not in out, f"stale redundancy fix-it:\n{out[-1000:]}"


def test_bitcopy_scalar_root_compiles_and_runs(tmp_path: Path) -> None:
	"""A bitcopy-root ternary borrow (`read(cond ? 1 : 2)` at `&Int`) has no
	drop work; it must NOT be rejected — over-rejection boundary."""
	res = _compile(_BITCOPY_SCALAR_ROOT, tmp_path, "bitcopy_scalar")
	assert res.returncode == 0, f"unexpected rejection:\n{(res.stderr + res.stdout)[-1000:]}"
	run = subprocess.run([str(tmp_path / "bitcopy_scalar.bin")],
	                     capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, f"runtime exit {run.returncode}"
