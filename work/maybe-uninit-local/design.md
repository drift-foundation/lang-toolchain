# `MaybeUninit<T>` as a first-class local — design note

**Status**: audit complete; awaiting decision before implementation.
**Scope**: enable standalone `var slot = mem.maybe_uninit<type T>()` locals.
**Trigger**: `NotImplementedError` at `lang/driftc/stage2/hir_to_mir.py:3329-3330`
blocks four downstream tests including
`test_lowering_mem_maybe_write_emits_moveout_for_non_copy_local`.

---

## 1. Audit summary

### What already exists

| Layer                                 | Status   | Notes                                                                                                                                                                                                                                       |
| ------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Stdlib surface (`std.mem`)            | Complete | All five intrinsics declared `@intrinsic pub unsafe fn …` in `stdlib/std/mem/mem.drift:24-137`. `pub struct MaybeUninit<T> { }` is a phantom wrapper.                                                                                       |
| Type checker / call resolver          | Complete | `lang/driftc/checker/call_resolver.py:4164-4333` — type-arg inference, signatures, return types all wired for the local case as well as the buffer case.                                                                                    |
| Arity validation                      | Complete | `lang/driftc/call_contract.py:244-248`.                                                                                                                                                                                                     |
| HIR→MIR for `maybe_write`             | Complete | Uses `_lower_owning_consume` for the value arg → emits `MoveOut` on non-Copy sources. `hir_to_mir.py:3331-3346`.                                                                                                                            |
| HIR→MIR for `maybe_assume_init_*`     | Complete | `_ref`/`_mut` are pass-throughs; `_read` emits `PtrFromRef + PtrRead + ZeroValue + PtrWrite`. `hir_to_mir.py:3347-3370`.                                                                                                                    |
| HIR→MIR for `maybe_uninit`            | **Stub** | `raise NotImplementedError("maybe_uninit intrinsic lowering is not implemented in v1")` at `hir_to_mir.py:3329-3330`. **The single blocker.**                                                                                               |
| Codegen layout for `MaybeUninit<T>`   | Complete | Four explicit unwrap sites in `lang/codegen/llvm/llvm_codegen.py`: `llvm_type_for_typeid` (1052), `_llvm_type_for_typeid` (7782), `_llvm_storage_type_for_typeid` (7862), `_size_align_typeid` (7493). Result: `sizeof(MaybeUninit<T>) == sizeof(T)`. |
| Container path (`RawBuffer<MaybeUninit<T>>`) | Complete | `stdlib/std/containers/array.drift:771-901` (HashMapCore) is the reference user.                                                                                                                                                          |
| Ownership ledger + drop_flags         | Capable  | `LiveState.MAYBE_UNINIT` and join semantics already exist (`ownership_ledger.py:85-119`, `drop_flags.py:366-376`); no MaybeUninit-typed special-casing in cleanup_authoring is needed for the local case (see §4).                          |

### What does **not** exist (and is not needed)

- No special drop classification for `MaybeUninit<T>`-typed locals.  Empty
  struct ⇒ no destructor ⇒ `needs_drop = false` ⇒ classifier returns
  `MUST_NOT_DROP` automatically.
- No explicit `LiveState` tracking per intrinsic call.  The slot's *type* is
  no-drop; what's inside it is the user's problem under `unsafe`.

---

## 2. The MIR shape decision

### Q: what should `maybe_uninit<T>()` lower to?

**Recommendation**: a single `ZeroValue(dest=temp, ty=MaybeUninit<T>)`.

The surrounding `var slot = …` machinery then `StoreLocal`s `temp` into the
user local exactly as for any other expression result. No new MIR op, no
changes to MIR builders.

### Why ZeroValue, not a new `UninitValue`

1. **Convention already established.**  `maybe_assume_init_read` already uses
   `ZeroValue(dest=zero, ty=ret_ty)` followed by `PtrWrite` to "tombstone" a
   slot after the value is read out (`hir_to_mir.py:3360-3364`).  Construction
   is the symmetric case: produce an initial zeroed slot.  Using the same op
   for both keeps the lowering coherent.
2. **Codegen already handles it correctly.**  Because of the four-site
   `MaybeUninit<T> → T` unwrap, `ZeroValue(MaybeUninit<T>)` lowers to
   `T`-sized zero bytes — exactly the byte shape `maybe_write` and
   `maybe_assume_init_read` expect.
3. **No new opcode surface.**  The MIR op set, mir_nodes serialization, mir
   verifier, and codegen visitor all already understand `ZeroValue`.  Adding
   `UninitValue` is a multi-file change for no observable behavior gain at
   this stage.
