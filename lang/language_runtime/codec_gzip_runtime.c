/* gzip codec shim — calls libz's deflate/inflate streaming API behind a
 * tiny stable C surface (see codec_runtime.h).
 *
 * Design contract:
 *   - Caller passes pointer + length; shim allocates output with malloc;
 *     caller frees via drift_codec_free.
 *   - z_stream is never visible to Drift; the shim owns its lifetime
 *     and ensures deflateEnd/inflateEnd run on every path.
 *   - Strict gzip wrapper on both sides (wbits = 31). No zlib auto-detect.
 *   - Internal failure (Z_MEM_ERROR, Z_STREAM_ERROR) aborts the process,
 *     matching the runtime convention for invariant violations
 *     (compare string_runtime.c / array_runtime.c on alloc failure).
 *   - Input-shaped failures (truncated stream, bad CRC, bad header)
 *     are surfaced through DRIFT_CODEC_* return codes.
 */
#include "codec_runtime.h"

#include <stdlib.h>
#include <string.h>
#include <zlib.h>

/* gzip wrapper: 15-bit window + 16 to request gzip framing.
 * See zlib manual on deflateInit2/inflateInit2 windowBits. */
#define DRIFT_GZIP_WBITS 31

/* Default memLevel for zlib (1..9; 8 is the documented default). */
#define DRIFT_GZIP_MEM_LEVEL 8

/* Initial output buffer for decode (encode uses deflateBound).
 * Geometric growth from here. */
#define DRIFT_GZIP_DECODE_INITIAL_OUTPUT 4096u

/* Chunk size for feeding zlib (its avail_in/avail_out are uInt == uint32_t;
 * inputs larger than UINT_MAX must be fed in pieces). Using a 1 MiB chunk
 * keeps deflate()/inflate() loop overhead negligible. */
#define DRIFT_GZIP_IO_CHUNK ((size_t)(1u << 20))

void drift_codec_free(uint8_t *p) {
	if (p != NULL) {
		free(p);
	}
}

void drift_codec_copy_bytes(uint8_t *dst, const uint8_t *src, size_t len) {
	if (len > 0) {
		memcpy(dst, src, len);
	}
}

/* Internal — abort with a useful prefix when libz says something
 * impossible-by-construction happened. */
static void drift_codec_abort(const char *fn, int zret) {
	(void)fn;
	(void)zret;
	abort();
}

