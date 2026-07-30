# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`&Concrete` → `&Interface` borrowed-view coercion (0.33.77 slice).

DriftQuery report 2026-07-08: passing an existing `&Concrete` (or a fresh
`&value`) where `&Interface` is expected rejected with a raw-TypeId overload
error (`no matching overload ... with args [2133]`), forcing per-type
borrowing-wrapper structs. Assessment
(`/tmp/drift-announce/2026-07-08T143012Z-ref-to-interface-coercion-assessment.md`)  ## drift-tmp-root-audit: allow docs repro-path reference
found an unimplemented coercion gap, not a deliberate boundary.

The fix: at call arguments, `&Concrete` widens to `&Interface` (and
`&mut Concrete` to `&mut Interface`) when Concrete has a non-generic
`implement Interface for Concrete`. The compiler synthesizes a BORROWED
interface view — a fat interface value whose flag byte carries the new
BORROWED bit (4): data slot points at the caller's storage, drop is a
complete no-op (no payload destroy, no free). The view temp is
compiler-created, used only in `&temp` argument position, and interfaces
are non-Copy — so the view can never escape its constructing frame as an
owned value, which is the ABI-neutrality argument.

Bundled per maintainer direction: the overload diagnostic renders pretty
type names (never raw TypeIds), and a non-implementing arg gets a targeted
message naming the missing implements relation.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_COMMON = """\
module main;

interface Greeter {
\tfn greet(self: &Self) nothrow -> String;
}

struct EnglishGreeter { name: String }

implement Greeter for EnglishGreeter {
\tfn greet(self: &EnglishGreeter) nothrow -> String {
\t\treturn "Hello, " + (self.name + "");
\t}
}

fn use_ref(g: &Greeter) nothrow -> String {
\treturn g.greet();
}
"""

# (A) The documented owned-value path — must stay green.
_OWNED_LOCAL_SOURCE = _COMMON + """
fn via_owned_local() nothrow -> String {
\tval e = EnglishGreeter(name = "Ann");
\tval g: Greeter = move e;
\treturn use_ref(g);
}

pub fn main() nothrow -> Int {
\tif via_owned_local() == "Hello, Ann" { return 0; }
\treturn 1;
}
"""

# (B) Fresh `&value` directly at the `&Interface` argument.
_FRESH_REF_SOURCE = _COMMON + """
fn via_fresh_reference() nothrow -> String {
\tval e = EnglishGreeter(name = "Bob");
\treturn use_ref(&e);
}

pub fn main() nothrow -> Int {
\tif via_fresh_reference() == "Hello, Bob" { return 0; }
\treturn 1;
}
"""

# (C) Existing borrowed param passed straight through.
_PASSTHROUGH_SOURCE = _COMMON + """
fn via_passthrough(e: &EnglishGreeter) nothrow -> String {
\treturn use_ref(e);
}

pub fn main() nothrow -> Int {
\tval e = EnglishGreeter(name = "Cid");
\tif via_passthrough(e) == "Hello, Cid" { return 0; }
\treturn 1;
}
"""

# (D) `&mut Concrete` → `&mut Interface`: the mutation must land in the
# CALLER's value (the view aliases, never copies) — observed after the call.
_MUT_VIEW_SOURCE = """\
module main;

interface Named {
\tfn rename(self: &mut Self, n: String) nothrow -> Void;
\tfn label(self: &Self) nothrow -> String;
}

struct Tagged { name: String }

implement Named for Tagged {
\tfn rename(self: &mut Tagged, n: String) nothrow -> Void {
\t\tself.name = n;
\t\treturn;
\t}
\tfn label(self: &Tagged) nothrow -> String {
\t\treturn self.name + "";
\t}
}

fn rename_via_iface(n: &mut Named, to: String) nothrow -> Void {
\tn.rename(to);
\treturn;
}

pub fn main() nothrow -> Int {
\tvar t = Tagged(name = "before");
\trename_via_iface(&mut t, "after");
\tif t.name == "after" { return 0; }
\treturn 1;
}
"""

