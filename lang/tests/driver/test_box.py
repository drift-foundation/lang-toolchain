# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression suite for `core.Box<T>` — the unique-ownership, single
heap-allocation value indirection (std.core.box).

Pins the §11 contract from work/feature-box/plan.md:
- construct / get / get_mut / take / run;
- move-only: no Copy / Share / ConstShare / Frozen;
- nested-droppable T dropped exactly once (drop counter);
- leak-/UAF-clean under valgrind, including take + scope-drop;
- breaks recursive value-type cycles (Box<Self> accepted; direct Self still
  rejected) — structurally, not by the name "Box";
- explicit access only: no auto-deref, no implicit unbox, no Box<T> -> T coercion;
- package emit -> consume round-trip (by-value field and recursive-broken variant).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from lang.codegen.llvm.test_utils import sanitizer_timeout, asan_active, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

_VALGRIND_SKIP = pytest.mark.skipif(
	__import__("shutil").which("valgrind") is None or asan_active(),
	reason="valgrind requires a non-ASan binary",
)


def _compile(tmp_path: Path, source: str, name: str = "box_bin") -> subprocess.CompletedProcess:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(150),
	)


def _compile_run(tmp_path: Path, source: str, name: str = "box_bin") -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess | None]:
	cc = _compile(tmp_path, source, name)
	if cc.returncode != 0:
		return cc, None
	run = subprocess.run([str(tmp_path / name)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	return cc, run


def _assert_compile_fails(cc: subprocess.CompletedProcess, *, must_mention: str | None = None) -> None:
	assert "Traceback (most recent call last)" not in (cc.stdout + cc.stderr), \
		f"compiler crashed instead of diagnosing:\n{cc.stderr[-1500:]}"
	assert cc.returncode != 0, f"expected a compile error, got success:\n{cc.stdout}\n{cc.stderr}"
	if must_mention is not None:
		assert must_mention.lower() in (cc.stdout + cc.stderr).lower(), \
			f"expected diagnostic mentioning {must_mention!r}:\n{cc.stderr[-1500:]}"


# ── 1. construct / get / get_mut / take / run ───────────────────────────────

def test_box_construct_access_take(tmp_path: Path) -> None:
	src = """\
module main;
import std.core as core;
import std.console as cons;
import std.format as fmt;
pub fn main() nothrow -> Int {
\tvar b = core.box<type Int>(42);
\tcons.println("get:" + fmt.format_int(*b.get()));
\tval m = b.get_mut();
\t*m = 100;
\tcons.println("mut:" + fmt.format_int(*b.get()));
\tval taken = b.take();
\tcons.println("take:" + fmt.format_int(taken));
\t// droppable payload round-trips
\tvar s = core.box<type String>("payload");
\tcons.println("str:" + *s.get());
\treturn 0;
}
"""
	cc, run = _compile_run(tmp_path, src)
	assert cc.returncode == 0, f"compile failed:\n{cc.stderr[-1500:]}"
	assert run is not None and run.returncode == 0, f"run failed: {run.stderr if run else cc.stderr}"
	assert run.stdout == "get:42\nmut:100\ntake:100\nstr:payload\n", run.stdout


# ── 2. move-only: use-after-move + copy-by-assignment rejected ───────────────

def test_box_use_after_move_rejected(tmp_path: Path) -> None:
	src = """\
module main;
import std.core as core;
pub fn main() nothrow -> Int {
\tvar b = core.box<type Int>(1);
\tval b2 = move b;
\tval x = b.get();   // use after move -> rejected
\treturn *x;
}
"""
	_assert_compile_fails(_compile(tmp_path, src))


def test_box_copy_by_assignment_rejected(tmp_path: Path) -> None:
	src = """\
module main;
import std.core as core;
pub fn main() nothrow -> Int {
\tvar b = core.box<type Int>(1);
\tval b2 = b;        // implicit copy of a non-Copy Box -> rejected (needs move)
\tval x = b.get();
\treturn *x;
}
"""
	_assert_compile_fails(_compile(tmp_path, src))


# ── 3. not Share / not ConstShare (direct trait-requirement probes) ─────────
#
# These instantiate generic functions with an explicit `require T is <trait>`
# bound and feed `core.Box<Int>`. Unlike a `.share()` call (which can fail merely
# because Share is not in method scope), a requirement probe fails ONLY because
# Box does not satisfy the bound — the precise contract under test.

_SHARE_PROBE = """\
module main;
import std.core as core;
import std.core.shareable as shareable;
fn require_share<T>() nothrow -> Void require T is shareable.Share { return; }
pub fn main() nothrow -> Int {
\trequire_share<type core.Box<Int>>();   // Box does not satisfy Share -> rejected
\treturn 0;
}
"""

_CONSTSHARE_PROBE = """\
module main;
import std.core as core;
import std.core.shareable as shareable;
fn require_cs<T>() nothrow -> Void require T is shareable.ConstShare { return; }
pub fn main() nothrow -> Int {
\trequire_cs<type core.Box<Int>>();   // Box does not satisfy ConstShare -> rejected
\treturn 0;
}
"""

# Positive control: an Arc<Int> DOES satisfy Share, so the same probe compiles —
# proving the probe actually exercises the bound (and isn't failing for an
# unrelated reason).
_SHARE_PROBE_POSITIVE = """\
module main;
import std.core as core;
import std.core.shareable as shareable;
fn require_share<T>() nothrow -> Void require T is shareable.Share { return; }
pub fn main() nothrow -> Int {
\trequire_share<type core.Arc<Int>>();   // Arc IS Share -> accepted
\treturn 0;
}
"""


def test_box_does_not_satisfy_share(tmp_path: Path) -> None:
	"""`require T is Share` instantiated with `Box<Int>` must be rejected."""
	_assert_compile_fails(_compile(tmp_path, _SHARE_PROBE, "share_probe"))


def test_box_does_not_satisfy_constshare(tmp_path: Path) -> None:
	"""`require T is ConstShare` instantiated with `Box<Int>` must be rejected —
	Box neither proves nor auto-derives ConstShare."""
	_assert_compile_fails(_compile(tmp_path, _CONSTSHARE_PROBE, "cs_probe"))


def test_share_probe_positive_control_arc(tmp_path: Path) -> None:
	"""Control: the same `require T is Share` probe ACCEPTS `Arc<Int>`, proving
	the probe exercises the bound rather than failing for an unrelated reason."""
	cc = _compile(tmp_path, _SHARE_PROBE_POSITIVE, "share_pos")
	assert cc.returncode == 0, f"Arc<Int> should satisfy Share:\n{cc.stderr[-1500:]}"


def test_box_drop_in_place_does_not_resolve(tmp_path: Path) -> None:
	"""The dangerous `_drop_in_place` helper has been removed — it must NOT be
	callable from user code (the destructor uses the public `take` path)."""
	src = """\
module main;
import std.core as core;
pub fn main() nothrow -> Int {
\tvar b = core.box<type String>("x");
\tb._drop_in_place();   // removed -> must not resolve
\treturn 0;
}
"""
	_assert_compile_fails(_compile(tmp_path, src, "no_dip"))


def test_box_internal_drained_helper_not_in_public_surface(tmp_path: Path) -> None:
	"""The destructor's `_box_is_drained` helper is NOT exported, so it is absent
	from the supported `core.*` surface — `core.Box`'s public API is exactly
	box/get/get_mut/take/destroy. (The helper is read-only and harmless; it must be
	`pub` only because a non-intrinsic generic destructor in core.drift must reach
	it, and Drift v1 cannot express sibling-only visibility.)"""
	src = """\
module main;
import std.core as core;
pub fn main() nothrow -> Int {
\tvar b = core.box<type Int>(1);
\tval d = core._box_is_drained<type Int>(&b);   // not re-exported -> must not resolve
\treturn 0;
}
"""
	_assert_compile_fails(_compile(tmp_path, src, "no_drained"),
	                      must_mention="does not export")


# ── 4. take moves out; use-after-take rejected ──────────────────────────────

def test_box_use_after_take_rejected(tmp_path: Path) -> None:
	src = """\
module main;
import std.core as core;
pub fn main() nothrow -> Int {
\tvar b = core.box<type Int>(7);
\tval t = b.take();
\tval x = b.get();   // use after take (consuming move) -> rejected
\treturn *x;
}
"""
	_assert_compile_fails(_compile(tmp_path, src))


# ── 5. nested-droppable T dropped exactly once (drop counter) ────────────────

_TRACKED = """\
pub struct Tracked { id: Int }
implement core.Destructible for Tracked {
\tpub fn destroy(var self: Tracked) nothrow -> Void {
\t\tcons.println("drop:" + fmt.format_int(self.id));
\t\treturn;
\t}
}
"""


def test_box_droppable_dropped_exactly_once(tmp_path: Path) -> None:
	src = f"""\
module main;
import std.core as core;
import std.console as cons;
import std.format as fmt;
{_TRACKED}
pub fn main() nothrow -> Int {{
\t{{
\t\tvar b = core.box<type Tracked>(Tracked(id = 1));   // dropped at block end
\t\tcons.println("live:" + fmt.format_int(b.get().id));
\t}}
\t// take path: the moved-out Tracked drops once; the drained box is a no-op
\tvar b2 = core.box<type Tracked>(Tracked(id = 2));
\tval t = b2.take();
\tcons.println("took:" + fmt.format_int(t.id));
\treturn 0;
}}
"""
	cc, run = _compile_run(tmp_path, src, "drop_bin")
	assert cc.returncode == 0, f"compile failed:\n{cc.stderr[-1500:]}"
	assert run is not None and run.returncode == 0, f"run failed:\n{(run.stderr if run else cc.stderr)}"
	out = run.stdout
	assert out.count("drop:1") == 1, f"id=1 must drop exactly once:\n{out}"
	assert out.count("drop:2") == 1, f"id=2 (taken) must drop exactly once:\n{out}"
	# id=1 drops at block end (before "took:"); id=2 drops at end of main.
	assert "live:1" in out and "took:2" in out, out


# ── 6. leak/UAF clean under valgrind ────────────────────────────────────────

@_VALGRIND_SKIP
def test_box_memcheck(tmp_path: Path) -> None:
	src = f"""\
module main;
import std.core as core;
import std.console as cons;
import std.format as fmt;
{_TRACKED}
pub fn main() nothrow -> Int {{
\t// construct + scope-drop
\t{{ var a = core.box<type Tracked>(Tracked(id = 10)); cons.println(fmt.format_int(a.get().id)); }}
\t// take + drop the moved-out value
\tvar b = core.box<type Tracked>(Tracked(id = 11));
\tval t = b.take();
\tcons.println(fmt.format_int(t.id));
\t// nested box of a droppable
\tvar n = core.box<type core.Box<String>>(core.box<type String>("inner"));
\tcons.println(*n.get().get());
\treturn 0;
}}
"""
	cc = _compile(tmp_path, src, "box_mc")
	assert cc.returncode == 0, f"compile failed:\n{cc.stderr[-1500:]}"
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--leak-check=full",
		             "--errors-for-leak-kinds=definite", "-q", str(tmp_path / "box_mc")),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode != 99, f"valgrind found leaks/errors:\n{res.stderr[:1200]}"
	assert res.returncode == 0, f"unexpected exit: rc={res.returncode}\n{res.stdout}\n{res.stderr[:600]}"


# ── 7/8. recursive value cycles: Box<Self> accepted, direct Self rejected ────

def test_box_recursive_variant_accepted_and_runs(tmp_path: Path) -> None:
	src = """\
module main;
import std.core as core;
import std.console as cons;
pub variant IrType {
\tTNull,
\tTArray(elem: core.Box<IrType>),
\tTOptional(inner: core.Box<IrType>)
}
pub fn main() nothrow -> Int {
\tvar leaf = core.box<type IrType>(IrType::TNull());
\tvar node = IrType::TArray(elem = move leaf);
\tmatch node {
\t\tIrType::TNull => { cons.println("null"); },
\t\tIrType::TArray(e) => { cons.println("array"); },
\t\tIrType::TOptional(i) => { cons.println("opt"); }
\t}
\treturn 0;
}
"""
	cc, run = _compile_run(tmp_path, src, "rec_bin")
	assert cc.returncode == 0, f"Box<Self> recursive variant wrongly rejected:\n{cc.stderr[-1500:]}"
	assert run is not None and run.returncode == 0 and run.stdout == "array\n", \
		f"{(run.stdout if run else cc.stderr)!r}"


def test_box_direct_recursive_still_rejected(tmp_path: Path) -> None:
	src = """\
module main;
pub variant IrType { TNull, TArray(elem: IrType) }
pub fn main() nothrow -> Int { return 0; }
"""
	cc = _compile(tmp_path, src, "rec_neg")
	_assert_compile_fails(cc, must_mention="E_RECURSIVE_VALUE_TYPE")
	# And the suggestion now recommends Box<Self>.
	assert "Box<" in (cc.stdout + cc.stderr), f"expected Box<Self> suggestion:\n{cc.stderr[-800:]}"


def test_recursion_break_is_structural_not_box_name(tmp_path: Path) -> None:
	"""A user RawBuffer-backed wrapper (phantom T) — NOT named Box — also breaks
	the cycle. Pins that the detector recognises indirection structurally, not by
	the name 'Box'."""
	src = """\
module main;
import std.mem as mem;
pub struct MyCell<T> { p: mem.RawBuffer<T> }
pub variant V { Leaf, Node(child: MyCell<V>) }
pub fn main() nothrow -> Int { return 0; }
"""
	cc = _compile(tmp_path, src, "struct_break")
	assert cc.returncode == 0, f"RawBuffer-backed wrapper should break the cycle structurally:\n{cc.stderr[-1500:]}"


# ── 10. explicit access only: no auto-deref / no coercion ───────────────────

def test_box_no_arithmetic_autoderef(tmp_path: Path) -> None:
	src = """\
module main;
import std.core as core;
pub fn main() nothrow -> Int {
\tvar b = core.box<type Int>(1);
\treturn b + 1;   // Box<Int> is not Int -> no auto-deref -> rejected
}
"""
	_assert_compile_fails(_compile(tmp_path, src))


def test_box_no_coercion_to_t_param(tmp_path: Path) -> None:
	src = """\
module main;
import std.core as core;
fn takes_int(x: Int) nothrow -> Int { return x; }
pub fn main() nothrow -> Int {
\tvar b = core.box<type Int>(1);
\treturn takes_int(b);   // Box<Int> -> Int coercion -> rejected
}
"""
	_assert_compile_fails(_compile(tmp_path, src))


# ── 11/12. package emit -> consume round-trip ───────────────────────────────

def _emit_pkg(tmp_path: Path, name: str, lib_src: str) -> None:
	(tmp_path / "lib").mkdir(parents=True, exist_ok=True)
	(tmp_path / "lib" / f"{name}.drift").write_text(lib_src)
	dmp = tmp_path / f"{name}.dmp"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--target-word-bits", "64",
		 "-M", str(tmp_path), str(tmp_path / "lib" / f"{name}.drift"),
		 "--emit-package", str(dmp), "--package-id", name,
		 "--package-version", "0.1.0", "--package-target", "test-target"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(150),
	)
	assert res.returncode == 0 and dmp.exists(), f"emit {name} failed:\n{res.stdout}\n{res.stderr[-1200:]}"


