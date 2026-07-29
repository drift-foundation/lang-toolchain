# LANGUAGE_BUG slice: thin fn-pointer `&T` argument — bare call miscompiles

Branch: authorized on the CURRENT branch (reject-redundant-call-borrows line of
work) per user direction 2026-07-29 — no branch creation or switching; the fix is
kept logically isolated via this work folder and its own regression file. All
changes are uncommitted working-tree edits (git reserved to the user).
Origin: probe e8d in `work/reject-redundant-call-borrows/probes/` (plan review,
2026-07-29). Independent of, and landing before, the reject-redundant-call-borrows
proposal (paused).

## Defect

```drift
fn read_len(arg: &String) nothrow -> Int { return arg.byte_length(); }

pub fn main() nothrow -> Int {
	val f: Fn(&String) nothrow -> Int = read_len;
	val s: String = "hello";
	return f(s);          // ← type checker ACCEPTS; codegen emits ill-typed IR
}
```

`clang` rejects the emitted module: `'%.t3' defined with type '%DriftString = type
{ i64, ptr }' but expected 'ptr'` — the indirect call passes the String by value where
the fn-pointer signature requires `ptr` (a `&String`). Explicit `f(&s)` compiles and
runs correctly (probe e8c, exit 5).

## Intended semantics (fixed by this slice)

For `f: Fn(&T) [nothrow] -> R`, bare `f(place)` **auto-borrows** — same
parameter-directed borrowing as direct calls — compiles, and runs. Explicit `f(&s)`
remains legal in this slice; whether it later becomes redundant is D8 policy of the
paused proposal and is NOT bundled here. The fix must make no source-provenance
assumptions: it keys on the resolved `FUNCTION` TypeId's REF params, so a generic
`Fn(T)` instantiated at `&String` behaves identically (control C3).

## Mandatory sequence (regression-first)

1. Record minimal repro → `repro_minimal.drift` (done).
2. New pinned full-compile-and-run regression
   `lang/tests/driver/test_fnptr_ref_arg_autoborrow.py`; confirm it FAILS on the
   current tree with the clang `%DriftString` vs `ptr` error.
3. Root-cause the checker→HIR→MIR path: find where the invoke-path argument check
   accepts `T` at a `&T` fn-pointer param without synthesizing a borrow.
4. Fix structurally: the auto-borrow must exist as an `HBorrow` node (or node-level
   metadata consumed downstream) — no checker-local type adjustment, no LLVM/codegen
   patch masking the missing borrow.
5. Controls: C1 explicit `f(&s)` (stays legal, runs); C2 bare `f(s)` (fixed, runs);
   C3 generic `Fn(T)` instantiated at a reference type, called with a matching `&T`
   value (no provenance assumptions); C4 `&mut` variant (`Fn(&mut T)`) bare + explicit;
   C5 mismatch still rejected (e.g. bare rvalue at `&mut`, or wrong inner type).
6. Verify: regression + controls compile and run; relevant checker/lowering test
   files; then the full appropriate gate (driver suite at minimum; memcheck lane if
   the fix touches borrow materialization/cleanup paths).
7. `DRIFTC_VERSION` bump (behavior-changing). NO ABI bump unless investigation finds
   a boundary-shape change (none expected: the fixed program's IR matches what
   explicit `f(&s)` already emits; the fn-pointer ABI itself is untouched).

`doc/refactor_triggers.md`: reviewed (per review direction) — no registered trigger
matches this slice; recorded here as required.

No stdlib/user-code workarounds. Cross-team announcement (local file only, per
policy) after verification. No commits/pushes without separate permission.

## Stop conditions

Stop only if: intended semantics change under investigation; a registered refactor
trigger fires after all; or an actual ABI boundary change is discovered.
