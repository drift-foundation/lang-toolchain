# Plan v3: reject source-written borrows at declared `&T` / `&mut T` parameters

Status: RESEARCH + REVISED PLAN — nothing implemented. Awaiting explicit up/down vote.
Date: 2026-07-28. Toolchain at time of research: 0.33.90 / ABI 22.
Artifacts: `probes/` (17 pinned behavior probes + results), `recount/` (site-level dataset
+ method + R2 scan script). Every "current behavior" claim below cites a probe.

Revision history:
- v1: narrowed the rule, voted on the narrowed version — rejected in review.
- v2: original rule restored, scoped recount, conditional up-vote — blocked on five
  findings: (1) row 19 silently kept `&Concrete→&Interface` borrows legal without calling
  it a decision; (2) thin function pointers (`Fn(&String) -> R`) missing entirely;
  (3) enforcement not centralized across call families; (4) R2 missed `&T` vs `&mut T`
  sets; (5) package provenance story not implementable as written.
- v3 (this document): all five addressed with new probes and scans — D7 (interface-view
  borrows), D8 (function pointers, incl. a newly found **latent miscompile** on the bare
  fn-pointer path), W0 (single shared enforcement policy), R2 reformulated as mode
  erasure and verified against the repo (0 additional pairs), D9 (package policy:
  provenance stripped at emission). `&mut` rvalues counted exactly (**2**, both in the
  pinning test). Totals relabeled as estimates.

---

## 1. The rule

When a function or method parameter is **syntactically declared** `&T` or `&mut T`, a
source-written borrow expression in that argument position is redundant and rejected:

```drift
fn read(arg: &String);
fn edit(arg: &mut String);

read(name);        // valid: parameter-directed auto-borrow
read(&name);       // error: redundant borrow for parameter `arg: &String`
edit(buffer);      // valid
edit(&mut buffer); // error
read(&"alice");    // error — rvalues in scope (the motivating example, decision D1)
read("alice");     // valid: auto-borrow materializes the temporary (probe e5)
```

**Redundancy criterion (precise):** a source-written borrow argument is redundant iff
deleting it yields a well-typed call with the same resolution — i.e. the operand is a
place of the parameter's inner type with compatible mutability, or an already-`&T` value
re-borrowed. A borrow whose deletion changes typing is NOT redundant; whether any such
borrows remain legal is decision D7 (interface views) — after W2-W5 land, D7's case is
the only known survivor of that test.

Diagnostic: `E_REDUNDANT_ARG_BORROW`, phase typecheck, span = the borrow expression:
`redundant borrow for parameter 'arg: &String'; pass 'name' directly` (operand text via
source-span slicing; fallback to the base variable name).

In scope: direct free/qualified/method calls, interface-dispatched calls (after W2),
`std.mem` intrinsics (after W3), and immediately-invoked lambdas (after W5).
Fn-pointer invokes are EXEMPT per resolved D8(b) — both spellings legal there. Out of scope by the rule's own terms:
- **Generic-by-value formals**: a parameter declared as a bare typevar `T`, even when
  instantiated at a reference — `identity<type &String>(&name)` stays legal. This covers
  `FnN`/`CallbackN.call(a: A)` (formal is `A`), i.e. static/dynamic callables. It does
  NOT cover thin fn pointers, whose signature spells the `&` — see D8. Conversely,
  `&`-over-typevar formals (`mem.replace<T>(ptr: &mut T, …)`) ARE in scope.
- **Constructor fields** (struct/variant/exception, positional or named): not function
  parameters. `Foo(f = &s)`, `Optional<&T>::Some(&x)` unaffected.
- **Non-argument positions**: `val r = &x`, returns, `match &x`, `for x in &v`,
  `captures(&x)`, assignment RHS. **Receiver position** (`obj.method()`,
  `(&obj).method()`).
- **Reference-typed values passed bare**: `val r: &String = &name; read(r)`.

## 2. Verified current behavior

All probes in `probes/` with per-probe results in `probes/README.md`. Summary:

