# C FFI MVP — Implementation Plan

**Date:** 2026-03-11
**Status:** Draft
**Priority:** High — enables libssl/libcrypto bindings and general native library interop

## 1. Design Summary

### 1.1 MVP Signature/Type Subset

The narrowest safe MVP supports extern C functions over **scalar types only**:

| Drift type | LLVM type | C equivalent | Notes |
|-----------|-----------|-------------|-------|
| `Int` | `isize` (i64) | `intptr_t` / `long` | Pointer-width signed |
| `UInt` | `isize` (i64) | `uintptr_t` / `unsigned long` | Pointer-width unsigned |
| `Uint64` | `i64` | `uint64_t` | Fixed-width |
| `Byte` | `i8` | `uint8_t` / `char` | |
| `Bool` | `i1`/`i8` | `_Bool` / `int` | i1 in registers, i8 at ABI boundary |
| `Float` | `double` | `double` | |
| `RawPtr<T>` | `T*` | `T*` | Opaque pointer passthrough |
| `RawPtr<Byte>` | `i8*` | `void*` / `char*` | Common C pattern |
| (void return) | `void` | `void` | Extern fn returning nothing |

**Explicitly excluded from MVP:**
- `String` (managed, ref-counted — needs marshaling layer, post-MVP)
- `Array<T>` (managed header — needs marshaling, post-MVP)
- Drift structs as value params/returns (layout compatibility unverified, post-MVP)
- Variadic functions (`printf`-style — post-MVP)
- Callbacks / function pointers passed to C (reverse FFI — post-MVP)
- Struct-by-value return (C ABI struct return conventions vary — post-MVP)

This subset is sufficient for libssl/libcrypto because those APIs use opaque pointer
handles (`SSL*`, `SSL_CTX*`, `EVP_MD_CTX*`), integer status codes, and `const char*`
buffers — all representable as `RawPtr<Byte>`, `Int`, and `RawPtr<Byte>`.

### 1.2 Syntax

```drift
module tls;

// Single declaration
extern "C" fn SSL_library_init() nothrow -> Int;

// Block form for multiple declarations from the same library
extern "C" {
    fn SSL_new(ctx: RawPtr<Byte>) nothrow -> RawPtr<Byte>;
    fn SSL_free(ssl: RawPtr<Byte>) nothrow -> Void;
    fn SSL_read(ssl: RawPtr<Byte>, buf: RawPtr<Byte>, num: Int) nothrow -> Int;
    fn SSL_write(ssl: RawPtr<Byte>, buf: RawPtr<Byte>, num: Int) nothrow -> Int;
    fn SSL_get_error(ssl: RawPtr<Byte>, ret: Int) nothrow -> Int;
}
```

**Rules:**
- `extern "C"` functions have no body (terminated by `;`)
- Must be declared `nothrow` (C functions do not participate in Drift exception handling)
- Symbol name is the function name verbatim (no mangling)
- `extern "C"` functions are module-scoped; they can be re-exported via `export { ... }`
- Only the `"C"` ABI string is supported in MVP; the grammar accepts a string literal
  for forward compatibility (`extern "stdcall"` etc. would be a post-MVP extension)
- `unsafe` is required at call sites (calling extern C is an unsafe operation)

### 1.3 Linker Interface

```bash
# Link against libssl and libcrypto
driftc main.drift --link-lib ssl --link-lib crypto -o app

# Add search path
driftc main.drift --link-lib ssl --link-search /opt/openssl/lib -o app

# Static linking
driftc main.drift --link-lib-static ssl -o app
```

CLI flags:
- `--link-lib <name>` → passes `-l<name>` to clang
- `--link-lib-static <name>` → passes `-l:lib<name>.a` to clang (or `-Bstatic -l<name>`)
- `--link-search <path>` → passes `-L<path>` to clang

These are also expressible in `driftc.json` project config:

```json
{
  "link_libs": ["ssl", "crypto"],
  "link_search": ["/opt/openssl/lib"]
}
```

### 1.4 Calling Convention and Safety

- All extern "C" calls are `unsafe`. The caller must be in an `unsafe { }` block
  or the enclosing function must be declared `unsafe`.
