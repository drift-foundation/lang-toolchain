#include "argv_runtime.h"

void drift_build_argv(DriftArrayString *out, int argc, char **argv) {
	if (!out) {
		abort();
	}
	out->len = (drift_isize)argc;
	out->cap = (drift_isize)argc;
	out->gen = 0;
	// Allocate backing store for DriftString elements.
	DriftString *data = (DriftString *)drift_alloc_array(sizeof(DriftString), _Alignof(DriftString), out->len, out->cap);
	for (int i = 0; i < argc; i++) {
		// Construct from C string; drift_string_from_cstr handles NULL by returning empty.
		data[i] = drift_string_from_cstr(argv[i]);
	}
	out->data = (void *)data;
}
