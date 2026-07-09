# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: synthesized `<Variant>::ConstShare::const_share`
crashes the SSA pass when two variant arms share a payload binder
name.

Surfaced by the SingularGateway app-team report against
0.31.94+abi14 (2026-05-17).  High severity: every `pub variant`
whose arms share any payload field name AND that flows through
`core.Diagnostic` / const-share infrastructure is affected.  The
app team's workaround was source-level (rename binders unique
per arm, e.g. `MissingKey(missing_key) + BadType(bad_type_key,
expected)`); that imposes an ergonomic tax on every variant
declaration and is exactly the kind of thing future contributors
silently reintroduce -- so the fix lives in the synthesizer, not
the source.

**Minimal repro** (matches `/tmp/sgw-stub/variant_repro.drift`  ## drift-tmp-root-audit: allow docs repro-path reference
from the app-team report verbatim):

    module variant_repro;
    import std.core as core;

    pub variant Kind {
        A,
        B,
        MissingKey(key: String),
        BadType(key: String, expected: String)
    }

    implement core.Diagnostic for Kind {
        pub fn to_json_text(self: &Kind) nothrow -> String {
            return match self {
                Kind::A => { "\"a\"" },
                Kind::B => { "\"b\"" },
                Kind::MissingKey(key) => { "\"missing-key\"" },
                Kind::BadType(key, expected) => { "\"bad-type\"" }
            };
        }
    }

    pub fn main() nothrow -> Int { return 0; }

The user-written `to_json_text` impl doesn't actually use the
`key` binders -- it returns literals.  The crash is in the
**compiler-synthesized** `Kind::ConstShare::const_share` body
(triggered by `implement core.Diagnostic for Kind`), which builds
a match arm per variant case that DOES reference each binder for
the per-field const-share/copy-frozen reconstruction.

**Pre-fix failure shape:**

    RuntimeError: SSA: load before store for local 'key__b<N>'
      in multi-block rename
      (fn=FunctionId(module='variant_repro',
                     name='Kind::ConstShare::const_share',
                     ordinal=0)
       block=match_dispatch_next<M>)

at `lang/driftc/stage4/ssa.py:420`.

**Root cause** -- `lang/driftc/const_share_synth.py::
_build_const_share_hir_variant` emitted raw schema field names
as `HMatchArm.binders` and raw `HVar(name=field_name)` references
in the result-side ctor reconstruction, never going through the
per-arm rename-to-`__match_binder_<N>_<orig>` pass that user-
source match arms get in `stage1/ast_to_hir.py::lower_match_expr`.

Without that rename:
  - the typechecker assigns DIFFERENT `binding_id`s to each arm's
    same-named binder (`key` in arm A gets id 3, `key` in arm B
    gets id 6);
  - MIR pattern-binding stores into raw local `key` for both arms
    (`stage2/hir_to_mir.py:1762-1764`);
  - MIR result-expression reads route through
    `_canonical_local(binding_id, name)`
    (`stage2/hir_to_mir.py:2577`), which suffixes the second arm
    to `key__b6`;
  - SSA multi-block rename sees a load of `key__b6` with no
    preceding store and raises.

**Fix shape:** localize binders per-arm inside the synthesizer
(matching the `__match_binder_<N>_<orig>` convention from
`ast_to_hir.py`).  Pure local fix; does NOT change `HMatchArm`
schema, `_canonical_local` logic, or MIR lowering.

If this test ever flakes, check that the per-arm binder counter
in `lang/driftc/const_share_synth.py::_build_const_share_hir_variant`
still allocates `__match_binder_<N>_<orig>` names and uses them
both as `HMatchArm.binders` AND in the result-side `HVar`
references.  The naming prefix is load-bearing -- `_canonical_local`
at `lang/driftc/stage2/hir_to_mir.py:1123-1125` recognizes
`__match_binder_*` and skips the binding-id suffix path that
would otherwise re-introduce the collision.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


