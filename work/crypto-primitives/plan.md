# Plan: Core Crypto Primitives Batch

## 0) Goal
Deliver a first usable cryptographic primitives set for higher-level libraries (TLS, auth, secure protocols) in one coordinated batch:
- `sha256`
- `sha384`
- `sha512`
- `hmac_sha256`
- `hkdf`
- one AEAD primitive: prefer `chacha20_poly1305`; only choose `aes_gcm` instead with explicit justification

This batch is intended to establish a solid low-level stdlib crypto base, not a full TLS implementation.

## 1) Scope and sequencing
Although the delivery may land as one batch, implementation should follow dependency order:
1. SHA-256
2. SHA-384 / SHA-512
3. HMAC-SHA256
4. HKDF (on HMAC-SHA256)
5. AEAD (`chacha20_poly1305` preferred)

Rationale:
- HMAC depends on SHA-256
- HKDF depends on HMAC-SHA256
- AEAD is independent of HKDF but larger and riskier; keep it last inside the batch

## 2) Proposed stdlib API surface
Final naming can be adjusted to existing stdlib conventions, but the public surface should be explicit and byte-oriented.

### 2.1 Hashing
```drift
pub fn sha256(data: &Array<Byte>) nothrow -> Array<Byte>
pub fn sha384(data: &Array<Byte>) nothrow -> Array<Byte>
pub fn sha512(data: &Array<Byte>) nothrow -> Array<Byte>
```

Expected output lengths:
- SHA-256 -> 32 bytes
- SHA-384 -> 48 bytes
- SHA-512 -> 64 bytes

### 2.2 HMAC
```drift
pub fn hmac_sha256(key: &Array<Byte>, data: &Array<Byte>) nothrow -> Array<Byte>
```

Expected output length:
- 32 bytes

### 2.3 HKDF
Minimum useful surface:
```drift
pub fn hkdf_extract_sha256(salt: &Array<Byte>, ikm: &Array<Byte>) nothrow -> Array<Byte>
pub fn hkdf_expand_sha256(prk: &Array<Byte>, info: &Array<Byte>, len: Int) nothrow -> core.Result<Array<Byte>, CryptoError>
pub fn hkdf_sha256(salt: &Array<Byte>, ikm: &Array<Byte>, info: &Array<Byte>, len: Int) nothrow -> core.Result<Array<Byte>, CryptoError>
```

### 2.4 AEAD
Preferred v1 surface:
```drift
pub fn chacha20_poly1305_encrypt(
    key: &Array<Byte>,
    nonce: &Array<Byte>,
    aad: &Array<Byte>,
    plaintext: &Array<Byte>
) nothrow -> core.Result<Array<Byte>, CryptoError>

pub fn chacha20_poly1305_decrypt(
    key: &Array<Byte>,
    nonce: &Array<Byte>,
    aad: &Array<Byte>,
    ciphertext_and_tag: &Array<Byte>
) nothrow -> core.Result<Array<Byte>, CryptoError>
```

If AES-GCM is chosen instead, document why before implementation starts.

## 3) Error model
Add a shared error type for crypto APIs that can fail due to invalid input sizes or verification failure.

```drift
pub struct CryptoError {
    pub tag: String,
    pub errno: Int
}
```

Notes:
- `errno` may remain `0` for pure-Drift validation failures
- tag values should be stable and low-cardinality
- likely tags:
  - `invalid-key-length`
  - `invalid-nonce-length`
  - `invalid-output-length`
  - `auth-failed`

Hash functions themselves should remain infallible / `nothrow` and return fixed-length outputs directly.

## 4) Implementation guidance
### 4.1 Purity / runtime boundary
- Prefer pure Drift where practical for determinism and portability of semantics.
- If any primitive requires runtime/native support, isolate that decision and justify it explicitly before landing.
- Do not add compiler magic for crypto.

### 4.2 Byte conventions
- All APIs operate on `Array<Byte>`.
- No string APIs in this batch.
- Do not silently reinterpret text encodings; callers provide bytes.

### 4.3 Constant-time concerns
- Comparisons used for MAC/tag verification must be constant-time.
- If constant-time compare is missing in stdlib, either:
  1. add it inside this batch, or
  2. implement it privately in the crypto module and mark as candidate for stdlib promotion

### 4.4 Output ownership
- Returned byte arrays should be fresh owned arrays.
- No aliasing of caller buffers.

## 5) Test plan
This batch needs strong known-answer coverage and misuse coverage.

### 5.1 SHA-256
Add e2e suite(s) with standard test vectors covering:
- empty input
- `"abc"`
- longer multi-block input
- output length = 32

Suggested suite:
- `std_crypto_sha256_vectors`

### 5.2 SHA-384 / SHA-512
Add vector coverage for both:
- empty input
- `"abc"`
- longer multi-block input
- output lengths = 48 / 64

Suggested suite:
- `std_crypto_sha2_family_vectors`

### 5.3 HMAC-SHA256
Add known-answer vectors covering:
- normal key/data
- key shorter than block size
- key longer than block size
- empty data
- output length = 32

Suggested suite:
- `std_crypto_hmac_sha256_vectors`

### 5.4 HKDF-SHA256
Add vectors covering:
- extract only
- expand only
- full extract+expand
- empty salt/info cases
- invalid output length handling (if bounded by API)

Suggested suite:
- `std_crypto_hkdf_sha256_vectors`
- `std_crypto_hkdf_sha256_invalid`

### 5.5 AEAD
Add vectors covering:
- encrypt/decrypt happy path
- empty plaintext
- non-empty AAD
- tag verification failure on modified ciphertext
- invalid key length
- invalid nonce length

Suggested suite:
- `std_crypto_chacha20_poly1305_vectors`
- `std_crypto_chacha20_poly1305_invalid`

### 5.6 Cross-cutting tests
Add or include:
- deterministic output repeatability for same inputs
- non-mutation of input arrays
- exact output lengths
- failure tags are stable and correct

## 6) Review gates
Before closing the batch:
1. verify actual vector provenance (record source in comments or plan notes)
2. verify no test is only checking length without content, except where length is the intended property
3. verify constant-time compare is used on auth/tag verification paths
4. run targeted ASAN/MEMCHECK if any runtime/native support is introduced

## 7) Out of scope
- TLS protocol
- X.509 / ASN.1 / DER
- PEM parsing
- keypair crypto / signatures
- backreferences to OpenSSL or external crypto engines unless explicitly approved
- streaming/incremental hash APIs (one-shot only in this batch)

## 8) Completion criteria
- all listed primitives implemented
- vector suites added and passing
- invalid-input suites added and passing
- review confirms semantics, test quality, and API stability
- only after review: update history and bump versions if required by the landed scope
