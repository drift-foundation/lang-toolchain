// Pure-C ownership pins for Phase 1 of the DV→JSON diagnostics-context
// migration.  Exercises the additive JSON helpers
// (`drift_error_set_params_json`, `drift_error_append_context_frame`,
// `drift_error_get_params_json`, `drift_error_get_context_json`) against
// the existing DriftError runtime and asserts:
//
//   1. Fresh `DriftError` carries `params_json == "{}"` and
//      `context_json == "[]"` per ABI spec §2.2.
//   2. `set_params_json` takes ownership (no clone).
//   3. `set_params_json` replacement releases prior exactly once.
//   4. `append_context_frame` first/multi-call produces well-formed JSON
//      arrays.
//   5. `append_context_frame` preserves caller-provided `frame_json`
//      bytes verbatim inside the merged array (ABI §2.2 fastpath
//      guarantee for the `e.encode_compact()` splice path).
//   6. `get_params_json` / `get_context_json` return RETAINED `DriftString`
//      (caller owns and releases).
//
// Slice 7c-1 (ABI 14, 2026-05-06): the DV-side ADDITIVE pins
// (DV path coexistence + dual release) are retired with the
// runtime DV exports.  At ABI 14 the runtime archive has no DV
// symbols; this test file pins the surviving JSON-helper
// ownership contract only.
//
// Run under valgrind from `lang/tests/memcheck/test_drift_error_phase1_helpers.py`.
// See `doc/design/drift-lang-abi.md` §2.3 for the canonical helper
// ownership contract and `memory/project_dv_to_json_diagnostics.md` for
// the multi-phase migration plan.

// Include order matters: `string_runtime.h` first installs the
// `DRIFT_STRING_RUNTIME_H` guard so `diagnostic_runtime.h` (pulled in
// via `error_dummy.h`) skips its fallback `struct DriftString`
// re-definition.
#include "string_runtime.h"
#include "error_dummy.h"
#include "diagnostic_runtime.h"

#include <assert.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

// Replicate `DriftStringHeader` for refcount peeking.  Layout pinned by
// `_Static_assert`s in `lang/language_runtime/string_runtime.c` —
// `sizeof(DriftStringHeader) == 16`, `flags` at offset 8, static-flag
// bit `1ULL << 0`.  If those asserts ever change, this struct must
// follow.
typedef struct {
	_Atomic uint64_t refcount;
	uint64_t flags;
} DriftStringHeader_test;

static uint64_t peek_refcount(struct DriftString s) {
	if (s.data == NULL) {
		return 0;
	}
	DriftStringHeader_test *hdr =
		(DriftStringHeader_test *)((char *)s.data - sizeof(DriftStringHeader_test));
	if (hdr->flags & 1ULL) {
		return UINT64_MAX;  // static — sentinel
	}
	return atomic_load_explicit(&hdr->refcount, memory_order_relaxed);
}

static int eq_str(struct DriftString s, const char *cstr) {
	size_t clen = strlen(cstr);
	if ((size_t)s.len != clen) {
		return 0;
	}
	return memcmp(s.data, cstr, clen) == 0;
}

// Per ABI spec §2.3 (post-2026-05-02 robustness fix): drift_error_new
// makes its own owned copy of event_fqn at construction time; the
// caller retains ownership of the input.  Tests therefore release
// their local `DriftString fqn` reference after handing it to
// drift_error_new.  drift_error_release independently drops the
// runtime's owned copy.  This helper folds the pattern.
static void release_err_and_fqn(struct DriftError *e, struct DriftString fqn) {
	drift_error_release(e);
	drift_string_release(fqn);
}

static int total_failures = 0;

#define CHECK(cond) do {                                            \
	if (!(cond)) {                                                  \
		fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
		return 1;                                                   \
	}                                                               \
} while (0)