| Fact | Probe |
|---|---|
| Bare literal/rvalue at `&T` compiles and runs | e5 |
| Explicit-vs-bare rvalue temp: **identical scope-end drop timing** | e6a/e6b |
| Bare rvalue at `&mut T` rejected ("bind to a local first") | e5b |
| Interface dispatch has no auto-borrow (bare arg → "argument 1 type mismatch") | e4 |
| Bare concrete at declared `&Interface` param → "no matching overload" (no auto-borrow+widen; `&f` is today's only spelling) | e9 |
| `mem.replace`/`mem.swap` bare fail on inference; explicit type arg doesn't help (`replace expects &mut T as the first argument`) | e1, e1c, e2 |
| Concrete `T`/`&T` method pair called bare → ambiguous (R2 forced) | e7 |
| Concrete `&T`/`&mut T` free-fn pair called bare with `var` → ambiguous (R2 mode-erasure form) | e10 |
| Mixed free-fn set `pick(&String)`+`pick<T>(T)`: ambiguous in BOTH spellings today (pre-broken; rule-neutral) | e3, e3b |
| Method mixed sets resolve bare via concrete-beats-generic | pinned fixture `method_overload_param_type_concrete_beats_generic` |
| Thin fn pointer `Fn(&String) nothrow -> Int`: explicit `f(&s)` works | e8c |
| **Thin fn pointer bare `f(s)`: type checker accepts, codegen emits ill-typed IR — clang rejects. Latent miscompile.** | e8d |

**Adjacent defect (independent of this proposal):** the fn-pointer invoke path neither
rejects nor borrows a bare `T` argument at a `&T` fn-pointer parameter — it accepts the
call and miscompiles (e8d). Zero in-repo `.drift` or embedded sources use `Fn(&…)`
types today, which is why it has gone unnoticed. Routed to the standard LANGUAGE_BUG
regression-first process, separate from this plan; D8/W-FP treat its resolution as a
precondition only.

Detectability (unchanged from earlier research): source-written borrows are
distinguishable (`HBorrow.loc` set only by `ast_to_hir`; plan adds a `source_written`
flag, precedent `for_iter_implicit_borrow`); declared-`&` vs typevar-instantiated is
decidable via template `FnSignature.param_type_ids` / `InterfaceParamSchema.type_expr`.
For fn-pointer types see D8's caveat. No ABI bump (front-end acceptance only; 0.33.83
precedent).

## 3. Scoped impact (estimate)

Parser-driven recount over 1,577 `.drift` files + extracted embedded/doc sources;
site-level dataset and method in `recount/`. Resolution is name-based (88%
high-confidence: same-unit or globally-unique), with overload-ambiguous sites resolved
toward "fires" (≤82 upward bias) and ~102 unclassifiable residuals.
**Estimated firing sites: ≈5,100 (±60).** Point classification: 5,091 parsed + 8
hand-verified legacy `tests/` sites. Measured exclusions (not assumptions): `.call(…)`
on Fn/Callback values 20, ctor slots 46, non-`&` formals 16.

| Area | Sites |
|---|---|
| `lang/tests/**.drift` (all in `codegen/e2e/`) | 2,911 |
| embedded Drift in `lang/tests/**/*.py` (145 files) | 936 |
| `stdlib/` | 782 |
| `issues/` (D3 resolved: 2 migrate — active test input; 192 archival, preserved verbatim) | 194 |
| `tools/` | 171 |
| `examples/` | 52 |
| `doc/` samples | 45 |
| legacy `tests/` | 8 |

Key splits:
- **Operand**: shared lvalue 3,166 · mutable lvalue 977 · shared rvalue 946 (853
  literals, 85 calls) · **mutable rvalue 2** — both in
  `test_borrow_rvalue_move_args.py` (`touch(&mut mk_widget(move s))`), the test that
  pins that behavior. This makes W4 a genuine choice, not forced work (§5-D1b).
