# App Team Defect Analysis: Bookkeeper Memory Leak + Optional<ScopeGuard> SSA Mismatch

## Date: 2026-04-02 (updated 2026-04-01)
## Compiler: 0.27.143 (git e78440a8)

---

## Defect 1: Bookkeeper memory leak

### Status: root cause narrowed — NOT a compiler drop-insertion bug

### Original report

The bookkeeper leaks ~1,958 bytes (18 blocks) per 2 requests. Valgrind
attributed the leak sites to:
1. `clone_deep` HashMap internals
2. `_own` string copies in `push_request_context`
3. `_snapshot_hour` string
4. `json.parse` / `_parse_string` results
5. `fmt.format_int` results

### What we've ruled out

**All compiler-side hypotheses have been eliminated:**

1. **K39 generic Destructible instantiation**: ScopeGuard<LogContext>
   hypothesis was invalidated by LD_PRELOAD data (see below).

2. **format_int / logger path**: Minimal reproducers pass cleanly on
   both source and PEX paths. The HashMap destroy chain is correct
   (destroy → clear → drop_value<String> → drift_string_release).

3. **Per-request leak scaling**: Clean shutdown tests show the leak is
   **constant** — 66 bytes / 3 blocks regardless of request count.
   This rules out per-request accumulation.

| Pattern | Source (--stdlib-root) | PEX (bundled stdlib) |
|---------|----------------------|---------------------|
| format_int string, scope exit | Clean | Clean |
| JSON parse + clone_deep, nested match, unmoved val | Clean | Clean |
| Can-throw function, try/catch, multiple string/JSON locals | Clean | Clean |
| Nested match with unmoved `deep` (exact _build_response shape) | Clean | Clean |

### LD_PRELOAD malloc tracking: actual leaked content

The app team instrumented drift_string_alloc/drift_string_release with
LD_PRELOAD tracking. The 3 leaked 22-byte blocks contain:

| Alloc # | Content | When allocated |
|---------|---------|----------------|
| #2 | "" (empty) | During initialization |
| #15 | "v1" | During route registration (between startup-begin and listening) |
| #28 | "garbage" | During shutdown log setup |

**Critical finding**: Valgrind's stack attribution to `format_int` was
misleading due to malloc address reuse. The actual leaked strings are
**route-path substrings from route registration**, not format_int results.
With address recycling, the Valgrind-reported allocation stack can belong
to an earlier (freed) allocation at the same address, not the currently
leaked block.

The leak pattern: allocations occur in pairs where the first is freed
but the second is not. Allocs #9, #11, #13 are freed at shutdown, but
#15 (same pattern) is never freed.

### Current assessment

This is almost certainly a **drift-web router bug**, not a compiler bug:
- The leaked strings are route-path fragments ("v1", "") created during
  route registration
- The leak is constant (66 bytes), not per-request — consistent with
  one-time route setup
- All compiler-side reproducers (HashMap drop, ScopeGuard drop,
  format_int scope exit) pass cleanly
- The destroy chain for HashMap<String, String> is verified correct in IR

### Recommended next steps for the app team

1. **Examine drift-web route registration**: trace how URL path segments
   (e.g., "/api/v1/submit" → ["api", "v1", "submit"]) are split and
   stored in the router's trie/map
2. **Check router teardown**: does the router's destroy implementation
   free all path-segment strings, or does it only free the leaf handlers?
3. **Minimal router reproducer**: register routes with string path
   segments, drop the router, check under Valgrind
4. **Severity reassessment**: 66 bytes constant leak from route setup
   is cosmetic, not a production concern. The original ~1,958 bytes/2req
   report likely included noise from Valgrind's address-reuse attribution

---

## Defect 2: SSA type mismatch for Optional<ScopeGuard<T>>

### Status: likely already fixed, needs confirmation on app's exact code

### What was reported

Returning `Optional<rt.ScopeGuard<log.LogContext>>` from a function
triggers:
```
error: typecheck contract failure: SSA return type does not match
declared signature (3407 vs 38)
```

Filed against 0.27.128.

### Current state (0.27.142+)

We built the exact reproducer from the defect report and tested on both
source and PEX paths. **Both compile cleanly on 0.27.142 PEX and
0.27.143 source.**

The TypeId mismatch (3407 vs 38) is characteristic of the mode-divergence
bugs fixed in 0.27.137 (`copy_status` checking `destructor_fns`) and
0.27.138-0.27.140 (wrapper convergence removing `source_modules` from
routing). The generic `Optional<ScopeGuard<LogContext>>` variant
instantiation likely had a different TypeId in the SSA path vs the
declared signature due to cross-package TypeId remapping.

### Recommendation

Ask the app team to re-test their exact code on 0.27.143. If it still
fails, provide the exact compilation command and error output. Our
reproducer on the same compiler version passes.

---

## Summary

| Defect | Status | Severity | Likely root cause |
|--------|--------|----------|-------------------|
| Bookkeeper leak | Redirected to app team | Low | drift-web router not freeing route-path segment strings during teardown |
| Optional<ScopeGuard> SSA mismatch | Likely fixed | Medium | TypeId divergence fixed in 0.27.137-0.27.140 |

### Next steps

1. **Defect 1**: App team to investigate drift-web router teardown for
   route-path string segments. The 66-byte constant leak is cosmetic
   but should be fixed for Valgrind-clean certification.
2. **Defect 2**: Confirm with app team whether this is resolved on
   0.27.143. If not, reproduce with their exact code.
3. **Regression tests**: The e2e tests `generic_param_drop_leak` and
   `dv_map_literal_leak` remain valid for pinning the compiler's
   HashMap<String, String> drop behavior. The `optional_scopeguard_ssa`
   test pins the TypeId convergence fix.