# The with_resource shape DriftQuery actually ships: borrowed view used
# repeatedly across calls in the same scope — caller keeps using the
# concrete value after each widened call.
_REUSE_AFTER_CALLS_SOURCE = _COMMON + """
fn via_repeat(e: &EnglishGreeter) nothrow -> String {
\tval first = use_ref(e);
\tval second = use_ref(e);
\treturn first + second;
}

pub fn main() nothrow -> Int {
\tval e = EnglishGreeter(name = "Dot");
\tval both = via_repeat(e);
\tval again = e.name + "";
\tif both == "Hello, DotHello, Dot" {
\t\tif again == "Dot" { return 0; }
\t\treturn 2;
\t}
\treturn 1;
}
"""

# Negative: a NON-implementing concrete at the `&Interface` position must
# reject with a message that names types (never raw TypeIds) and points at
# the missing implements relation.
_NON_IMPLEMENTING_SOURCE = """\
module main;

interface Greeter {
\tfn greet(self: &Self) nothrow -> String;
}

struct Silent { n: Int }

fn use_ref(g: &Greeter) nothrow -> String {
\treturn g.greet();
}

pub fn main() nothrow -> Int {
\tval s = Silent(n = 1);
\tval _r = use_ref(&s);
\treturn 0;
}
"""

# Negative: owned upcast of a non-implementing type must be a clean
# diagnostic, not the pre-slice codegen ICE
# (`NotImplementedError: interface impl not found for interface value`).
_NON_IMPLEMENTING_OWNED_SOURCE = """\
module main;

interface Greeter {
\tfn greet(self: &Self) nothrow -> String;
}

struct Silent { n: Int }

pub fn main() nothrow -> Int {
\tval s = Silent(n = 1);
\tval g: Greeter = move s;
\treturn 0;
}
"""


def _compile(tmp_path: Path, source: str, *extra: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 *extra, str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)


def _run_ok(tmp_path: Path, source: str, *extra: str) -> None:
	res = _compile(tmp_path, source, *extra)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"


def test_owned_local_upcast_still_green(tmp_path: Path) -> None:
	"""(A) The documented owned-value upcast path is untouched."""
	_run_ok(tmp_path, _OWNED_LOCAL_SOURCE)


def test_fresh_ref_arg_widens(tmp_path: Path) -> None:
	"""(B) `use_ref(&e)` with e: EnglishGreeter compiles and runs."""
	_run_ok(tmp_path, _FRESH_REF_SOURCE)


def test_existing_ref_param_widens(tmp_path: Path) -> None:
	"""(C) an existing `&EnglishGreeter` param passes straight through."""
	_run_ok(tmp_path, _PASSTHROUGH_SOURCE)


def test_mut_ref_widens_and_aliases(tmp_path: Path) -> None:
	"""(D) `&mut Concrete` → `&mut Interface`; mutation lands in the
	caller's value — the view aliases the original, never copies it."""
	_run_ok(tmp_path, _MUT_VIEW_SOURCE)


def test_view_reuse_and_source_alive_after_calls(tmp_path: Path) -> None:
	"""Repeated widened calls; the concrete value stays usable after."""
	_run_ok(tmp_path, _REUSE_AFTER_CALLS_SOURCE)


def test_fresh_ref_arg_widens_asan(tmp_path: Path) -> None:
	"""ASAN row: the borrowed view must not double-drop the payload (its
	drop is a no-op) and must not leak (it owns nothing)."""
	res = _compile(tmp_path, _REUSE_AFTER_CALLS_SOURCE, "--sanitize=address,undefined")
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-800:]


def _run_ok_asan(tmp_path: Path, source: str) -> None:
	res = _compile(tmp_path, source, "--sanitize=address,undefined")
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-800:]


def test_multi_instance_owned_dispatch_asan(tmp_path: Path) -> None:
	"""ASAN row for the miscompile fix: two owned instance views on one
	struct — instance-keyed dispatch plus both fat-value drops must be
	clean (no leak, no double-drop) under address+UB sanitizers."""
	_run_ok_asan(tmp_path, _MULTI_INSTANCE_OWNED_SOURCE)


def test_multi_instance_widened_dispatch_asan(tmp_path: Path) -> None:
	"""ASAN row: widened views of both instances — borrowed fat values
	own nothing, their drops are no-ops, the viewed Boxes drop exactly
	once in the caller's scope."""
	_run_ok_asan(tmp_path, _MULTI_INSTANCE_WIDENED_SOURCE)


