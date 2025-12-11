# work-in-progress: `lang.core.print` / `println` / `eprintln` for Drift v1

## 0. Status

- **Decision**: v1 of Drift will *not* expose `std.console` yet.
- Instead, we introduce a minimal **trio of prelude functions** in `lang.core`:
  - `fn print(text: String) returns Void`
  - `fn println(text: String) returns Void`
  - `fn eprintln(text: String) returns Void`
- These are **auto-imported** for every module (no `import` required).
- Implementation in v1 calls directly into the C runtime (stdout/stderr).
- A future revision will introduce `std.console` with proper handle-based APIs; at that point, the trio will be reimplemented in terms of `std.console`, but their *signatures and behavior remain stable*.

This file tracks what needs to change in the spec, the compiler, and the runtime to ship this in v1.

---

## 1. Language / spec changes

### 1.1. Define the trio in `lang.core`

In the spec section that currently documents `lang.core` (the `source_location()` helper), expand it to describe `lang.core` as the **core prelude module**, and add the trio:

```drift
module lang.core

/// Writes UTF-8 text to the process standard output.
/// Does not append a newline.
fn print(text: String) returns Void

/// Writes UTF-8 text to the process standard output,
/// then appends a single '\n' newline.
fn println(text: String) returns Void

/// Writes UTF-8 text to the process standard error,
/// then appends a single '\n' newline.
fn eprintln(text: String) returns Void
```

Semantics (to capture in the spec):

- All three functions accept `String` (UTF‑8); they do not perform formatting or interpolation.
- `print` writes exactly the bytes of `text` to **stdout**.
- `println` writes the bytes of `text` followed by a single `\n` to **stdout**.
- `eprintln` writes the bytes of `text` followed by a single `\n` to **stderr**.
- The encoding is UTF‑8 on all platforms; the runtime does whatever translation is necessary for the host console.
- They **do not throw** in v1; if the underlying write fails, the runtime is allowed to terminate the process or ignore the error (implementation note; we can tighten this later if we introduce `Result`-based I/O APIs).

### 1.2. Auto-import / prelude semantics

Add a short rule in the spec:

- `lang.core` is **implicitly imported** into every Drift module.
- Its public functions, including `print`, `println`, and `eprintln`, are always available without an explicit `import`.

Concretely:

- Update the “Imports and standard I/O” chapter to say:
  - “Apart from the explicit `import` mechanism, the module `lang.core` is *always in scope* for every module. Its public functions can be called directly (e.g., `println("hello")`).”
- Keep explicit imports of `lang.core` legal if you want, e.g. `import lang.core.source_location`, but make clear that for the trio it is *not* required.

### 1.3. Canonical hello world

Update the “hello world” / first program example to:

```drift
fn main() returns Int {
    println("hello, world")
    return 0
}
```

Mention briefly that `println` comes from `lang.core` and needs no `import`.

### 1.4. Standard I/O chapter (Chapter 18) – v1 simplification

Right now Chapter 18 still talks about `std.io` / `std.console` and global `out`/`err`/`in` instances.

For v1, do the following:

1. Replace the current `std.console` subchapter with a short **“v1 console I/O”** section:

   - Say that v1 only guarantees the trio in `lang.core`.
   - Reserve the names `std.io` and `std.console` for future expansion, but explicitly mark them **out of scope** for this revision.

2. The new section can be something like:

   > In v1, console I/O is intentionally minimal. The language guarantees only three functions, defined in `lang.core` and auto-imported into every module:
   >
   > ```drift
   > fn print(text: String) returns Void
   > fn println(text: String) returns Void
   > fn eprintln(text: String) returns Void
   > ```
   >
   > These write UTF‑8 text to the process standard output or standard error. Future revisions will introduce richer stream-based APIs in `std.io` / `std.console`.

3. If you want to keep the old `std.console` design notes, move them into an appendix or a “future directions” subsection, clearly labeled as **non-normative / not implemented in v1**.

### 1.5. Imports chapter (7.1)

The imports section currently shows examples like:

```drift
import std.console.out        // ...
```

For the trio-only v1:

- Remove (or mark as “future”) any examples involving `std.console.out`.
- Add a very short note that `lang.core` is auto-imported, and that `println` is therefore usable without an `import`.

