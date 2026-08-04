# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""A/B lowering-route ownership parity for reject-redundant-call-borrows
(D5 §D) and the bare-temp field/index-projection UAF fix
(accepted 0.34.1; doc/history.md 2026-07-31).

Two lowering routes, one contract
---------------------------------
A shared borrow of a projection off an owned rvalue reaches MIR by two
different routes, and that is BY DESIGN, not a defect:

  * The EXPLICIT source spelling (`peek(&mk().root)`) is normalized by
    stage1 `BorrowMaterializeRewriter` before MIR lowering.
  * The BARE spelling (`peek(mk().root)`) receives its synthetic
    `HBorrow(source_written=False)` from the checker AFTER stage1 runs,
    so it is lifted during MIR lowering (`_validate_lifted_chain`).

The two routes therefore emit DIFFERENT LLVM IR. That is expected and is
NOT a language-contract failure — an earlier note (0.33.91) that the
surviving spelling was "IR byte-identical" overstated the guarantee. The
real compatibility promise is SEMANTIC parity: identical observable
result, identical scope-end drop timing, and exactly-one-drop of the
owned payload. This module asserts that parity under base / ASan /
memcheck, plus a path-specific STRUCTURAL check on the bare route (one
owning base materialized, field address-projected, no second owned leaf
temp). It never compares whole IR text.

The baseline that the rule forbids as source (`peek(&mk().root)` →
E_REDUNDANT_ARG_BORROW) is reached PROGRAMMATICALLY: the explicit-shape
program is parsed, then every `HBorrow.source_written` flag is cleared,
producing the compiler-synthesized borrow shape the rule permits, and
driven through the FULL pipeline (HIR → MIR → LLVM → clang link →
EXECUTION). A borrow-checker-only harness does NOT satisfy this gate —
the gate is about runtime temp lifetime, so the baseline must run
(ratified D5 constraint, review 2026-07-29; A/B parity re-ratified
2026-07-31).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_EXPLICIT_SHAPE = """\
module main;

import std.core as core;

struct Session { drops: Int }

struct Token { session: &mut Session }

implement core.Destructible for Token {
	pub fn destroy(self: Token) nothrow -> Void {
		self.session.drops = self.session.drops + 1;
	}
}

fn mk(sess: &mut Session) nothrow -> Token {
	return Token(session = sess);
}

fn probe(t: &Token) nothrow -> Int {
	return 1;
}

pub fn main() nothrow -> Int {
	var sess = Session(drops = 0);
	var mid: Int = -1;
	{
		val a = probe(&mk(sess));
		if a != 1 { return 3; }
		mid = sess.drops;
	}
	val after = sess.drops;
	if mid != 0 { return 1; }
	if after != 1 { return 2; }
	return 0;
}
"""


# --- CF (field projection) and IDX (index projection) A/B programs -------
#
# CF carries a Destructible leaf whose `destroy` bumps a &mut counter, so
# scope-end drop TIMING is directly observable (mid == pass before the
# scope end, after == pass+1 exactly once).  It also owns a heap String
# so ASan/memcheck exercise the payload.  IDX indexes an owning Array
# whose element owns a heap String (an Array cannot carry a &mut-counter
# element in v1), so exactly-one-drop is asserted by the sanitizers
# (double-free → ASan/memcheck abort, leak → memcheck) with observable
# result + liveness-at-use as the semantic pin.  Both loop THREE passes so
# a premature free corrupts a later pass (the multi-pass memcheck
# sensitivity the trigger asks for; this lane is valgrind memcheck, not a
# separate DRIFT_ALLOC_TRACK harness).