def test_non_implementing_ref_arg_pretty_diagnostic(tmp_path: Path) -> None:
	"""Fallback diagnostic: names the types and the missing implements
	relation; never leaks raw TypeIds like `args [2133]`."""
	res = _compile(tmp_path, _NON_IMPLEMENTING_SOURCE)
	assert res.returncode != 0, "non-implementing arg must reject"
	err = res.stderr + res.stdout
	assert not re.search(r"args \[[0-9]+(, [0-9]+)*\]", err), (
		f"raw TypeId list leaked into diagnostic:\n{err[-1000:]}"
	)
	assert "Silent" in err and "Greeter" in err, (
		f"diagnostic must name the concrete and interface types:\n{err[-1000:]}"
	)


# Escape negatives: the widened `&Interface` inherits the standard MVP
# reference-escape discipline — it cannot outlive the frame that owns the
# viewed value, whether returned directly or smuggled in an aggregate.
_ESCAPE_RETURN_SOURCE = _COMMON + """
fn keep(g: &Greeter) nothrow -> &Greeter {
\treturn g;
}

fn escapes() nothrow -> &Greeter {
\tval e = EnglishGreeter(name = "X");
\treturn keep(&e);
}

pub fn main() nothrow -> Int {
\tval g = escapes();
\tval _s = g.greet();
\treturn 0;
}
"""

_ESCAPE_AGGREGATE_SOURCE = _COMMON + """
struct Holder { g: &Greeter }

fn stash(g: &Greeter) nothrow -> Holder {
\treturn Holder(g = g);
}

fn escapes() nothrow -> Holder {
\tval e = EnglishGreeter(name = "X");
\treturn stash(&e);
}

pub fn main() nothrow -> Int {
\tval h = escapes();
\tval _s = h.g.greet();
\treturn 0;
}
"""


def test_widened_ref_cannot_escape_by_return(tmp_path: Path) -> None:
	"""Returning a widened `&Greeter` derived from a local's view must
	reject via the MVP reference-escape rule."""
	res = _compile(tmp_path, _ESCAPE_RETURN_SOURCE)
	assert res.returncode != 0, "escaping widened ref must reject"
	err = res.stderr + res.stdout
	assert "reference return must be derived from a reference parameter" in err, err[-800:]


def test_widened_ref_cannot_escape_in_aggregate(tmp_path: Path) -> None:
	"""Smuggling a widened `&Greeter` out inside a ref-holding struct must
	reject via the MVP borrowed-aggregate escape rule."""
	res = _compile(tmp_path, _ESCAPE_AGGREGATE_SOURCE)
	assert res.returncode != 0, "escaping widened ref in aggregate must reject"
	err = res.stderr + res.stdout
	assert "borrowed aggregate return must derive from a reference parameter" in err, err[-800:]


# GENERIC INTERFACE INSTANCES participate in widening (0.33.77 follow-up):
# `implement Sink<Int> for Box` widens `&Box` to `&Sink<Int>` — the
# implements relation AND the codegen impl index are keyed on the exact
# canonical interface instance (type_key_string: package/module + name +
# args), so `&Sink<String>` must never accept a `Sink<Int>` impl.
# Impl-parametric forms (`implement<T> Sink<T> for ...`) remain deferred.
_GENERIC_IFACE_COMMON = """\
module main;

interface Sink<T> {
\tfn put(self: &Self, v: T) nothrow -> Int;
}

struct Box { n: Int }

implement Sink<Int> for Box {
\tfn put(self: &Box, v: Int) nothrow -> Int { return self.n + v; }
}
"""

_GENERIC_IFACE_INSTANCE_SOURCE = _GENERIC_IFACE_COMMON + """
fn use_int_sink(s: &Sink<Int>) nothrow -> Int {
\treturn s.put(2);
}

pub fn main() nothrow -> Int {
\tval b = Box(n = 40);
\tif use_int_sink(&b) == 42 { return 0; }
\treturn 1;
}
"""

