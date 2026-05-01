# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression: ConstructVariant pointer-autoload uses
the abstract field type tag instead of the concrete payload storage
type.

**Repro shape (no synthesis / no ConstShare involvement).**  A
`match` over a borrowed variant `&V` where an arm reconstructs the
SAME variant case from a Copy-typed payload binder:

    variant V { Empty, N(n: Int) }
    fn dup(v: &V) nothrow -> V {
        return match v {
            V::Empty() => { V::Empty() },
            V::N(n)    => { V::N(n) },   // ← bug trips here
        };
    }

Before the fix, the LLVM codegen path for ConstructVariant
auto-loaded the borrowed payload field via:

    %autoload = load drift.int, ptr %fieldptr

`drift.int` is the abstract type tag (`llvm_codegen.py:207
DRIFT_INT_TAG`), not a concrete LLVM type.  clang's IR parser
rejected it with `error: expected type`.

**Triggers (all required).**
  - Borrowed scrutinee `&V` (an owned-scrutinee match works).
  - A match-arm payload binder of a Copy-typed primitive
    (`Int`, `Uint`, `Bool`, `Byte`, `Float`).
  - Same-arm reconstruction `V::N(n) => V::N(n)` — the binder
    feeds back into a `ConstructVariant` of the same case.

The driver-level Phase 4 ConstShare variant tests use
`--test-build-only` and stop short of binary linking, so this
LLVM IR error did not surface there.  This regression must reach
LLVM and clang link, so we run a real binary build (no
`--test-build-only`).

**Fix locus.**  `lang/codegen/llvm/llvm_codegen.py:3354` —
ConstructVariant's autoload from a payload-pointer must use the
concrete payload-storage LLVM type
(`arm_layout.field_storage_lltys[idx]`), NOT the abstract
`field_lltys[idx]`.  Bool's i1/i8 storage discipline is preserved
because storage_llty is already i8 for Bool fields and the
existing `_is_bool_storage_pair` branch handles the value/store
pairing.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


SRC = """\
module main;

variant V {
\tEmpty,
\tN(n: Int)
}

fn dup(v: &V) nothrow -> V {
\tval r = match v {
\t\tV::Empty() => { V::Empty() },
\t\tV::N(n) => { V::N(n) },
\t};
\treturn r;
}

fn main() nothrow -> Int {
\tval v = V::N(42);
\tval v2 = dup(&v);
\treturn 0;
}
"""


def test_borrowed_match_int_payload_reconstruct_same_variant_links(
	tmp_path: Path,
) -> None:
	"""Pin: borrowed `&V` match arm reconstructing `V::N(n)` with
	an Int payload binder must produce well-typed LLVM IR and
	link cleanly.

	Before the fix, clang rejected the IR at
	`load drift.int, ptr %fieldptr` with `error: expected type`,
	and `lang.driftc.driftc` returned non-zero from the link step.

	After the fix, the binary is produced with no clang errors.
	"""
	src = tmp_path / "main.drift"
	src.write_text(SRC)
	out_bin = tmp_path / "bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	assert res.returncode == 0, (
		"borrowed-match Int-payload variant reconstruction must link "
		f"cleanly. compile/link output:\n{res.stdout}\n---\n{res.stderr[:2000]}"
	)
	assert out_bin.exists(), "binary not produced after successful link"