- **Formal**: concrete `&T` 4,835 · declared `&`-over-typevar 256 (`mem.replace` 50,
  map `get`/`get_mut`/`remove`/`contains_key` ~111, `mem.swap` 14, …).
- **Family**: free 4,339 · concrete method 404 · assoc 56 · `std.mem` intrinsics 284 ·
  interface dispatch 4 in the raw census, of which **1 is an active migration site**
  (`repro_single_file.drift:211`; the other 3 are archival snapshots per D3) ·
  immediately-invoked lambdas 4 (one test file) · fn-pointer invokes with `&` formals
  **0** (the type is unused in-repo).
- **Interface-view upper bound (D7 sizing)**: ≤43 firing sites repo-wide have a formal
  whose inner type is named like an interface (name-collision caveat; includes the 4
  `issues/` interface-dispatch sites). Small either way D7 goes.
- **e2e fixtures**: 431 dirs contain ≥1 firing site; **415 inside the frozen
  ownership-corpus baseline universe** → one full reviewed promotion (D5).
- **Gray zone (+32, held out)**: compiler-internal builtin formals (`Array.extend` 27,
  `byte_length`/`string_*` 5) — decision D2.

## 4. Implementation architecture

**W0 — one shared declared-reference argument policy (new, per review finding 3).**
A single checker component owns, for every call family: (i) "is this formal
syntactically `&`/`&mut`-rooted" (template signature / interface schema / fn-pointer
type per D8), (ii) auto-borrow synthesis for bare place arguments, (iii) rvalue
materialization policy, and (iv) the `E_REDUNDANT_ARG_BORROW` rejection with one
diagnostic shape. Every family below is *wiring* to this policy — no family implements
its own variant. A validator assert (typed mode) checks that **every call argument has
received a policy classification** and that **no argument classified REDUNDANT was
accepted** — so checker-path drift is caught structurally while remaining consistent
with the deliberate non-rejections (D7(a) coercion borrows, D8(b) fn-pointer
exemption), which carry their own classifications.

- **W1 — provenance + core wiring.** `source_written` flag on `HBorrow` (set in
  `ast_to_hir`, preserved by the `replace()`-based rewriters, validator-asserted);
  policy applied in `_apply_autoborrow_args` (covers the equal-type arm, mismatched-
  mutability arm, and the `&&T` hidden-deref arm); template-signature REF gate;
  diagnostic + operand span-slicing. Small.
- **W2 — interface dispatch** (`call_resolver.py:2156-2213`) wired to W0 using
  `InterfaceParamSchema` declared shapes. Small-to-moderate; required for language
  correctness/uniformity — migration impact is **one active site**
  (`repro_single_file.drift:211`; the census's other 3 interface-dispatch sites are
  archival per D3). Must be validated against the runtime's `log.Sink` calling path,
  not just Drift-source call sites.
- **W3 — `std.mem` intrinsic path** (`call_resolver.py:5000-5250`) wired to W0, incl.
  element-type inference from bare places (probes e1/e1c/e2 show inference currently
  leans on the ref-typed argument) and converting `swap`'s structural
  `E_INTRINSIC_SWAP_MUT_BORROW_REQUIRED` to a type-based check (0.31.81 `replace`
  precedent). **Moderate — the largest compiler work item**; 284 sites depend on it.
- **W4 — mutable-rvalue policy** per D1b (see §5): either (a) extend bare
  materialization to `&mut T` params, or (b) keep today's "bind to a `var` first"
  rejection and migrate the 2 sites. Small either way.
- **W5 — direct-lambda call paths** wired to W0. Small; 4 sites.
- **W-FP — fn-pointer invoke path** (`HInvoke`): under resolved D8(b), no rejection
  here — W-FP reduces to giving fn-pointer invoke arguments their W0 policy
  classification (EXEMPT), keeping the validator total. The e8d miscompile fix is a
  separate LANGUAGE_BUG prerequisite (`work/fnptr-ref-arg-autoborrow-miscompile/`),
  not a work item of this plan. Trivial.
- **W6 — R2 definition-site check** (mode-erasure form, §6) + `json._encode_node`
  rename + retirement of the two `&`-as-selector tests. Small.
