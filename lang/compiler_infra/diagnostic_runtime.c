// Slice 7c-1 (ABI 14, 2026-05-06): all `drift_dv_*`,
// `drift_dv_object_from_entries`, `drift_dv_get`, `drift_dv_index`,
// `drift_dv_kind`, `drift_dv_as_*`, `drift_dv_get_field`,
// `drift_dv_len`, `drift_dv_entries`, `drift_dv_clone`,
// `drift_dv_release`, and the legacy `drift_diag_from_*` aliases
// are deleted.  No production lowering or runtime path references
// these helpers post-Slice 7b's migration of the throw-side params
// projection to `core.Diagnostic.to_json_text` + `ExcSetParamsJson`,
// and the captured-locals projection to direct per-scalar
// `core.diagnostic_json_*` dispatch.
//
// The `DriftDiagnosticValue` / `DriftDiagnosticEntry` /
// `DriftDiagnosticField` struct types remain declared in
// `diagnostic_runtime.h` for transitive includers but carry no
// callable surface at ABI 14.  Slice 7c-2 retires the struct
// types alongside the compiler-side `H.HDVInit` /
// `M.ConstructDV` / `TypeKind.DIAGNOSTICVALUE` cleanup.
//
// At ABI 14 this translation unit is empty by design — the
// runtime archive at this ABI exports no DV symbols.

#include "diagnostic_runtime.h"
