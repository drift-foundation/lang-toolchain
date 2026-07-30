# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Throwing `Fn` stored in a struct field — fat-pointer storage agreement (CORE_BUG).

A may-throw `Fn(...) -> T` struct field is stored fat (`%DriftFatFnPtr =
{adapter, env}`), and the struct *declaration* plus the `ConstructStruct` /
`StructGetField` paths knew that — but the generic aggregate storage mapper
still answered thin `ptr`, so every other field-level path disagreed with the
declared layout.  Three distinct failures fell out:

1. Zero/tombstone materialization emitted `insertvalue ... ptr null` into the
   `{ptr, ptr}` field → clang rejected the module ("insertvalue operand and
   field disagree in type").  This was the reported failure: any droppable
   struct (e.g. one with a String field) plus a throwing-Fn field.
2. `_size_align_typeid` sized the field via the 8-byte fallback while the real
   layout is 16 bytes → `Array<OpBinding>` element stride was 8 short → heap
   corruption (`malloc(): corrupted top size`) on the second push.
3. The place-projection path (`ops[i].run` → AddrOfField + LoadRef) loaded one
   thin word out of the fat slot and called the forwarding thunk without its
   env argument → SIGSEGV.

The fix centralizes the rule in `_llvm_storage_type_for_typeid` /
`_llvm_type_for_typeid`: every throwing `Fn(...) -> T` value uses the fat
representation, including struct fields, params, locals, array elements,
variant payloads, return values, and phi values.  Nothrow `Fn` values remain
thin — their layout and behavior are unchanged.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _stdlib() -> Path:
	return stdlib_root() or (ROOT / "stdlib")


def _compile(tmp_path: Path, source: str, entry: str = "main::main") -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(_stdlib()),
		 str(src), "--entry", entry, "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	res = _compile(tmp_path, source)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	return subprocess.run([str(tmp_path / "bin")], capture_output=True, text=True, timeout=sanitizer_timeout(20))


_PRELUDE = """\
module main;

struct OpBinding {
	name: String,
	run: Fn(Int) -> Int
}

fn double(x: Int) -> Int { return x * 2; }
fn triple(x: Int) -> Int { return x * 3; }

fn bind(name: String, run: Fn(Int) -> Int) -> OpBinding {
	return OpBinding(name = name, run = run);
}
"""

# 1. The reported shape: droppable struct + throwing Fn field, direct ctor with
#    a named fn, field extract, call.  Failed with invalid LLVM IR (clang
#    reject) before the fix.
_SRC_DIRECT = _PRELUDE + """
pub fn main() nothrow -> Int {
	val b = OpBinding(name = "dbl", run = double);
	val f = b.run;
	val n = try f(21) catch { -1 };
	return n - 42;
}
"""

# 2. Construction through a helper whose `run` arrived as a parameter (the
#    fn-ptr's throw-state must be resolved from the MIR local type).
_SRC_HELPER = _PRELUDE + """
pub fn main() nothrow -> Int {
	val b = bind("tri", triple);
	val f = b.run;
	val n = try f(14) catch { -1 };
	return n - 42;
}
"""

# 3. The catalog shape that motivated the report: Array<OpBinding> populated
#    via the helper, indexed field call in a loop (AddrOfField + LoadRef on
#    the fat slot), pop + match (element-take tombstone), field mutation with
#    a named fn (StoreRef), and fat-to-fat propagation (ctor + StoreRef from
#    another struct's field).  Before the fix this hit, in order: invalid IR,
#    then heap corruption (short element stride), then SIGSEGV (thin load of
#    the fat slot).
_SRC_CATALOG = _PRELUDE + """
pub fn main() nothrow -> Int {
	val direct = OpBinding(name = "dbl", run = double);
	val f0 = direct.run;
	val a = try f0(10) catch { -1 };

	var ops: Array<OpBinding> = [];
	ops.push(bind("dbl", double));
	ops.push(bind("tri", triple));
	var sum = 0;
	var i = 0;
	while i < ops.len() {
		val g = ops[i].run;
		sum = sum + (try g(i + 1) catch { -100 });
		i = i + 1;
	}

	val last = ops.pop();
	val h = match last {
		Some(op) => { op.run },
		None => { direct.run }
	};
	val c = try h(5) catch { -1 };

	var m = bind("one", double);
	m.run = triple;
	val f2 = m.run;
	val d = try f2(3) catch { -1 };

	val cross = OpBinding(name = "x", run = direct.run);
	val f3 = cross.run;
	val e = try f3(4) catch { -1 };
	m.run = cross.run;
	val f4 = m.run;
	val q = try f4(5) catch { -1 };

	// a=20, sum=2+6=8, c=15, d=9, e=8, q=10 -> total 70
	return a + sum + c + d + e + q - 70;
}
"""

# 4. Nothrow guard: `Fn(...) nothrow -> T` fields stay thin and keep working.
_SRC_NOTHROW = """\
module main;

struct OpBinding {
	name: String,
	run: Fn(Int) nothrow -> Int
}

fn double(x: Int) nothrow -> Int { return x * 2; }

pub fn main() nothrow -> Int {
	val b = OpBinding(name = "dbl", run = double);
	val f = b.run;
	return f(21) - 42;
}
"""


def test_throwing_fn_field_direct_ctor(tmp_path: Path) -> None:
	run = _compile_and_run(tmp_path, _SRC_DIRECT)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_throwing_fn_field_ctor_through_helper_param(tmp_path: Path) -> None:
	run = _compile_and_run(tmp_path, _SRC_HELPER)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_throwing_fn_field_array_catalog(tmp_path: Path) -> None:
	run = _compile_and_run(tmp_path, _SRC_CATALOG)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_nothrow_fn_field_still_thin_and_working(tmp_path: Path) -> None:
	run = _compile_and_run(tmp_path, _SRC_NOTHROW)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


# ---------------------------------------------------------------------------
# Uniform fat representation: shapes that previously crossed the field-only
# fat/thin seam must now compile and run.
# ---------------------------------------------------------------------------

_INVOKE_BY_REF = """
fn invoke(run_ref: &Fn(Int) -> Int) -> Int {
	val f = *run_ref;
	return try f(21) catch { -1 };
}
"""


def test_borrow_of_throwing_fn_field_cross_function(tmp_path: Path) -> None:
	"""`&s.run` — was SIGSEGV when the callee thin-loaded the fat slot."""
	src = _PRELUDE + _INVOKE_BY_REF + """
pub fn main() nothrow -> Int {
	val op = OpBinding(name = "dbl", run = double);
	return (try invoke(op.run) catch { -2 }) - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_mut_borrow_of_throwing_fn_field_cross_function(tmp_path: Path) -> None:
	"""`&mut s.run` stores through a cross-function ref without truncating the pair."""
	src = _PRELUDE + """
fn rebind(run_ref: &mut Fn(Int) -> Int) -> Void {
	run_ref = triple;
	return;
}

pub fn main() nothrow -> Int {
	var op = OpBinding(name = "dbl", run = double);
	rebind(op.run);
	val f = op.run;
	val n = try f(14) catch { -1 };
	return n - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_borrow_of_local_holding_field_fn_cross_function(tmp_path: Path) -> None:
	"""`val g = op.run; invoke(&g)` — was SIGSEGV through an addr-taken local."""
	src = _PRELUDE + _INVOKE_BY_REF + """
pub fn main() nothrow -> Int {
	val op = OpBinding(name = "dbl", run = double);
	val g = op.run;
	return (try invoke(g) catch { -2 }) - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_byval_field_fn_arg_cross_function(tmp_path: Path) -> None:
	"""`apply(op.run, ...)` passes the full adapter/env pair by value."""
	src = _PRELUDE + """
fn apply(run: Fn(Int) -> Int, x: Int) -> Int {
	return try run(x) catch { -1 };
}

pub fn main() nothrow -> Int {
	val op = OpBinding(name = "dbl", run = double);
	return (try apply(op.run, 21) catch { -2 }) - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_ref_to_local_named_fn_still_works(tmp_path: Path) -> None:
	"""`val h = double; invoke(&h)` works with the new throwing-Fn fat local."""
	src = _PRELUDE + _INVOKE_BY_REF + """
pub fn main() nothrow -> Int {
	val h = double;
	return (try invoke(h) catch { -2 }) - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_optional_fn_named_payload_still_works(tmp_path: Path) -> None:
	"""Variant payloads carry throwing named fns as fat values."""
	src = """\
module main;

fn double(x: Int) -> Int { return x * 2; }

pub fn main() nothrow -> Int {
	val maybe: Optional<Fn(Int) -> Int> = Some(double);
	val n = match maybe {
		Some(f) => { try f(21) catch { -1 } },
		None => { -2 }
	};
	return n - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_optional_fn_field_payload_works(tmp_path: Path) -> None:
	"""`Some(op.run)` used to surface a raw ConstructVariant type mismatch."""
	src = _PRELUDE + """
pub fn main() nothrow -> Int {
	val op = OpBinding(name = "dbl", run = double);
	val maybe: Optional<Fn(Int) -> Int> = Some(op.run);
	val n = match maybe {
		Some(f) => { try f(21) catch { -1 } },
		None => { -2 }
	};
	return n - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_array_fn_push_field_payload_works(tmp_path: Path) -> None:
	"""`Array<Fn>.push(op.run)` used to emit a raw clang store type error."""
	src = _PRELUDE + """
pub fn main() nothrow -> Int {
	val op = OpBinding(name = "tri", run = triple);
	var runs: Array<Fn(Int) -> Int> = [];
	runs.push(double);
	runs.push(op.run);
	val a = try runs[0](9) catch { -1 };
	val b = try runs[1](8) catch { -1 };
	return a + b - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_throwing_fn_return_value_from_field_works(tmp_path: Path) -> None:
	"""Returning `op.run` used the signature thin slot before uniform fat lowering."""
	src = _PRELUDE + """
fn pick(op: OpBinding) -> Fn(Int) -> Int {
	return op.run;
}

pub fn main() nothrow -> Int {
	val op = OpBinding(name = "dbl", run = double);
	val f = pick(move op);
	val n = try f(21) catch { -1 };
	return n - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_nothrow_fn_returns_throwing_fn_from_nothrow_named(tmp_path: Path) -> None:
	"""A nothrow fn returning a throwing `Fn` must widen a thin nothrow named-fn
	value to the fat pair at the return boundary (was: ICE, no %DriftFatFnPtr
	arm in _emit_nothrow_return_value and no coercion on the nothrow path)."""
	src = """\
module main;

fn dbl_nt(x: Int) nothrow -> Int { return x * 2; }

fn pick() nothrow -> Fn(Int) -> Int {
	return dbl_nt;
}

pub fn main() nothrow -> Int {
	val f = pick();
	val n = try f(21) catch { -1 };
	return n - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_nothrow_fn_returns_throwing_fn_from_field(tmp_path: Path) -> None:
	"""A nothrow fn returning `op.run` passes the fat pair through `ret` (was:
	same ICE — the fat llty was not an accepted nothrow return type)."""
	src = _PRELUDE + """
fn pick(op: OpBinding) nothrow -> Fn(Int) -> Int {
	return op.run;
}

pub fn main() nothrow -> Int {
	val op = OpBinding(name = "dbl", run = double);
	val f = pick(move op);
	val n = try f(21) catch { -1 };
	return n - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_phi_mixes_field_and_named_throwing_fn(tmp_path: Path) -> None:
	"""A match yielding `op.run` in one arm and `double` in another used to ICE at phi lowering."""
	src = _PRELUDE + """
fn choose(op: OpBinding, want_field: Bool) -> Fn(Int) -> Int {
	return match want_field {
		true => { op.run },
		false => { double }
	};
}

pub fn main() nothrow -> Int {
	val op1 = OpBinding(name = "tri", run = triple);
	val op2 = OpBinding(name = "tri", run = triple);
	val f = choose(move op1, true);
	val g = choose(move op2, false);
	val a = try f(10) catch { -1 };
	val b = try g(6) catch { -1 };
	return a + b - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"
