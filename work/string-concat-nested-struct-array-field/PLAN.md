# PLAN — CORE_BUG: heap `String` double-free via `arrayElem.struct.stringField + ""`

## Classification
- **LANGUAGE_BUG** (compiler codegen/ownership lowering) — confirmed by the reporter
  (DriftQuery) and reproduced here.
- Matches refactor trigger **String ownership-authoring conformance matrix**
  (`doc/refactor_triggers.md`): heap-String corruption/UAF/double-free involving
  array/struct field projection + concat. Per the trigger, the deliverable is a
  ROOT-CAUSE fix + a bounded ownership matrix — **not** a narrow "missing retain at
  this one projection" patch, unless the matrix proves that is the whole root cause.
- ABI: expected **no runtime boundary change** (lowering/codegen only) → DRIFTC_VERSION
  bump, ABI 18 stays. (Re-confirm once the fix is localized.)

## The bug (reporter: drift-query, on certified 0.33.56 | abi 18)
Concatenating (`+`) a heap-allocated `String` reached by **array index → by-value
struct field → String field** (`fields[j].value.s + ""`) frees the array's *live*
buffer. Values silently degrade, then a later allocation aborts
(`malloc(): unaligned tcache chunk detected`, rc 134). Plain, safe, immutable reads;
no `unsafe`, no `move`, no aliasing in source.

Minimal repro: `work/string-concat-nested-struct-array-field/repro/corebug.drift`
(also staged in scratchpad). Observed (WIP, this tree):
```
pass0 p0
pass0 p1
pass1 p1     <- WRONG (should be p0; buffer freed/reused)
pass1 p1
pass2 pa     <- garbage
pass2 pa
```
Expected `p0 p1` on all three passes.

## Isolation (reporter; four conditions JOINTLY required)
| variant | array index | hops to String | String | `+` concat | result |
|---|---|---|---|---|---|
| **failing** | yes `fields[j]` | **2** `.value.s` | **heap** | **yes** | CORRUPT |
| literal string | yes | 2 | literal | yes | OK |
| one hop | yes | 1 `fields[j].s` | heap | yes | OK |
| no concat | yes | 2 | heap | no (`println` direct) | OK |
| no array (plain var) | no `f.value.s` | 2 | heap | yes | OK |
| safe idiom | yes | 2 via `&fields[j].value` | heap | yes | OK |
Independent of struct width and `nothrow` vs `throws`.

## Root-cause hypothesis (to confirm via MIR/IR)
To feed `fields[j].value.s` to `+`, lowering appears to materialize a temp copy of
the intermediate **by-value struct** `.value`, read the `String` out as a SHALLOW
view (not flagged for deep-copy), then drop that temp at end-of-expression — freeing
the array's live buffer (shared, not deep-copied). The borrow-penultimate idiom skips
the temp, hence clean. Suspect region:
`stage2/hir_to_mir.py::_visit_expr_HField` — `source_is_owned_rvalue` /
`_ref_field_temps` / `_copy_if_ref_alias` interplay for a STRUCT intermediate whose
extracted field is a non-bitcopy `String`.

## Plan (7 steps; status in PROGRESS.md)
1. Minimal failing regression (driver test).               — repro file done
2. Confirm fails on WIP/main.                              — DONE (corruption shown)
3. Companion controls (literal, one-hop, no-concat, no-array, borrow-penultimate).
4. Root-cause in ownership lowering (MIR/IR diff failing vs safe).
5. Bounded ownership matrix coverage:
   - producers: heap concat, static literal control;
   - consumers/projections: array element, nested struct field, local/borrow control;
   - exits: normal teardown at minimum; memcheck/ASAN if available.
6. Fix root cause. No stdlib/product-source workaround.
7. Version bump (DRIFTC if behavior-changing; ABI only if runtime boundary changes).

## Constraints
- No stdlib or product-source workaround (reporter's no-workaround policy).
- Keep this SEPARATE from the package/app trust commit.