_GENERIC_IFACE_CROSS_INSTANCE_SOURCE = _GENERIC_IFACE_COMMON + """
fn use_str_sink(s: &Sink<String>) nothrow -> Int {
\treturn s.put("x");
}

pub fn main() nothrow -> Int {
\tval b = Box(n = 40);
\tval _r = use_str_sink(&b);
\treturn 0;
}
"""


# TWO instance impls on one struct: dispatch must route each interface
# instance through ITS OWN impl. Pre-fix, codegen's impl index was keyed on
# (iface_BASE, target) with first-impl-wins method merge — the Sink<String>
# view silently dispatched through the Sink<Int> impl (wrong result, no
# diagnostic; reproduced on certified 0.33.76). The index is now keyed on
# the canonical interface INSTANCE (type_key_string — includes
# package/module identity, so pkgA's Sink<Int> can never collide with a
# local or foreign Sink<Int>).
_MULTI_INSTANCE_COMMON = """\
module main;

interface Sink<T> {
\tfn put(self: &Self, v: T) nothrow -> Int;
}

struct Box { n: Int }

implement Sink<Int> for Box {
\tfn put(self: &Box, v: Int) nothrow -> Int { return self.n + v; }
}

implement Sink<String> for Box {
\tfn put(self: &Box, v: String) nothrow -> Int { return self.n * 100; }
}

fn use_int_sink(s: &Sink<Int>) nothrow -> Int { return s.put(2); }
fn use_str_sink(s: &Sink<String>) nothrow -> Int { return s.put("x"); }
"""

_MULTI_INSTANCE_OWNED_SOURCE = _MULTI_INSTANCE_COMMON + """
pub fn main() nothrow -> Int {
\tval b1 = Box(n = 40);
\tval s1: Sink<Int> = move b1;
\tval b2 = Box(n = 40);
\tval s2: Sink<String> = move b2;
\tval r1 = use_int_sink(s1);
\tval r2 = use_str_sink(s2);
\tif r1 == 42 {
\t\tif r2 == 4000 { return 0; }
\t\treturn 2;
\t}
\treturn 1;
}
"""


def test_multi_instance_owned_dispatch_correct(tmp_path: Path) -> None:
	"""Each owned interface-instance view dispatches through its own impl
	(r1=42 via Sink<Int>, r2=4000 via Sink<String>) — pins the
	base-keyed-index miscompile fix."""
	_run_ok(tmp_path, _MULTI_INSTANCE_OWNED_SOURCE)


# Coherence: a duplicate concrete instance impl must reject even when it is
# NOT the first impl in source order (review finding: the arg-sensitive
# duplicate check must compare against ALL previously-seen concrete
# (target, trait_args) pairs, not only the group's first impl — otherwise
# [String, Int, Int] lets the exact-duplicate Int pair slip through and
# codegen's first-wins merge inside the exact key recreates ambiguous
# dispatch). Uses the cross-module shape: impls of an IMPORTED interface
# are the ones classified onto the trait-coherence path per-module.
_DUP_IFACE_MOD = """\
module sinkmod;

export { Sink };

pub interface Sink<T> {
\tfn put(self: &Self, v: T) nothrow -> Int;
}
"""

_DUP_NOT_FIRST_MAIN = """\
module main;
import sinkmod;

struct Box { n: Int }

implement sinkmod.Sink<String> for Box {
\tfn put(self: &Box, v: String) nothrow -> Int { return self.n * 100; }
}

implement sinkmod.Sink<Int> for Box {
\tfn put(self: &Box, v: Int) nothrow -> Int { return self.n + v; }
}

implement sinkmod.Sink<Int> for Box {
\tfn put(self: &Box, v: Int) nothrow -> Int { return self.n - v; }
}

pub fn main() nothrow -> Int {
\tval b = Box(n = 40);
\tval s: sinkmod.Sink<Int> = move b;
\treturn 0;
}
"""

_DUP_COEXIST_MAIN = """\
module main;
import sinkmod;

struct Box { n: Int }

implement sinkmod.Sink<String> for Box {
\tfn put(self: &Box, v: String) nothrow -> Int { return self.n * 100; }
}

implement sinkmod.Sink<Int> for Box {
\tfn put(self: &Box, v: Int) nothrow -> Int { return self.n + v; }
}

fn use_int(s: &sinkmod.Sink<Int>) nothrow -> Int { return s.put(2); }
fn use_str(s: &sinkmod.Sink<String>) nothrow -> Int { return s.put("x"); }

pub fn main() nothrow -> Int {
\tval b3 = Box(n = 40);
\tval b4 = Box(n = 40);
\tif use_int(&b3) == 42 {
\t\tif use_str(&b4) == 4000 { return 0; }
\t\treturn 2;
\t}
\treturn 1;
}
"""


