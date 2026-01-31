// Drift console runtime (lang2, v1).
#include "console_runtime.h"

#include <stdio.h>
#include <stdlib.h>

void drift_console_write(DriftString s) {
	if (s.len == 0 || s.data == NULL) {
		drift_string_release(s);
		return;
	}
	size_t n = (size_t)s.len;
	size_t written = fwrite(s.data, 1, n, stdout);
	if (written < n && ferror(stdout)) {
		abort();
	}
	drift_string_release(s);
}

void drift_console_writeln(DriftString s) {
	drift_console_write(s);
	fputc('\n', stdout);
}

void drift_console_eprintln(DriftString s) {
	if (s.len > 0 && s.data != NULL) {
		size_t n = (size_t)s.len;
		size_t written = fwrite(s.data, 1, n, stderr);
		if (written < n && ferror(stderr)) {
			abort();
		}
	}
	fputc('\n', stderr);
	drift_string_release(s);
}