def _cf_program(spelling: str) -> str:
	return f"""\
module main;

import std.core as core;

struct Session {{ drops: Int }}

struct Leaf {{ session: &mut Session, tag: String }}

implement core.Destructible for Leaf {{
	pub fn destroy(self: Leaf) nothrow -> Void {{
		self.session.drops = self.session.drops + 1;
	}}
}}

struct Wrap {{ leaf: Leaf }}

fn mk(sess: &mut Session) nothrow -> Wrap {{
	return Wrap(leaf = Leaf(session = sess, tag = "leaf" + ""));
}}

fn peek(l: &Leaf) nothrow -> Int {{ return l.tag.byte_length(); }}

pub fn main() nothrow -> Int {{
	var sess = Session(drops = 0);
	var pass = 0;
	while pass < 3 {{
		var mid: Int = -1;
		{{
			val a = peek({spelling});
			if a != 4 {{ return 10; }}
			mid = sess.drops;
		}}
		val after = sess.drops;
		if mid != pass {{ return 20; }}
		if after != pass + 1 {{ return 21; }}
		pass = pass + 1;
	}}
	if sess.drops != 3 {{ return 30; }}
	return 0;
}}
"""


def _idx_program(spelling: str) -> str:
	return f"""\
module main;

struct Holder {{ tag: String }}

fn mk() nothrow -> Array<Holder> {{
	var xs: Array<Holder> = [];
	xs.push(Holder(tag = "leaf" + ""));
	return move xs;
}}

fn peek(h: &Holder) nothrow -> Int {{ return h.tag.byte_length(); }}

pub fn main() nothrow -> Int {{
	var pass = 0;
	while pass < 3 {{
		val a = peek({spelling});
		if a != 4 {{ return 10; }}
		pass = pass + 1;
	}}
	return 0;
}}
"""


# The bare vs explicit projection spellings for each shape.
_CF_BARE = "mk(sess).leaf"
_CF_EXPLICIT = "&mk(sess).leaf"
_IDX_BARE = "mk()[0]"
_IDX_EXPLICIT = "&mk()[0]"


def _clear_source_written(node, seen: set[int]) -> int:
	"""Recursively clear HBorrow.source_written across a HIR tree."""
	from lang.driftc.stage1 import hir_nodes as H

	if id(node) in seen:
		return 0
	seen.add(id(node))
	cleared = 0
	if isinstance(node, H.HBorrow) and getattr(node, "source_written", False):
		node.source_written = False
		cleared += 1
	for field_name in getattr(node, "__dataclass_fields__", {}) or {}:
		val = getattr(node, field_name, None)
		if isinstance(val, (list, tuple)):
			for item in val:
				if hasattr(item, "__dataclass_fields__"):
					cleared += _clear_source_written(item, seen)
		elif hasattr(val, "__dataclass_fields__"):
			cleared += _clear_source_written(val, seen)
	return cleared


def _compile_ir(src_text: str, tmp_path: Path, *, clear_source_written: bool,
                expect_cleared: int | None = None, name: str = "src") -> str:
	"""Parse `src_text`, optionally clear every HBorrow.source_written in
	module `main`, and compile the FULL pipeline to LLVM IR. Sources are
	written under the caller's pytest `tmp_path` (auto-cleaned) — one
	subdir per `name` so repeated compiles in a test do not collide."""
	from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
	from lang.driftc.module_lowered import flatten_modules
	from lang.driftc import driftc as D
	from lang.driftc.core.function_id import function_symbol

	tmp = tmp_path / name
	tmp.mkdir(parents=True, exist_ok=True)
	src = tmp / "main.drift"
	src.write_text(src_text)
	modules, type_table, exc, mexp, mdeps, pdiags = parse_drift_workspace_to_hir(
		[src], stdlib_root=stdlib_root(), test_build_only=True
	)
	assert not pdiags, [d.message for d in pdiags]
	func_hirs, signatures, _ = flatten_modules(modules)
	if clear_source_written:
		cleared = 0
		seen: set[int] = set()
		for fn_id, fh in func_hirs.items():
			if fn_id.module == "main":
				cleared += _clear_source_written(fh, seen)
		if expect_cleared is not None:
			assert cleared == expect_cleared, (
				f"expected to clear {expect_cleared} explicit borrow(s), cleared {cleared}"
			)
		else:
			assert cleared >= 1, f"expected to clear at least one explicit borrow, cleared {cleared}"
	main_id = [i for i, s in signatures.items() if i.name == "main" and not s.is_method][0]
	ir, checked = D.compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc,
		entry=function_symbol(main_id),
		type_table=type_table,
		module_exports=mexp,
		module_deps=mdeps,
		origin_by_fn_id={},
		enforce_entrypoint=True,
		reserved_namespace_policy=D.ReservedNamespacePolicy.ALLOW_DEV,
	)
	errors = [d for d in getattr(checked, "diagnostics", []) if getattr(d, "severity", None) == "error"]
	assert not errors, [d.message for d in errors]
	return ir


