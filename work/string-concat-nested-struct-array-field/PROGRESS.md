# PROGRESS — heap `String` double-free via `arrayElem.struct.stringField + ""`

Power-loss recovery point. Newest on top. See `PLAN.md`.

## Status table
| Step | State |
|---|---|
| 1. Minimal failing regression (repro file) | **DONE** — `repro/corebug.drift` |
| 2. Confirm fails on WIP | **DONE** — corruption reproduced (pass1→`p1`, pass2→`pa`) |
| 3. Companion controls | IN PROGRESS |
| 4. Root-cause in ownership lowering | IN PROGRESS (suspect `_visit_expr_HField`) |
| 5. Bounded ownership matrix coverage | TODO |
| 6. Fix root cause | TODO |
| 7. Version bump (DRIFTC; ABI 18 unless runtime boundary) | TODO |

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

## Diagnosis notes
- No `--dump-mir` flag; `--emit-ir <path>` writes driftc's LLVM IR (pre-clang). MIR
  inspection via a small front-end harness or IR read of `main`'s inner loop
  (`drift_string_release` call count is the tell for the double free).
