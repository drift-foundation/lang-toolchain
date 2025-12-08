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

Do this in two steps: **literals + pass-through** first; **concat/print** second.

### 3A. Minimal: literals and pass-through (in progress)

* LLVM module seeds `%DriftString = { %drift.size, i8* }`.
* ConstString lowering emits a private UTF-8 global with escaped bytes and builds the struct inline (len/data); no runtime call.
* Return/call lowering now understands `%DriftString` when a shared TypeTable is provided (headers and arguments use `%DriftString`; return types are `%DriftString`); Int remains the default fallback.
* IR tests cover a literal return and a pass-through call `id("abc")`.

TODO in this phase:
* Tighten literal escaping if we start using non-ASCII or embedded quotes (currently emits `\XX` hex escapes, quotes/backslashes escaped).

### 3B. Then: concat and print

4. **Concat**

   * In the expression lowering:

     * For `String + String`, emit:

       ```llvm
       %res = call %DriftString @drift_string_concat(%DriftString %a, %DriftString %b)
       ```
     * `%a` and `%b` are the lowered operands.

5. **Print**

   * If you bring over a `drift_print_string` helper:

     * Lower whatever print primitive you have to:

       ```llvm
       call void @drift_print_string(%DriftString %s)
       ```
   * If lang2 doesn’t have a print builtin yet, you can skip this and just keep `concat` and “return string” working.

6. **No frees**

   * For v1, do not insert any `drift_string_free` calls from the compiler. That avoids accidental frees of literals or copies.
   * The helpers remain available for hand-written C/runtime usage and future language intrinsics.

---

## Phase 4 – Array<String> readiness

Goal: make `Array<String>` work with the new struct representation.

1. **Element type**

   * In `_llvm_array_elem_type(type_id)`:

     * When `type_id` is `String`, return `%DriftString` type, just like for regular scalars.
   * Ensure your array storage uses the element type directly, so `Array<String>` becomes a buffer of `{len, data}` structs.

2. **Checker**

   * Arrays should already be generic over element type. Confirm there’s no “only numeric/scalar” assertion that breaks when the element is a struct.

3. **Tests**

   * Add tests:

     ```drift
     fn first(xs: Array<String>) returns String {
         return xs[0];
     }

     fn make() returns Array<String> {
         return ["a", "b"];
     }
     ```

---

## Phase 5 – entrypoint groundwork

Goal: once `String` and `Array<String>` lower, design the `main(argv: Array<String>)` shim.

1. **C entry shim**

   * In runtime C:

     ```c
     int drift_entry(int argc, char **argv);
     ```
   * `main`:

     ```c
     int main(int argc, char **argv) {
         return drift_entry(argc, argv);
     }
     ```

2. **Inside `drift_entry`**

   * Build an `Array<String>` of length `argc` by:

     * Calling `drift_string_from_cstr(argv[i])` for each arg.
   * Call the Drift `fn main(argv: Array<String>) returns Int`.
   * Return result as `int` (truncate/extend as needed from the Drift `Int` ABI carrier).

3. **Wire in the backend**

   * Teach lang2’s codegen to:

     * Find the user `main(argv: Array<String>) returns Int` symbol.
     * Export a C-visible `drift_entry` wrapper that calls it.

(If you’re not ready to expose `main(argv: Array<String>)` yet, you can defer this until after literals and concat are stable.)

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