_VARIANT_REPRO_SOURCE = """\
module variant_repro;

import std.core as core;

pub variant Kind {
\tA,
\tB,
\tMissingKey(key: String),
\tBadType(key: String, expected: String)
}

implement core.Diagnostic for Kind {
\tpub fn to_json_text(self: &Kind) nothrow -> String {
\t\treturn match self {
\t\t\tKind::A => { "\\"a\\"" },
\t\t\tKind::B => { "\\"b\\"" },
\t\t\tKind::MissingKey(key) => { "\\"missing-key\\"" },
\t\t\tKind::BadType(key, expected) => { "\\"bad-type\\"" }
\t\t};
\t}
}

pub fn main() nothrow -> Int { return 0; }
"""


def _compile_via_subprocess(
	tmp_path: Path,
	source: str,
) -> subprocess.CompletedProcess[str]:
	"""Compile via subprocess so an uncaught Python exception from
	driftc (e.g. the pre-fix `RuntimeError` from SSA rename) is
	visible as a non-zero exit + stderr traceback rather than
	bubbling up and aborting the pytest worker.

	`--test-build-only` is intentionally NOT passed: the bug
	surfaces during SSA rename, which is downstream of stage1/2
	and runs as part of the normal compile pipeline; running
	through to attempted link keeps the failure shape on the
	exact path the app team hit.
	"""
	main_path = tmp_path / "variant_repro.drift"
	main_path.write_text(source)
	out_bin = tmp_path / "variant_repro_out"
	cmd = [
		sys.executable, "-m", "lang.driftc",
		"--stdlib-root", str(ROOT / "stdlib"),
		"--entry", "variant_repro::main",
		str(main_path),
		"-o", str(out_bin),
	]
	return subprocess.run(
		cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)


def test_synth_const_share_shared_binder_name_does_not_crash_ssa(
	tmp_path: Path,
) -> None:
	"""`pub variant` arms sharing a payload binder name (e.g.
	`MissingKey(key)` + `BadType(key, expected)`) must compile
	cleanly when the variant flows through a Diagnostic impl that
	triggers `const_share` synthesis.

	Pre-fix, driftc crashed with `RuntimeError: SSA: load before
	store for local 'key__b<N>' in multi-block rename
	(fn=...Kind::ConstShare::const_share...)`.

	Pre-fix carrier signal: stderr contains ALL of
	  `SSA: load before store`,
	  `Kind::ConstShare::const_share`,
	  and a `__b<N>` suffix on a binder name.
	Those three together pin the failure shape to this bug
	(rather than some unrelated SSA crash or some unrelated
	const_share issue).

	Post-fix expectation:
	  - driftc exits 0 (compile + link succeed end-to-end);
	  - stderr does not carry the pre-fix SSA-crash signature.

	This test deliberately keeps `key` as the binder name in BOTH
	arms.  The fix shape is per-arm binder identity localization
	in `const_share_synth`, NOT forcing source-level unique binder
	names -- the test must regress if anyone reverts the
	synthesizer fix and tries to "fix" it by re-imposing the
	source-level rename.
	"""
	res = _compile_via_subprocess(tmp_path, _VARIANT_REPRO_SOURCE)
	stderr = res.stderr or ""

	import re
	pre_fix_signature = (
		"SSA: load before store" in stderr
		and "Kind::ConstShare::const_share" in stderr
		and bool(re.search(r"\b[a-zA-Z_]\w*__b\d+\b", stderr))
	)
	assert not pre_fix_signature, (
		"driftc crashed with the pre-fix SSA-rename signature on a "
		"variant with shared payload binder names flowing through a "
		"Diagnostic impl.  The per-arm binder localization in "
		"`lang/driftc/const_share_synth.py::"
		"_build_const_share_hir_variant` was reverted or never landed:\n"
		"  - binder names must use the `__match_binder_<N>_<orig>` "
		"shape (not raw schema field names);\n"
		"  - the same internal names must appear both as "
		"`HMatchArm.binders` AND inside the result-side `HVar` "
		"references for the const-share/copy-frozen reconstruction.\n\n"
		f"STDERR (tail):\n{stderr[-2000:]}"
	)
	assert res.returncode == 0, (
		f"driftc exited {res.returncode} on a variant with shared "
		f"payload binder names, but the failure is NOT the known "
		f"pre-fix SSA-rename shape -- something else is wrong:\n\n"
		f"STDERR (tail):\n{stderr[-2000:]}"
	)
