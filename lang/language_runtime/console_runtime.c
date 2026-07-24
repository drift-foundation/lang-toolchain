// Drift console runtime (lang, v1).
#include "console_runtime.h"

#include <stdio.h>
#include <stdlib.h>

/* drift-owned-string-audit: allow internal-borrowed-helper -- s
 * Writes s to stdout without taking ownership.  Public entry points
 * (drift_console_write / _writeln) wrap this with DRIFT_OWNED_STRING
 * on a single shadow local so the stake is released exactly once at
 * the outer scope exit -- delegating to a release-on-entry helper
 * would double-release on the writeln path. */
static void _drift_console_write_borrowed(DriftString s) {
	drift_isize len = drift_string_len(s);  /* fails closed on tombstone/malformed */
	if (len == 0) {
		return;
	}
	size_t n = (size_t)len;
	size_t written = fwrite(drift_string_data(s), 1, n, stdout);
	if (written < n && ferror(stdout)) {
		abort();
	}
}

/* drift-owned-string-audit: allow internal-borrowed-helper -- s
 * Stderr counterpart of _drift_console_write_borrowed.  Same
 * ownership contract: caller retains the stake. */
static void _drift_console_eprint_borrowed(DriftString s) {
	drift_isize len = drift_string_len(s);  /* fails closed on tombstone/malformed */
	if (len == 0) {
		return;
	}
	size_t n = (size_t)len;
	size_t written = fwrite(drift_string_data(s), 1, n, stderr);
	if (written < n && ferror(stderr)) {
		abort();
	}
}

void drift_console_write(DriftString s_in) {
	DRIFT_OWNED_STRING DriftString s = s_in;
	_drift_console_write_borrowed(s);
}

void drift_console_writeln(DriftString s_in) {
	DRIFT_OWNED_STRING DriftString s = s_in;
	_drift_console_write_borrowed(s);
	fputc('\n', stdout);
}

void drift_console_eprint(DriftString s_in) {
	DRIFT_OWNED_STRING DriftString s = s_in;
	_drift_console_eprint_borrowed(s);
}

void drift_console_eprintln(DriftString s_in) {
	DRIFT_OWNED_STRING DriftString s = s_in;
	_drift_console_eprint_borrowed(s);
	fputc('\n', stderr);
}
