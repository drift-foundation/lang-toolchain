# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: projected lambda captures (`p.field`, not a whole local `p`)
were broken end-to-end at the LOWERING level, independent of the boxed-
callback UAF fixed in 0.33.69.

A plain immediate-invoked lambda with an IMPLICIT REF-kind projected capture
(no `captures(...)` clause at all — not the MOVE-kind path the 0.33.69 fix
rejects) crashed LLVM codegen:

	fn use_it(p: Prepared) -> Int {
		return (| | => { return p.count + 1; })();   // p.count : Int (Copy)
	}
	# => NotImplementedError: LLVM codegen v1: integer binop requires
	#    matching Int/Uint operands (have %Struct_main_Prepared_..., drift.int)

Root cause (see work/callback-env-uaf-ref-args/research-copy-projected-captures.md
for the full trace): outer lowering correctly records the projected capture's
env slot as `&Int`, but the driftc.py hidden-lambda worklist re-derives the
slot's type from the CAPTURE ROOT's origin type (the whole `Prepared` struct)
at several sites that all key by root binding id, ignoring `key.proj`:
  - the env_field_types[slot] overwrite,
  - the root-named _local_types preseed,
  - the candidate-slot-type preseed fallback (when the root's own type is
    unresolved),
  - the name-to-slot fallback table (collides when a root has >1 projection).
`_emit_lambda_capture_prologue` then materializes a body-visible local named
after the ROOT for every slot (including projected ones), storing the
(corrupted) slot value into it — for a projection this is a bogus root-named
local; for two projections of the same root, a name collision.

Once the metadata/prologue bugs are fixed, two more gaps (verified directly
in the code, not just inferred) would silently reopen a UAF for non-bitcopy
fields specifically:
  - COPY-branch env construction never called `_copy_if_ref_alias` before
    storing the captured value into the closure's heap env — every OTHER
    ownership-transfer boundary (struct/variant construct, return, call arg)
    already does.
  - `_load_capture_from_env`'s REF/REF_MUT branch never marked its `LoadRef`
    result in `_ref_field_temps`, unlike the structurally-identical general
    deref path — so a REF-projected read of a non-bitcopy field (`&String`)
    was invisible to every downstream ownership-transfer-boundary copy.

