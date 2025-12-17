# TODO

[String]
- Remaining surface/runtime work:
  - Expose/route any user-facing string printing helper once available.
  - Keep expanding test coverage as features land (print, more negative cases).

[Error handling]
- Deferred follow-ups:
  - Captures (`^`): implement unwind-time frame capture (locals per frame + frames list), runtime representation, lowering/codegen, and e2e tests.
  - DiagnosticValue payloads: design/implement a stable ownership/handle model for opaque/object/array payload kinds so they can be stored in `Error.attrs` without ABI/lifetime churn.
  - Try-expression restriction: decide whether to relax “attempt must be a call” beyond call/method-call in v1 (spec + checker + lowering).

[Borrow / references]
- Deferred follow-ups:
  - Escape sink #3 (closures/unknown contexts): once closures exist, enforce “borrowing closures are non-escaping only” and reject passing `&`/`&mut` captures (or ref-typed params) to unknown call sites; add e2e negatives.
  - Place model expansion: extend `HPlaceExpr.base` beyond locals/params once globals/captures land (so borrow/move/assign can target them).
  - NLL-lite: replace lexical borrow lifetimes with loan liveness ending at last use (CFG-based), so “borrow then write after last use” stops being rejected.
  - Autoref/autoderef: decide whether to introduce limited autoderef for method calls / operators (currently explicit `*p` / `(*p).field`).
  - Extra overlap tests as syntax grows: deepen projection overlap coverage once new projection forms are added.

[Iteration]
- Deferred follow-ups:
  - Variant constructor collisions: improve diagnostics/UX when unqualified constructors are ambiguous (list candidates; add coverage).
  - Named-field construction/patterns for variants: support `Some(value = x)` and matching on named fields once the surface design is pinned.
  - Replace iterator intrinsics with real modules/traits: migrate `Array<T>.iter()` / `__ArrayIter_<T>.next()` from compiler intrinsics to a real `Iterator` trait + library implementation when module support lands.