No changes are required to the *mechanics* of `import`; the trio sidestep imports entirely by living in the core prelude.

---

## 2. Compiler work (driftc)

### 2.1. Represent `lang.core.print` / `println` / `eprintln` as prelude functions

We already have:

- A type table with `String`, `Void`, etc.
- Some concept of pre-populated functions for `lang.core.source_location` (intrinsic).

Extend the prelude initialization step to register three **intrinsic functions** in the `lang.core` module:

- `lang.core.print : (String) -> Void`
- `lang.core.println : (String) -> Void`
- `lang.core.eprintln : (String) -> Void`

Implementation outline (pseudo-Python around your existing tables):

```python
lang_core_mod_name = "lang.core"
lang_core_mod_id = module_ids.setdefault(lang_core_mod_name, len(module_ids))

string_tid = type_table.lookup("String")
void_tid = type_table.lookup("Void")

def register_lang_core_trio():
    for name in ("print", "println", "eprintln"):
        sig = FunctionSignature(
            module_id=lang_core_mod_id,
            name=name,
            param_types=(string_tid,),
            result_type=void_tid,
            is_exported=True,      # visible everywhere
            is_intrinsic=True,     # special lowering in codegen
        )
        function_table.register(sig)
```

Key points:

- They live in the `lang.core` module.
- Mark them as **predeclared intrinsics** so codegen can treat them specially.
- They are always visible as if the user had `import lang.core` in every module.

### 2.2. Name resolution / auto-import behavior

We do **not** want to hard-code three magic identifiers; they should flow through the same machinery as other functions but with `lang.core` automatically in scope.

Implementation sketch:

1. When you build the symbol environment for a module:
   - Add a synthetic `ImportDecl` for `lang.core` (internally), or
   - Add `lang.core`’s exported symbols into the module’s initial namespace.

2. A simple approach:

   - At the start of type-checking a module, treat `lang.core` as **always imported under its module name**.
   - When looking up a bare identifier like `println`, check:
     1. Locals / current module definitions.
     2. Imported modules’ re-exported symbols (if you have that).
     3. Fallback: `lang.core.println` by-name lookup, if present and unshadowed.

3. Keep the resolution rules deterministic:

   - If the user defines `fn println(...)` in their own module, it should **shadow** `lang.core.println`.
   - If that’s too much for v1, you can disallow shadowing and emit a “cannot redefine prelude function `println`” error. Pick one behavior and document it.

For a first implementation, easiest is:

- Treat `print`, `println`, and `eprintln` as **reserved names at top-level**:
  - If user defines them: error.
  - Otherwise, calls to these names bind to the `lang.core` intrinsics.

Later, you can soften this to allow shadowing if you really want.

### 2.3. Type-checking

The type checker just needs to know:

- Each of the trio has signature `(String) -> Void`.
- Arguments must be explicit `String` values; no implicit formatting or integer-to-string conversion.

So:

```drift
println("x = " + x.to_string())
```

type-checks as usual, and `println` is just another function with known signature.

### 2.4. Lowering / codegen

For v1, we *don’t* want to drag in module / interface machinery; we just call C helpers.

Define three runtime entry points (C ABI), for example:

```c
// runtime/lang_core_io.h
void drift_lang_core_print(const char* utf8, size_t len);
void drift_lang_core_println(const char* utf8, size_t len);
void drift_lang_core_eprintln(const char* utf8, size_t len);
```

Then, in your backend:

- When you see a call to `lang.core.print`:
  1. Evaluate the `String` argument into your runtime string representation.
  2. Extract `(data_ptr, byte_len)` from the string (however your current runtime models `String`).
  3. Emit a call to `drift_lang_core_print(data_ptr, byte_len)`.

Same for `println`/`eprintln`.

If you don’t have a real String runtime yet, you can temporarily:

- Restrict calls to string literals (for bootstrapping tests), or
- Implement a minimal string ABI just for literals (static data pointer + length).

**Important**: this lowering is *pure implementation detail*; spec just says “writes text to stdout/stderr.”

### 2.5. Codegen gating by `-o`

You probably want the trio available only when you’re producing a binary (`-o`):

