# Plan: Secure Random Bytes API (Linux-first)

## 0) Goal
Add a secure-random-bytes API to Drift stdlib for cryptographic consumers (TLS, auth, tokens, nonces, keys), using Linux kernel entropy via the C runtime and returning structured errors instead of aborting.

## 1) API decision
### 1.1 Primary public API
```drift
pub fn random_secure_bytes(n: Int) -> core.Result<Array<Byte>, RandomError>
```

Rationale:
- Best fit for Drift’s move semantics.
- Simpler and more natural for callers than caller-managed fill buffers.
- Matches common crypto/TLS usage patterns (nonce/IV/token/key generation).

### 1.2 Secondary API
Not in scope for v1:
```drift
pub fn random_secure_fill(buf: &mut Array<Byte>) -> core.Result<Void, RandomError>
```
Can be added later if buffer reuse/perf becomes important.

## 2) Error model
### 2.1 Public error type
```drift
pub struct RandomError {
    pub tag: String,
    pub errno: Int
}
```

### 2.2 Semantics
- Failures are returned as `Err(RandomError)`.
- The runtime must not abort the process on OS entropy failure.
- Caller/framework decides whether to fail closed, retry, or terminate.

### 2.3 Initial tags
- `invalid-length`
- `os-random-failed`
- `short-read` (only if partial-fill path is surfaced as distinct failure)

## 3) Platform scope
### 3.1 v1 target
- Linux only

### 3.2 Underlying runtime dependency
- `getrandom(2)` via C runtime

### 3.3 Runtime behavior
- Call `getrandom()` in a loop until requested bytes are filled or a real error occurs.
- Retry on `EINTR`.
- Return `Err(RandomError(...))` on failure.
- No `/dev/urandom` fallback in v1 unless explicitly approved later.

## 4) Runtime/stdlib boundary
### 4.1 Expected runtime helper
C runtime helper, shape indicative:
```c
int drift_random_secure_bytes(uint8_t *buf, size_t len, int *out_errno);
```

Behavior:
- return `0` on success
- return nonzero on failure and set `*out_errno`

### 4.2 Stdlib wrapper
- Allocate `Array<Byte>` of length `n`
- Ask runtime to fill it
- Return `Ok(bytes)` or `Err(RandomError(...))`

## 5) Validation rules
- `n < 0` -> `Err(RandomError(tag = "invalid-length", errno = 0))`
- `n == 0` -> `Ok([])`
- Large sizes: either succeed or return `os-random-failed`; no truncation

## 6) Regression-first plan
### 6.1 Positive e2e
Add `lang/tests/codegen/e2e/std_random_secure_bytes_basic/` covering:
- `n = 0` returns empty array
- `n = 1`, `16`, `32` return arrays of exact requested length
- two consecutive 32-byte calls are not byte-identical in a trivial smoke sense (best-effort sanity check, not a proof)

### 6.2 Negative e2e
Add `lang/tests/codegen/e2e/std_random_secure_bytes_invalid_len/` covering:
- negative length returns `invalid-length`

### 6.3 Driver/runtime test
Add a focused runtime-facing test if needed for errno mapping / helper contract.

## 7) Hardening requirements
- Run new random suites under normal mode.
- Run under ASAN.
- Memcheck should be clean.
- Verify no uninitialized-byte issues in returned arrays.

## 8) Security posture
- This API is intended for cryptographic randomness only.
- No custom PRNG or time-based seeding in stdlib.
- Entropy source is OS-managed.
- Failure is operationally severe, but still returned as an error rather than process abort.

## 9) Out of scope
- Non-Linux implementations
- Deterministic PRNG APIs
- fill-into-buffer API
- secure key storage / zeroization guarantees
- `/dev/urandom` fallback unless explicitly approved

## 10) Completion criteria
- Public API implemented in stdlib.
- Linux runtime helper implemented and wired.
- Positive/negative regressions pass.
- ASAN/MEMCHECK clean.
- Review clears implementation and semantics.
- Only after review clears: update `docs/history.md` and close the chapter.