- **W7 — release mechanics.** `DRIFTC_VERSION` bump; ABI stays 22; `doc/history.md`
  MIGRATION section (0.33.83 template); spec + effective-drift edits (§7); package
  emission change per D9.

## 5. Decisions required before implementation

- **D1 — rvalues in scope** (shared): included per the motivating example; probes
  e5/e6 verify the bare spelling exists with identical drop timing. Implementation
  gates the 946-site rvalue sweep on A/B memcheck fixtures (§8, risk R-2).
- **D1b — mutable rvalues** (exactly 2 sites, both in the pinning test):
  **(a)** extend bare materialization (`edit(mk())` mints a mutable scope temp —
  symmetry, small new lowering surface), or **(b)** keep the "bind to a `var` first"
  rejection (zero compiler work, matches the spec's §3.5 text, preserves today's
  asymmetry, migrates 2 test sites). No recommendation forced by data; (b) is cheaper,
  (a) is cleaner language.
- **D2 — gray-zone builtin formals** (32 sites): include (recommended — users see
  `&T` params in docs and the bare form works) or exclude on the literal
  "syntactically declared" wording.
- **D3 — `issues/` snapshots: RESOLVED 2026-07-29.** No compiler/path exemption for
  `issues/`. Non-executed historical/context snapshots are preserved verbatim, even on
  superseded syntax (they are simply never compiled — the sweep skips them). Issue
  sources that are **active regression-test inputs** migrate so they stay valid on the
  current language. Verified impact: three `issues/` files are live compile inputs
  (all via `lang/tests/driver/test_drift_query_slice12_ices.py:48-50`) —
  `repro_ssa_ice.drift` and `repro_variant_ctor_diag.drift` contain 0 firing sites;
  `mir-missing-binding-id-conditional-move-ice/repro_single_file.drift` contains the
  **2 sites to migrate**. The remaining 192 archival sites stay unchanged. Standing
  rule: an archived source that later becomes an active test input must first be
  updated to current syntax.
- **D4 — R2 scope**: adopt the mode-erasure formulation (§6). Confirm.
- **D5 — test rewrites/deletions + corpus promotion require sign-off** (repository
  rule): ~431 e2e dirs edited, ~15 purpose-losing fixtures retired or repurposed
  (`borrow_string_param`, `borrow_mut_int`, `reborrow_mut_to_shared_*`,
  `test_ref_to_value_arg_coercion.py`, `test_autoborrow_diagnostics_span.py`
  expectations, `test_borrow_rvalue_move_args.py` pins), `om_*` regen (~16 emitter
  strings), and **one full reviewed ownership-corpus promotion (415 in-universe
  deltas)**. Nothing proceeds without explicit approval of the list.
- **D6 — alias-declared references** (`type Handle = &Session`): no in-repo instances;
  recommend alias transparency (resolved-REF template params count as declared), pinned
  either way.
- **D7 — interface-view borrows (new).** `drive(&f)` where `f: FileSink` and the formal
  is declared `&Sink` is a *coercion* borrow — deleting it fails today (probe e9), so it
  is not redundant under §1's criterion. Options:
  **(a)** keep coercion borrows legal (documented exception: the rule rejects only
  deletion-equivalent borrows) — zero work, but `&` survives in one argument context;
  **(b)** extend W0 with auto-borrow+widen (`Concrete lvalue → &Interface` at declared
  `&`-interface formals), then reject `&f` too — uniform outcome, moderate work that
  lands inside the open `&Concrete→&Interface` coercion-gap thread (the [2133]
  diagnostic bug and D1 borrowed-view design), which this would partially implement.
  ≤43 candidate sites repo-wide. This is a genuine policy fork the vote must settle;
  (b) is the fully uniform reading of the proposal.