- Extern "C" functions are always `nothrow` at the Drift level. If the C function
  signals errors via return codes, the Drift wrapper handles that.
- No automatic null checking on `RawPtr` — the user is responsible.
- No lifetime tracking on `RawPtr` — it is an unmanaged raw pointer.

Typical usage pattern:

```drift
module tls;

extern "C" {
    fn SSL_CTX_new(method: RawPtr<Byte>) nothrow -> RawPtr<Byte>;
    fn TLS_client_method() nothrow -> RawPtr<Byte>;
    fn SSL_CTX_free(ctx: RawPtr<Byte>) nothrow -> Void;
}

pub fn create_tls_context() nothrow -> RawPtr<Byte> {
    return unsafe { SSL_CTX_new(TLS_client_method()) };
}
```

## 2. Implementation Phases

### Phase 1: Parser + AST (grammar, AST nodes, basic validation)

**Files:**
- `lang/driftc/parser/grammar.lark` — add `extern_block` and `extern_fn` productions
- `lang/driftc/parser/ast.py` — add `is_extern_c: bool` to `FunctionDef`; add `ExternBlock` node
- `lang/driftc/parser/__init__.py` — transformer rules for extern declarations

**Grammar additions:**

```lark
extern_block: EXTERN STRING_LIT "{" extern_fn_decl* "}"
extern_fn_decl: FN_KW ident "(" [params] ")" NOTHROW return_sig TERMINATOR

// Single-line form integrated into existing top_level:
extern_fn: EXTERN STRING_LIT FN_KW ident "(" [params] ")" NOTHROW return_sig TERMINATOR

EXTERN: "extern"
```

**Validation at parse time:**
- String literal must be `"C"` (reject others with clear diagnostic)
- Body must be absent (`;` terminator required)
- `nothrow` is mandatory (diagnostic: "extern C functions must be declared nothrow")
- `throws` is rejected (diagnostic: "extern C functions cannot throw")
- Type params are rejected (diagnostic: "extern C functions cannot be generic")

**AST representation:**
- `FunctionDef.is_extern_c = True`
- `FunctionDef.extern_abi = "C"` (string, for forward compat)
- `FunctionDef.body` is empty/`None` (similar to `@intrinsic`)

### Phase 2: Checker + Type Validation

**Files:**
- `lang/driftc/checker/__init__.py` — validate extern fn signatures
- `lang/driftc/checker/call_resolver.py` — register extern fns in callable registry
- `lang/driftc/core/types_core.py` — no changes expected (existing types suffice)

**Checker rules:**

1. **Signature validation**: Only MVP-supported types allowed in extern "C" signatures.
   Reject with diagnostic:
   - `String` params/return → "String is not FFI-safe; use RawPtr<Byte> for C strings"
   - `Array<T>` → "Array is not FFI-safe; use RawPtr<T> for C arrays"
   - Drift struct by value → "struct-by-value is not supported in extern C signatures"
   - `FnResult<T>` → "extern C functions cannot return FnResult; use nothrow"
   - Interface/trait types → "interface types are not FFI-safe"
   - `Ref<T>` (borrow ref) → "borrow references are not FFI-safe; use RawPtr<T>"

2. **Call-site validation**: Calls to extern "C" functions require `unsafe` context.
   Diagnostic: "call to extern C function requires unsafe block"

3. **FnSignature**: Set new flag `is_extern_c = True` (distinct from existing
   `is_extern` which means "from external Drift package"). Set `declared_can_throw = False`.

4. **Export**: extern "C" functions can be listed in `export { }` blocks for
   re-export from a module (allows wrapper packages).

### Phase 3: Stage1 (HIR) + Stage2 (MIR)

**Files:**
- `lang/driftc/stage1/ast_to_hir.py` — pass through `is_extern_c` flag
- `lang/driftc/stage1/hir_nodes.py` — add `is_extern_c` to HIR function representation
- `lang/driftc/stage2/hir_to_mir.py` — skip body lowering for extern fns (like intrinsics)
- `lang/driftc/stage2/mir_nodes.py` — add `is_extern_c` to `MirFunc` or use sentinel