def _build_ir(tmp_path: Path) -> tuple[str, int]:
	"""Back-compat helper for the R-2 whole-call baseline: compile the
	explicit shape with source_written cleared (exactly one outer borrow)."""
	ir = _compile_ir(_EXPLICIT_SHAPE, tmp_path, clear_source_written=True, expect_cleared=1)
	return ir, 1


def _rt_archive(profile: str) -> Path:
	from lang.versions import DRIFT_RT_ABI_VERSION

	return ROOT / "build" / "runtime_libs" / profile / f"libdrift_rt_abi{DRIFT_RT_ABI_VERSION}.a"


def _link(ir: str, tmp_path: Path, *, asan: bool, name: str = "ab") -> Path:
	ll = tmp_path / f"{name}{'_asan' if asan else ''}.ll"
	ll.write_text(ir)
	out = tmp_path / f"{name}{'_asan' if asan else ''}.bin"
	cmd = ["clang", "-fuse-ld=gold"]
	profile = "default"
	if asan:
		cmd += ["-fsanitize=address", "-g", "-fsanitize=undefined", "-fno-sanitize-recover=undefined"]
		profile = "asan_ubsan"
	cmd += ["-O2", "-x", "ir", str(ll), "-x", "none", str(_rt_archive(profile)), "-lz", "-Wl,--as-needed", "-o", str(out)]
	res = subprocess.run(cmd, capture_output=True, text=True, timeout=sanitizer_timeout(240))
	assert res.returncode == 0, f"link failed:\n{res.stderr[-1200:]}"
	return out


def _run(binary: Path, *, timeout_s: int = 30) -> subprocess.CompletedProcess:
	return subprocess.run([str(binary)], capture_output=True, text=True, timeout=sanitizer_timeout(timeout_s))


def _valgrind(binary: Path) -> None:
	if not shutil.which("valgrind"):
		return
	vg = subprocess.run(
		["valgrind", "--error-exitcode=97", "--leak-check=full",
		 "--errors-for-leak-kinds=definite", str(binary)],
		capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert vg.returncode == 0, f"valgrind: exit {vg.returncode}\n{vg.stderr[-1200:]}"


def _user_main_ir(ir: str) -> str:
	"""Return the body of the user `main` function (`@drift_main`), not the
	`@main` runtime shim that only calls `drift_run_main_on_vt`."""
	lines = ir.splitlines()
	out: list[str] = []
	cap = False
	for ln in lines:
		if re.match(r"define .*@drift_main\b", ln):
			cap = True
		if cap:
			out.append(ln)
			if ln.strip() == "}":
				break
	return "\n".join(out)


# --- R-2 whole-call baseline (unchanged D5 gate) -------------------------


def test_r2_programmatic_baseline_full_pipeline_plain_and_memcheck(tmp_path: Path) -> None:
	"""Baseline (source_written=False, explicit whole-call shape) compiles
	through the full pipeline and EXECUTES with the pinned drop behavior;
	also under valgrind when available (memcheck half of the gate)."""
	ir, _ = _build_ir(tmp_path)
	binary = _link(ir, tmp_path, asan=False, name="r2")
	run = _run(binary)
	assert run.returncode == 0, f"baseline drop parity broken (exit {run.returncode})"
	_valgrind(binary)


def test_r2_programmatic_baseline_full_pipeline_asan(tmp_path: Path) -> None:
	"""ASan/UBSan half of the gate for the same baseline binary."""
	archive = _rt_archive("asan_ubsan")
	if not archive.exists():
		pytest.skip(f"asan runtime archive not built: {archive}")
	ir, _ = _build_ir(tmp_path)
	binary = _link(ir, tmp_path, asan=True, name="r2")
	run = _run(binary, timeout_s=60)
	assert run.returncode == 0, run.stderr[-1200:]
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-1200:]