#define RUN(test_fn) do {                                           \
	int _rc = test_fn();                                            \
	if (_rc != 0) {                                                 \
		fprintf(stderr, "  -> %s FAILED\n", #test_fn);              \
		total_failures += 1;                                        \
	} else {                                                        \
		fprintf(stderr, "  -> %s ok\n", #test_fn);                  \
	}                                                               \
} while (0)


// ─────────────────────────────────────────────────────────────────
// Test 1: drift_error_new initializes empty JSON segments.
// Per ABI spec §2.2: empty error carries `params_json == "{}"` and
// `context_json == "[]"`.
// ─────────────────────────────────────────────────────────────────
static int test_new_initializes_empty_json(void) {
	struct DriftString fqn = drift_string_from_cstr("test:Empty");
	struct DriftError *err = drift_error_new(0, fqn);
	CHECK(err != NULL);

	struct DriftString p = drift_error_get_params_json(err);
	struct DriftString c = drift_error_get_context_json(err);
	CHECK(eq_str(p, "{}"));
	CHECK(eq_str(c, "[]"));

	drift_string_release(p);
	drift_string_release(c);
	release_err_and_fqn(err, fqn);
	return 0;
}

// ─────────────────────────────────────────────────────────────────
// Test 2: set_params_json takes ownership (refcount transfer, no clone).
// ─────────────────────────────────────────────────────────────────
static int test_set_params_takes_ownership(void) {
	struct DriftString fqn = drift_string_from_cstr("test:SetOwnership");
	struct DriftError *err = drift_error_new(0, fqn);

	struct DriftString new_params = drift_string_from_cstr("{\"k\":1}");
	CHECK(peek_refcount(new_params) == 1);

	drift_error_set_params_json(err, new_params);
	// Ownership transferred — refcount on the caller-side reference
	// stays 1 from the runtime's perspective (no extra retain/release).

	struct DriftString stored = drift_error_get_params_json(err);  // retained
	CHECK(eq_str(stored, "{\"k\":1}"));
	// 1 stored owner + 1 returned-retained reference = 2.
	CHECK(peek_refcount(stored) == 2);
	drift_string_release(stored);

	release_err_and_fqn(err, fqn);  // drops final owner — buffer freed
	return 0;
}

// ─────────────────────────────────────────────────────────────────
// Test 3: replacement releases prior exactly once.
// valgrind alone catches: prior-not-released → leak; double-release
// → use-after-free / invalid-free.  This test merely exercises the
// path; the gate is the valgrind output.
// ─────────────────────────────────────────────────────────────────
static int test_set_params_replacement_releases_prior(void) {
	struct DriftString fqn = drift_string_from_cstr("test:Replacement");
	struct DriftError *err = drift_error_new(0, fqn);

	// Initial "{}" is owned by err; first set must release it.
	struct DriftString A = drift_string_from_cstr("{\"first\":1}");
	drift_error_set_params_json(err, A);

	struct DriftString B = drift_string_from_cstr("{\"second\":2}");
	drift_error_set_params_json(err, B);  // must release A exactly once

	struct DriftString stored = drift_error_get_params_json(err);
	CHECK(eq_str(stored, "{\"second\":2}"));
	drift_string_release(stored);

	release_err_and_fqn(err, fqn);  // releases B
	return 0;
}

// ─────────────────────────────────────────────────────────────────
// Test 4: append_context_frame — first call produces single-element
// JSON array.
// ─────────────────────────────────────────────────────────────────
static int test_append_context_frame_first(void) {
	struct DriftString fqn = drift_string_from_cstr("test:AppendOne");
	struct DriftError *err = drift_error_new(0, fqn);

	struct DriftString frame =
		drift_string_from_cstr("{\"fn_name\":\"f\",\"locals\":{}}");
	drift_error_append_context_frame(err, frame);  // ownership transferred

	struct DriftString ctx = drift_error_get_context_json(err);
	CHECK(eq_str(ctx, "[{\"fn_name\":\"f\",\"locals\":{}}]"));
	drift_string_release(ctx);

	release_err_and_fqn(err, fqn);
	return 0;
}

// ─────────────────────────────────────────────────────────────────
// Test 5: append_context_frame — multiple calls produce well-formed
// JSON array with frames separated by single commas, in append order.
// ─────────────────────────────────────────────────────────────────
static int test_append_context_frame_multiple(void) {
	struct DriftString fqn = drift_string_from_cstr("test:AppendMulti");
	struct DriftError *err = drift_error_new(0, fqn);

	struct DriftString f1 =
		drift_string_from_cstr("{\"fn_name\":\"a\",\"locals\":{}}");
	struct DriftString f2 =
		drift_string_from_cstr("{\"fn_name\":\"b\",\"locals\":{}}");
	drift_error_append_context_frame(err, f1);
	drift_error_append_context_frame(err, f2);

	struct DriftString ctx = drift_error_get_context_json(err);
	CHECK(eq_str(ctx,
		"[{\"fn_name\":\"a\",\"locals\":{}},{\"fn_name\":\"b\",\"locals\":{}}]"));
	drift_string_release(ctx);

	release_err_and_fqn(err, fqn);
	return 0;
}

// ─────────────────────────────────────────────────────────────────
// Test 6: append_context_frame preserves frame_json bytes verbatim
// (ABI §2.2 fastpath guarantee).  The runtime must NOT canonicalize
// whitespace, key order, or any other content within the spliced
// frame_json — `e.encode_compact()` lowering depends on byte-exact
// preservation.
// ─────────────────────────────────────────────────────────────────
static int test_append_preserves_frame_bytes(void) {
	struct DriftString fqn = drift_string_from_cstr("test:AppendBytes");
	struct DriftError *err = drift_error_new(0, fqn);

	const char *frame_lit = "{\n  \"fn_name\": \"x\" ,\n  \"locals\": {}\n}";
	struct DriftString frame = drift_string_from_cstr(frame_lit);
	drift_error_append_context_frame(err, frame);

	struct DriftString ctx = drift_error_get_context_json(err);
	char expect[256];
	int n = snprintf(expect, sizeof(expect), "[%s]", frame_lit);
	CHECK(n > 0 && (size_t)n < sizeof(expect));
	CHECK(eq_str(ctx, expect));
	drift_string_release(ctx);

	release_err_and_fqn(err, fqn);
	return 0;
}

// ─────────────────────────────────────────────────────────────────
// Test 7: getters return retained DriftString.  Two consecutive gets
// produce independent owners (each must be released by the caller).
// ─────────────────────────────────────────────────────────────────
static int test_getters_retain(void) {
	struct DriftString fqn = drift_string_from_cstr("test:GetRetain");
	struct DriftError *err = drift_error_new(0, fqn);

	struct DriftString A = drift_string_from_cstr("{\"k\":1}");
	drift_error_set_params_json(err, A);

	struct DriftString r1 = drift_error_get_params_json(err);
	struct DriftString r2 = drift_error_get_params_json(err);
	// 1 stored + 2 returned-retained = 3.
	CHECK(peek_refcount(r1) == 3);
	CHECK(eq_str(r1, "{\"k\":1}"));
	CHECK(eq_str(r2, "{\"k\":1}"));

	drift_string_release(r1);
	drift_string_release(r2);

	release_err_and_fqn(err, fqn);
	return 0;
}

// Slice 7c (0.31.64, ABI 14, 2026-05-06): the legacy DV path
// (`drift_error_add_attr_dv` + `drift_dv_*`) is deleted at the
// runtime boundary.  The previous `test_old_dv_path_still_works`
// probe (Phase 1 ADDITIVE check) is retired here because the
// helpers it called are no longer exported.

// ─────────────────────────────────────────────────────────────────
// Test 10: getter-return release safety — params_json.
//
// Pins the retained-return contract end-to-end (more directly than
// refcount peeking).  If `drift_error_get_params_json` returned a
// borrow instead of a retained reference, releasing the returned
// `DriftString` would damage the runtime's stored copy and the
// second `get` would observe a freed buffer (UAF caught by valgrind)
// or wrong contents.
// ─────────────────────────────────────────────────────────────────
static int test_get_params_release_safety(void) {
	struct DriftString fqn = drift_string_from_cstr("test:GetParamsReleaseSafety");
	struct DriftError *err = drift_error_new(0, fqn);

	struct DriftString stored_in = drift_string_from_cstr("{\"k\":\"v\"}");
	drift_error_set_params_json(err, stored_in);

	struct DriftString s1 = drift_error_get_params_json(err);  // retained
	CHECK(eq_str(s1, "{\"k\":\"v\"}"));
	drift_string_release(s1);
	// If get returned a borrow, the runtime's stored copy is now freed.
	// The next get must produce the original bytes intact.
	struct DriftString s2 = drift_error_get_params_json(err);  // retained
	CHECK(eq_str(s2, "{\"k\":\"v\"}"));
	drift_string_release(s2);

	release_err_and_fqn(err, fqn);  // valgrind catches UAF / leak otherwise
	return 0;
}

// ─────────────────────────────────────────────────────────────────
// Test 11: getter-return release safety — context_json.
// Mirror of test 10 for the context-array surface.
// ─────────────────────────────────────────────────────────────────
static int test_get_context_release_safety(void) {
	struct DriftString fqn = drift_string_from_cstr("test:GetContextReleaseSafety");
	struct DriftError *err = drift_error_new(0, fqn);

	struct DriftString frame =
		drift_string_from_cstr("{\"fn_name\":\"q\",\"locals\":{}}");
	drift_error_append_context_frame(err, frame);

	struct DriftString s1 = drift_error_get_context_json(err);
	CHECK(eq_str(s1, "[{\"fn_name\":\"q\",\"locals\":{}}]"));
	drift_string_release(s1);
	struct DriftString s2 = drift_error_get_context_json(err);
	CHECK(eq_str(s2, "[{\"fn_name\":\"q\",\"locals\":{}}]"));
	drift_string_release(s2);

	release_err_and_fqn(err, fqn);
	return 0;
}

// Slice 7c (2026-05-06): `test_both_paths_release_clean` retired —
// the "both DV and JSON" coexistence contract is gone.  ABI 14
// keeps only the JSON path.


int main(void) {
	fprintf(stderr, "test_drift_error_phase1 — running JSON-helper ownership pins\n");
	RUN(test_new_initializes_empty_json);
	RUN(test_set_params_takes_ownership);
	RUN(test_set_params_replacement_releases_prior);
	RUN(test_append_context_frame_first);
	RUN(test_append_context_frame_multiple);
	RUN(test_append_preserves_frame_bytes);
	RUN(test_getters_retain);
	RUN(test_get_params_release_safety);
	RUN(test_get_context_release_safety);

	if (total_failures != 0) {
		fprintf(stderr, "test_drift_error_phase1: %d test(s) failed\n", total_failures);
		return 1;
	}
	fprintf(stderr, "test_drift_error_phase1: all tests passed\n");
	return 0;
}