**MIR handling:**
- Extern "C" functions get a `MirFunc` with no blocks (empty body), similar to intrinsics
- `Call` instruction works as-is: `Call(dest, fn_id, args, can_throw=False)`
- The `fn_id` resolves to the extern function; codegen recognizes it by `is_extern_c`

**Stage3/Stage4 (analysis):**
- Throw summary: extern "C" fns are always nothrow — no analysis needed
- SSA: no body to SSA-transform
- Borrow checker: `RawPtr` is untracked (no borrow analysis on raw pointers)

### Phase 4: LLVM Codegen

**Files:**
- `lang/codegen/llvm/llvm_codegen.py` — emit `declare` for extern fns, lower calls

**Changes:**

1. **Module preamble** (around line 1100-1150 where existing runtime `declare`s live):
   For each extern "C" function in the program, emit:
   ```llvm
   declare <ret_type> @<symbol_name>(<param_types>)
   ```
   Example: `declare i8* @SSL_new(i8*)` for `fn SSL_new(ctx: RawPtr<Byte>) nothrow -> RawPtr<Byte>`

2. **Type mapping**: Use existing `_llvm_type_for_typeid()` (line 6843). The MVP types
   already have correct LLVM mappings. For `Void` return, emit `void`.

3. **Call lowering** (`_lower_call`, line 3596): Add a check early in the function:
   ```python
   if callee_sig.is_extern_c:
       # Direct call, no FnResult wrapping, no throw handling
       llvm_ret = self._llvm_type_for_typeid(callee_sig.return_type_id)
       llvm_args = [self._emit_value(a) for a in instr.args]
       if llvm_ret == "void":
           self._emit(f"  call void @{symbol}({', '.join(llvm_args)})")
       else:
           self._emit(f"  {dest} = call {llvm_ret} @{symbol}({', '.join(llvm_args)})")
       return
   ```
   This bypasses the entire FnResult/throw/exception machinery.

4. **Skip body emission**: Similar to intrinsics (line 410-412), skip `define` for
   extern "C" functions — they are declaration-only.

### Phase 5: Linker Integration

**Files:**
- `lang/driftc/driftc.py` — CLI flag parsing, pass-through to clang link command

**Changes:**

1. **Argument parser** (around line 6877): Add:
   ```python
   ap.add_argument("--link-lib", action="append", default=[], help="Link against library (-l)")
   ap.add_argument("--link-lib-static", action="append", default=[], help="Link statically against library")
   ap.add_argument("--link-search", action="append", default=[], help="Add library search path (-L)")
   ```

2. **Link command construction** (around line 9750-9800): Append to clang args:
   ```python
   for path in args.link_search:
       link_cmd.extend(["-L", path])
   for lib in args.link_lib:
       link_cmd.extend(["-l", lib])
   for lib in args.link_lib_static:
       link_cmd.extend([f"-l:lib{lib}.a"])
   ```

3. **driftc.json integration** (around line 7000): Read `link_libs`, `link_search`
   from project config and merge with CLI flags.

### Phase 6: Diagnostics

New diagnostic codes for FFI-specific errors:

| Code | Phase | Message |
|------|-------|---------|
| E-FFI-001 | parser | `extern block ABI must be "C"` |
| E-FFI-002 | parser | `extern C functions must be declared nothrow` |
| E-FFI-003 | parser | `extern C functions cannot have a body` |
| E-FFI-004 | parser | `extern C functions cannot be generic` |
| E-FFI-005 | typecheck | `type '{T}' is not FFI-safe in extern C signature` |
| E-FFI-006 | typecheck | `call to extern C function requires unsafe block` |
| E-FFI-007 | linker | `unresolved extern symbol '{sym}'; did you forget --link-lib?` |

E-FFI-007 is a linker error; the compiler can't detect it, but the diagnostic
message can be improved by catching clang's `undefined reference` errors and
mapping them to a more helpful message pointing at `--link-lib`.

## 3. Test Plan

### Positive e2e tests

