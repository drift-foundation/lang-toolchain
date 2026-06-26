# PROGRESS — heap `String` double-free via `arrayElem.struct.stringField + ""`

Power-loss recovery point. Newest on top. See `PLAN.md`.

## Status table
| Step | State |
|---|---|
| 1. Minimal failing regression (repro file) | **DONE** — `repro/corebug.drift` |
| 2. Confirm fails on WIP | **DONE** — corruption reproduced (pass1→`p1`, pass2→`pa`) |
| 3. Companion controls | **DONE** — one-hop, no-array, borrow all clean |
| 4. Root-cause in ownership lowering | **DONE** — `_visit_expr_HField` fast-path missing `_ref_field_temps` flag |
| 5. Bounded ownership matrix coverage | **DONE** — `test_string_concat_nested_struct_array_field.py` (7 shapes + valgrind) |
| 6. Fix root cause | **DONE** — lowering-only, ABI 18 unchanged |
| 7. Version bump (DRIFTC; ABI 18 unless runtime boundary) | **DONE** — 0.33.57→0.33.58, ABI 18; history.md entry |

## Log

### 2026-06-25 — Un-parked; plan + repro
- CORE_BUG report read (`/tmp/drift-announce/2026-06-26T...core-bug-report.md`). Classified
  LANGUAGE_BUG; matches refactor trigger *String ownership-authoring conformance matrix* →
  root-cause + bounded matrix required, not a one-projection patch.
- Repro confirmed on this tree (build via `bin/driftc --target-word-bits 64`):
  values degrade then would abort — heap buffer freed while still live in the array.
- Suspect: `stage2/hir_to_mir.py::_visit_expr_HField` — intermediate by-value struct
  (`.value`) materialized as a temp; leaf `String` read as a shallow alias not routed
  through `_copy_if_ref_alias`, then the temp dropped at end-of-expr → frees the array's
  live buffer. Borrow-penultimate (`&fields[j].value`) skips the temp → clean.
- Next: build the control variants, then MIR/IR-diff failing vs safe to pin the exact
  spurious drop / missing deep-copy.

### 2026-06-25 — ROOT CAUSE + FIX
- **Diagnosis via IR diff** (minimal `probe.drift`: `two_hop` vs `one_hop`):
  - `one_hop` (`flats[j].s + ""`): leaf String deep-copied (`drift_string_retain`) →
    balanced. Clean.
  - `two_hop` (`fields[j].value.s + ""`): NO leaf retain, plus a SPURIOUS
    `%drop_field = extractvalue Value, 0; drift_string_release(%drop_field)` — drops the
    intermediate `.value` struct's String, which is a shallow `extractvalue` from the
    LIVE array element → frees the array's buffer. (Whole-module count: failing had +1
    release / +0 retain vs the safe variant — one unbalanced release = the double free.)
- **Root cause:** `stage2/hir_to_mir.py::_visit_expr_HField`, the `HField(HIndex)` fast
  path (≈3504-3541). It borrows the element's field via AddrOfArrayElem+AddrOfField+
  LoadRef. For a Copy field (String) it emits CopyValue (deep copy) — fine (one-hop). For
  a NON-Copy struct field (`Value`) it returned the raw LoadRef WITHOUT adding it to
  `_ref_field_temps`. A subsequent projection (`.s`) off that unflagged struct then hit
  `source_is_owned_rvalue` → materialized-and-dropped it → the spurious String release.
  The GENERAL field path (≈3730) already flags non-bitcopy reads; the fast path omitted it.
- **Fix (lowering-only, ABI 18 stays):** in the fast path's non-Copy return, flag the
  borrowed field read as a ref-field alias when not bitcopy:
  `if not self._drop_policy(field_ty).is_bitcopy: self._ref_field_temps.add(dest)`.
  Now the downstream `.s` read treats `.value` as a borrowed alias (no owned-drop), and
  `concat` borrows the leaf — no copy needed, no spurious free.
- **Verified:** repro now prints `p0 p1` on all 3 passes; controls (one-hop, no-array,
  borrow) clean; **valgrind memcheck clean** (rc 0, no UAF/leak) on the failing shape.
- Lesson: parallel field-projection lowering paths (fast `HField(HIndex)` vs general
  `StructGetField`) must apply the SAME `_ref_field_temps` aliasing rule — same omission
  class as the prior place-walker/projection bugs.

## Diagnosis notes
- No `--dump-mir` flag; `--emit-ir <path>` writes driftc's LLVM IR (pre-clang). MIR
  inspection via a small front-end harness or IR read of `main`'s inner loop
  (`drift_string_release` call count is the tell for the double free).
