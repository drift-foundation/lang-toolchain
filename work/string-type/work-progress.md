## Phase 0 – reconnaissance in lang/

Goal: know exactly what you’re reusing and where it diverges from lang2.

1. **Find the runtime impl**

   * In `lang/runtime` (or similar), locate:

     * `string.c` / `string.h` or `drift_string.c` / `drift_string.h`.
   * Record:

     * `struct DriftString` layout (field order, types).
     * All exported helpers and their signatures:

       * `drift_string_from_cstr`
       * `drift_string_from_utf8_bytes`
       * `drift_string_concat`
       * `drift_string_free`
       * `drift_string_to_cstr` / `drift_print_string` (or similar).

2. **Check old ABI types**

   * Confirm what `drift_size_t` was in lang/ (`size_t` or a typedef).
   * Confirm how `usize` or `Size` mapped to C/LLVM there.

3. **Inspect String lowering in lang/**

   * In the old LLVM backend:

     * How does it lower a `String` type to LLVM? (`{ iN, i8* }` or `i8*` to a heap object, etc.)
     * How are **string literals** lowered?

       * `.rodata` constant + struct build vs. runtime helper.
     * How does `+` on `String` lower?

       * Direct call to `drift_string_concat`?
     * How does printing lower?

       * Direct call to `drift_print_string` or `printf` wrapper?

4. **Look for String / Array<String> tests**

   * Locate any tests that:

     * Use `fn f() returns String`.
     * Use string literals in expressions.
     * Use `Array<String>`.
   * Note what they assert (type behavior, runtime output, IR patterns).

You don’t change anything here; just take notes so the port isn’t guesswork.

---

## Phase 1 – runtime port into lang2

Goal: copy the working C code, adjust it to **Uint-based size semantics** and lang2’s directory layout.

**Done:** `lang2/codegen/runtime/string_runtime.[ch]` copied/adapted from lang/:

* Layout: `typedef uint64_t drift_size_t;` and `typedef struct DriftString { drift_size_t len; char *data; }`.
* Helpers: `drift_string_from_cstr`, `_from_utf8_bytes`, `_from_int64`, `_from_bool`, `_literal`, `_concat`, `_free`, `_to_cstr`, `_eq`.
* Notes: len is treated as Uint carrier (`i64` in v1). No frees inserted by the compiler yet.

Remaining: wire these runtime files into the lang2 build when we start emitting String codegen.

---

## Phase 2 – type/core alignment in lang2

Goal: ensure `String` is already a first-class type in the compiler’s TypeTable and is mapped to `DriftString` in the ABI.

1. **Type table**

   * Verify there is a single `TypeId` for `String`, and a helper like `_string_type()` to retrieve it.
   * Don’t introduce new `TypeKind`; reuse the existing `String` kind/hard-coded slot.

2. **ABI mapping**

   * In the lang2 ABI layer:

     * Map `String` TypeId → `DriftString` ABI:

       * C-side: `struct DriftString`.
       * LLVM-side: literal struct type:

         ```llvm
         %DriftString = type { i64, i8* }    ; or equivalent if Uint != i64
         ```

3. **Checker / SSA typing**

   * Ensure `_infer_hir_expr_type` returns the `String` TypeId for string literals.
   * Ensure SSA typing treats `String` as a valid scalar / value type (no “must be integer” assumptions for arithmetic; that’s handled in the checker, not the type layer).
   * No automatic destruction or lifetime tracking yet.

---

## Phase 3 – LLVM lowering for String

**Status: complete for literals, params/returns, len/eq/concat.**

* `%DriftString = { %drift.size/i64, i8* }` seeded; literals emit private UTF-8 globals (with escaping) and inline struct builds.
* Calls/params/returns respect `%DriftString` when a shared `TypeTable` is provided; non-void calls without dest now assert instead of emitting `call void`.
* HIR→MIR emits `StringLen`/`StringEq`/`StringConcat` for `s.len`, `s == t`, `s + t` (including non-trivial expressions via `_infer_expr_type`). Builtins `byte_length(x)`/`len(x)`, `string_eq(a,b)`, `string_concat(a,b)` are routed through these MIR ops. LLVM lowers:
  * `StringLen` → `extractvalue %DriftString %v, 0` (Uint carrier).
  * `StringEq` / `StringConcat` → runtime calls, with module-level `declare` once.
* Array lowering understands `Array<String>`: element type maps to `%DriftString`, `_sizeof/_alignof` handle the struct (16/8), bounds checks stay on `i64`.
* Type system: `Uint` is real; `.len` on `String`/`Array` returns Uint end-to-end. E2E Drift files updated accordingly.
* Entry wrapper: skipped when entry is `main`; e2e runner now prefers `main` over `drift_main`.
* Tests: LLVM IR tests cover string literals/params/ops and Array<String>; e2e covers byte_length/eq/concat and Array<String> length sums (all green under `just lang2-codegen-test`).
* Added canonical empty string support: HIR→MIR injects `String.EMPTY` as a zero-length literal; runtime uses `{len=0, data=NULL}` for empties (no heap alloc). 
* `%drift.size` alias reinstated in IR (Uint carrier); tests updated accordingly. Checker now maps `"Uint"` in declared/opaque types to the canonical Uint TypeId so `.len`-typed values and annotations match.

TODOs:
* Keep literal escaping robust for more non-ASCII cases.
* Move more string op detection into HIR→MIR (remaining BinaryOp string fallback is minimal now).
* Future: `char_length` helper for user-visible characters; argv shim for `main(argv: Array<String>)`; more negative type-error tests (String misuse, etc.).

---

## Phase 4 – Array<String> readiness

**Status: initial support done.**

* `_llvm_array_elem_type` maps String TypeIds to `%DriftString`; size/align for `%DriftString` are hard-coded 16/8 for v1.
* Bounds checks remain on `i64` (`%drift.size`); `.len`/`.cap` return Uint.
* IR test covers Array<String> literal + index; e2e sums element lengths successfully.
* Checker enforces index `Int` and element type consistency; `.len` returns Uint.

Next: add more e2e/IR cases for stores into Array<String> and richer indexing.

---

## Phase 5 – entrypoint groundwork

Goal: allow `fn main(argv: Array<String>) returns Int` with a C-visible shim.

Plan (not started):
* Runtime: add `drift_entry(argc, argv)` that builds `Array<String>` via string/array runtimes and calls the Drift `main`.
* Backend: when user `main` has the `Array<String>` signature, emit the shim; otherwise, if entry is plain `main()`, use it directly (no wrapper).
* E2E: argv length/content tests once shim exists.

---

## Phase 6 – tests

Add targeted tests as you go:

1. **Parser / adapter**

   * Type-annotated `String` parameters and returns.
   * `Array<String>` type annotations.

2. **Checker**

   * String literal type inference → `String`.
   * Function:

     ```drift
     fn id(s: String) returns String { return s; }
     ```
   * Ensure `id("abc")` type-checks.

3. **Backend IR tests**

   * Function returning a string literal:

     * Assert emitted IR contains:

       * `.rodata` global for bytes.
       * `%DriftString` build with correct `len` and pointer.
   * Function doing `"a" + "b"`:

     * Assert there is a call to `@drift_string_concat`.

4. **e2e (once len exists)**

   * Drift:

     ```drift
     fn main() returns Int {
         val s = "abc";
         return length(s);  // however you expose len; even a runtime helper test is fine
     }
     ```
   * Run and check you get `3` (or whatever route you use to surface the result).
