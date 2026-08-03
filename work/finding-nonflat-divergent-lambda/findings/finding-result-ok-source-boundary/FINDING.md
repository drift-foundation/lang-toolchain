# Child finding: unqualified `Ok(...)` crosses inconsistent source boundaries

Date filed: 2026-08-03

Parent: `work/finding-nonflat-divergent-lambda/`

Discovery context: the full-suite failure in the parent's R4.4 return-contract
migration. The failed Phase 5 test claimed `HResultOk.value` source coverage;
probing the intended local-expression form exposed an LLVM payload mismatch.

Status: queued reviewer research. `PROGRESS.md` is implementer-owned and is
intentionally not created or edited here.

## Classification

**Observed LANGUAGE_BUG:** a source program containing local
`val r = Ok(a)` passes checking but crashes during LLVM generation with a
`ConstructResultOk` ok-payload type mismatch. A user program must either be
accepted through all stages or rejected upstream with a source diagnostic.

This is not fixed by the parent's R4.4 clean rejection of `return Ok(a)`;
placing the same `HResultOk` in a local bypasses return compatibility and
reaches the inconsistent downstream typing.

## Minimal observed repro

See `repro_const_share_result_ok_local.drift`. The important body is:

```drift
fn make_ok(a: core.ConstArc<String>) throws -> Int {
	val r = Ok(a);
	val n = use_arc(a);
	return n - 2;
}
```

The second use of `a` also makes the Phase 5 payload-duplication contract
load-bearing. On the 2026-08-03 tree, type checking succeeds and LLVM codegen
raises:

```text
ok payload type mismatch for ConstructResultOk in repro::make_ok:
have ConstArc, expected Int
```

## Confirmed producer/consumer disagreement

- `stage1/ast_to_hir.py:425-429` unconditionally recognizes every unqualified,
  one-argument source `Ok(expr)` as `HResultOk`, before normal constructor
  resolution has contextual type information.
- `type_checker.py:12009-12012` types `HResultOk(value)` as
  `FnResult<payload, Unknown>`.
- phase-2 `checker/__init__.py:2184-2191` instead infers every `HResultOk` as
  the enclosing function's declared user return type when one exists.
- `stage2/hir_to_mir.py:8538-8548` lowers the node to
  `ConstructResultOk(value=payload)`.
- `stage2/hir_to_mir.py:9733-9783` independently wraps every normal return of a
  can-throw surface function in `ConstructResultOk`.

Those policies explain both observed shapes:

- direct `return Ok(a)` would double-wrap and is now rejected by the parent's
  shared return authority;
- local `val r = Ok(a)` reaches MIR/codegen with incompatible opinions about
  the ok payload type and ICEs.

## Spec evidence and uncertainty

**Observed:** `doc/design/drift-lang-spec.md` §10.3 defines `Ok(...)` as an
unqualified constructor for the public `Result<T, E>` variant. `FnResult` is a
reserved internal type name, and no reviewed spec text was found granting
unqualified `Ok(...)` a second meaning as an internal throwing-ABI constructor.

**Inferred, not authoritative:** the unconditional AST-to-HIR rewrite may be a
legacy/internal test seam that escaped into source lowering. A likely clean
contract is that source `Ok(...)` goes through ordinary contextual variant
constructor resolution, while throwing-function success wrapping remains
implicit at `return`. `HResultOk` could then become strictly compiler-internal
or be removed if it has no production consumer.

The implementer must verify this against parser/resolver history and all
current producers. Do not silently change the spec. If evidence supports a
different source meaning, stop and request Slawomir's explicit ruling before
editing spec or broadening acceptance.

## Inventory signal

A 2026-08-03 source scan found essentially no real unqualified `Ok(...)`
expression producers outside the two tests in this return-authority work;
production Drift code overwhelmingly uses qualified `core.Result::Ok(...)`.
Pattern arms named `Ok(...)` are a separate syntax and are not evidence for the
expression rewrite. Re-run a precise AST-oriented inventory before deletion or
migration; raw text counts are noisy because they include patterns/comments.

## Proposed patch directions

These are alternatives to validate, not instructions:

1. **Spec-aligned source separation (preferred hypothesis):** delete the
   unconditional source `Ok(...) -> HResultOk` rewrite and allow the normal
   contextual Result constructor resolver to own source `Ok`. Keep or delete
   `HResultOk` based on real internal producers. Update contradictory tests and
   history descriptions in the same clean break.
2. **First-class internal success expression:** if source `Ok(...)` is proven to
   be an approved throwing-ABI construct, centralize its type as the enclosing
   function's user payload and teach return/lowering not to double-wrap it.
   This expands the language contract and requires explicit spec evidence or
   Slawomir approval, plus end-to-end ownership/drop coverage.
3. **Upstream rejection:** if local/internal `Ok(...)` is intentionally invalid,
   reject every non-return occurrence before MIR and retain the clean direct-
   return negative. This still leaves the public spec's unqualified Result
   constructor promise to reconcile through the normal variant resolver.

Do not implement a local codegen cast or weaken the LLVM assertion. The mismatch
originates in earlier semantic/type disagreement.

## Acceptance criteria

- The repro never reaches a traceback/ICE.
- A positive public `Result<T, E>::Ok` construction works end-to-end using the
  current spec spelling (including unqualified contextual `Ok` if that contract
  is confirmed).
- Unsupported internal-FnResult construction is rejected clearly upstream, or
  supported construction compiles/runs with correct payload ownership.
- Direct `return Ok(value)` in a throwing `-> T` function follows one explicit
  contract and is not double-wrapped.
- Phase 5 ConstShare behavior for any surviving owned payload slot is pinned
  structurally and, if source-reachable, by full compile/run with reuse/drop.
- Negative coverage distinguishes public `Result` construction from internal
  throwing ABI construction.
- No contradictory source comments, history claims, or tests remain.

## Version/ABI/spec notes

- No language-spec edit is authorized.
- Any source acceptance/rejection or diagnostic change is user-visible and
  falls under the compiler SemVer minor rule; evaluate against the branch's
  already-selected version before staging.
- A source-routing/typecheck/lowering fix should not require runtime ABI change
  unless it changes the actual compiler/runtime FnResult layout or call
  convention. Re-evaluate after the patch shape is known.

## Refactor-trigger scan

`doc/refactor_triggers.md` was scanned on 2026-08-03. No registered trigger
clearly matches Result/FnResult source-constructor separation. Re-scan when the
implementer starts the LANGUAGE_BUG.
