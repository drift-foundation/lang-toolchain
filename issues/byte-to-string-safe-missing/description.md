Summary: Missing safe user-land bytes -> UTF-8 string API (MVP blocker)

Classification
- Feature request / capability gap
- Priority: MVP showstopper

Problem
- User-land packages need to decode UTF-8 text from protocol byte payloads.
- Current public path relies on pointer/len intrinsic shape (`std.core.string_from_utf8_bytes(ptr, len)`), which is not a safe/ergonomic user-land API for common `Array<Byte>` workflows.
- Unsafe/rawbuffer routes are correctly restricted by policy, but there is no first-class safe replacement API for package authors.

Why this is blocking
- Protocol clients (MariaDB/MySQL, HTTP, etc.) routinely convert length-prefixed bytes to text.
- Without a safe standard API, package authors are pushed toward restricted internals or awkward pointer plumbing.
- This blocks practical MVP user-land library development.

Current behavior
- Unsafe usage requires `--allow-unsafe`.
- Rawbuffer intrinsics are restricted to trusted/toolchain modules.
- `std.core.string_from_utf8_bytes(ptr, len)` exists, but no safe `Array<Byte>`-first API with structured error semantics.

Expected capability (MVP)
- Provide a safe stdlib API usable in normal user-land mode, no unsafe required in user code.
- Proposed API shape:
  - `std.text.utf8_from_bytes(bytes: &Array<Byte>) -> Result<String, Utf8Error>`
  - Optional follow-up: slice variant with offset/length.

Required properties
1. No unsafe in user code.
2. Deterministic UTF-8 validation behavior.
3. Structured error on invalid input (`Utf8Error` with stable tag contract).
4. Works in standard user-land compilation (no trusted-module privileges needed by callers).

Acceptance criteria
1. User-land package can decode protocol bytes to `String` using only public std APIs.
2. Invalid UTF-8 returns `Result::Err(Utf8Error(...))` with machine-processable tag.
3. No `--allow-unsafe` requirement for callers.
4. Driver/e2e coverage:
   - valid UTF-8 byte array -> `Ok(String)`
   - invalid UTF-8 -> `Err(Utf8Error(tag=...))`
   - empty bytes -> `Ok("")`
   - mixed multibyte sequences coverage

Notes
- This is not a request to relax unsafe/trusted-module policy.
- The fix is to add the safe public API surface on top of trusted internals.
