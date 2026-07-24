/*
 * Backtrace symbolization is gated on the dual-runtime debug-style variant.
 *
 * The libdwfl + libunwind walk is the only consumer of libdw / libunwind /
 * libunwind-x86_64 / libelf in the entire runtime.  Including those headers
 * here drags those shared libraries into every linked binary's DT_NEEDED
 * closure.  The dual-runtime contract says only the explicit `_debug` opt-in
 * variant should carry symbolization machinery (and, transitively, those
 * dependency closures); the production "normal" lane must be runnable on
 * hosts that do not have libdw / libunwind / libelf installed at all.
 *
 * Selection is gated by the same -DDRIFT_RT_MODE_DEBUG cflag the runtime
 * identity sentinel uses (lang/language_runtime/abi_version_stamp.c).
 *
 *   Normal variant   : `drift_debug_print_stacktrace()` prints a single
 *                      explanatory hint line and returns.  No libdw /
 *                      libunwind / libelf headers are included; no symbols
 *                      from those libraries are referenced; the produced .o
 *                      has zero DT_NEEDED contribution from them.
 *   Debug-style      : the existing libunwind + libdwfl walk symbolizes the
 *                      stack and prints frame names + source locations.
 */
#ifdef DRIFT_RT_MODE_DEBUG
#if __has_include(<elfutils/libdwfl.h>)
#include <elfutils/libdwfl.h>
#define DRIFT_HAVE_LIBDW 1
#else
#define DRIFT_HAVE_LIBDW 0
#endif
#if __has_include(<libunwind.h>)
#include <libunwind.h>
#define DRIFT_HAVE_LIBUNWIND 1
#else
#define DRIFT_HAVE_LIBUNWIND 0
#endif
#endif /* DRIFT_RT_MODE_DEBUG */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#include "string_runtime.h"

extern void drift_alloc_report_now(void) __attribute__((weak));

#ifndef DRIFT_RT_MODE_DEBUG
static void drift_debug_print_stacktrace(void) {
	/* Normal lane: backtrace symbolization is omitted to keep libdw /
	 * libunwind / libelf out of the production binary's dependency
	 * closure.  Operators see a single stable line that points at the
	 * opt-in. */
	fprintf(stderr,
		"  <stacktrace unavailable in normal build; "
		"rebuild with DRIFT_DEBUG=1 for backtraces>\n");
}
#else
static void drift_debug_print_stacktrace(void) {
#if !DRIFT_HAVE_LIBUNWIND
	fprintf(stderr, "  <stacktrace unavailable>\n");
	return;
#endif
#if DRIFT_HAVE_LIBUNWIND
	unw_context_t ctx;
	unw_cursor_t cursor;
	if (unw_getcontext(&ctx) != 0) {
		return;
	}
	if (unw_init_local(&cursor, &ctx) != 0) {
		return;
	}

	Dwfl *dwfl = NULL;
#if DRIFT_HAVE_LIBDW
	Dwfl_Callbacks cb = {
		.find_elf = dwfl_linux_proc_find_elf,
		.find_debuginfo = dwfl_standard_find_debuginfo,
		.section_address = dwfl_offline_section_address,
	};
	dwfl = dwfl_begin(&cb);
	if (dwfl) {
		dwfl_linux_proc_report(dwfl, getpid());
		dwfl_report_end(dwfl, NULL, NULL);
	}
#endif

	int frame = 0;
	while (unw_step(&cursor) > 0) {
		unw_word_t ip = 0;
		unw_word_t off = 0;
		char name[256];
		name[0] = '\0';
		unw_get_reg(&cursor, UNW_REG_IP, &ip);
		int has_name = (unw_get_proc_name(&cursor, name, sizeof(name), &off) == 0);

		const char *file = NULL;
		int line = 0;
		if (dwfl) {
			Dwfl_Line *dwln = dwfl_getsrc(dwfl, (Dwarf_Addr)ip);
			if (dwln) {
				Dwarf_Addr addr = 0;
				file = dwfl_lineinfo(dwln, &addr, &line, NULL, NULL, NULL);
			}
		}

		// Keep user-frame formatting stable in e2e expectations: only print
		// source suffixes for relative runtime/libc paths ("../...").
		if (file && file[0] == '.') {
			fprintf(stderr, "  #%d %s (%s:%d)\n", frame, has_name ? name : "<unknown>", file, line);
		} else {
			fprintf(stderr, "  #%d %s\n", frame, has_name ? name : "<unknown>");
		}
		frame++;
	}
	if (dwfl) {
		dwfl_end(dwfl);
	}
#endif
}
#endif /* DRIFT_RT_MODE_DEBUG */

/* drift-owned-string-audit: allow read-only-borrow -- file, expr, msg
 * `assert(cond, msg)` is a language built-in; its Drift IR call site
 * does NOT pre-retain before the extern call (unlike the normal
 * function-call extern pattern used by env.get / io.file_builder /
 * cons.println), so this function must NOT release file/expr/msg --
 * doing so would double-free the stake and UAF on a heap-allocated msg.
 *
 * As of 0.33.55 `_visit_stmt_HAssert` lowers the assertion so the
 * message is only built on the FAILING branch and AssertLoc is emitted
 * there immediately before an `Unreachable` terminator (the passing
 * branch never constructs the message -- fixing the await_signal
 * heap-message leak).  Consequently this function is only ever reached
 * with cond==false and always aborts; the early `if (cond) return;` is
 * now defensive-only, there is no post-call path, and the unreleased
 * msg stake is reclaimed by process death.  Verified by
 * lang/tests/memcheck/test_assert_message_pass_memcheck.py (and, for the
 * historical fail-path double-free, the DRIFT_OWNED_STRING slice
 * 2026-05-16). */
void drift_assert_loc(int cond, DriftString file, drift_isize line, DriftString expr, DriftString msg) {
	if (cond) {
		return;
	}
	char *file_c = drift_string_to_cstr(file);
	char *expr_c = drift_string_to_cstr(expr);
	char *msg_c = drift_string_to_cstr(msg);

	if (drift_string_len(expr) > 0 && drift_string_len(msg) > 0) {
		fprintf(stderr, "assertion failed: %s — %s\n", expr_c, msg_c);
	} else if (drift_string_len(expr) > 0) {
		fprintf(stderr, "assertion failed: %s\n", expr_c);
	} else if (drift_string_len(msg) > 0) {
		fprintf(stderr, "assertion failed: %s\n", msg_c);
	} else {
		fprintf(stderr, "assertion failed\n");
	}
	fprintf(stderr, "  at %s:%lld\n", file_c, (long long)line);
	drift_debug_print_stacktrace();
	free(file_c);
	free(expr_c);
	free(msg_c);
	if (drift_alloc_report_now) {
		drift_alloc_report_now();
	}
	abort();
}