def _compile_two_modules(tmp_path: Path, mod_src: str, main_src: str) -> subprocess.CompletedProcess[str]:
	(tmp_path / "sinkmod.drift").write_text(mod_src)
	(tmp_path / "main.drift").write_text(main_src)
	out = tmp_path / "test_bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 "-M", str(tmp_path), str(tmp_path / "main.drift"), str(tmp_path / "sinkmod.drift"),
		 "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)


def test_duplicate_instance_impl_not_first_rejected(tmp_path: Path) -> None:
	"""[Sink<String>, Sink<Int>, Sink<Int>] — the exact-duplicate pair is
	not in first position and must still reject with E-IMPL-DUPLICATE."""
	res = _compile_two_modules(tmp_path, _DUP_IFACE_MOD, _DUP_NOT_FIRST_MAIN)
	assert res.returncode != 0, "duplicate-not-first must reject"
	err = res.stderr + res.stdout
	assert "E-IMPL-DUPLICATE" in err, err[-800:]


def test_distinct_instance_impls_cross_module_coexist(tmp_path: Path) -> None:
	"""Sink<String> + Sink<Int> for one type (imported interface, the
	trait-coherence-path classification) coexist and dispatch correctly."""
	res = _compile_two_modules(tmp_path, _DUP_IFACE_MOD, _DUP_COEXIST_MAIN)
	assert res.returncode == 0, f"coexist compile failed:\n{res.stderr[-1200:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"dispatch wrong (exit {run.returncode})"


_GENERIC_IFACE_OWNED_SOURCE = _GENERIC_IFACE_COMMON + """
fn use_int_sink(s: &Sink<Int>) nothrow -> Int {
\treturn s.put(2);
}

pub fn main() nothrow -> Int {
\tval b = Box(n = 40);
\tval s: Sink<Int> = move b;
\tif use_int_sink(s) == 42 { return 0; }
\treturn 1;
}
"""


def test_generic_interface_instance_widens(tmp_path: Path) -> None:
	"""`implement Sink<Int> for Box` lets `&Box` widen to `&Sink<Int>` —
	compiles and dispatches correctly (was deferred in initial 0.33.77)."""
	_run_ok(tmp_path, _GENERIC_IFACE_INSTANCE_SOURCE)


_MULTI_INSTANCE_WIDENED_SOURCE = _MULTI_INSTANCE_COMMON + """
pub fn main() nothrow -> Int {
\tval b3 = Box(n = 40);
\tval b4 = Box(n = 40);
\tval r3 = use_int_sink(&b3);
\tval r4 = use_str_sink(&b4);
\tif r3 == 42 {
\t\tif r4 == 4000 { return 0; }
\t\treturn 2;
\t}
\treturn 1;
}
"""


def test_multi_instance_widened_dispatch_correct(tmp_path: Path) -> None:
	"""Widened views route each interface instance through ITS OWN impl —
	the borrowed-view twin of the owned multi-instance dispatch pin."""
	_run_ok(tmp_path, _MULTI_INSTANCE_WIDENED_SOURCE)


_GENERIC_MUT_VIEW_SOURCE = """\
module main;

interface Store<T> {
\tfn set(self: &mut Self, v: T) nothrow -> Void;
\tfn get(self: &Self) nothrow -> Int;
}

struct Cell { n: Int }

implement Store<Int> for Cell {
\tfn set(self: &mut Cell, v: Int) nothrow -> Void {
\t\tself.n = v;
\t\treturn;
\t}
\tfn get(self: &Cell) nothrow -> Int { return self.n; }
}

fn bump(s: &mut Store<Int>, v: Int) nothrow -> Void {
\ts.set(v);
\treturn;
}

pub fn main() nothrow -> Int {
\tvar c = Cell(n = 1);
\tbump(&mut c, 42);
\tif c.n == 42 { return 0; }
\treturn 1;
}
"""


