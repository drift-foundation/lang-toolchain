# stdlib crypt+codec MVP plan (for JWT foundation)

## Goal
Add minimal, security-focused stdlib primitives required to build JWT in user-space libraries:
- SHA-256 digest
- HMAC-SHA256
- Base64url encode/decode (strict)
- Constant-time byte comparison helper

JWT token construction/validation remains out of stdlib and belongs in user-space web/auth libraries.

## Why now
We already have SHA-1 for MariaDB auth, but that is insufficient for modern signing use-cases.
A minimal REST/auth stack needs HS256-class primitives before framework work starts.

## Scope (MVP)

### 1) std.crypto
- `pub fn sha256(data: &Array<Byte>) nothrow -> Array<Byte>`
  - Pure Drift implementation (no FFI/intrinsics), consistent with existing SHA-1.
  - Deterministic 32-byte output.
- `pub fn hmac_sha256(key: &Array<Byte>, msg: &Array<Byte>) nothrow -> Array<Byte>`
  - Pure Drift implementation, built on top of sha256.
  - Deterministic 32-byte output.
  - Key handling per RFC 2104:
    1. If `key.len > 64`, use `sha256(key)` as effective key.
    2. Zero-pad effective key to 64 bytes (SHA-256 block size).
    3. Apply standard HMAC ipad/opad flow.
- `pub fn constant_time_eq(a: &Array<Byte>, b: &Array<Byte>) nothrow -> Bool`
  - Length mismatch returns `false`.
  - No data-dependent early return.
  - Lives in std.crypto (security semantics, not generic equality).

### 2) std.codec (existing module)
Public API added to existing `std.codec` surface. Reuse current base64 machinery (alphabet + strict decode policy) to avoid duplicate logic. Implementation can live in a separate internal file (URL-safe alphabet/config path) and be re-exported.
- `pub fn base64url_encode(bytes: &Array<Byte>) nothrow -> String`
  - RFC4648 URL-safe alphabet (`A-Z a-z 0-9 - _`), no padding.
- `pub fn base64url_decode(s: &String) nothrow -> core.Result<Array<Byte>, DecodeError>`
  - Reuse existing `std.codec.DecodeError` — no new error type.
  - Strict by default.
  - Reject non-URL-safe chars.
  - Reject `=` padding chars (URL-safe variant is unpadded-only).
  - Reject invalid unpadded lengths: `len % 4 == 1` is malformed (no valid byte mapping).
  - Return deterministic error tag + offset.

## Non-goals (for this phase)
- Full JWT implementation in stdlib.
- JWK/JWKS, RSA/ECDSA/EdDSA.
- Permissive decoding modes.
- Streaming hash/HMAC APIs.

## Security requirements
- No SHA-1 usage for new signing features.
- Use constant-time signature compare in all verification consumers.
- Strict decoding defaults only.
- Keep APIs explicit and small to reduce misuse surface.

## API notes
- Prefer pure byte-oriented crypto interfaces.
- Keep error taxonomy simple and deterministic for codec decode.
- Keep behavior stable and documented so user-space JWT code can rely on exact semantics.

## Test plan

### constant_time_eq
- Equal/unequal/length mismatch correctness.
- Basic structural check that loop runs full length (no early return behavior in implementation).

### SHA-256
- Known vectors (empty input, short ASCII, long multi-block input).
- Repeated-run determinism test.

### HMAC-SHA256
- RFC test vectors.
- Key lengths: empty, short, block-size (64), > block-size (triggers sha256 key reduction).

### Base64url
- Round-trip tests for binary payloads.
- Known vectors with and without trailing bytes.
- Strict-negative matrix:
  - invalid char (non-URL-safe alphabet)
  - `=` padding present (rejected — unpadded-only contract)
  - `len % 4 == 1` (no valid byte mapping)
  - deterministic error tag + offset in all reject cases

### Tooling modes
- Run under normal, ASAN, and memcheck for codec/crypto e2e regressions.

## Delivery phases

### Phase 1: constant-time compare
- Implement helper + correctness tests.
- Lowest risk, no dependencies, early availability.

### Phase 2: SHA-256
- Implement + vectors + ASAN/memcheck sanity.

### Phase 3: HMAC-SHA256
- Implement on top of SHA-256 + vectors.

### Phase 4: Base64url strict
- Implement encoder/decoder in existing std.codec + strict error coverage.

### Phase 5: integration docs
- Document intended JWT layering:
  - stdlib primitives only
  - JWT/auth policy in user-space package

## Proposed consumer direction (next step, not in this phase)
Create user-space package (e.g. `packages/web-auth-jwt`) with strict defaults:
- HS256 only
- `alg` allowlist
- claim validation (`exp`, `nbf`, `iat`) with explicit clock-skew option
- verify using original token segments (no JSON reserialization before signature check)