def test_explicit_source_shape_is_rejected(tmp_path: Path) -> None:
	"""A-half sanity: the SAME program with source_written intact (compiled
	from source) is rejected by the rule — proving the baseline is only
	reachable programmatically."""
	src = tmp_path / "main.drift"
	src.write_text(_EXPLICIT_SHAPE)
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(tmp_path / "x.bin")],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode != 0
	assert "E_REDUNDANT_ARG_BORROW" in (res.stderr + res.stdout)


# --- A/B ownership parity: field (CF) and index (IDX) projections --------

_AB_CASES = [
	("cf_field", _cf_program, _CF_BARE, _CF_EXPLICIT),
	("idx_index", _idx_program, _IDX_BARE, _IDX_EXPLICIT),
]


@pytest.mark.parametrize("label,builder,bare,explicit", _AB_CASES,
                         ids=[c[0] for c in _AB_CASES])
def test_ab_ownership_parity_base_and_memcheck(tmp_path: Path, label, builder, bare, explicit) -> None:
	"""Bare and programmatic explicit-baseline routes of the SAME shape
	must produce the identical observable result and exactly-one-drop
	behavior, under base then valgrind memcheck (three passes, so a
	premature free corrupts a later pass)."""
	bare_ir = _compile_ir(builder(bare), tmp_path, clear_source_written=False, name=f"{label}_bare_ir")
	expl_ir = _compile_ir(builder(explicit), tmp_path, clear_source_written=True, expect_cleared=1, name=f"{label}_expl_ir")

	bare_bin = _link(bare_ir, tmp_path, asan=False, name=f"{label}_bare")
	expl_bin = _link(expl_ir, tmp_path, asan=False, name=f"{label}_expl")

	bare_run = _run(bare_bin)
	expl_run = _run(expl_bin)
	# Observable-result parity + drop-timing/exactly-once (rc 0 encodes the
	# per-pass mid==pass / after==pass+1 checks for CF; for IDX rc 0 pins
	# liveness-at-use and the sanitizers below pin exactly-once).
	assert bare_run.returncode == 0, f"bare {label} exit {bare_run.returncode}\n{bare_run.stdout}"
	assert expl_run.returncode == 0, f"explicit-baseline {label} exit {expl_run.returncode}"
	assert bare_run.returncode == expl_run.returncode, "A/B observable-result parity broken"

	_valgrind(bare_bin)
	_valgrind(expl_bin)


@pytest.mark.parametrize("label,builder,bare,explicit", _AB_CASES,
                         ids=[c[0] for c in _AB_CASES])
def test_ab_ownership_parity_asan(tmp_path: Path, label, builder, bare, explicit) -> None:
	"""ASan/UBSan half: both routes are free of use-after-free / double
	free on the owned payload (the exactly-one-drop proof for the
	array-element IDX row, and a second check for CF)."""
	archive = _rt_archive("asan_ubsan")
	if not archive.exists():
		pytest.skip(f"asan runtime archive not built: {archive}")
	bare_ir = _compile_ir(builder(bare), tmp_path, clear_source_written=False, name=f"{label}_bare_ir")
	expl_ir = _compile_ir(builder(explicit), tmp_path, clear_source_written=True, expect_cleared=1, name=f"{label}_expl_ir")
	for tag, ir in (("bare", bare_ir), ("expl", expl_ir)):
		binary = _link(ir, tmp_path, asan=True, name=f"{label}_{tag}")
		run = _run(binary, timeout_s=60)
		assert run.returncode == 0, f"{label}/{tag}: {run.stderr[-1200:]}"
		assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-1200:]