int32_t drift_codec_gzip_encode(
	const uint8_t *in, size_t in_len,
	int32_t level,
	uint8_t **out, size_t *out_len) {

	*out = NULL;
	*out_len = 0;

	/* Validate level defensively even though the Drift wrapper already
	 * pre-validates: shim must be safe in isolation. */
	if (level != Z_DEFAULT_COMPRESSION && (level < 0 || level > 9)) {
		return DRIFT_CODEC_INVALID_LEVEL;
	}

	z_stream strm;
	memset(&strm, 0, sizeof(strm));
	int rc = deflateInit2(
		&strm,
		(int)level,
		Z_DEFLATED,
		DRIFT_GZIP_WBITS,
		DRIFT_GZIP_MEM_LEVEL,
		Z_DEFAULT_STRATEGY);
	if (rc != Z_OK) {
		/* Z_STREAM_ERROR (bad params) or Z_MEM_ERROR — internal. */
		drift_codec_abort("deflateInit2", rc);
	}

	/* deflateBound is an upper bound on the compressed output given the
	 * total input length. Pad slightly so we never realloc on the hot path. */
	uLong bound = deflateBound(&strm, (uLong)in_len);
	size_t cap = (size_t)bound + 64;
	uint8_t *buf = (uint8_t *)malloc(cap > 0 ? cap : 1);
	if (buf == NULL) {
		(void)deflateEnd(&strm);
		drift_codec_abort("malloc", 0);
	}

	size_t in_pos = 0;
	size_t out_pos = 0;
	int finished = 0;
	while (!finished) {
		if (strm.avail_in == 0 && in_pos < in_len) {
			size_t chunk = in_len - in_pos;
			if (chunk > DRIFT_GZIP_IO_CHUNK) {
				chunk = DRIFT_GZIP_IO_CHUNK;
			}
			strm.next_in = (Bytef *)(in + in_pos);
			strm.avail_in = (uInt)chunk;
			in_pos += chunk;
		}
		if (strm.avail_out == 0) {
			size_t remaining = cap - out_pos;
			if (remaining == 0) {
				/* Should not happen: deflateBound + 64 covers worst case for
				 * level 0..9 + default strategy. If it does, treat as internal. */
				free(buf);
				(void)deflateEnd(&strm);
				drift_codec_abort("encode-bound-exceeded", 0);
			}
			size_t chunk = remaining;
			if (chunk > DRIFT_GZIP_IO_CHUNK) {
				chunk = DRIFT_GZIP_IO_CHUNK;
			}
			strm.next_out = buf + out_pos;
			strm.avail_out = (uInt)chunk;
			out_pos += chunk;
		}
		int flush = (in_pos >= in_len) ? Z_FINISH : Z_NO_FLUSH;
		uInt before_out = strm.avail_out;
		rc = deflate(&strm, flush);
		/* Reclaim the unused tail of the output window we just gave zlib,
		 * so the next iteration's out_pos points at the true write head. */
		out_pos -= strm.avail_out;
		(void)before_out;
		if (rc == Z_STREAM_END) {
			finished = 1;
		} else if (rc != Z_OK && rc != Z_BUF_ERROR) {
			/* Z_STREAM_ERROR / Z_MEM_ERROR — encode has no input-level
			 * failure mode; this is internal. */
			free(buf);
			(void)deflateEnd(&strm);
			drift_codec_abort("deflate", rc);
		}
		/* Z_BUF_ERROR with no progress and avail_in==0 + flush==Z_FINISH would
		 * mean we lost the loop invariant. deflateBound guarantees it won't. */
	}

	size_t total = (size_t)strm.total_out;
	rc = deflateEnd(&strm);
	if (rc != Z_OK) {
		free(buf);
		drift_codec_abort("deflateEnd", rc);
	}

	/* Trim to exact size. realloc(.., 0) is implementation-defined; guard. */
	if (total == 0) {
		/* Empty gzip stream is ~20 bytes — total cannot actually be zero,
		 * but defend the realloc call anyway. */
		free(buf);
		buf = (uint8_t *)malloc(1);
		if (buf == NULL) {
			drift_codec_abort("malloc-trim", 0);
		}
		*out = buf;
		*out_len = 0;
		return DRIFT_CODEC_OK;
	}
	uint8_t *trimmed = (uint8_t *)realloc(buf, total);
	if (trimmed != NULL) {
		buf = trimmed;
	}
	*out = buf;
	*out_len = total;
	return DRIFT_CODEC_OK;
}