4. **Future optimization is non-blocking.**  If profiling later shows the
   zero-fill cost matters (it shouldn't — these slots are tiny and short-lived),
   we can introduce `UninitValue` lowering to LLVM `undef`/`poison` then.  That
   refactor is local to a single intrinsic case.

### What we are *not* doing

- Not introducing an `AllocLocal`/`StackAlloc` MIR op.  Locals are already
  registered via `_local_types` and emitted as LLVM `alloca`s; the value
  produced by `maybe_uninit()` flows through the standard `var = expr`
  binding path.
- Not minting a stable runtime ABI for `MaybeUninit<T>`.  The phantom-wrapper
  unwrap stays internal to the codegen.

---

## 3. Ledger / cleanup_authoring model

The local case **does not require any new ledger machinery**.  Reasoning:

- The local is typed `MaybeUninit<T>`.  `MaybeUninit<T>` has no fields, no
  Drop impl, and `needs_drop = false`.
- `classify(state, needs_drop=False) → MUST_NOT_DROP`
  (`ownership_ledger.py:132-147`), regardless of the live-state.
- Therefore on every scope exit, every break/continue, every match arm exit,
  every panic-edge, the cleanup authority correctly emits **no destroy** for
  the slot itself.
- Whatever value lives inside the slot is, by the unsafe contract, the user's
  responsibility to extract via `maybe_assume_init_read` before the slot is
  abandoned.  The compiler will not (and must not) try to drop it — it does
  not know whether the slot is occupied.

Per-intrinsic state, restated for the record:

| Intrinsic                      | Effect on slot local                                                          | Effect on other operands                                       |
| ------------------------------ | ----------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `maybe_uninit()`               | Defines slot in `LIVE` (no-drop type) — equivalent to UNINIT semantically.    | —                                                              |
| `maybe_write(&mut slot, v)`    | No state change on slot (still LIVE, no-drop).                                | `v` source local: `MOVED_OUT` via `_lower_owning_consume`.     |
| `maybe_assume_init_ref/_mut`   | No state change on slot.                                                      | Returns reference; no ownership transfer.                      |
| `maybe_assume_init_read`       | No state change on slot (still LIVE, no-drop). Slot bytes zeroed by lowering. | Returns owned `T`; the receiving binding takes ownership.      |

The "uninit-vs-occupied" bit is **only meaningful inside an unsafe block** and
is not modeled by the ledger.  This is the correct boundary: tracking it
statically would require a flow-sensitive sub-typing of `MaybeUninit` (which
Rust also does not do for the value side).

---

## 4. Diagnostics

**No new compiler diagnostics in v1.**  The contract is enforced by the type
checker's existing `unsafe`-required rule on the four read/write intrinsics.

Explicitly out of scope for v1:

- "Leak warning" on a `MaybeUninit<T>` local that exits scope without a
  matching `maybe_assume_init_read`.  This is intentionally permitted — it is
  the user's unsafe contract, and (a) we cannot prove a slot is occupied and
  (b) deliberate forget-and-leak is a legitimate pattern for some unsafe code.
- "Read before write" detection.  Path-sensitive; out of scope for v1; would
  require lifting `LiveState.MAYBE_UNINIT` semantics to be type-aware.
- Double-write / use-after-read.  Same reasoning.

If we later want a single forward-progress check, the cheapest is the
"non-trivially-Drop content but no `_read` in scope" pattern as a `--lint`
warning.  Defer.

---

## 5. ABI

**Compiler-only.  No ABI bump.**

- `MaybeUninit<T>` already has stable codegen layout via the phantom-wrapper
  unwrap.
- No new MIR ops, no changes to `.dmp`/`.zdmp` schemas, no changes to package
  metadata.
- The `@intrinsic` declarations and intrinsic kinds are unchanged.

Per the AGENTS.md rule, this is a compiler-version bump only (behavior change:
previously errored on `mem.maybe_uninit()` at HIR→MIR; now compiles).
Suggested target: 0.31.10.

---

## 6. Implementation plan (minimal)

### Patch surface

1. **`lang/driftc/stage2/hir_to_mir.py:3329-3330`** — replace the
   `NotImplementedError` with the `ZeroValue` lowering.

   ```python
   if intrinsic is IntrinsicKind.MAYBE_UNINIT:
       if info is None:
           raise AssertionError("maybe_uninit(...) missing CallInfo (checker bug)")
       ret_ty = info.sig.user_ret_type   # MaybeUninit<T>
       dest = self.b.new_temp()
       self.b.emit(M.ZeroValue(dest=dest, ty=ret_ty))
       self._local_types[dest] = ret_ty
       return dest
   ```

   Mirrors the shape of the other four cases above it; no new helpers.

2. **`lang/driftc/driftc_versions.py`** — bump to 0.31.10.  ABI unchanged
   (still 10).

### Tests (regression-first)

Per the LANGUAGE_BUG / cleanup-authoring rules in memory: write tests first,
confirm they fail against current `main`, then apply the lowering patch.

| # | Test                                                                                  | Layer        | Pins                                                                                                  |
|---|---------------------------------------------------------------------------------------|--------------|-------------------------------------------------------------------------------------------------------|
| 1 | `test_lowering_mem_maybe_write_emits_moveout_for_non_copy_local` (unskip + rewrite)   | stage2       | `maybe_write` consumes non-Copy `Box` via `MoveOut`; 1 explicit destroy total.                        |
| 2 | `test_lowering_mem_maybe_uninit_emits_zero_value` (new)                               | stage2 unit  | `var slot = mem.maybe_uninit<type Int>()` → exactly one `ZeroValue` op of type `MaybeUninit<Int>`.    |
| 3 | `test_lowering_mem_maybe_assume_init_read_emits_moveout_chain` (new)                  | stage2       | Round-trip non-Copy `Box`: write → read → drop_value; 1 explicit destroy, no scope-exit drop on slot. |
| 4 | `test_maybe_uninit_local_no_scope_drop` (new, driver)                                 | acceptance   | `var slot = mem.maybe_uninit<type Box>()` then drop slot at scope exit ⇒ 0 destroys (intentional).   |
| 5 | `lang/tests/memcheck/test_maybe_uninit_local_string.drift` (new, memcheck)            | memcheck     | Non-Copy `String` round-trip in standalone local; **no leaks, no UAF, no double-free** under valgrind. |
| 6 | `lang/tests/memcheck/test_maybe_uninit_local_arc.drift` (new, memcheck)               | memcheck     | `Arc<T>` round-trip; refcount sanity end-to-end.                                                       |

#5 and #6 are mandatory under the standing
[memcheck-in-gate-for-site-3 / authority work](../../.claude/projects/-home-sl-src-drift-lang/memory/feedback_memcheck_in_gate.md)
rule.  Even though this patch is not formally site-3, it touches
ownership-via-intrinsic and creates a new local-storage pattern; the rule's
spirit applies.

### Verification gate

Per the [phase-verification-unfiltered](../../.claude/projects/-home-sl-src-drift-lang/memory/feedback_phase_verification_unfiltered.md)
rule, the final pass must be unfiltered:

1. `PYTHONPATH=. pytest lang/tests/stage2 -n16` (one run, no name filter)
2. `PYTHONPATH=. pytest lang/tests/checker -n16`
3. `PYTHONPATH=. pytest lang/tests/codegen -n16`
4. `PYTHONPATH=. pytest lang/tests/driver -n16`
5. `PYTHONPATH=. pytest lang/tests/memcheck` (sequential, valgrind)
6. `PYTHONPATH=. pytest lang/tests/acceptance -n16`
7. PEX e2e runner: `1023 passed, 5 skipped, 0 failed`

Per [no-parallel-n16](../../.claude/projects/-home-sl-src-drift-lang/memory/feedback_no_parallel_n16.md):
run these sequentially, not overlapped.

### Estimated effort

- Lowering: ~10 lines.
- Tests: ~150 lines across 6 files.
- Verification: ~30 minutes wall.

Total: a contained patch.  No follow-on work expected.

---

## 7. Open questions for review

1. **Naming**: keep `mem.maybe_uninit` / `mem.maybe_write` /
   `mem.maybe_assume_init_*` as-is, or harmonize with Rust's `MaybeUninit::new`
   / `write` / `assume_init` family?  Recommend keep — names are internal to
   `std.mem` and unsafe-gated; bikeshed cost > clarity gain.
2. **`UninitValue` MIR op**: defer or land alongside?  Recommend defer — see
   §2 reasoning.
3. **Documentation**: add a short "MaybeUninit local pattern" section to
   `stdlib/std/mem/mem.drift` doc-comments?  Recommend yes, in the same patch
   (Tier 1 module per [doc convention](../../.claude/projects/-home-sl-src-drift-lang/memory/project_doc_convention.md)).
4. **Linting follow-up**: should we open an issue for the
   "MaybeUninit-with-Drop content, no `_read` in scope" leak hint?  Recommend
   yes — file under `issues/`, not v1.

---

## 8. Recommendation

Land the 10-line lowering + 6 tests + version bump as a single patch on a
short-lived branch.  No ABI implications, no ledger changes, no codegen
changes.  The architectural heavy lifting was already done when
`MaybeUninit<T>` got its phantom-wrapper unwrap in the LLVM layout pipeline;
this patch only finishes the constructor side of the same story.
