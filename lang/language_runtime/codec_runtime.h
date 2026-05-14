#ifndef DRIFT_CODEC_RUNTIME_H
#define DRIFT_CODEC_RUNTIME_H

#include <stddef.h>
#include <stdint.h>

/* Shared return codes for std.codec native shims.
 *
 * Both the gzip shim (codec_gzip_runtime.c) and any future codec shim
 * (e.g. zstd) return one of these. Each Drift-side wrapper maps these
 * to a CodecError tag with a codec-specific prefix.
 *
 * Internal failures inside a codec (memory allocation failure,
 * unrecoverable library-state errors) are *not* surfaced through this
 * enum — the shim calls abort() so the caller never sees a corrupted
 * stream. This matches the convention in string_runtime.c /
 * array_runtime.c. The fallible-input path is what these codes are for.
 */
#define DRIFT_CODEC_OK                  0
#define DRIFT_CODEC_TRUNCATED           1
#define DRIFT_CODEC_BAD_DATA            2
#define DRIFT_CODEC_OUTPUT_TOO_LARGE    3
#define DRIFT_CODEC_INVALID_LEVEL       4

/* Free a buffer returned by a codec shim's encode/decode.
 *
 * Codec-agnostic: every shim mallocs its output buffer with plain
 * malloc(); Drift wrappers copy into an Array<Byte> and free via this
 * function. Safe to call with p == NULL.
 */
void drift_codec_free(uint8_t *p);

/* Bulk memcpy from `src` into `dst`. Codec-agnostic helper that lets
 * the Drift wrapper transfer the shim's malloc'd output into a Drift
 * Array<Byte> without a per-byte loop.
 *
 * Safe to call with `len == 0` (no-op).
 */
void drift_codec_copy_bytes(uint8_t *dst, const uint8_t *src, size_t len);

/* gzip (RFC 1952) encode/decode via zlib. See codec_gzip_runtime.c. */

/* Encode `in_len` bytes from `in` as a gzip stream.
 *
 * `level` is the zlib compression level:
 *   -1          zlib default (currently maps to 6, but treat as opaque)
 *    0          no compression (valid gzip; deflate "stored" blocks)
 *    1..9       zlib levels (1 = fastest, 9 = best ratio)
 *   any other   returns DRIFT_CODEC_INVALID_LEVEL without allocating.
 *
 * On DRIFT_CODEC_OK:
 *   *out points to a malloc'd buffer of exactly *out_len bytes that
 *   the caller must free with drift_codec_free().
 *
 * On any non-OK return:
 *   *out is set to NULL and *out_len to 0.
 *
 * Aborts the process on Z_MEM_ERROR or Z_STREAM_ERROR (internal
 * failure with no input-level explanation — matches runtime
 * convention for invariant violations).
 */
int32_t drift_codec_gzip_encode(
	const uint8_t *in, size_t in_len,
	int32_t level,
	uint8_t **out, size_t *out_len);

/* Decode a strict gzip stream (RFC 1952 only; not zlib auto-detect).
 *
 * Output is capped at `max_out` bytes — exceeding the cap returns
 * DRIFT_CODEC_OUTPUT_TOO_LARGE. The cap protects callers from
 * decompression bombs without forcing them to think about it.
 *
 * On DRIFT_CODEC_OK:
 *   *out points to a malloc'd buffer of exactly *out_len bytes that
 *   the caller must free with drift_codec_free().
 *
 * On DRIFT_CODEC_TRUNCATED:
 *   stream ended mid-frame (incomplete header / data / trailer).
 *
 * On DRIFT_CODEC_BAD_DATA:
 *   bad magic / unknown method / bad block / CRC mismatch.
 *
 * On DRIFT_CODEC_OUTPUT_TOO_LARGE:
 *   decompressed output would exceed max_out.
 *
 * On any non-OK return, *out is set to NULL and *out_len to 0.
 *
 * Aborts on Z_MEM_ERROR or Z_STREAM_ERROR (internal failure).
 */
int32_t drift_codec_gzip_decode(
	const uint8_t *in, size_t in_len,
	size_t max_out,
	uint8_t **out, size_t *out_len);

#endif /* DRIFT_CODEC_RUNTIME_H */
