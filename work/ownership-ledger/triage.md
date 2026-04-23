# Phase 3A Task #5 Triage

Source: `work/ownership-ledger/triage-raw/*.log`
Cases producing records: 442
Total records (incl. duplicates across cases): 518176
Total UNIQUE decisions (deduped by site+fn+point+local): 9757

## Bucket counts (unique decisions)

| # | Bucket | Count | Notes |
|---|---|---|---|
| 1 | per_field_gap | 360 | Defer to 3B (per-field tracking) |
| 2 | droppolicy_approximation | 540 | Quarantined — 3B must NOT consume `has_drop` |
| 3 | path_dependent | 28 | Direct input to 3C design |
| 4 | semantic_equivalent | 41 | Tolerated (Tombstoned drop = no-op) |
| 5 | implicit_return_move_gap | 15 | Missing MIR ownership edge for LoadLocal+Return; 3B/3C input, NOT a site leak |
| 6 | real_disagreement | **0** | Gate-blocking: must be empty before 3B |
|   | (agree)          | 8773 | — |

## Gate verdict: ✅ Bucket 6 is empty — 3A→3B verdict-disagreement gate is satisfied.

## Samples — `per_field_gap`

```json
{"classification": "ledger_stricter", "fn_name": "std.cli::_slice_string", "ledger_verdict": "must_drop", "local": "__match_scrut_tmpt77", "program_point": ["match_arm_1", 9], "raw_state": "live", "site": "match_cleanup", "site_reason": "field_moved", "site_verdict": "must_not_drop"}
{"classification": "ledger_stricter", "fn_name": "std.cli::ArgParser::parse", "ledger_verdict": "must_drop", "local": "__match_scrut_tmpt9", "program_point": ["match_arm_0", 9], "raw_state": "live", "site": "match_cleanup", "site_reason": "field_moved", "site_verdict": "must_not_drop"}
{"classification": "ledger_stricter", "fn_name": "std.cli::ArgParser::parse", "ledger_verdict": "must_drop", "local": "__match_scrut_tmpt362", "program_point": ["match_arm_11", 9], "raw_state": "live", "site": "match_cleanup", "site_reason": "field_moved", "site_verdict": "must_not_drop"}
{"classification": "ledger_stricter", "fn_name": "std.cli::ArgParser::parse", "ledger_verdict": "must_drop", "local": "__match_scrut_tmpt663", "program_point": ["match_arm_12", 9], "raw_state": "live", "site": "match_cleanup", "site_reason": "field_moved", "site_verdict": "must_not_drop"}
{"classification": "ledger_stricter", "fn_name": "std.cli::ParsedArgs::get_int", "ledger_verdict": "must_drop", "local": "__match_scrut_tmpt50", "program_point": ["match_arm_11", 9], "raw_state": "live", "site": "match_cleanup", "site_reason": "field_moved", "site_verdict": "must_not_drop"}
```

## Samples — `droppolicy_approximation`

```json
{"classification": "site_stricter", "fn_name": "lang.atomic::_order_code", "ledger_verdict": "must_not_drop", "local": "__match_scrut_tmpt14", "program_point": ["match_arm_0", 2], "raw_state": "live", "site": "match_cleanup", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "site_stricter", "fn_name": "lang.atomic::_order_code", "ledger_verdict": "must_not_drop", "local": "__match_scrut_tmpt18", "program_point": ["match_arm_1", 2], "raw_state": "live", "site": "match_cleanup", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "site_stricter", "fn_name": "lang.atomic::_order_code", "ledger_verdict": "must_not_drop", "local": "__match_scrut_tmpt22", "program_point": ["match_arm_2", 2], "raw_state": "live", "site": "match_cleanup", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "site_stricter", "fn_name": "lang.atomic::_order_code", "ledger_verdict": "must_not_drop", "local": "__match_scrut_tmpt26", "program_point": ["match_arm_3", 2], "raw_state": "live", "site": "match_cleanup", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "site_stricter", "fn_name": "lang.atomic::_order_code", "ledger_verdict": "must_not_drop", "local": "__match_scrut_tmpt30", "program_point": ["match_arm_4", 2], "raw_state": "live", "site": "match_cleanup", "site_reason": "needs_drop", "site_verdict": "must_drop"}
```

