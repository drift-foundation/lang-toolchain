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

The fix centralizes the rule in `_llvm_field_storage_type_for_typeid` (used by
construct/get/zero/tombstone/copy/clone and AddrOfField), sizes struct fields
accordingly, and makes LoadRef/StoreRef/MoveFromRef fat-aware for pointers
proven (via AddrOfField provenance) to address a throwing-Fn field slot.
Nothrow `Fn` fields remain thin — their layout and behavior are unchanged.

Because the fat representation exists ONLY in struct-field slots while every
other Fn slot is thin, values cannot cross between the two worlds through
references or by-value passing.  Rather than corrupting memory, those shapes
are now refused:

- `&s.run` / `&mut s.run` — rejected by the type checker with
  E_THROWING_FN_FIELD_BORROW (a callee would thin-load the fat slot: env lost,
  adapter called with the wrong signature → SIGSEGV before the fix).
- `&g` where `g = s.run` — the local's slot adapts to fat, so a reference to
  it has the same hazard; refused at codegen (AddrOfLocal guard).
- `apply(s.run, ...)` — a fat value cannot narrow into a thin fn-ptr param
  (the env word has nowhere to go); refused at codegen (call-arg guard, which
  previously surfaced as a raw clang insertvalue/call type error).

KNOWN SEAM (pre-existing, not covered here): merging a field-extracted fat
value with a thin named-fn reference at one phi (e.g. `match` arms yielding
`s.run` and a bare fn name) still raises "phi with mixed incoming types".
"""
from __future__ import annotations

import json
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


def _error_diags(tmp_path: Path, source: str) -> list[dict]:
	"""Compile-only and return the list of error-severity diagnostics (JSON)."""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--stdlib-root",
		 str(_stdlib()), "--test-build-only", str(src), "--json"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(40),
	)
	payload = json.loads(out.stdout) if out.stdout.strip() else {}
	return [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]


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
# Representation-seam containment: shapes that would narrow the fat pair into
# a thin slot are refused instead of corrupting memory.
# ---------------------------------------------------------------------------

_INVOKE_BY_REF = """
fn invoke(run_ref: &Fn(Int) -> Int) -> Int {
	val f = *run_ref;
	return try f(21) catch { -1 };
}
"""


def test_borrow_of_throwing_fn_field_rejected(tmp_path: Path) -> None:
	"""`&s.run` — was SIGSEGV (callee thin-loads the fat slot); now a checker error."""
	src = _PRELUDE + _INVOKE_BY_REF + """
pub fn main() nothrow -> Int {
	val op = OpBinding(name = "dbl", run = double);
	return (try invoke(&op.run) catch { -2 }) - 42;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_THROWING_FN_FIELD_BORROW" in codes, codes


def test_mut_borrow_of_throwing_fn_field_rejected(tmp_path: Path) -> None:
	"""`&mut s.run` — same rejection (was: compiled and returned garbage)."""
	src = _PRELUDE + """
fn rebind(run_ref: &mut Fn(Int) -> Int) -> Void {
	return;
}

pub fn main() nothrow -> Int {
	var op = OpBinding(name = "dbl", run = double);
	rebind(&mut op.run);
	return 0;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_THROWING_FN_FIELD_BORROW" in codes, codes


def test_borrow_of_local_holding_field_fn_refused(tmp_path: Path) -> None:
	"""`val g = op.run; invoke(&g)` — the local's slot adapts to fat, so the
	reference has the same thin-load hazard; refused at codegen (was SIGSEGV)."""
	src = _PRELUDE + _INVOKE_BY_REF + """
pub fn main() nothrow -> Int {
	val op = OpBinding(name = "dbl", run = double);
	val g = op.run;
	return (try invoke(&g) catch { -2 }) - 42;
}
"""
	res = _compile(tmp_path, src)
	assert res.returncode != 0, "borrowing a fat-valued local must not compile"
	assert "cannot take a reference" in res.stderr, res.stderr[-600:]


def test_byval_field_fn_arg_refused(tmp_path: Path) -> None:
	"""`apply(op.run, ...)` — a fat value cannot narrow into a thin fn-ptr
	param; refused with a targeted message (was a raw clang type error)."""
	src = _PRELUDE + """
fn apply(run: Fn(Int) -> Int, x: Int) -> Int {
	return try run(x) catch { -1 };
}

pub fn main() nothrow -> Int {
	val op = OpBinding(name = "dbl", run = double);
	return (try apply(op.run, 21) catch { -2 }) - 42;
}
"""
	res = _compile(tmp_path, src)
	assert res.returncode != 0, "passing a fat field value by value must not compile"
	assert "cannot pass a struct-field Fn value" in res.stderr, res.stderr[-600:]


def test_ref_to_local_named_fn_still_works(tmp_path: Path) -> None:
	"""`val h = double; invoke(&h)` — all-thin cross-function ref keeps working."""
	src = _PRELUDE + _INVOKE_BY_REF + """
pub fn main() nothrow -> Int {
	val h = double;
	return (try invoke(&h) catch { -2 }) - 42;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-400:]}"


def test_optional_fn_named_payload_still_works(tmp_path: Path) -> None:
	"""Variant payloads stay thin and self-consistent for named fns."""
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