def test_generic_instance_mut_ref_widens_and_aliases(tmp_path: Path) -> None:
	"""`&mut Cell` → `&mut Store<Int>`: the mutation lands in the caller's
	value through the generic-instance view."""
	_run_ok(tmp_path, _GENERIC_MUT_VIEW_SOURCE)


def test_generic_interface_cross_instance_rejected(tmp_path: Path) -> None:
	"""The same impl must certainly not widen `&Box` to `&Sink<String>` —
	pins that the deferral never degrades into a base-keyed accept."""
	res = _compile(tmp_path, _GENERIC_IFACE_CROSS_INSTANCE_SOURCE)
	assert res.returncode != 0, "cross-instance widening must reject"
	err = res.stderr + res.stdout
	assert "Sink<String>" in err and "Box" in err, err[-1000:]


def test_generic_interface_owned_upcast_still_compiles(tmp_path: Path) -> None:
	"""OWNED upcast to a generic interface instance keeps working
	(certified-0.33.76 behavior) — the new implements-verification must
	DEFER for generic instances, not reject them."""
	_run_ok(tmp_path, _GENERIC_IFACE_OWNED_SOURCE)


def test_widened_ref_across_package_boundary(tmp_path: Path) -> None:
	"""A borrowed view passed into a PACKAGE-exported `&Interface` function
	must dispatch correctly — pins the impl-method seeding for
	`ConstructIfaceBorrowed` (found during the mixed-artifact validation:
	without seeding, the vtable thunk referenced an unemitted impl fn and
	clang failed with `use of undefined value`)."""
	lib_dir = tmp_path / "lib"
	lib_dir.mkdir()
	(lib_dir / "greet.drift").write_text(
		"module greet;\n\n"
		"export { Greeter, use_ref };\n\n"
		"pub interface Greeter {\n"
		"\tfn greet(self: &Self) nothrow -> String;\n"
		"}\n\n"
		"pub fn use_ref(g: &Greeter) nothrow -> String {\n"
		"\treturn g.greet();\n"
		"}\n"
	)
	pkg = tmp_path / "greet.dmp"
	emit = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "-M", str(lib_dir), str(lib_dir / "greet.drift"),
		 "--package-id", "greet", "--package-version", "0.0.1",
		 "--package-target", "test-target", "--emit-package", str(pkg)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert emit.returncode == 0, f"pkg emit failed:\n{emit.stderr[-1200:]}"
	app_dir = tmp_path / "app"
	app_dir.mkdir()
	(app_dir / "main.drift").write_text(
		"module main;\n"
		"import greet;\n\n"
		"struct EnglishGreeter { name: String }\n"
		"implement greet.Greeter for EnglishGreeter {\n"
		"\tfn greet(self: &EnglishGreeter) nothrow -> String { return \"Hello, \" + (self.name + \"\"); }\n"
		"}\n\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval e = EnglishGreeter(name = \"Mix\");\n"
		"\tif greet.use_ref(&e) == \"Hello, Mix\" { return 0; }\n"
		"\treturn 1;\n"
		"}\n"
	)
	out = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 "-M", str(app_dir), "--package-root", str(tmp_path),
		 "--dep", "greet@0.0.1", "--allow-unsigned-from", str(tmp_path),
		 str(app_dir / "main.drift"), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"consumer compile failed:\n{res.stderr[-1500:]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-500:]}"


def test_non_implementing_owned_upcast_clean_diagnostic(tmp_path: Path) -> None:
	"""Owned upcast of a non-implementing type: clean checker diagnostic,
	not the pre-slice codegen NotImplementedError traceback."""
	res = _compile(tmp_path, _NON_IMPLEMENTING_OWNED_SOURCE)
	assert res.returncode != 0, "non-implementing owned upcast must reject"
	err = res.stderr + res.stdout
	assert "Traceback" not in err and "NotImplementedError" not in err, (
		f"codegen ICE leaked instead of a checker diagnostic:\n{err[-1200:]}"
	)
	assert "Silent" in err and "Greeter" in err, (
		f"diagnostic must name the concrete and interface types:\n{err[-1000:]}"
	)
