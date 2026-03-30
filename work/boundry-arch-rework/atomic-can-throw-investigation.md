# atomic can_throw investigation

## Status: post-checker can_throw flip — mutation site not yet found

## What is known

1. Checker's free function resolution at call_resolver.py:5659 correctly
   sets call_can_throw=False for atomic_uint and atomic_store_uint
2. Alignment code at driftc.py:5168 sees orig_ct=True for calls from
   user main() to lang.atomic functions
3. Same calls from std.sync.epoch_domain show orig_ct=False (correct)
4. The flip happens AFTER record_call_info but BEFORE the alignment reads
   call_info_by_callsite_id

## What is NOT the cause

- FunctionId mismatch in signatures_by_id (keys match correctly)
- MIR lowering _can_throw_by_id default (typed mode doesn't use it)
- Parser rewriting (confirmed HCall(fn=HVar) after rewrite)
- Method-call boundary upgrade (these are free function calls)
- Package-name special cases (wrong fix direction)

## Next step

Trace one specific callsite_id end-to-end through:
1. checker record_call_info (where False is recorded)
2. post-checker alignment/normalization in compile_stubbed_funcs
3. MIR lowering read of call_info_by_callsite_id

The exact breakpoint is: the line where CallInfo.sig.can_throw changes
from False to True (or where one CallInfo is replaced by another).

## Repro

```
module main;
import lang.atomic as atomic;
struct S { a: atomic.AtomicUint }
pub fn main() nothrow -> Int {
    var s = S(a = atomic.atomic_uint(cast<Uint>(0)));
    atomic.atomic_store_uint(&s.a, cast<Uint>(1), 0);
    return 0;
}
```

Run via compile_to_llvm_ir_for_tests (no semantic_world, no pass1_state).
Fails with: atomic_store_uint returns Void; result cannot be captured

## Files involved

- lang/driftc/checker/call_resolver.py:5655-5664 — free fn can_throw
- lang/driftc/checker/call_resolver.py:2978-2996 — method boundary upgrade
- lang/driftc/driftc.py:5160-5197 — post-checker alignment
- lang/driftc/stage2/hir_to_mir.py:5241 — MIR statement call can_throw
- lang/driftc/stage2/hir_to_mir.py:2720 — MIR expression call can_throw
