#include <stdio.h>
#include "../runtime/error_runtime.h"
#include "../runtime/string_runtime.h"

extern struct Pair level1(void);

int main(void) {
    struct Pair p = level1();
    if (p.err) {
        fprintf(stderr, "frames=%zu\n", p.err->frame_count);
        for (size_t i = 0; i < p.err->frame_count; i++) {
            char* module = drift_string_to_cstr(p.err->frame_modules ? p.err->frame_modules[i] : drift_string_empty());
            char* file = drift_string_to_cstr(p.err->frame_files ? p.err->frame_files[i] : drift_string_empty());
            char* func = drift_string_to_cstr(p.err->frame_funcs ? p.err->frame_funcs[i] : drift_string_empty());
            long line = p.err->frame_lines ? (long)p.err->frame_lines[i] : -1;
            fprintf(stderr, "%s:%s:%s:%ld\n", module ? module : "<unknown>", file ? file : "<unknown>", func ? func : "<unknown>", line);
            free(module);
            free(file);
            free(func);
        }
        error_free(p.err);
        return 1;
    }
    printf("ok %ld\n", p.val);
    return 0;
}