def test_bare_cf_structural_one_base_addr_project(tmp_path: Path) -> None:
	"""Path-specific STRUCTURAL pin for the bare field-projection route
	(no whole-IR comparison): the checker-synthesized borrow must
	materialize exactly ONE owning base (the Wrap temp) and ADDRESS-project
	the leaf field into it — NOT copy the leaf into a second owned temp
	(the pre-fix double-owner shape that double-freed at teardown)."""
	ir = _compile_ir(_cf_program(_CF_BARE), tmp_path, clear_source_written=False)
	body = _user_main_ir(ir)
	base_allocas = re.findall(r"alloca %Struct_main_Wrap_[0-9a-f]+", body)
	leaf_allocas = re.findall(r"alloca %Struct_main_Leaf_[0-9a-f]+", body)
	# Exactly one owning base temp; NO second owned leaf temp.
	assert len(base_allocas) == 1, f"expected one owning Wrap base, got {len(base_allocas)}:\n{body}"
	assert len(leaf_allocas) == 0, f"expected no second owned Leaf temp, got {len(leaf_allocas)}:\n{body}"
	# The leaf is reached by an address projection INTO the base temp, and
	# that pointer is what feeds @peek.
	gep = re.search(
		r"(%[\w.]+) = getelementptr inbounds %Struct_main_Wrap_[0-9a-f]+, ptr (%[\w.]+)", body)
	assert gep is not None, f"expected an address projection into the Wrap base:\n{body}"
	proj_ptr, base_ptr = gep.group(1), gep.group(2)
	assert "borrow_tmp" in base_ptr, f"projection base is not the materialized borrow temp: {base_ptr}"
	assert re.search(rf"call i64 @peek\(ptr {re.escape(proj_ptr)}\)", body), (
		f"@peek is not called on the address-projected leaf {proj_ptr}:\n{body}")


# --- &mut field/index projection: two source spellings, pinned SEPARATELY -
#
# The two &mut source spellings legitimately differ in shape and
# diagnostic; we pin each from SOURCE and do NOT manufacture equivalence
# through a programmatic source_written bypass (stage1 has already changed
# the explicit shape).


def _driftc_source(src_text: str, tmp_path: Path) -> subprocess.CompletedProcess:
	src = tmp_path / "main.drift"
	src.write_text(src_text)
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(tmp_path / "x.bin")],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)


_MUT_PROGRAM = """\
module main;
struct Node {{ text: String }}
struct PR {{ root: Node }}
fn mk() nothrow -> PR {{ return PR(root = Node(text = "x" + "")); }}
fn bump(n: &mut Node) nothrow -> Void {{ n.text = "y"; }}
pub fn main() nothrow -> Int {{ bump({spelling}); return 0; }}
"""


def test_mut_bare_field_projection_rejected_bind_first(tmp_path: Path) -> None:
	"""Bare `bump(mk().root)` at &mut: a mutable borrow of temp-derived
	storage has no argument spelling — rejected bind-first.  Since 0.34.1 the
	bare mutable-rvalue ARGUMENT shares the stable
	E_MUT_RVALUE_ARG_BINDING_REQUIRED category with the explicit `&mut`
	spelling (its message stays context-appropriate: "addressable place; bind
	to a local first")."""
	res = _driftc_source(_MUT_PROGRAM.format(spelling="mk().root"), tmp_path)
	assert res.returncode != 0
	out = res.stderr + res.stdout
	assert "borrow requires an addressable place; bind to a local first" in out, out[-1200:]
	assert "E_MUT_RVALUE_ARG_BINDING_REQUIRED" in out, out[-1200:]


def test_mut_explicit_field_projection_rejected_redundant_or_mut_rvalue(tmp_path: Path) -> None:
	"""Explicit `bump(&mut mk().root)` at &mut: the SOURCE-written `&mut`
	yields the mutable-rvalue diagnostic with the same NAMED code as the bare
	form (E_MUT_RVALUE_ARG_BINDING_REQUIRED) — one stable category, distinct
	context-appropriate message ("mutable borrow of a temporary …")."""
	res = _driftc_source(_MUT_PROGRAM.format(spelling="&mut mk().root"), tmp_path)
	assert res.returncode != 0
	out = res.stderr + res.stdout
	assert "E_MUT_RVALUE_ARG_BINDING_REQUIRED" in out, out[-1200:]
