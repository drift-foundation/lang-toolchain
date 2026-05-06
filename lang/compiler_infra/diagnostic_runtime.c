// Slice 7c-1 (ABI 14, 2026-05-06): all `drift_dv_*` and
// `drift_diag_from_*` symbols deleted from the runtime archive —
// post-Slice 7b the throw-side params projection routes through
// `core.Diagnostic.to_json_text` + `ExcSetParamsJson`, and
// captured-locals projection goes through direct per-scalar
// `core.diagnostic_json_*` dispatch.  No production lowering or
// runtime path references DV.
//
// Slice 7c-3 (ABI 14, 2026-05-06): `DriftDiagnosticTag`,
// `DriftDiagnosticValue`, `DriftDiagnosticEntry`,
// `DriftDiagnosticField`, `DriftDiagnosticArray`, and
// `DriftDiagnosticObject` deleted from `diagnostic_runtime.h`.  No
// remaining DV-related declarations or layout asserts.  The header
// retains the foundational scalar typedefs (`drift_isize` /
// `drift_usize`) and the `DriftString` forward struct because they
// are pulled in transitively by `error_dummy.h` and several
// language-runtime translation units; relocating them is a separate
// slice (see follow-up note in `diagnostic_runtime.h`).
//
// At ABI 14 this translation unit is empty by design — the
// runtime archive at this ABI exports no DV symbols.

#include "diagnostic_runtime.h"