- **D8 — thin function pointers: RESOLVED 2026-07-29 → option (b).** Fn-pointer
  invokes are **exempt** from R1: after the separate e8d LANGUAGE_BUG fix, both bare
  `f(s)` and explicit `f(&s)` are legal for `f: Fn(&String) nothrow -> R`. Rationale:
  R-7 is answered YES (probe e11 — fn-pointer types can carry typevars), so a
  `FUNCTION` TypeId with a REF param is not provenance-decidable (written `Fn(&String)`
  vs generic `Fn(T)` instantiated at `&String` are indistinguishable at the invoke
  site); enforcing there would knowingly violate the agreed generic-by-value exemption,
  and zero current call sites does not make permanent over-rejection acceptable. A
  future provenance-carrying function-type design may revisit uniform enforcement.
  The e8d miscompile is handled in the standard LANGUAGE_BUG regression-first process
  on its own track (`work/fnptr-ref-arg-autoborrow-miscompile/`), a prerequisite of
  this plan, not part of it.
- **D9 — package policy (new; replaces v2's incorrect "packages migrate like source").**
  Recommend **source-only enforcement with provenance stripped at emission**: canonical
  DMIR is typed/desugared with formatting metadata stripped (spec §package-format), so
  `source_written` is never encoded; decoders default it false, which also makes ALL
  pre-existing packages valid automatically (their serialized borrows decode as
  compiler-generated — no payload-version or backfill work). The rule then fires
  exactly where the `&` is written: in source compiles, including the package author's
  own build. Alternative (package-side enforcement with a payload version bump +
  backfill) buys nothing — the author's source compile already enforced the rule — and
  costs a format revision. Pinned test: encode→decode→recompile accepts a pre-rule
  package body containing explicit borrows.

## 6. R2 — overload-set restriction (mode-erasure form)

> Within one overload set, two concrete candidates whose signatures are identical after
> erasing each **non-receiver** parameter's outer mode among {`T`, `&T`, `&mut T`} are
> rejected at definition site (`E_OVERLOAD_PARAM_MODE_ONLY_DIFF`). Receiver (`self`)
> modes are untouched — receiver selection is a separate mechanism (the scan already
> excludes `self`).

Justification, all verified:
- `T` vs `&T`: bare call ambiguous (e7); with `&` banned such sets are uncallable.
- `&T` vs `&mut T` (review finding 4): bare call with a mutable lvalue ambiguous (e10) —
  and this is true *today*, independent of the rule; the ban codifies the status quo.
- Repo-wide scan (`recount/overload_mode_erasure_scan.py`): exactly **3** T-vs-& pairs
  (the known `json._encode_node` + the two selector-test fixtures) and **0** &-vs-&mut
  pairs. The ban breaks one stdlib overload (rename) and nothing else.
- Mixed concrete/generic sets need no ban: methods resolve bare via concrete-beats-
  generic (pinned fixture); free-fn mixed sets are ambiguous in both spellings today
  (e3/e3b) — pre-broken, rule-neutral. Porting the tiebreak to free fns is optional
  extension O1.

## 7. Migration and compatibility

- **In-tree sweep**: ≈4,900 sites after D3 (192 archival `issues/` sites excluded;
  the 2 active-input sites in `repro_single_file.drift` migrate), all one-token
  deletions (places and rvalues alike; bare spellings verified). Span-driven via the
  compiler's own `E_REDUNDANT_ARG_BORROW` diagnostics — never regex (`captures(&x)`,
  ctor fields, `match &x` are lookalikes).
  The 936 embedded-Python sites need an extraction-aware pass (~1/3 have escape-shifted
  line attribution).
- **Order**: the separate e8d LANGUAGE_BUG fix is a prerequisite that lands
  independently and first; then W0/W1; W2-W5 + W6 before or with the sweep (stdlib
  must keep compiling); fixture edits + `om_*` regen + corpus promotion + doc edits
  ride the same release (0.33.65 precedent for docs-in-same-commit).
- **Docs**: spec §3.6 flips ("explicit forms remain legal" → rejected), §1.3
  predict-the-verdict drill, §3.2 coercion table, §3.5 rvalue sentence (reconcile the
  existing spec/implementation divergence), receiver §6.3 cross-note; effective-drift
  auto-borrow section + its ~50 explicit-style samples; grammar doc note; SCR addendum
  recording the reversal.