def _consume_run(tmp_path: Path, dep: str, app_src: str) -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess | None]:
	(tmp_path / "main.drift").write_text(app_src)
	out = tmp_path / "consumer"
	cc = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--target-word-bits", "64",
		 "-M", str(tmp_path), "--package-root", str(tmp_path),
		 "--dep", f"{dep}@0.1.0", "--allow-unsigned-from", str(tmp_path),
		 str(tmp_path / "main.drift"), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(150),
	)
	if cc.returncode != 0:
		return cc, None
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	return cc, run


def test_box_package_by_value_field_round_trip(tmp_path: Path) -> None:
	"""A package exports a struct with a Box<T> field; a consumer builds + runs."""
	_emit_pkg(tmp_path, "boxlib", """\
module boxlib;
import std.core as core;
export { Cfg, make_cfg, cfg_value };
pub struct Cfg { slot: core.Box<Int> }
pub fn make_cfg(v: Int) nothrow -> Cfg { return Cfg(slot = core.box<type Int>(v)); }
pub fn cfg_value(c: &Cfg) nothrow -> Int { return *c.slot.get(); }
""")
	cc, run = _consume_run(tmp_path, "boxlib", """\
module main;
import boxlib as boxlib;
import std.console as cons;
import std.format as fmt;
pub fn main() nothrow -> Int {
\tval c = boxlib.make_cfg(99);
\tcons.println(fmt.format_int(boxlib.cfg_value(&c)));
\treturn 0;
}
""")
	assert cc.returncode == 0, f"consumer compile failed:\n{cc.stderr[-1500:]}"
	assert run is not None and run.returncode == 0 and run.stdout == "99\n", \
		f"{(run.stdout if run else cc.stderr)!r}"


