# JSON API Refactor Work Progress

## Goal

Improve user-facing JSON construction/mutation ergonomics with compile-time shape-safe APIs, while preserving `JsonNode` as the generic parsed/transport type.

## Problem Statement

Current constructors return `JsonNode`:
- `JsonNode::new_array() -> JsonNode`
- `JsonNode::new_object() -> JsonNode`

This forces shape-dependent runtime methods on one polymorphic type (`array_push`, `object_set`, etc.), which is safe but less ergonomic and easier to misuse.

## Pinned Direction

Introduce explicit shape wrappers:
- `JsonArray`
- `JsonObject`

And keep `JsonNode` as the common sum type.

### Construction

- `json.new_array() -> JsonArray`
- `json.new_object() -> JsonObject`

### Conversion boundaries

- `JsonArray.to_node() -> JsonNode`
- `JsonObject.to_node() -> JsonNode`
- `JsonNode.as_array() -> Optional<JsonArray>`
- `JsonNode.as_object() -> Optional<JsonObject>`
- strict `expect_array/expect_object` variants may throw `JsonError`.

### Shape-specific mutation API

- `JsonArray.push(value: JsonNode)`
- `JsonArray.get(index: Int) -> Optional<&JsonNode>` (or pinned alternative)
- `JsonArray.len() -> Int`
- `JsonObject.set(key: String, value: JsonNode)`
- `JsonObject.get(key: String) -> Optional<&JsonNode>` (or pinned alternative)
- iteration helpers over entries/items.

### `JsonNode` role

- Keep parse/encode/path and generic inspectors on `JsonNode`.
- Remove/avoid new shape-mutation entrypoints on `JsonNode` where wrappers cover them.

## Compatibility Strategy

1. Add wrappers and new APIs first.
2. Migrate std/examples/effective-drift to wrapper-first style.
3. Remove legacy `JsonNode` shape-mutation helper entrypoints (done).

## Current Status

### Landed

1. Wrapper types added in `stdlib/std/json/json.drift`:
   - `JsonArray`
   - `JsonObject`
2. Wrapper-first constructors added:
   - `new_array() -> JsonArray`
   - `new_object() -> JsonObject`
3. Wrapper APIs added:
   - `JsonArray.len/push/get/to_node`
   - `JsonObject.len/set/get/to_node`
4. Conversion from `JsonNode` to wrappers added:
   - `JsonNode.into_array() -> Optional<JsonArray>`
   - `JsonNode.into_object() -> Optional<JsonObject>`
5. Regression test added and passing:
   - `lang/tests/codegen/e2e/std_json_wrapper_build_encode`
6. Dependency detour completed (LANGUAGE_BUG + container throw-contract tightening):
   - pinned regression: `lang/tests/driver/test_equatable_nothrow_ssa_return_regression.py`
   - checker fix in `lang/driftc/checker/__init__.py` for `BinaryOpInstr` bool-result typing
   - `Equatable`/`Comparable` trait methods made `nothrow` in `stdlib/std/core/cmp.drift`
   - `HashMap`/`HashSet` surfaces tightened where valid so wrapper object APIs can stay `nothrow`
7. Test hygiene cleanup for file-writing suites:
   - moved fixed-path std.io test artifacts from repo-root filenames to `/tmp/drift_*` paths in affected e2e + driver tests.
8. Underscore semantics cleanup:
   - removed underscore-prefixed special-case borrow-liveness behavior in borrow checker (`_name` no longer treated differently from regular names).
   - added regression: `lang/tests/borrow_checker/test_regions.py::test_unused_underscore_borrow_same_block_still_blocks_write`.
9. Match-pattern regression pin:
   - added e2e regression `lang/tests/codegen/e2e/match_result_err_underscore_expr_value` confirming `Err(_)` is accepted in expression-match arms.
10. Wrapper roundtrip test hardening:
   - stabilized ownership/borrow flows in:
     - `lang/tests/codegen/e2e/std_json_wrapper_roundtrip_to_node_into`
     - `lang/tests/codegen/e2e/std_json_parse_into_wrappers`
   - both now pass.

### Verified

- Driver:
  - `test_equatable_nothrow_ssa_return_regression`
  - `test_hash_map_smoke`
  - `test_std_json_regressions`
- e2e:
  - `std_json_wrapper_build_encode`
  - `std_json_entries_iter_behavior`
  - `std_json_parse_duplicate_as_object_get`
  - `std_json_parse_duplicate_get_only`
  - `hashmap_clear`
  - `hashmap_iter_invalidate`
  - `hashmap_jsonnode_duplicate_get_no_double_free`
  - `match_result_err_underscore_expr_value`
  - `std_json_wrapper_roundtrip_to_node_into`
  - `std_json_parse_into_wrappers`
  - `std_io_file_read_write`
  - `std_io_file_builder_read_write_api`
  - `std_io_file_builder_chunked_large`
  - `std_io_stdin_line_edge_matrix`
  - `std_io_buffer_len_updates`
  - `std_io_double_close_ok`

## Next Steps

1. Run ASAN + alloc-track validation pass for the wrapper roundtrip/new JSON cluster (in progress by user).
2. Hold docs/examples migration until memory run is complete.
3. After memory run is clean:
   - migrate top-level examples/docs to wrapper-first construction style.

## Regression-First Plan

1. Add compile-pass e2e showing shape-safe construction:
   - `new_array().push(...)`
   - `new_object().set(...)`
   - encode output correctness.
2. Add negative e2e for wrong-shape operations (if old API retained, ensure clear runtime tag).
3. Add conversion roundtrip tests:
   - wrapper -> node -> wrapper
   - parse node -> as_array/as_object.
4. Validate normal + `DRIFT_ASAN=1` + `DRIFT_ALLOC_TRACK=1`.

## Open Pins

1. Exact naming:
   - `json.new_array` vs `JsonArray::new`
   - `set/get` vs `put/get`.
2. Borrow-return shape for getters:
   - `Optional<&JsonNode>` vs by-value `Optional<JsonNode>`.
3. Iterator surface:
   - direct `for val x : arr` support once iterable traits align.

## Out of Scope

1. Full JSONPath/query language.
2. New number model changes.
3. Canonicalization/signing policy changes.