## Samples — `path_dependent`

```json
{"classification": "path_dependent", "fn_name": "main", "ledger_verdict": "path_dependent", "local": "popped", "program_point": ["if_then", 1], "raw_state": "maybe_uninit", "site": "scope_drop", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "path_dependent", "fn_name": "main", "ledger_verdict": "path_dependent", "local": "popped2", "program_point": ["if_then1", 1], "raw_state": "maybe_uninit", "site": "scope_drop", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "path_dependent", "fn_name": "main", "ledger_verdict": "path_dependent", "local": "popped", "program_point": ["if_then1", 3], "raw_state": "maybe_uninit", "site": "scope_drop", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "path_dependent", "fn_name": "main", "ledger_verdict": "path_dependent", "local": "popped3", "program_point": ["match_join2", 3], "raw_state": "maybe_uninit", "site": "scope_drop", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "path_dependent", "fn_name": "main", "ledger_verdict": "path_dependent", "local": "popped2", "program_point": ["match_join2", 5], "raw_state": "maybe_uninit", "site": "scope_drop", "site_reason": "needs_drop", "site_verdict": "must_drop"}
```

## Samples — `semantic_equivalent`

```json
{"classification": "semantic_equivalent", "fn_name": "std.json::JsonObject::get", "ledger_verdict": "must_not_drop", "local": "__try_errt3", "program_point": ["tryexpr_join", 1], "raw_state": "tombstoned", "site": "string_arc_return", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "semantic_equivalent", "fn_name": "main", "ledger_verdict": "must_not_drop", "local": "__try_errt2", "program_point": ["try_cont", 1], "raw_state": "tombstoned", "site": "scope_drop", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "semantic_equivalent", "fn_name": "main", "ledger_verdict": "must_not_drop", "local": "__try_errt19", "program_point": ["tern_join", 3], "raw_state": "tombstoned", "site": "string_arc_return", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "semantic_equivalent", "fn_name": "main", "ledger_verdict": "must_not_drop", "local": "__try_errt19", "program_point": ["idx_join3", 3], "raw_state": "tombstoned", "site": "scope_drop", "site_reason": "needs_drop", "site_verdict": "must_drop"}
{"classification": "semantic_equivalent", "fn_name": "main", "ledger_verdict": "must_not_drop", "local": "__try_errt21", "program_point": ["idx_join4", 5], "raw_state": "tombstoned", "site": "string_arc_return", "site_reason": "needs_drop", "site_verdict": "must_drop"}
```

## Samples — `implicit_return_move_gap`

```json
{"classification": "ledger_stricter", "fn_name": "std.json::_parse_array", "ledger_verdict": "must_drop", "local": "values", "program_point": ["match_arm_0", 11], "raw_state": "live", "site": "scope_drop", "site_reason": "moved", "site_verdict": "must_not_drop"}
{"classification": "ledger_stricter", "fn_name": "std.json::_parse_array", "ledger_verdict": "must_drop", "local": "values", "program_point": ["if_then2", 6], "raw_state": "live", "site": "scope_drop", "site_reason": "moved", "site_verdict": "must_not_drop"}
{"classification": "ledger_stricter", "fn_name": "std.json::_parse_array", "ledger_verdict": "must_drop", "local": "values", "program_point": ["if_join4", 6], "raw_state": "live", "site": "scope_drop", "site_reason": "moved", "site_verdict": "must_not_drop"}
{"classification": "ledger_stricter", "fn_name": "std.json::_parse_object_throwing", "ledger_verdict": "must_drop", "local": "fields", "program_point": ["match_arm_0", 11], "raw_state": "live", "site": "scope_drop", "site_reason": "moved", "site_verdict": "must_not_drop"}
{"classification": "ledger_stricter", "fn_name": "std.json::_parse_object_throwing", "ledger_verdict": "must_drop", "local": "fields", "program_point": ["if_then2", 6], "raw_state": "live", "site": "scope_drop", "site_reason": "moved", "site_verdict": "must_not_drop"}
```