def test_box_package_recursive_variant_round_trip(tmp_path: Path) -> None:
	"""A package exports a recursive variant broken by Box<Self>; the consumer
	(loaded-package two-pass path) accepts and runs it."""
	_emit_pkg(tmp_path, "irlib", """\
module irlib;
import std.core as core;
export { IrType, leaf, wrap };
pub variant IrType { TNull, TArray(elem: core.Box<IrType>) }
pub fn leaf() nothrow -> IrType { return IrType::TNull(); }
pub fn wrap(inner: IrType) nothrow -> IrType { return IrType::TArray(elem = core.box<type IrType>(move inner)); }
""")
	cc, run = _consume_run(tmp_path, "irlib", """\
module main;
import irlib as irlib;
import std.console as cons;
pub fn main() nothrow -> Int {
\tval n = irlib.wrap(irlib.leaf());
\tmatch n {
\t\tirlib.IrType::TNull => { cons.println("null"); },
\t\tirlib.IrType::TArray(e) => { cons.println("array"); }
\t}
\treturn 0;
}
""")
	assert cc.returncode == 0, f"recursive-variant package consumer failed:\n{cc.stderr[-1500:]}"
	assert run is not None and run.returncode == 0 and run.stdout == "array\n", \
		f"{(run.stdout if run else cc.stderr)!r}"