| Case | What it validates |
|------|-------------------|
| `ffi_c_basic_int` | extern "C" fn returning Int, call in unsafe block, link against test .c file |
| `ffi_c_void_return` | extern "C" fn returning Void |
| `ffi_c_rawptr_roundtrip` | Pass RawPtr<Byte> to C, get RawPtr<Byte> back |
| `ffi_c_multi_param` | Multiple scalar params (Int, UInt, Byte, Float) |
| `ffi_c_bool_param` | Bool ABI (i1 vs i8 at boundary) |
| `ffi_c_block_syntax` | extern "C" { ... } block with multiple declarations |
| `ffi_c_link_lib` | --link-lib flag, link against system libm (call `sqrt`) |
| `ffi_c_link_search` | --link-search flag with custom library path |
| `ffi_c_export_reexport` | extern "C" fn re-exported via module export block |
| `ffi_c_wrapper_pattern` | Safe Drift wrapper around unsafe extern C calls |

Test infrastructure: Each positive test includes a small `.c` file compiled to `.o`
alongside the `.drift` source. The test runner compiles both and links them. For
`ffi_c_link_lib`, use system `libm` (`sqrt`, `floor`) to avoid test-specific C code.

### Negative diagnostic tests

| Case | Expected diagnostic |
|------|---------------------|
| `ffi_c_string_param_rejected` | "String is not FFI-safe" |
| `ffi_c_array_param_rejected` | "Array is not FFI-safe" |
| `ffi_c_throws_rejected` | "extern C functions must be declared nothrow" |
| `ffi_c_generic_rejected` | "extern C functions cannot be generic" |
| `ffi_c_body_rejected` | "extern C functions cannot have a body" |
| `ffi_c_unsafe_required` | "call to extern C function requires unsafe block" |
| `ffi_c_struct_byval_rejected` | "struct-by-value is not supported in extern C" |
| `ffi_c_bad_abi_string` | `extern "Java" fn ...` → "extern block ABI must be C" |

### CLI/linker flag tests

| Case | What it validates |
|------|-------------------|
| `ffi_link_lib_missing_rejected` | --link-lib omitted, extern symbol unresolved |
| `ffi_link_search_precedence` | --link-search overrides system paths |

## 4. Versioning / ABI Impact

**Compiler version bump: required.** New syntax (`extern "C"`) and new checker
rules are behavior-changing additions.

**ABI bump: not required.** This feature adds new surface syntax and codegen
capability but does not change:
- The runtime/compiler boundary (no new runtime C functions needed)
- The package format (extern declarations are module-local metadata)
- TypeId encoding (no new TypeKind — `RawPtr` already exists)
- Calling convention for Drift-to-Drift calls

The `extern "C"` declarations are module-internal; they produce `declare` in LLVM IR
and resolve at link time. No package boundary change, no ABI boundary change.

If a future phase adds struct-by-value or callback FFI that requires new TypeKind
entries or package metadata, that would require an ABI bump at that time.

## 5. Execution Order

| Step | Phase | Estimated scope | Depends on |
|------|-------|----------------|------------|
| 1 | Parser + AST | grammar.lark, ast.py, parser/__init__.py | — |
| 2 | Checker | checker/__init__.py, call_resolver.py | Step 1 |
| 3 | Stage1 + Stage2 | ast_to_hir.py, hir_to_mir.py | Step 2 |
| 4 | LLVM codegen | llvm_codegen.py | Step 3 |
| 5 | Linker CLI | driftc.py (argparse + link command) | Step 4 |
| 6 | Negative tests | e2e diagnostic cases | Step 2 |
| 7 | Positive tests | e2e compile+run cases with C code | Step 5 |
| 8 | Version bump | driftc_versions.py | Step 7 |

Steps 1-5 form the critical path. Steps 6-7 can partially overlap (negative
tests can land after Step 2, positive tests require Step 5).

## 6. Known Risks and Unresolved Design Points

### 6.1 Bool ABI mismatch
Drift `Bool` is `i1` in registers but C `_Bool` is `i8` at the ABI boundary on
most platforms. The codegen must ensure `zext i1 to i8` when passing Bool to
extern C and `trunc i8 to i1` when receiving. This is a small but critical detail
in Phase 4.

