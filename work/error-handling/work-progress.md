## 1. Front-end / syntax scope

Your “Front-end syntax & parsing” bullet is fine, but I’d make two things explicit:

1. **Single canonical catch syntax**

   Decide up front whether `catch Foo e`, `catch Foo as e`, or a pattern-like form is the only allowed syntax and *match lang/* exactly. Right now the plan just says “catch arms with optional event binders”; that’s enough for you, but it’s easy to accidentally end up with “legacy lang syntax + new lang2 experiments”.

   → **Adjustment:** add a sub-bullet:

   > Mirror the exact try/catch syntax from `lang/` (including binding form and catch-all spelling) and forbid any legacy / experimental variants.

2. **Capture marker semantics**

   You mention `^` with optional alias. Make sure you also pin down:

   * whether `^` is allowed on temporaries, or only on named locals
   * what happens when the same local is captured multiple times in nested scopes
   * if there are lifetime/borrow implications in lang2’s checker (even if rudimentary right now)

   → **Adjustment:** explicitly note in the plan that captures are *purely diagnostic* and must **not** alter dataflow / mutability rules; they only synthesize `DiagnosticValue`s for locals that *already* type-check.

---

## 2. Type system & event codes

Your “Type system & metadata” section is good, but the open questions are going to bite you if you don’t lock them in before touching MIR/SSA.

### Event code ABI

You already call out the hash question later. I’d lock this down in the plan itself:

* Reuse exactly whatever `lang/` uses (likely xxhash64 or similar) with:

  * FQN string as input (`module.path.ExceptionName`)
  * high 4 bits for domain tag (`0b0001 << 60`) and low 60 bits from the hash
* Treat collisions as a **hard compile error** at exception-declaration time.

→ **Adjustment:** under “Type system & metadata”, add:

> Compute `event_code` using the same hash + layout as `lang/` and treat collisions as fatal diagnostics so lang2 stays ABI-compatible with the existing runtime.

### Exception type modeling

Also, be explicit how exceptions appear at the type level in lang2:

* Are they real nominal types (`Exception Foo`)?
* Or are they just metadata attached to `Error`?

Your checker bullet implies exceptions are first-class; SSA/codegen assumes `Error*` is the only runtime carrier.

→ **Adjustment:** clarify:

> Exceptions are compile-time descriptors only; at runtime we always traffic in `Error*`. The exception declaration does not introduce a new first-class value type, only metadata for constructing and decoding `Error*`.

(If that’s not true in lang/, match whatever lang/ actually does, but commit it here.)

---

## 3. Checker semantics

Your checker bullet is correct but too compressed. A couple of spots to tighten:

1. **Can-error function marking**

You say “ensure only can-error functions throw” and “Throw only in can-error fns” later in LLVM section. In lang2, that needs a *single source of truth*:

* Decide where “can-error” is represented (function type flag, result type wrapper, or attribute).
* Ensure the checker sets it and MIR/SSA just trusts the flag, not re-deriving it.

→ **Adjustment:** add:

> The checker is the only stage that decides whether a function is can-error; later stages treat that as trusted metadata for validating Throw / error edges.

2. **Error.attrs typing**

You mention validating `Error.attrs` indexing and `DiagnosticValue` methods; I’d explicitly state what the type rules are:

* `Error.attrs["key"]` → `DiagnosticValue`
* `dv.as_int()` / others have well-defined return types and fail only at runtime, not type-checking

→ **Adjustment:** append:

> Treat `Error.attrs[...]` as returning a `DiagnosticValue` with a fixed, known SSA type; `.as_*` helpers are typed (e.g., `as_int : DiagnosticValue → Int`) but may still produce runtime failures for mismatched kinds.

3. **Catch exhaustiveness**

Even if you don’t support full pattern exhaustiveness, you should explicitly decide:

* Is a `try` with no catch-all allowed?
* What happens to uncaught errors? Bubble up as `Error*` or become trapped/unreachable?

→ **Adjustment:** add:

> In lang2, lack of a catch-all in a `try` is allowed; uncaught exceptions propagate as `Error*` to the caller. Checker does not require exhaustiveness, only that event-specific catch arms refer to known exceptions.

If lang/ is stricter, copy that; but write it down.

---

## 4. MIR/SSA & codegen interactions

The lowering bullet is good but glosses over two tricky edges:

1. **Void + error combinations**

You already solved Void normal returns. Now combine that with errors:

* For can-error functions:

  * non-void result: `{T, Error*}`
  * void-like result: `Error*` only

Make sure MIR/SSA has a **single canonical representation** of that pair (a dedicated “can-error result” type) so you don’t start hand-rolling tuple structs all over the place.

→ **Adjustment:** in “LLVM/codegen invariants”, append:

> Represent can-error results with a dedicated internal type (`CanError<T>`), which is `{T, Error*}` for non-void and `Error*` for void-like, to keep SSA and codegen uniform.

2. **Error edges & block params**

You call out threading live locals via block params, which is right. I’d explicitly guard against *half-wired* edges:

* Every `Call` in a can-error function must have *both* a normal and error successor, even if error just re-throws.
* Non-can-error functions must not have error edges at all.

→ **Adjustment:** under “HIR→MIR/SSA lowering”:

> Enforce that all calls in can-error functions have both normal and error edges; calls in non-can-error functions are lowered without error edges and must not be able to receive `Error*`.

This makes your SSA invariants checkable with simple MIR tests.

---

## 5. DiagnosticValue & runtime ABI

Your runtime section is fine but I’d add two explicit decisions:

1. **Single shared runtime vs fork**

You wrote “port `diagnostic_runtime` and `error_dummy` under `lang2/` build”. Decide:

* Are you physically duplicating C files under `lang2/` or just reusing the same runtime with different link targets?

My advice: **do not fork** unless absolutely necessary.

→ **Adjustment:** change the wording to:

> Reuse the existing `diagnostic_runtime` and `error_dummy` C sources for lang2 (shared runtime), ensuring the struct layout and function signatures remain ABI-identical. Only lang2’s build system wiring should differ.

2. **Optional helpers: scope**

You mention Optional helpers “if needed”. That’s ambiguous. If lang2’s type system already exposes `Optional<T>` with some error helpers, include them now; if not, explicitly defer.

→ **Adjustment:** specify:

> Defer Optional-related helpers until lang2’s `Optional<T>` story is stable; in this port, only implement the DiagnosticValue + Error APIs needed to satisfy exception construction and attributes.

---

## 6. Tests & staging

Your “Tests to add” bullet is good but still a bit “happy path”. I’d add a short list of *negative* cases so you don’t regress silently:

* Throw in a non-can-error function → checker error.
* Catch arm referencing unknown exception name → checker error.
* Duplicate exception declarations (same FQN) → checker error.
* Event code collision → diagnostic with both source locations.
* Use of `.attrs["key"]` on non-Error → type error.
* Use of DiagnosticValue helpers on non-DV values → type error.

→ **Adjustment:** under “Tests to add”, add a line:

> Include negative tests for: throws in non-can-error functions, unknown exception names in catch arms, duplicate exception declarations, event-code collisions, and misuse of `Error.attrs`/DiagnosticValue helpers.

### Migration phasing

Your last open question asks about staging. My recommendation:

1. **Phase 1 – core exception + event code + throw/catch + can-error results**

   * No captured locals, no `DiagnosticValue` attrs beyond a trivial string/primitive payload.

2. **Phase 2 – DiagnosticValue & attrs**

   * Bring in full `DiagnosticValue` helper surface and `Error.attrs[...]` lowering.

3. **Phase 3 – captures (^)**

   * Wire captures into diagnostics once the DV pipeline is stable.

→ **Adjustment:** in “How to stage migration”, explicitly commit to a 3-phase rollout like above. It makes it easier to keep lang2 compiling at each checkpoint.

---

## Phase 1 progress (in-flight)

- Multi-attr throw lowering is now deterministic: payload is always stored under the fixed "payload" key, all named fields are attached via `ErrorAddAttrDV`, and DV construction uses the exception/ctor name (not the attr key) to keep semantics stable.
- Parser adapter now handles try/catch/while/for/import/exception ctors/ternary/placeholder, so lang2 front end accepts the same surface constructs as lang/.
- Exception declarations emit event codes via the shared xxhash64 ABI helper (`event_code`), with diagnostics for duplicates/payload collisions; catalog is threaded through driver/e2e runner, checker, and HIR→MIR lowering (`exc_env`).

## 2025-XX-XX progress update

- Throw payloads are now **exception constructors only** (or existing `Error` handles); DV-as-payload throws are rejected in the checker. Stage2 lowering uses the event name for the first attr and attaches all declared fields under their names; no implicit `"payload"` slot remains.
- Stage0 adapter now wraps all locations in structured `Span`, closing the last raw/None leaks (imports/catch arms/etc.), and the checker emits spans for resolution errors.
- Driver/stage4 integration tests updated to use exception ctors as throw payloads; full `just` suite is passing after the throw-payload model change.
- Added parser + driver tests covering event-code generation, duplicate exception diagnostics, and checker rejection of unknown catch events when no exception is declared.
- HIR→MIR now enforces can-throw semantics when known (asserts on throw/try in known non-can-throw functions; allows when undecided) to keep invariants tight without breaking legacy tests.
- LLVM DV/attrs path now has an end-to-end round-trip test that constructs an Error, attaches an additional attr, retrieves it via `Error.attrs[...]`, converts with `dv.as_int`, unwraps Optional, and returns the value to lock the ABI.
- Rolled back MIR call edges in favor of the value-based FnResult model; pre-analysis now records call sites separately and treats only error constructors as `may_fail`, and stage4 invariants forbid ConstructError in non-can-throw functions while keeping ordinary calls allowed.
- Added MIR Call/MethodCall shape tests to lock the plain-instruction form (no error edges).
- Stage4 now enforces that can-throw signatures are real FnResult<_, Error> via the available TypeEnv to keep the ABI honest before codegen; added targeted tests.
- LLVM codegen now requires TypeTable info for can-throw FnResult lowering, emits named FnResult types for supported ok payloads (Int/String/Void-like), checks payload type consistency on ConstructResultOk/Err, and fails fast on unsupported shapes instead of falling back silently.
- Fixed FnResult<Ref<T>> Err lowering to zero the ptr ok slot correctly (opaque pointer literal) and added coverage; headers now call out that arrays are supported as values but not yet as FnResult ok payloads.
- Phase 2 scaffolding started: TypeTable/resolve support for DiagnosticValue and Optional<T>; checker knows Error.attrs[String] returns DiagnosticValue and dv.as_{int,bool,string} return Optional<T>; HIR→MIR lowers Error.attrs[...] to ErrorAttrsGetDV and dv.as_* to dedicated DVAs* MIR ops with new tests. LLVM backend still fails fast on DV ops (to be implemented next).
- Catch-arm spans are now threaded end-to-end: HIR nodes carry a `Span`, normalization preserves it, collection asserts it, and `validate_catch_arms` attaches it (with notes pointing to first occurrences for duplicates/catch-alls).
- Exception constructors are now a dedicated HIR node (`HExceptionInit`) that is only valid as a throw payload; DV ctor (`HDVInit`) is primitive-only. HIR→MIR throw lowering treats the thrown value as an Error: fields become attrs under their declared names (no special “payload” key); type checker rejects exception ctors outside `throw` and any non-exception throw payloads with span diagnostics.
- DV helper methods (`as_int` / `as_bool` / `as_string`) now short-circuit before registry/signature resolution; misuse on non-DiagnosticValue receivers emits a span-carrying diagnostic rather than falling through.
- Checker now enforces DiagnosticValue-only throw payloads and attr field values (with span diagnostics), and parser exception diagnostics wrap parser locs in structured `Span`.

### Next steps (attr/DV correctness focus)

- Checker (surface-level): require throw payloads and any attr/value constructs to be `DiagnosticValue`; emit span-carrying diagnostics on the source nodes for unsupported payload types. Keep checker rules independent of MIR op names (e.g., no coupling to `ErrorAddAttrDV`). (Done)
- Spans: extend structured spans to remaining exception diagnostics (duplicate/unknown exception declarations/catches) for UX parity. (Done)
- Tests: (1) checker-level negative for non-DV attr payload rejected with span, (2) LLVM/codegen round-trip for a multi-attr error using DV payloads only to exercise runtime/ABI. (Done)
- Deferred: opaque/object/array attrs until a stable runtime handle/ownership model exists; current attrs remain limited to existing DV kinds (Int/Bool/String/Missing), no implicit coercions.
---

## 7. Answering your open questions directly

From the “Open questions / decisions to confirm” section:

1. **Synthetic arg-key/args-view structs?**

Drop them for lang2 if the refactor doc already moves you to an attrs-first model.

* They complicate codegen and don’t buy much now that attrs/DV exist.
* If lang/ still has them for legacy reasons, you can keep them there but not introduce them to lang2.

2. **Hash function / namespace?**

Reuse lang/’s function and namespace exactly, including:

* hash algo
* FQN format
* domain tag layout

Any deviation is a future ABI headache.

3. **Staging: minimal vs full DV surface?**

Go **minimal parity first** (exceptions + event codes + throw/catch + can-error results) then layer in full DiagnosticValue helpers. Trying to bring everything in one step will make debugging miserable.

---
