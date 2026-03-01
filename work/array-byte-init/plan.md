# Plan: Byte Array Uninitialized Allocation + Mutable Pointer Access (v1)

## 0) Goal
Provide a reusable, non-random-specific way for stdlib and user libraries to allocate `Array<Byte>` storage and initialize it through a raw pointer, without exposing a one-off magic path for secure random bytes.

This is intentionally byte-only in v1.

## 1) Why this exists
Current choices are poor:
- build `Array<Byte>` via `push` one byte at a time after a C/runtime fill into separate memory
- allocate an array and then overwrite backing storage without a sanctioned pointer/init contract

We need a generic pattern that user libraries (TLS, codecs, DB clients, parsers) can reuse.

## 2) v1 API shape (byte-only)
Proposed low-level APIs (likely in `std.mem` or another low-level stdlib module):

```drift
@intrinsic pub unsafe fn array_byte_alloc_uninit(n: Int) nothrow -> Array<Byte>
@intrinsic pub unsafe fn array_byte_as_mut_ptr(a: &mut Array<Byte>) nothrow -> Ptr<Byte>
```

Optional helper (not required in the same patch):
```drift
pub unsafe fn array_byte_fill_from_ptr(
    n: Int,
    init: core.Callback2<Ptr<Byte>, Int, Int>
) nothrow -> core.Result<Array<Byte>, Int>
```

But v1 should start with just the two primitives.

## 3) Semantics / contract
### 3.1 `array_byte_alloc_uninit(n)`
- `n < 0` is rejected (decide: diagnostic vs runtime Result wrapper in higher layer)
- returns an `Array<Byte>` with:
  - logical length = `n`
  - backing storage allocated for `n` bytes
  - contents uninitialized
- caller must treat bytes as unreadable until initialized

### 3.2 `array_byte_as_mut_ptr(a)`
- returns a writable `Ptr<Byte>` to the array backing storage
- valid only while the array remains alive and not moved/reallocated
- pointer is for initialization/mutation, not for changing array length/capacity metadata

### 3.3 Safety boundary
These APIs must be `unsafe` because they can create uninitialized-byte reads if misused.

## 4) Intended usage pattern
Example target pattern for libraries:

```drift
var bytes = unsafe mem.array_byte_alloc_uninit(n);
val ptr = unsafe mem.array_byte_as_mut_ptr(&mut bytes);
val rc = some_runtime_fill(ptr, n);
if rc != 0 {
    // decide failure contract carefully; see §5
}
return move bytes;
```

This is the intended building block for:
- secure random bytes
- future TLS/crypto buffers
- network/parser libraries that receive bytes from runtime/C calls

## 5) Failure semantics (important)
If the external fill operation fails after allocation:
- the partially/uninitialized `Array<Byte>` must not be exposed to safe callers
- the wrapper should either:
  1. drop the array immediately and return `Err(...)`, or
  2. fill/zero before returning an error if required by higher-level policy

Recommendation for v1 wrappers:
- allocate
- call runtime fill
- on failure: drop local array and return `Err`
- never surface the array on error

## 6) Implementation strategy
### 6.1 Runtime / ABI
- Reuse the existing array runtime allocator/layout used by `Array<Byte>`
- Do not create a special-case random helper
- Compiler/runtime should already understand `Array<Byte>` ABI representation; these intrinsics just expose allocation + pointer access in a sanctioned form

### 6.2 Scope discipline
- Byte-only in v1
- No generic `array_alloc_uninit<T>` yet
- No public array metadata mutation primitives

## 7) Regression-first plan
### 7.1 Positive e2e
Add `lang/tests/codegen/e2e/array_byte_alloc_uninit_basic/` covering:
- allocate 0 bytes
- allocate N bytes, write through returned ptr, then read back via array indexing
- move array after fill, verify contents preserved

### 7.2 Negative / safety tests
Add `lang/tests/codegen/e2e/array_byte_alloc_uninit_misuse_rejected/` if checker restrictions are involved, otherwise add targeted driver/unit tests for intrinsic typing.

### 7.3 Consumer proof
Use one real consumer after primitives land:
- `secure_random_bytes` is the first intended user
- prove the pattern works without extra magic/intrinsics

## 8) Hardening
- ASAN: no invalid writes/reads from sanctioned usage
- MEMCHECK: no leaks in happy path or error path wrappers
- verify no uninitialized-byte reads in the positive path

## 9) Out of scope
- Generic `T` version
- safe wrapper combinator
- array view/slice types
- mutable pointer exposure for non-byte arrays
- length/capacity mutation APIs

## 10) Completion criteria
- `array_byte_alloc_uninit` and `array_byte_as_mut_ptr` implemented
- positive regression suite passes
- first consumer (secure random bytes) can be built on top without extra special magic
- ASAN/MEMCHECK clean
- review clears semantics and safety contract before history update
