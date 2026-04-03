# Package Consumer Open Defects

## Status: active tracking
## Date: 2026-04-03
## Baseline: mainline (current main), not CI-gated

All 20 defects reproduce on current main with the same test selection.
Goal: fix each, then move into the CI-gated subset.

---

## Category 1: ArrayRange/DequeRange method resolution (16 tests)

**Root cause**: `ArrayRange<T>` and `DequeRange<T>` methods (`len`, `compare_at`, `swap`)
are not resolved by the package-consumer type checker. The error pattern is:
`no matching method 'len' for receiver Ref<std::std.containers.ArrayRange<Int>>`

**Likely mechanism**: These are generic struct types with methods defined
via `implement<T>` blocks. The method resolution for generic impl methods
on range types may fail because the impl target type (`ArrayRange<T>`)
isn't being matched correctly in the consumer's callable_registry.

**Tests**:
1. algo_sort_random
2. algo_sort_reverse
3. algo_sort_sorted
4. algo_sort_duplicates
5. algo_binary_search_basic
6. algo_binary_search_duplicates
7. array_range_compare_at_bounds
8. array_range_compare_at_invalidated
9. array_range_len_invalidated
10. array_range_reserve_noop_invalidates
11. array_range_swap_invalidated
12. array_range_swap_twice
13. array_sort_binary_search_after_growth
14. deque_range_compare_at_invalidated
15. deque_range_pop_back_noop_no_invalidate
16. deque_range_pop_front_noop_no_invalidate

**Root cause found**: The `impl_target_type_id` on trait impl method
signatures (e.g., `ArrayRange<Int>::len`) points to the generic BASE
(`ArrayRange`) instead of the concrete instantiation (`ArrayRange<Int>`).
The `typeid_to_type_expr` in the payload serializer produces
`{"name": "ArrayRange"}` (no type args). The consumer's
`resolve_opaque_type` returns UNKNOWN because it can't resolve the
generic base without args.

**Fix direction**: The producer's `impl_target_type_id` for concrete
trait impls should be the TypeId of `ArrayRange<Int>` (the instantiation),
not `ArrayRange` (the base). This is in the checker/impl registration
that populates `impl_target_type_id` on FnSignature objects.

---

## Category 2: Generic wrapper instantiation in lambda callbacks (4 tests)

**Root cause**: Lambda callback bodies reference `__wrap_method::Arc<T>::borrow__inst__`
and `__wrap_method::Cell<T>::get__inst__` — generic wrapper instantiations
that exist in the fn_infos but whose MIR bodies are not synthesized.

**Error pattern**: `codegen contract: unknown call target
std.concurrent::__wrap_method::Arc<T>::std.core.Borrow<T>::borrow__inst__...`

**Tests**:
1. callback_move_capture_arc_lifetime
2. callback_move_capture_nested_callback
3. callback_move_capture_replace_state
4. cell_counter_fn0

**Fix direction**: The generic instantiation drain inside compile_stubbed_funcs
creates the wrapper signature and fn_info but may not emit the wrapper MIR
body for all instantiated wrappers. Investigate whether the wrapper body
synthesis at line 7086 covers generic instantiation cases, or whether the
instantiation drain needs to also synthesize wrapper bodies.

---

## Progress tracking

| Category | Total | Fixed | Remaining | Gated |
|----------|------:|------:|----------:|------:|
| ArrayRange/DequeRange | 16 | 16 | 0 | Yes (test_pkg_trait_impl_target_type) |
| Generic wrapper lambda | 4 | 4 | 0 | Yes (test_pkg_generic_wrapper_lambda) |
| **Total** | **20** | **20** | **0** | **Yes** |

### Fix for Category 2 (generic wrapper lambda)

**Root cause**: Two bugs in compile_stubbed_funcs:

1. **Missing drain after hidden lambda processing** (`driftc.py:~6911`):
   Hidden lambda type-checking creates generic instantiation requests
   (e.g., wrapper for `Cell<T>::get`) that were never drained. Added
   `_drain_instantiations()` + late wrapper MIR synthesis after the
   hidden lambda processing loop.

2. **Wrapper instantiation's `wraps_target_fn_id` pointed to generic base**
   (`driftc.py:~4881`): When the generic drain instantiates a wrapper,
   `wraps_target_fn_id` inherited the template's base FunctionId
   (e.g., `Cell<T>::get`) instead of the instantiated target
   (e.g., `Cell<T>::get__inst__...`). Fixed by looking up the target
   instantiation in `inst_cache` using the target's template key +
   same type_args.

**Regression**: `test_pkg_generic_wrapper_lambda.py::test_cell_get_in_lambda`