@_VALGRIND_SKIP
def test_box_package_droppable_destructor_memcheck(tmp_path: Path) -> None:
	"""A consumer of an emitted package CONSTRUCTS and DROPS a Box<String> (via a
	package-exported producer), run under valgrind. Proves the consumer discovers
	and invokes the generic Box<T> destructor across the package boundary — and
	frees the String payload + cell — leak-free."""
	_emit_pkg(tmp_path, "sboxlib", """\
module sboxlib;
import std.core as core;
export { Holder, make, value };
pub struct Holder { slot: core.Box<String> }
pub fn make(s: String) nothrow -> Holder { return Holder(slot = core.box<type String>(move s)); }
pub fn value(h: &Holder) nothrow -> &String { return h.slot.get(); }
""")
	(tmp_path / "main.drift").write_text("""\
module main;
import sboxlib as sboxlib;
import std.console as cons;
pub fn main() nothrow -> Int {
\tvar h = sboxlib.make("heap-owned string payload");
\tcons.println(*sboxlib.value(&h));
\t// h (and its Box<String>) drops at scope end -> the consumer must discover and
\t// invoke the generic Box<String> destructor, freeing the String + cell.
\treturn 0;
}
""")
	out = tmp_path / "consumer"
	cc = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--target-word-bits", "64",
		 "-M", str(tmp_path), "--package-root", str(tmp_path),
		 "--dep", "sboxlib@0.1.0", "--allow-unsigned-from", str(tmp_path),
		 str(tmp_path / "main.drift"), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(150),
	)
	assert cc.returncode == 0 and out.exists(), f"consumer compile failed:\n{cc.stderr[-1500:]}"
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--leak-check=full",
		             "--errors-for-leak-kinds=definite", "-q", str(out)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode != 99, f"valgrind found leaks/errors in package Box destructor:\n{res.stderr[:1200]}"
	assert res.returncode == 0, f"unexpected exit: rc={res.returncode}\n{res.stdout}\n{res.stderr[:600]}"
	assert res.stdout == "heap-owned string payload\n", res.stdout
