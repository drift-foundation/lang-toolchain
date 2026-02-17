# std.codec proposal (MVP)

## Goal
Add a dedicated `std.codec` module for generic byte/text encoding utilities, while keeping Unicode/text semantics in `std.text`.

## Why
- `std.text` should remain focused on string/UTF-8/tokenization semantics.
- Hex/Base32/Base64 are general codecs and fit better in a separate module.
- A dedicated module gives us a clean expansion point for future codecs.

## Module boundary
- Keep in `std.text`:
  - `utf8_from_bytes`
  - `utf8_from_bytes_range`
  - tokenizer/string-oriented helpers
- Add in `std.codec`:
  - base-N codec families and related byte/text transforms.

## MVP surface
- New module file:
  - `stdlib/std/codec/codec.drift`
- Initial API:
  - `pub fn hex_encode(bytes: &Array<Byte>) nothrow -> String`
  - `pub fn hex_decode(s: &String) nothrow -> core.Result<Array<Byte>, CodecError>`
  - `pub fn base64_encode(bytes: &Array<Byte>) nothrow -> String`
  - `pub fn base64_decode(s: &String) nothrow -> core.Result<Array<Byte>, CodecError>`
- Strict defaults for decode APIs:
  - `hex_decode(...)` and `base64_decode(...)` are strict by default.
  - strict means no whitespace/separators/prefixes unless explicitly enabled via decoder config.
- Configurable permissive path (builder-based, explicit opt-in):
  - `hex_decoder().allow_whitespace(flag: Bool).allow_prefix_0x(flag: Bool).decode(s)`
  - `base64_decoder().allow_whitespace(flag: Bool).allow_url_safe(flag: Bool).decode(s)`
  - no implicit permissive mode in default decode functions.
- Error type:
  - `pub struct CodecError { pub tag: String, pub offset: Int }`
  - tags should be deterministic and stable for tests/docs.

## Strict decode contracts (pinned)
- `hex_decode`:
  - even length required,
  - allowed chars only `[0-9a-fA-F]`,
  - no whitespace/separators/`0x` prefix in strict mode,
  - tags:
    - `hex-odd-length` (`offset = s.byte_length()`),
    - `hex-invalid-char` (`offset = offending index`).
- `base64_decode`:
  - RFC4648 standard alphabet only (`A-Z a-z 0-9 + /`) plus `=`,
  - input length multiple of 4,
  - padding only in final quantum (`=` placement validated),
  - no whitespace/line-wraps/url-safe alphabet in strict mode,
  - tags:
    - `base64-invalid-length`,
    - `base64-invalid-char`,
    - `base64-invalid-padding`,
    - `base64-trailing-data`,
  - `base64-invalid-length` uses `offset = s.byte_length()`,
  - `offset = first offending index`.

## Delivery order
1. Hex encode/decode ✅
2. Base64 encode/decode ✅
3. Base32 encode/decode ✅

## Testing expectations
- Driver + e2e regressions per codec:
  - valid round-trips,
  - invalid alphabet,
  - invalid length/padding,
  - deterministic `tag` + `offset`.
- Run new e2e codec subset under:
  - normal mode,
  - `DRIFT_ASAN=1`,
  - `DRIFT_ALLOC_TRACK=1`.
  - memcheck (`DRIFT_MEMCHECK=1`).

## Implemented in this pass
- Added `std.codec` module at `stdlib/std/codec/codec.drift`.
- Added:
  - `hex_encode`
  - `hex_decode` (strict default)
  - `hex_decoder` + `HexDecoder` builder (`allow_whitespace`, `allow_prefix_0x`, `decode`)
  - `base64_encode`
  - `base64_decode` (strict default)
  - `base64_decoder` + `Base64Decoder` builder (`allow_whitespace`, `allow_url_safe`, `decode`)
  - `base32_encode`
  - `base32_decode` (strict default)
  - `base32_decoder` + `Base32Decoder` builder (`allow_whitespace`, `allow_lowercase`, `decode`)
  - `CodecError { tag, offset }` + `core.Diagnostic` impl.
- Added e2e regression coverage:
  - `lang/tests/codegen/e2e/std_codec_hex_base64_strict`
  - `lang/tests/codegen/e2e/std_codec_decoder_builder_permissive`
  - `lang/tests/codegen/e2e/std_codec_hex_fixture_source_style` (recommended source-fixture hex pattern)
- Validation completed:
  - normal mode: pass
  - `DRIFT_ASAN=1`: pass
  - `DRIFT_ALLOC_TRACK=1`: pass
  - `DRIFT_MEMCHECK=1`: pass
