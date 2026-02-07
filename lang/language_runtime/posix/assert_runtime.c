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
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#include "string_runtime.h"

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

		if (file) {
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

void drift_assert_loc(int cond, DriftString file, drift_isize line, DriftString expr, DriftString msg) {
	if (cond) {
		return;
	}
	char *file_c = drift_string_to_cstr(file);
	char *expr_c = drift_string_to_cstr(expr);
	char *msg_c = drift_string_to_cstr(msg);

	if (expr.len > 0 && msg.len > 0) {
		fprintf(stderr, "assertion failed: %s — %s\n", expr_c, msg_c);
	} else if (expr.len > 0) {
		fprintf(stderr, "assertion failed: %s\n", expr_c);
	} else if (msg.len > 0) {
		fprintf(stderr, "assertion failed: %s\n", msg_c);
	} else {
		fprintf(stderr, "assertion failed\n");
	}
	fprintf(stderr, "  at %s:%lld\n", file_c, (long long)line);
	drift_debug_print_stacktrace();
	free(file_c);
	free(expr_c);
	free(msg_c);
	abort();
}
