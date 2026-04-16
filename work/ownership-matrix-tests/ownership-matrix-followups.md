# Ownership-transfer matrix — deferred expansions

Tracking doc for axes that the current generated matrix
(`lang/tests/codegen/e2e/__ownership_matrix__/`) does NOT yet cover.

The current matrix protects Copy / non-bitcopy retain/drop balance at
explicit transfer sites in same-source compilation.  Each item below
broadens coverage along an axis the current matrix elides — sequenced
roughly by how directly it relates to the bug family that motivated
the matrix (drift-net-tls v0.3.14 cert UAF / 0.27.192 fix).

When picking up any of these, mirror the existing generator pattern:
add the axis to a compact table, keep fixture count bounded by
collapsing related sub-tests into one per-fixture, and document
known compiler bugs in `KNOWN_SKIP_COMBOS` rather than hand-editing
fixtures.

## Landed in 0.27.193 / 0.27.194 / 0.27.195 / 0.27.196 / 0.27.197 (no longer pending)

- Non-Copy destructor-bearing type axis — landed in 0.27.197 as the
  `token` type.  `Token { session: &mut Session }` + `implement
  core.Destructible for Token` gives an observable `sess.drops` side
  channel; six non-array transfer sites (struct_ctor, variant_ctor,
  result_ok, return_value, local_assign, fn_arg), each as a single
  fixture with two per-shape scenarios (hvar_move, hcall_rvalue) —
  6 new fixtures / 12 scenarios, matrix 56 → 62.  Array sites elided
  because `Array<Token>` is rejected ("owning Array cannot contain
  borrowed aggregate element type in v1") — extending Array coverage
  requires a different side-channel design (shared refcount Int cell
  vs `&mut Session`) and stays deferred.  Hand-authored regression
  `match_named_local_non_copy_drop_once/` anchors the match-scrutinee
  ownership fix independently of the matrix.


- `array_insert` checker UnboundLocalError — fixed in 0.27.193;
  matrix re-enabled `om_array_insert_diag_entry`.
- ArrayLit Copy-non-bitcopy struct MIR invariant — fixed in
  0.27.193; matrix re-enabled `om_array_literal_diag_entry`.
- `.set` reconciliation — landed in 0.27.194:
  - Public API is `arr.set(index, value)`.
  - Checker `call_resolver.py` no longer shares push's arg-type
    validator with set; set has its own (idx=Int, value=elem_ty)
    validator mirroring insert.
  - `_lower_array_intrinsic_method` set lowering switched from
    raw `lower_expr` to `_lower_call_arg`+`_ensure_array_elem_copy`,
    aligning with push/insert and matching what
    `_call_arg_yields_owned_temp` predicts.
  - Positive + negative contract tests:
    `array_set_index_value_contract_ok` and
    `array_set_swapped_args_rejected` — both passing.
  - `array_set` added to the matrix `SITES`; 4 fixtures all pass
    plain + memcheck + ASAN.
- Function-call by-value args (`sink(value)`) — landed in 0.27.195
  as the `fn_arg` SITE.  4 fixtures pass plain + memcheck + ASAN.
- Return-value transfer (`return value`) — landed in 0.27.195 as
  the `return_value` SITE.  Exposed and fixed a LANGUAGE_BUG: HVar
  source for move-classified types lowered as plain load and
  scope-drops then double-released.  Fix: `_lower_owning_consume`
  helper now MoveOuts at return / assign boundaries.
- Local assignment / reassignment (`x = …;`) — landed in 0.27.195
  as the `local_assign` SITE.  Same fix family as return.
- For-loop binding (`for x in xs`) — landed in 0.27.196 as the
  `for_loop_bind` SITE.  Iteration borrows; iterable is intact
  post-loop.  4 fixtures pass plain + memcheck.
- True source-array-shape axis for `extend` — landed in 0.27.196
  as the `extend_source` SITE.  Varies the iterable expression at
  the extend call site (HVar local, HCall rvalue bound to a local,
  projected `&bag.items`).  4 fixtures pass plain + memcheck.

## Deferred: path-sensitive partial-ownership — drop flags

Today the compiler handles moved-from storage with two cheaper
representations:

- `_moved_locals` — compile-time "this local is moved on all paths."
- `TombstoneValue` / `ArrayElemTake` tombstones — overwrite storage
  with a drop-safe value (variant `__drift_internal_tombstone` tag,
  null String/Array header, etc.) so the later `DropValue` is a
  provable no-op.

These work until the ownership state is **path-sensitive** — i.e.
the answer to "should this slot drop?" depends on which runtime CFG
edge executed.  Example:

```drift
val x = make_token();
if cond {
    consume(move x);
}
// scope exit: drop x only if cond was false
```

No single compile-time set can represent that without statically
proving `cond`.  The general answer is **per-slot drop flags**:
runtime side metadata (`alloca i1`, SSA bool + phi, or a compact
bitset for aggregates with partial moves) next to each owning
storage slot, flipped by `MoveOut` and consulted at scope-drop.
Layout/ABI stay unchanged; no `Box<T>` indirection.  The optimizer
can elide flags entirely when ownership is statically obvious (the
common case — single move with no conditional branch around it,
or unconditional drop with no moves).

Current stopgap answer for match arms: **per-unbound-field drop**
(landed 0.27.197) — after any binder moves a field, the arm
explicitly drops the remaining unbound droppable fields rather than
dropping the whole variant.  Combined with scrutinee tombstone
(`TombstoneValue + StoreLocal(source_local)` for value-producing,
`_moved_locals` for statement-context), this closes the specific
leak class exposed by `match_subset_bind_leaves_unbound_fields_dropped/`
without introducing drop-flag infrastructure.  A full drop-flag
implementation would subsume both paths: match arms would
manipulate per-binder flags like any other local.

Drop flags are worth revisiting when:

- A new path-sensitive ownership pattern appears that can't be
  expressed as tombstone + `_moved_locals`.
- We need conditional-consume ergonomics (e.g. `if cond { consume(x) }`
  followed by implicit drop) without authors threading control-flow
  through `Option<T>` wrappers.
- Aggregate-with-partial-moves coverage expands beyond what match
  arms alone exercise.

## Next expansion (after `array_set`)

Each of these is a distinct ownership-transfer site that the current
matrix does not isolate.  The TLS bug family lived at the intersection
of several of these; the current matrix catches the specific shape
that shipped, but a regression on a different site could still slip
through.

### Package-boundary / source-vs-package axis — LANDED (pkgb_* fixtures)
- 6 hand-authored fixtures under `lang/tests/codegen/e2e/pkgb_*/`
  with per-fixture `producer/` subdirs; dedicated runner at
  `lang/tests/codegen/e2e/__ownership_matrix__/pkgb_runner.py`
  builds a signed producer `.dmp` for each fixture and compiles
  the consumer against the producer + stdlib packages.
  - `pkgb_struct_ctor_string_heap` — imported generic struct
    `Bag<String>` with heap-concat String field AND nested
    `Array<String>` (exercises "Array<T> inside exported struct"
    from the follow-up list).
  - `pkgb_struct_ctor_diag_entry` — imported generic struct
    `Bag<core.DiagnosticEntry>` with DV-bearing payload.
  - `pkgb_variant_ctor_string_heap` — imported generic variant
    `Msg<String>` with `@tombstone` ctor; consumer constructs and
    matches.
  - `pkgb_result_ok_string_heap` — producer returns
    `core.Result<String, Int>`; consumer matches the Ok arm.
  - `pkgb_result_err_diag_entry` — producer returns
    `core.Result<Int, core.DiagnosticEntry>`; consumer matches
    the Err arm.
  - `pkgb_match_bind_value_producing_diag_entry` — imported
    generic variant `Box<core.DiagnosticEntry>` consumed via
    value-producing match whose arm result IS the bound binder.
    Pins the 0.27.198 match-bind LANGUAGE_BUG fix family
    (`_lower_owning_consume` for arm result +
    `VariantGetField` owned-tracking in `string_arc.py`) against
    regression when the scrutinee type is imported across a
    signed package boundary.
- Isolation: fixtures are marked `package_consumer_only: true` so
  the standard e2e runner skips them, and the stdlib-only
  `pkg_consumer_runner.py` skips any case with a `producer/`
  subdir (matrix pkgb has its own runner).
- Coverage: all 6 fixtures pass plain + ASAN + memcheck via
  `just ownership-matrix-pkgb` / `-asan` / `-memcheck` targets.
- Total elapsed: ~60s for the 6-fixture run (11s stdlib build +
  ~9s per fixture = 6 × producer build + consumer compile + link
  + run); acceptable for local dev and CI gates.

### Non-Copy destructor-bearing type axis — Array<Token> still deferred
- Positive-path coverage landed in 0.27.197: 6 non-array Token
  fixtures (struct_ctor, variant_ctor, result_ok, return_value,
  local_assign, fn_arg) with per-shape scenarios, plus hand-authored
  `match_named_local_non_copy_drop_once/` regression that anchored
  the match-scrutinee ownership fix.
- Negative contract landed too:
  `token_hvar_use_after_consume_rejected/` locks in "move exactly
  once" for non-Copy HVar — Drift allows IMPLICIT move at last use,
  but the borrow checker rejects any subsequent read as `use after
  move of 'tok'` (phase: borrowcheck, code: E_USE_AFTER_MOVE).
- Array sites (`array_push`, `array_insert`, `array_set`) remain
  uncovered for Token because the type system rejects
  `Array<Token>` ("owning Array cannot contain borrowed aggregate
  element type in v1").  Extending Array coverage requires a
  different side-channel design for Token — e.g. a shared refcount
  `Int` cell instead of `&mut Session` — so the "premature/missing/
  double destructor" check survives Array element storage.

### Match-arm binding matrix
- Current matrix tests match binding INCIDENTALLY via variant /
  Result destructuring (e.g. `Msg::Payload(inner) => …`,
  `core.Result::Ok(v) => …`) — the payload-survives-construction
  check.
- A dedicated match-binding matrix would isolate the BIND step:
  - Bind by name vs. wildcard `_` (wildcard must drop ignored
    owned payloads exactly once).
  - Multiple arms where binding shape varies per arm (one binds, one
    ignores).
  - Reassignment-stress: `var s = …; val b = Box::T(s); s = …;`
    then match b — binder must be independent of s.
  - Match expression returning the payload (if supported).
  - Nested match (outer arm bind, inner pattern destructure further).
  - Same scenarios for `Boxed::Text(String)` and
    `Boxed::Entry(core.DiagnosticEntry)` element types.
- Failure modes the dedicated matrix would isolate:
  - variant ctor transfer vs. match payload extraction (today's
    matrix can't disambiguate).
  - arm binder lifetime / drop.
  - wildcard payload drop.
  - per-arm binder ownership divergence.

### `utf8_bytes` String source flavor
- Current String flavors: `static`, `heap_concat`.
- `utf8_bytes` (via `core.string_from_utf8_bytes` from a buffer) is
  semantically equivalent to `heap_concat` for runtime ownership
  (both produce a heap-backed non-static buffer where release
  decrements refcount), so dropped from the matrix as redundant.
- Source-path diversity argument (worth considering): even if both
  end up as heap-backed strings, the construction path differs in
  flags, length/capacity metadata, refcount initialization, buffer
  ownership, and unsafe buffer lifetime interaction.  The TLS
  failure's strings came from ASN.1 bytes via UTF-8 construction —
  not concat.
- Cost: each `utf8_bytes` fixture needs `import std.io as io;` and an
  `unsafe { var buf = io.buffer(len); … }` boilerplate, which adds
  noise and a separate failure surface (io.buffer / unsafe).
- Recommended path (when adopted): add to the hand-written net-tls
  canary OR a small high-risk subset (String × push/insert/set/
  extend × utf8_bytes), not every generated site immediately.

## Notes on prioritization

The first three (function-call args, return-value, for-loop) directly
extend the same family of `_lower_call_arg` / array-store / lower_expr
machinery the current matrix exercises.  Local-assignment is closely
related — same ownership boundary — but its lowering machinery may
overlap with the others.

Package-boundary multiplies any of the above by a factor of ~3-5x in
fixture cost; pick a small representative subset rather than mirroring
the full source-mode matrix.

Non-Copy destructor types broaden the matrix from "copy/retain
correctness" to "move/drop correctness".  Worth doing, but requires
side-channel observable destructors and likely some diagnostic golden
tests for negative cases.