### 6.2 RawPtr nullability
`RawPtr<T>` can be null. Drift does not have a `Nullable<T>` wrapper for pointers.
Users must check for null manually in unsafe blocks. This is acceptable for MVP
but should be documented prominently.

### 6.3 String marshaling (post-MVP)
The most-requested follow-on will be passing Drift `String` to C (`const char*`).
This requires:
- Null-termination (Drift strings are length-prefixed, not null-terminated)
- Lifetime pinning (the C side must not hold the pointer past the call)
- A helper like `String.as_c_str() -> RawPtr<Byte>` that returns a temporary
  null-terminated copy or pins the existing buffer

This is explicitly post-MVP but should be the first follow-on.

### 6.4 Struct layout compatibility (post-MVP)
C struct layout depends on platform ABI (alignment, padding). Drift struct layout
is compiler-defined and may not match. Supporting struct-by-value FFI requires
either:
- A `#[repr(C)]` annotation that forces C-compatible layout
- Automatic layout matching using target data layout info

### 6.5 Callback FFI (post-MVP)
Passing Drift closures/function pointers to C (e.g., `qsort` comparator, signal
handlers) requires generating C-callable thunks. This is a significant feature
beyond MVP scope.

### 6.6 Thread safety
Extern C functions may not be thread-safe. Drift's concurrency model (fibers on
a thread pool) means multiple fibers could call the same extern function
concurrently. This is the user's responsibility to manage (via mutexes or
ensuring the C library is thread-safe). No compiler enforcement in MVP.

### 6.7 Platform-specific link libraries
Some C libraries have different names on different platforms (e.g., `ws2_32` on
Windows). MVP targets Linux only (matching current Drift platform support).
Cross-platform link-lib configuration is post-MVP.

## 7. MVP / Non-MVP Boundary

| Capability | MVP | Post-MVP |
|-----------|-----|----------|
| `extern "C" fn` scalar signatures | Yes | |
| `RawPtr<T>` params and returns | Yes | |
| `Void` return | Yes | |
| `--link-lib` / `--link-search` CLI | Yes | |
| `unsafe` requirement at call site | Yes | |
| Diagnostic suite (type rejection, missing unsafe) | Yes | |
| `String` marshaling | | Phase 2 |
| Struct by-value | | Phase 2 |
| Variadic functions | | Phase 3 |
| Callback / reverse FFI | | Phase 3 |
| `#[repr(C)]` struct layout | | Phase 2 |
| driftc.json `link_libs` config | | Phase 2 |
| `--link-lib-static` | | Phase 2 |

## 8. Smallest End-to-End Slice for libssl/libcrypto

With the MVP, a user can write:

```drift
module tls;

extern "C" {
    fn OPENSSL_init_ssl(opts: Uint64, settings: RawPtr<Byte>) nothrow -> Int;
    fn TLS_client_method() nothrow -> RawPtr<Byte>;
    fn SSL_CTX_new(method: RawPtr<Byte>) nothrow -> RawPtr<Byte>;
    fn SSL_CTX_free(ctx: RawPtr<Byte>) nothrow -> Void;
    fn SSL_new(ctx: RawPtr<Byte>) nothrow -> RawPtr<Byte>;
    fn SSL_free(ssl: RawPtr<Byte>) nothrow -> Void;
    fn SSL_set_fd(ssl: RawPtr<Byte>, fd: Int) nothrow -> Int;
    fn SSL_connect(ssl: RawPtr<Byte>) nothrow -> Int;
    fn SSL_read(ssl: RawPtr<Byte>, buf: RawPtr<Byte>, num: Int) nothrow -> Int;
    fn SSL_write(ssl: RawPtr<Byte>, buf: RawPtr<Byte>, num: Int) nothrow -> Int;
    fn SSL_get_error(ssl: RawPtr<Byte>, ret: Int) nothrow -> Int;
    fn ERR_print_errors_fp(fp: RawPtr<Byte>) nothrow -> Void;
}
```

Build: `driftc main.drift --link-lib ssl --link-lib crypto -o tls_app`

All OpenSSL functions use opaque pointer handles and integer return codes —
fully within the MVP type subset. Buffer operations use `RawPtr<Byte>` for
`void*` / `char*` parameters.