- **Downstream**: hard source-compat break on toolchain upgrade (drift-workflows,
  DriftQuery, uflowsd, mariadb-client), 0.33.83-shaped but larger; each site is a
  one-token fix guided by the diagnostic. Packages per D9: existing artifacts stay
  valid; authors hit the rule on their next source compile.
- **ABI**: unchanged (22).

## 8. Acceptance / rejection matrix (after W0-W6, decisions as recommended)

Given `fn read(arg: &String)`, `fn edit(arg: &mut String)`, `fn identity<T>(x: T) -> T`,
interface `Sink { fn write(self: &Self, rec: &String); }`, `val name: String`,
`var buffer: String`, `val r: &String = &name`, `val fp: Fn(&String) nothrow -> Int`:

| # | Call | Verdict | Note |
|---|---|---|---|
| 1 | `read(name)` / `edit(buffer)` | OK | unchanged |
| 2 | `read(&name)` / `edit(&mut buffer)` | **error** | headline cases |
| 3 | `read(&mut buffer)` | **error** | bare `buffer` auto-borrows shared |
| 4 | `read(r)` | OK | no borrow written |
| 5 | `read(&r)` | **error** | today accepted via hidden deref |
| 6 | `read(&"alice")` / `read(&make())` | **error** | bare forms verified (e5, e6) |
| 7 | `edit(&mut make())` | **error** | bare per D1b(a), or bind-to-`var` per D1b(b) |
| 8 | `read(&obj.field)` / `read(&arr[i])` | **error** | fix-it slices operand text |
| 9 | `identity<type &String>(&name)` | OK | generic-by-value formal |
| 10 | `cb.call(&mut scope)` (FnN/CallbackN) | OK | formal is generic `A` |
| 11 | `fp(&s)` / `fp(s)` (thin fn pointer) | OK (both) | D8 resolved (b): exempt; bare form fixed by the separate e8d LANGUAGE_BUG slice |
| 12 | `Ctor(field = &s)` / `Optional<&T>::Some(&x)` | OK | fields, not parameters |
| 13 | `mem.swap(&mut a, &mut b)` / `mem.replace(&mut p, v)` | **error** | bare works after W3 |
| 14 | `sink.write(&rec)` (interface dispatch, `rec: &String`) | **error** | bare works after W2 |
| 15 | `(|y: &mut Int| => …)(&mut x)` | **error** | bare works after W5 |
| 16 | `drive(&f)` — concrete at declared `&Sink` | **D7**: OK under (a) (coercion borrow, not redundant) / **error** under (b) (bare widens) | the one surviving non-redundant borrow context |
| 17 | concrete pair `pick(&String)`/`pick(String)` — or `peek(&Int)`/`peek(&mut Int)` | **definition-site error** (R2) | bare is ambiguous today (e7, e10) |
| 18 | method mixed set `b.pick(s)` vs `pick<T>` | OK — selects concrete | existing tiebreak |
| 19 | free-fn mixed set, either spelling | ambiguous (unchanged) | pre-broken (e3/e3b); O1 |
| 20 | `captures(&x)`, `for x in &v`, `match &x`, `val r2 = &x`, `(&obj).method()` | OK | outside argument position / receiver |

## 9. Required regressions