int32_t drift_codec_gzip_decode(
	const uint8_t *in, size_t in_len,
	size_t max_out,
	uint8_t **out, size_t *out_len) {

	*out = NULL;
	*out_len = 0;

	z_stream strm;
	memset(&strm, 0, sizeof(strm));
	int rc = inflateInit2(&strm, DRIFT_GZIP_WBITS);
	if (rc != Z_OK) {
		drift_codec_abort("inflateInit2", rc);
	}

	size_t cap = DRIFT_GZIP_DECODE_INITIAL_OUTPUT;
	if (cap > max_out) {
		cap = max_out;
		if (cap == 0) {
			cap = 1;
		}
	}
	uint8_t *buf = (uint8_t *)malloc(cap > 0 ? cap : 1);
	if (buf == NULL) {
		(void)inflateEnd(&strm);
		drift_codec_abort("malloc", 0);
	}

	size_t in_pos = 0;
	size_t out_pos = 0;
	int32_t result_code = DRIFT_CODEC_OK;
	int finished = 0;

	while (!finished) {
		if (strm.avail_in == 0 && in_pos < in_len) {
			size_t chunk = in_len - in_pos;
			if (chunk > DRIFT_GZIP_IO_CHUNK) {
				chunk = DRIFT_GZIP_IO_CHUNK;
			}
			strm.next_in = (Bytef *)(in + in_pos);
			strm.avail_in = (uInt)chunk;
			in_pos += chunk;
		}

		if (strm.avail_out == 0) {
			/* Grow output if exhausted. */
			if (out_pos >= cap) {
				if (cap >= max_out) {
					result_code = DRIFT_CODEC_OUTPUT_TOO_LARGE;
					break;
				}
				size_t new_cap = cap * 2;
				if (new_cap < cap) {
					new_cap = max_out; /* overflow guard */
				}
				if (new_cap > max_out) {
					new_cap = max_out;
				}
				uint8_t *new_buf = (uint8_t *)realloc(buf, new_cap);
				if (new_buf == NULL) {
					free(buf);
					(void)inflateEnd(&strm);
					drift_codec_abort("realloc", 0);
				}
				buf = new_buf;
				cap = new_cap;
			}
			size_t remaining = cap - out_pos;
			size_t chunk = remaining;
			if (chunk > DRIFT_GZIP_IO_CHUNK) {
				chunk = DRIFT_GZIP_IO_CHUNK;
			}
			strm.next_out = buf + out_pos;
			strm.avail_out = (uInt)chunk;
			out_pos += chunk;
		}

		/* Defensive no-progress guard: capture totals before inflate()
		 * so we can detect a stuck state machine (Z_OK / Z_BUF_ERROR
		 * with both buffers available but no input consumed and no
		 * output produced). Should not happen in healthy zlib; fail
		 * closed as malformed if it does. */
		uLong pre_in = strm.total_in;
		uLong pre_out = strm.total_out;
		uInt pre_avail_in = strm.avail_in;
		uInt pre_avail_out = strm.avail_out;

		rc = inflate(&strm, Z_NO_FLUSH);
		out_pos -= strm.avail_out; /* reclaim untouched tail of this window */

		if (rc == Z_STREAM_END) {
			finished = 1;
			break;
		}
		if (rc == Z_OK || rc == Z_BUF_ERROR) {
			int made_progress = (strm.total_in != pre_in)
				|| (strm.total_out != pre_out);
			if (rc == Z_BUF_ERROR) {
				/* Need more input or more output. If we have input
				 * left or output room left, the next iteration handles
				 * it. If no input remains AND zlib can't produce more,
				 * the stream is truncated. */
				if (in_pos >= in_len && strm.avail_in == 0) {
					result_code = DRIFT_CODEC_TRUNCATED;
					break;
				}
			}
			if (!made_progress && pre_avail_in > 0 && pre_avail_out > 0) {
				/* zlib was given input AND output room and consumed
				 * neither / produced nothing. State machine is stuck —
				 * treat as malformed rather than spinning. */
				result_code = DRIFT_CODEC_BAD_DATA;
				break;
			}
			continue;
		}
		if (rc == Z_DATA_ERROR || rc == Z_NEED_DICT) {
			/* Z_NEED_DICT shouldn't happen for gzip (no preset dict in
			 * RFC 1952), but treat as malformed if it does. */
			result_code = DRIFT_CODEC_BAD_DATA;
			break;
		}
		/* Z_MEM_ERROR / Z_STREAM_ERROR — internal. */
		free(buf);
		(void)inflateEnd(&strm);
		drift_codec_abort("inflate", rc);
	}

	size_t total = (size_t)strm.total_out;
	/* Capture trailing-input state BEFORE inflateEnd, which is permitted
	 * to invalidate `strm`'s state pointers / fields. RFC 1952 allows
	 * concatenated gzip members; we accept exactly one and reject any
	 * bytes past the trailer (callers that need member concatenation
	 * should build it themselves). */
	int had_trailing = (in_pos < in_len) || (strm.avail_in > 0);
	(void)inflateEnd(&strm);

	if (result_code != DRIFT_CODEC_OK) {
		free(buf);
		return result_code;
	}

	if (had_trailing) {
		free(buf);
		return DRIFT_CODEC_BAD_DATA;
	}

	if (total == 0) {
		free(buf);
		buf = (uint8_t *)malloc(1);
		if (buf == NULL) {
			drift_codec_abort("malloc-empty", 0);
		}
		*out = buf;
		*out_len = 0;
		return DRIFT_CODEC_OK;
	}
	uint8_t *trimmed = (uint8_t *)realloc(buf, total);
	if (trimmed != NULL) {
		buf = trimmed;
	}
	*out = buf;
	*out_len = total;
	return DRIFT_CODEC_OK;
}