- When `-o` is **passed**:
  - Register `lang.core.print/println/eprintln` as intrinsics.
  - Link in `lang_core_io.c` which defines the three C helpers.
- When `-o` is **omitted**:
  - Either:
    - Still allow the trio for interpreter mode (and provide host-side implementations), **or**
    - For now, forbid calls to them and emit a friendly error: “console I/O is only available when producing a binary.”

Pick whichever matches how your driver already handles runtime features.

---

## 3. Runtime implementation (C layer)

### 3.1. POSIX sketch

Minimal implementation:

```c
#include <stdio.h>
#include <stdlib.h>

void drift_lang_core_print(const char* utf8, size_t len) {
    if (!utf8 || len == 0) return;
    fwrite(utf8, 1, len, stdout);
    // no automatic flush
}

void drift_lang_core_println(const char* utf8, size_t len) {
    if (len > 0 && utf8) {
        fwrite(utf8, 1, len, stdout);
    }
    fputc('\n', stdout);
    // consider fflush(stdout) here (optional)
}

void drift_lang_core_eprintln(const char* utf8, size_t len) {
    if (len > 0 && utf8) {
        fwrite(utf8, 1, len, stderr);
    }
    fputc('\n', stderr);
    // consider fflush(stderr) here (optional)
}
```

Notes / decisions:

- **Flushing**:
  - For now, we *don’t* flush automatically beyond what the C library does for line-buffered terminals.
  - If you want deterministic behavior, you can explicitly `fflush(stdout)` / `fflush(stderr)` in the println/eprintln functions; just write that into the spec as “implementation-defined” for v1.

- **Encoding**:
  - Assume `String` is already UTF‑8 and pass through unchanged.
  - If you ever add platform-specific console encoding quirks (e.g., Windows wide chars), you can hide those conversions inside these helpers.

### 3.2. Windows and others

On non-POSIX targets:

- Use `WriteFile` on `GetStdHandle(STD_OUTPUT_HANDLE)` / `STD_ERROR_HANDLE`, or
- Use the C runtime `fwrite`/`fputc` as above; let the C library deal with console specifics.

The ABI signature stays: `(const char* utf8, size_t len)`.

---

## 4. Testing plan

### 4.1. Unit tests (compiler-level)

- Parse + type-check programs that call `println`, `print`, `eprintln` with `String` arguments.
- Verify type errors when calling with non-`String` arguments (e.g., `println(42)`).
- Confirm the names are visible without explicit imports.
- (If you allow shadowing) verify that local `fn println(...)` correctly hides the prelude one, or (if you forbid it) that you get the expected error.

### 4.2. Integration tests (runtime)

Small end-to-end tests:

1. Simple:
   ```drift
   fn main() returns Int {
       println("hello")
       return 0
   }
   ```
   - Run compiled binary.
   - Assert that stdout contains `hello\n`.

2. Mixed:
   ```drift
   fn main() returns Int {
       print("a")
       println("b")
       eprintln("err")
       return 0
   }
   ```
   - Assert stdout = `ab\n`.
   - Assert stderr = `err\n`.

3. Empty string:
   ```drift
   fn main() returns Int {
       println("")
       eprintln("")
       return 0
   }
   ```
   - Assert stdout = `\n`.
   - Assert stderr = `\n`.

These can be implemented with your existing test harness (capture stdout/stderr of the compiled binary, compare against expected text).

---

## 5. Future evolution notes (for later, not v1)

Keep this at the bottom of the file so future you remembers the plan:

- Introduce `std.console` with **handle-returning** functions:

  ```drift
  module std.console {
      public fn out()   returns ConsoleWriter
      public fn err()   returns ConsoleWriter
      public fn input() returns ConsoleReader
  }
  ```

- Make `ConsoleWriter` / `ConsoleReader` cheap, `Copy` handle types pointing into a runtime-managed singleton.
- Re-implement `lang.core.println` / `eprintln` in terms of `std.console.out()` / `.err()` once that module exists.
- Deprecate exposing any global `out`/`err` objects; keep them as runtime implementation details.

For now, **v1 only cares about the trio**, implemented directly in C and described in the spec as part of `lang.core`.

