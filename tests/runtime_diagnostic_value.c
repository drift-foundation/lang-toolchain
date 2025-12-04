#include "runtime/diagnostic_runtime.h"
#include "runtime/string_runtime.h"
#include <stdio.h>
#include <string.h>

static int expect_int(struct DriftOptionalInt opt, int64_t value) {
    return opt.is_some && opt.value == value;
}

static int expect_string(struct DriftOptionalString opt, const char* s, size_t len) {
    if (!opt.is_some) return 0;
    if (opt.value.len != len) return 0;
    return opt.value.data && (memcmp(opt.value.data, s, len) == 0);
}

int main(void) {
    // Scalar path: int -> as_int succeeds, as_string fails.
    struct DriftDiagnosticValue dv_int = drift_dv_int(42);
    if (!expect_int(drift_dv_as_int(&dv_int), 42)) return 1;
    if (drift_dv_as_string(&dv_int).is_some) return 2;

    // to_diag helpers for primitives and optionals.
    if (drift_dv_kind(drift_diag_from_optional_int(OPTIONAL_INT_NONE)) != DV_NULL) return 9;
    struct DriftDiagnosticValue opt_int_dv = drift_diag_from_optional_int((struct DriftOptionalInt){1, 99});
    if (!expect_int(drift_dv_as_int(&opt_int_dv), 99)) return 10;
    struct DriftDiagnosticValue diag_str =
        drift_diag_from_optional_string((struct DriftOptionalString){1, drift_string_literal("hello", 5)});
    if (!expect_string(drift_dv_as_string(&diag_str), "hello", 5)) return 11;

    // Array path: index in-range / out-of-range.
    struct DriftDiagnosticValue arr_items[2] = {drift_dv_int(1), drift_dv_int(2)};
    struct DriftDiagnosticValue dv_arr = drift_dv_array(arr_items, 2);
    struct DriftDiagnosticValue idx0 = drift_dv_index(dv_arr, 0);
    if (!expect_int(drift_dv_as_int(&idx0), 1)) return 3;
    if (drift_dv_kind(drift_dv_index(dv_arr, 5)) != DV_MISSING) return 4;

    // Object path: nested get.
    struct DriftDiagnosticValue code_val = drift_dv_int(123);
    struct DriftDiagnosticValue id_val = drift_dv_string(drift_string_literal("cust1", 5));
    struct DriftDiagnosticField cust_fields[] = {
        {drift_string_literal("id", 2), &id_val},
    };
    struct DriftDiagnosticValue cust_obj = drift_dv_object(cust_fields, 1);
    struct DriftDiagnosticField order_fields[] = {
        {drift_string_literal("customer", 8), &cust_obj},
        {drift_string_literal("code", 4), &code_val},
    };
    struct DriftDiagnosticValue order_obj = drift_dv_object(order_fields, 2);

    struct DriftDiagnosticValue order_code = drift_dv_get(order_obj, drift_string_literal("code", 4));
    if (!expect_int(drift_dv_as_int(&order_code), 123)) return 5;
    struct DriftDiagnosticValue cust_view =
        drift_dv_get(order_obj, drift_string_literal("customer", 8));
    struct DriftDiagnosticValue cust_id =
        drift_dv_get(cust_view, drift_string_literal("id", 2));
    if (!expect_string(drift_dv_as_string(&cust_id), "cust1", 5)) return 6;

    // Missing handling.
    struct DriftDiagnosticValue missing =
        drift_dv_get(order_obj, drift_string_literal("missing", 7));
    if (drift_dv_kind(missing) != DV_MISSING) return 7;
    if (drift_dv_as_int(&missing).is_some) return 8;

    return 0;
}