- **Positive**: bare forms across all families (free/method/assoc/interface/intrinsic/
  lambda/fn-pointer); generic-by-value exemptions (#9, #10); ctor fields; D7-per-decision
  behavior; alias pin (D6); A/B explicit-vs-bare rvalue fixtures under memcheck +
  valgrind pinning drop counts/order (extends e6); package encode→decode→recompile with
  pre-rule bodies (D9).
- **Negative**: rows 2-8, 11, 13-15 asserting `E_REDUNDANT_ARG_BORROW` with rendered
  operand text (plain var, projection, index, parenthesized, `& x` whitespace);
  diagnostic goldens; W0 validator assert (no family accepts a source-written borrow at
  a declared-`&` formal).
- **Overload**: R2 fixtures for both pair shapes (T/&T and &/&mut, free + method);
  post-rename `json._encode_node` pin; status-quo pins for mixed sets (e3/e7/e10 as
  fixtures).
- **Intrinsics**: bare-form matrix over all 11 `std.mem` intrinsics incl. inference from
  bare places and `<type …>` interplay (188 combined sites); reworked (not deleted)
  `swap_requires_var_rejected`.
- **Fn pointers**: e8d miscompile regression (bare at `&` fn-pointer param must never
  reach codegen unresolved), plus D8-per-decision accept/reject pins.
- **Full**: complete e2e suite post-sweep; ownership-corpus `--require-zero-delta` after
  the reviewed promotion; `just ownership-matrix-check`; memcheck suite. **The
  implementer runs the full suite and reports results as part of the implementation
  slice** (targeted tests during development; the full run and its outcome are part of
  the deliverable, not deferred to the reviewer).

## 10. Optional extensions (separate votes)

- **O1**: concrete-beats-generic overload ranking for free functions (fixes e3's
  pre-existing ambiguity; independent of this rule).
- **O2**: text-mode rendering of `Diagnostic.notes` (today JSON-only).
- **O3**: receiver forms (`(&obj).method()`) under a redundancy rule.
- **O4**: redundant `&`-of-a-reference outside call positions.
- **O5**: D1b(a) mutable-rvalue materialization, if D1b(b) is chosen initially.

## 11. Risks / unresolved questions

- **R-1**: W3 intrinsic inference is the one work item touching inference order; its
  interplay with explicit `<type …>` args needs its own fixture matrix before the sweep
  depends on it.
- **R-2**: e6 is a single drop-timing probe; unwind paths and sanitizer lanes could
  still differ between the two temp mechanisms. The rvalue sweep is gated on the A/B
  memcheck fixtures.
- **R-3**: totals are estimates (±60; ≤82 upward bias from ambiguous-overload
  resolution). Does not change any conclusion.
- **R-4**: downstream corpuses unmeasured; in-tree evidence predicts heavy usage.
- **R-5**: W2 must be validated against the runtime-side `log.Sink` dispatch path.
- **R-6**: D7(b), if chosen, lands inside the open `&Concrete→&Interface` coercion-gap
  design thread and must not fork it.
- **R-7 — ANSWERED (yes, probe e11)**: fn-pointer type expressions can carry typevars
  (`Fn(T)` in generic context compiles and instantiates). Consequence folded into D8:
  option (a) accepts over-rejection for generic-written fn types at reference
  instantiations; there is no clean total-provenance variant.

## 12. Recommendation

**Conditional up-vote**, unchanged in direction from v2 and now resting on a complete
decision surface. The rule is coherent under one uniform principle (a borrow whose
deletion preserves typing is redundant and rejected), enforcement is centralized by
design (W0) rather than replicated per call family, and the two newly examined edges
are small in the data: interface-view borrows ≤43 sites (D7), fn pointers 0 sites plus
a miscompile that needs fixing regardless (D8). Compiler work ≈ one moderate slice
(W3) + several small ones; migration ≈ 4,900 one-token edits (after D3's archival
exclusion) + one corpus promotion + doc rewrite.

Conditions: (1) decisions D1-D9 ratified (D3 resolved 2026-07-29: migrate active
test inputs, preserve archival snapshots verbatim, no compiler exemption) — D7 and D8
are genuine policy forks the vote must settle, not defaults this plan may assume; (2) W0-W6 land with the rule in one
release, with the separate e8d LANGUAGE_BUG fix as an independent prerequisite landing
first — the rule must never reject a spelling whose bare form does not compile; (3) rvalue A/B memcheck fixtures pass before
the 946-site rvalue sweep; (4) the frozen-corpus reviewed promotion (415 deltas), the
D5 test-retirement list, and downstream migration are approved as first-class release
items.
