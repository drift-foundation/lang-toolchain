#include <stdio.h>
#include "../runtime/error_runtime.h"

extern struct Pair level1(void);

int main(void) {
    struct Pair p = level1();
    if (p.err) {
        const char* msg = error_to_cstr(p.err);
        if (msg) fprintf(stderr, "%s\n", msg);
        fprintf(stderr, "frames=%zu\n", p.err->frame_count);
        for (size_t i = 0; i < p.err->frame_count; i++) {
            const char* module = p.err->frame_modules ? p.err->frame_modules[i] : "<unknown>";
            const char* file = p.err->frame_files ? p.err->frame_files[i] : "<unknown>";
            const char* func = p.err->frame_funcs ? p.err->frame_funcs[i] : "<unknown>";
            long line = p.err->frame_lines ? (long)p.err->frame_lines[i] : -1;
            fprintf(stderr, "%s:%s:%s:%ld\n", module, file, func, line);
        }
        error_free(p.err);
        return 1;
    }
    printf("ok %ld\n", p.val);
    return 0;
}