This file covers the LOWERING MECHANISM directly (immediate-invoked lambdas,
which don't need `capture_as_move`/boxed-callback machinery to exercise
projected captures). See `test_boxed_callback_projected_move_capture_rejected.py`
for the boxed-callback-specific MOVE-vs-COPY capture-kind decision (which
reuses this same lowering once it's fixed).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

# The exact repro from the design doc: an implicit REF-kind projected
# capture (no `captures(...)` clause) inside an immediate-invoked lambda.
_REF_PROJECTED_IMMEDIATE_SOURCE = """\
module main;

struct Prepared {
\tcount: Int,
}

fn use_it(p: Prepared) -> Int {
\treturn (| | => { return p.count + 1; })();
}

pub fn main() nothrow -> Int {
\tval p = Prepared(count = 41);
\tif use_it(p) == 42 {
\t\treturn 0;
\t}
\treturn 1;
}
"""

# Two projections of the same root (`p.count` AND `p.name`) read inside the
# SAME lambda body -- proves the prologue skip doesn't collide two slots
# under one root-named local (both would otherwise `_canonical_local(p,"p")`
# to the same name, and the second StoreLocal would clobber the first).
_TWO_PROJECTIONS_ONE_ROOT_SOURCE = """\
module main;

struct Prepared {
\tcount: Int,
\tflag: Bool,
}

fn use_it(p: Prepared) -> Int {
\treturn (| | => {
\t\tif p.flag {
\t\t\treturn p.count + 1;
\t\t}
\t\treturn p.count;
\t})();
}

pub fn main() nothrow -> Int {
\tval p = Prepared(count = 41, flag = true);
\tif use_it(p) == 42 {
\t\treturn 0;
\t}
\treturn 1;
}
"""

# Bare root `p` (whole struct, REF kind) AND a projection `p.count` of the
# SAME root, both used in one lambda body -- proves the `_overlaps` REF/REF
# allowance holds and the root gets a real local while the projection does
# not (design doc §6, third bullet).
_BARE_ROOT_PLUS_PROJECTION_SOURCE = """\
module main;

struct Prepared {
\tcount: Int,
\tflag: Bool,
}

fn describe(p: &Prepared) -> String {
\tif p.flag {
\t\treturn "yes";
\t}
\treturn "no";
}

fn use_it(p: Prepared) -> Int {
\treturn (| | => {
\t\tval _s = describe(p);
\t\treturn p.count;
\t})();
}

pub fn main() nothrow -> Int {
\tval p = Prepared(count = 42, flag = true);
\tif use_it(p) == 42 {
\t\treturn 0;
\t}
\treturn 1;
}
"""

# Non-bitcopy alias/CopyValue proof: a REF-projected capture of a `String`
# field, returned from the immediate-invoked lambda. Without the
# `_ref_field_temps` marking in `_load_capture_from_env`, this is a shallow
# view aliasing `p`'s own backing allocation; when BOTH the lambda's
# returned copy and `p` itself are later dropped, that's a double-free.
_NON_BITCOPY_ALIAS_PROOF_SOURCE = """\
module main;

struct Prepared {
\tname: String,
}

fn use_it(p: Prepared) -> String {
\treturn (| | => { return p.name; })();
}

pub fn main() nothrow -> Int {
\tval p = Prepared(name = "hello" + "-world");
\tval s = use_it(move p);
\tif s == "hello-world" {
\t\treturn 0;
\t}
\treturn 1;
}
"""


def _compile(tmp_path: Path, source: str, name: str = "test_bin", *, sanitize: str | None = None) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	cmd = [sys.executable, "-m", "lang.driftc.driftc", "--dev",
	 "--stdlib-root", str(ROOT / "stdlib")]
	if sanitize:
		cmd += [f"--sanitize={sanitize}"]
	cmd += [str(src), "--entry", "main::main", "-o", str(out)]
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120))


def test_ref_projected_capture_immediate_lambda_compiles_and_runs(tmp_path: Path) -> None:
	"""The exact design-doc repro: an implicit REF-kind projected capture
	(`p.count`, no `captures(...)` clause) inside an immediate-invoked
	lambda must compile and run correctly, not crash codegen."""
	res = _compile(tmp_path, _REF_PROJECTED_IMMEDIATE_SOURCE)
	assert res.returncode == 0, f"compile failed: {res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-500:]}"


def test_two_projections_from_one_root(tmp_path: Path) -> None:
	"""Two projected captures of the same root (`p.count`, `p.flag`) in one
	lambda body must not collide under a single root-named local."""
	res = _compile(tmp_path, _TWO_PROJECTIONS_ONE_ROOT_SOURCE)
	assert res.returncode == 0, f"compile failed: {res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-500:]}"


def test_bare_root_plus_projection_from_same_root(tmp_path: Path) -> None:
	"""Bare root `p` (whole-struct REF capture) and a projection `p.count`
	of the same root, used together in one lambda body, must both resolve
	correctly (root gets a real local; the projection does not)."""
	res = _compile(tmp_path, _BARE_ROOT_PLUS_PROJECTION_SOURCE)
	assert res.returncode == 0, f"compile failed: {res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-500:]}"


def test_non_bitcopy_projected_field_no_double_free(tmp_path: Path) -> None:
	"""A REF-projected capture of a non-bitcopy field (`String`), returned
	from the lambda body, must not double-free when both the lambda's
	result and the source struct are later dropped. Compiled under ASAN;
	this is the alias/CopyValue proof case, not just a compile-success
	check."""
	res = _compile(tmp_path, _NON_BITCOPY_ALIAS_PROOF_SOURCE, sanitize="address,undefined")
	assert res.returncode == 0, f"compile failed: {res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(15))
	assert run.returncode == 0, (
		f"expected exit 0 (no ASAN error), got {run.returncode}; stdout:\n{run.stdout[-500:]}\n"
		f"stderr:\n{run.stderr[-2000:]}"
	)
