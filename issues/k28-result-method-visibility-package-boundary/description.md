# K28: Result method visibility through package boundary

## Status
Open — pre-existing, discovered during K27 investigation.

## Symptom
Consumer imports a package that returns `Result<Int, ProducerError>`.
Calling `(move r).or_throw()` fails with:
```
method 'or_throw' exists but is not visible here
```

This also affects any inherent `Result` method (`on_error`, `is_ok`, etc.)
when the `Result` instance's TypeId originates from a package return type.

## Root cause (suspected)
The pre-linked type table imports `Result` from the package's type table,
creating a linked `Result` base TypeId (e.g. 230).  Later, `_seed_builtin_types`
/ source compilation creates a separate `Result` base TypeId (e.g. 12).

`or_throw` is registered in the callable registry with the source-compiled
Result base TypeId.  But the receiver `Result<Int, ProducerError>` from the
package return type uses the package-linked Result base TypeId.  The callable
registry lookup misses because the base TypeIds differ.

The method IS found by the hidden-candidate scan (which iterates all entries),
but then `_is_prelude_type_method` or `_candidate_visible` fails, producing
"exists but is not visible".

## Suspected subsystem
- `lang/driftc/packages/type_table_link_v0.py` — type table linking
- `lang/driftc/parser/__init__.py` — `_seed_builtin_types` interaction
- Same class of issue as the Void TypeId duplication fixed in K27

## Repro
```
lang/tests/driver/test_external_consumer.py::test_ext_cross_package_or_throw
```
Currently marked `xfail(strict=True)`.

## Impact
The web team's reported `.or_throw()` failure is not fully resolved until
this is fixed.  K27 fixed the Throw impl visibility (terminal-throws
signature normalization), but the Result method visibility is a separate
gap in the same pipeline.

## Scope
This is a package-consumer type-table linking issue, not a runtime ABI
change.  Fix should require a compiler version bump only.
