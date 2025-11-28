#include <stdio.h>
#include "../runtime/error_runtime.h"
#include "../runtime/string_runtime.h"

extern struct Pair drift_entry(void);

int main(void) {
    struct Pair p = drift_entry();
    if (p.err) {
        char* dom = drift_string_to_cstr(p.err->domain);
        if (dom) {
            fprintf(stderr, "domain=%s\n", dom);
            free(dom);
        }
        error_free(p.err);
        return 1;
    }
    printf("ok %ld\n", p.val);
    return 0;
}
