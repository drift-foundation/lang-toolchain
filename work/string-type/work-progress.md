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

1. **Create lang2 runtime string module**

   * Under `lang2/codegen/runtime/` (or wherever you keep runtime C):

     * Add `drift_string.c` and `drift_string.h` copied from lang/.
   * Keep the **struct layout** identical conceptually:

     ```c
     typedef <Uint-carrier> drift_size_t;   // see next bullet
     typedef struct DriftString {
         drift_size_t len;
         char *data;
     } DriftString;
     ```

2. **Map `drift_size_t` to lang2’s `Uint` carrier**

   * Identify what LLVM and ABI use for `Uint` in lang2 (most likely `i64`).
   * In `drift_string.h`, define:

     ```c
     typedef uint64_t drift_size_t;  // or whatever the Uint ABI is
     ```
   * This must match the `%drift.size` / `Uint` TypeId carrier you use in LLVM.

3. **Adjust helper signatures if needed**

   * Ensure all helpers use `drift_size_t` instead of `size_t` where appropriate.
   * Keep the API set:

     * `DriftString drift_string_from_cstr(const char *s);`
     * `DriftString drift_string_from_utf8_bytes(const uint8_t *data, drift_size_t len);`
     * `DriftString drift_string_concat(DriftString a, DriftString b);`
     * `void drift_string_free(DriftString s);`
     * `void drift_print_string(DriftString s);` or `char *drift_string_to_cstr(DriftString s);` if that’s what you had.
   * Fix any `size_t` / `ssize_t` arithmetic to use `drift_size_t` consistently and guard against overflow on `len` additions.

4. **Integrate with lang2 build**

   * Add the new C files to whatever build system / CMake / ninja step builds the lang2 runtime.
   * Confirm you get a compiled runtime library exposing these symbols.

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

### 3A. Minimal: literals and pass-through

1. **LLVM type for String**

   * Implement `_llvm_type_for_string` or equivalent:

     * Return `ir.LiteralStructType([ llvm_uint_type, ir.IntType(8).as_pointer() ])`.

2. **String literal lowering**

   * For each `HStringLiteral`:

     * Emit a private global:

       ```llvm
       @.str0 = private unnamed_addr constant [N x i8] c"..."
       ```
     * Lower the expression to:

       ```llvm
       %p   = getelementptr [N x i8], [N x i8]* @.str0, i32 0, i32 0
       %len = <constant N as i64>   ; or Uint type
       %s   = insertvalue %DriftString undef, %len, 0
       %s2  = insertvalue %DriftString %s, i8* %p, 1
       ```
   * No runtime call; this is purely structural.

3. **Function params/returns**

   * Ensure functions with `String` parameters/returns use `%DriftString` as the ABI type and follow normal struct-by-value convention.

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

