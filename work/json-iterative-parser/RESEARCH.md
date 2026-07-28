# std.json: recursive-descent → iterative explicit-stack parser (RESEARCH ONLY)

Status: **RESEARCH + ownership prototype done.  Escalated to
RELEASE-BLOCKING for 0.33.89 (maintainer, 2026-07-27); the 2 MiB
workflows fiber stack only moves the failure point).  8 binding
decisions recorded in §9.**
Trigger: drift-workflows cert rejection 2 (2026-07-27) — a fiber
stack overflow inside `std.json::_parse_literal`, fault 8 bytes below
RSP in a 256 KiB serve-fiber's guard page, on a client-reachable
recursive parse path.  drift-workflows fixed it locally with 2 MiB
fiber stacks; this researches the durable platform-side fix.

## 1. Problem

The parser is recursive descent: nesting depth in the input maps
1:1 to native C stack frames.  `_parse_value` → `_parse_array` /
`_parse_object_throwing` → `_parse_value` (per element/member) →
… .  Because parse depth scales with **client-supplied** JSON
nesting, a deep body drives any parsing fiber into its guard page —
a remote-triggerable SIGSEGV, in the release lane too (debug just
lowered the threshold via fatter frames).  `max_depth` exists and is
enforced (`_over_limit` in `_parse_array`/`_parse_object_throwing`),
but the default config `permissive()` sets it to `None` (unbounded),
so the shipped `parse()` has no cap.

An iterative parser with an explicit heap stack removes the
native-stack failure mode: depth then costs heap, not C frames, and
a depth cap converts a would-be crash into a clean parse error.

## 2. Current structure (what a rewrite must preserve exactly)

Only two productions recurse; everything else is leaf and unchanged:

* leaf, no recursion: `null` / `true` / `false` (`_parse_literal`),
  strings (`_parse_string`), numbers (`_parse_number`).  Each sets a
  Leaf span via `_set_leaf`.
* `_parse_array`: `[`, then a loop of `_parse_value` (depth+1)
  separated by `,`, until `]`.  State: `values: Array<JsonNode>`,
  `items: Array<_SpanTree>` (locate only), `arr_start`, per-element
  `elem_start`, the running `max_array_items` count.
* `_parse_object_throwing`: `{`, then a loop of key(`_parse_string`)
  `:` value(`_parse_value`, depth+1) separated by `,`, until `}`.
  State: `fields: HashMap<String,JsonNode>`, `member_count`,
  `occurrences: Array<_KeyOccurrence>` + `vspans:
  HashMap<String,_SpanTree>` (locate only), `obj_start`, per-member
  `key`/`key_start`/`key_end`, and the duplicate-key policy branch
  (Reject probes before the value; KeepFirst uses
  `insert_if_absent`; KeepLast/unique use `insert`).

Cross-cutting invariants that MUST survive byte-for-byte:

* the `idx: &mut Int` cursor threaded through every call;
* error tags AND offsets (`invalid-syntax`, `limit-depth`,
  `limit-array-items`, `limit-object-fields`, `duplicate-key`, …)
  at their exact current offsets;
* the `_SpanTree` sidecar shape (Leaf / Arr(items) / Obj(occurrences,
  values)) and its population ONLY when `ctx.locate`;
* the object parser's THROWING nature: `HashMap` insert can throw, so
  `_parse_object` wraps `_parse_object_throwing` in try/catch →
  `internal-error`.  `_parse_array` is nothrow.
* top-level policy check + trailing-garbage check in
  `_parse_document`.

## 3. Proposed design

One explicit stack of in-progress container frames, one main loop, a
single "value just completed" hand-off register.

Frame (sum type) carries exactly the per-level state §2 lists:

* `ArrayFrame { values, items, arr_start, expect: Element|CommaOrEnd }`
* `ObjectFrame { fields, occurrences, vspans, member_count,
  obj_start, pending_key, key_span, expect: Key|Colon|Value|
  CommaOrEnd }`

Loop shape (prose, not code):

1. To parse a value at the current cursor, dispatch on the first
   byte.  Scalars: parse in place, produce a completed `JsonNode`
   (+ Leaf span) → step 3.  `[`/`{`: check depth cap, push a fresh
   Array/Object frame, continue.
2. When a frame needs its next child value, it re-enters step 1.
3. **Completion hand-off** (replaces the recursive return): the
   completed child node (and its child `_SpanTree`) is delivered to
   the frame now on top of the stack — pushed into `values` /
   inserted into `fields` under the duplicate-key policy, child span
   appended to `items` / `vspans`.  If the stack is empty, the child
   is the document root.
4. On `]`/`}`: finalize the frame's `_SpanTree` (Arr/Obj), pop it,
   turn it into a completed node, and hand THAT off via step 3 to the
   new top (or make it the root).

The `expect` enum makes each frame a tiny state machine, replacing
the position within the recursive function body (before-element,
after-comma, after-value, …).

## 4. The hard parts (Drift-specific; the real content of this research)

1. **Move semantics through the stack.**  Handing a completed
   non-Copy `JsonNode` from a popped frame into the parent frame's
   `Array`/`HashMap` is a move; the frame stack itself holds non-Copy
   containers and must be indexed/mutated in place (`stack[top]`
   mutated, then popped with `mem.replace`/`pop`-move).  This is the
   central question: can the frame stack be expressed so the borrow
   checker accepts "mutate top of stack, then move it out on pop"
   without a partial-move on the `Array<Frame>`?  Likely needs the
   `mem.replace` take-first idiom (per the v1 borrow-checker
   feedback) at the pop site.  **Must be prototyped before commit.**

2. **The throwing object path.**  `HashMap` insert can throw, so the
   object frame's hand-off (step 3) is a throwing operation.  The
   whole loop must run inside the `_parse_object`-style try/catch so
   any insert-throw still maps to `internal-error` at the right
   offset — i.e. the iterative driver is one throwing function with a
   nothrow wrapper, not the current per-object wrapping.  Error
   offset on an insert-throw must match today's (`*idx` at the throw).

3. **Span-sidecar construction becomes bottom-up on pop.**  Today the
   child span is built by the recursive callee and assigned into the
   parent's `items`/`vspans` on return.  Iteratively, the child
   `_SpanTree` is produced at pop and appended during hand-off —
   semantically identical, but the ordering (source order for
   `occurrences`; value-span map mirroring `fields` under each
   duplicate-key policy) must be reproduced precisely, including the
   KeepFirst/KeepLast/Reject asymmetries.

4. **Duplicate-key policy is per-frame mid-parse state.**  Reject
   probes `contains_key` at `key_start` BEFORE the value parses —
   with an explicit stack the "before the value" moment is between
   pushing the value's parse and receiving its hand-off, so the probe
   must fire at key-accept time, not hand-off time, to keep the
   failure offset (`key_start`) and ordering identical.

5. **Cursor + limits are unchanged** (shared `idx`, same
   `_over_limit` calls) — but `limit-depth` now also bounds the
   explicit stack height, which is the point.

## 5. What does NOT change

Scalar parsers, string/number/escape handling, `_skip_ws`, all error
tags/offsets, the `JsonNode` and `_SpanTree` types, `JsonParseConfig`
/ limits / policies, every public entry point signature
(`parse`, `parse_with_config`, `parse_strict`, `parse_located`), and
the located-cursor surface.  **No ABI change** (pure stdlib source;
no runtime/layout/codegen).  No compiler-version bump required by the
parser itself.

## 6. Depth policy (orthogonal decision the crash surfaced)

The iterative rewrite removes the *crash*, but a deep input still
allocates O(depth) frames — a heap-growth DoS instead of a
guard-page DoS.  So a **default depth cap is still wanted**,
independent of recursive-vs-iterative:

* Option A: give `permissive()` (hence `parse()`) a sane default
  `max_depth` (e.g. 128 or 256).  Behavior change: some currently
  accepted very-deep docs would now `limit-depth`.  Must be measured
  against the corpus + downstream (drift-workflows SP docs are
  ≤3 deep, far under any cap).
* Option B: keep the default unbounded but make the iterative parser
  the guarantee that "unbounded" means graceful heap growth, not a
  crash — then depth capping stays opt-in.
* The two platform findings from the workflows report — (1)
  client-controllable recursion depth, (2) no stack-overflow
  diagnostic — are BOTH satisfied better by the iterative parser +
  a default cap than by enlarging fiber stacks, which only moves the
  cliff.

Recommend A (bounded default) layered on the iterative parser;
confirm the cap value against the corpus and downstream JSON shapes.

## 7. Verification obligation (if this proceeds to implementation)

* **Differential harness**, same discipline as the regex
  dual-engine differential: keep the recursive parser as a shadow
  oracle over a large generated corpus (valid + malformed + deep +
  wide + all duplicate-key policies + located and non-located),
  requiring identical `(node, error tag, error offset, span tree)`
  on every case, before deleting the recursive functions.
* A dedicated deep-nesting tooth: an input at cap+1 returns
  `limit-depth` at the right offset; an input just under the cap
  parses; and — the original bug — a very deep input parses (or
  cleanly errors) on a 256 KiB fiber WITHOUT SIGSEGV (the memcheck /
  fiber-stack fixture).
* The full ownership-corpus zero-delta gate (stdlib change → expect a
  modal per-fixture delta; measure/attribute/promote via the
  governed promotion tool).
* memcheck/ASAN on the parser fixtures (the move-through-stack in §4.1
  is exactly where a leak/double-free would hide).

## 8. Risks / open questions for review

* §4.1 (move-through-frame-stack + borrow checker) is the make-or-
  break feasibility question — a scratch prototype of just the
  frame-stack push/mutate-top/pop-move should run BEFORE committing
  to the rewrite; if it needs awkward `mem.replace` gymnastics or a
  language change, that reshapes the effort.
* Iterative object parsing still throws (HashMap); confirm the single
  outer try/catch reproduces `internal-error` offsets exactly.
* Performance: the explicit stack adds per-container heap frames vs
  native frames — measure against the shipped parser on the
  representative SP-doc and a large-array workload; the recursive
  parser is fast on shallow inputs and the iterative one must not
  regress them materially.
* Scope: this is a std.json internal rewrite — no public API, no ABI.
  It can ride a future stdlib slice; it is NOT required for the
  current 0.33.89 certification (drift-workflows already has its
  local 2 MiB-stack mitigation).

## 8a. Ownership prototype RESULT (binding decision 1 — DONE)

`frame_stack_ownership_probe.drift` (checked in beside this doc)
models the three load-bearing operations at minimal scale — non-Copy
`Node` (Leaf|Branch(Array<Node>)), non-Copy `Frame { kids }`, an
explicit `Array<Frame>` stack — building `[[1,2],[3,[4,5]]]`
iteratively with NO recursion.  Compiled on the CERTIFIED toolchain
(0.33.87); runs (sum=15); **valgrind clean** (leak-check=full,
errors-for-leak-kinds=all, rc 0).

Findings:

* **Mutate-top-through-index of a non-Copy element's field is
  accepted directly**: `stack[stack.len - 1].kids.push(child)`
  compiles with no borrow gymnastics — the larger of the two §4.1
  worries is a non-issue.
* **Pop-move + take-field-out uses the documented idiom**: the match
  binder must be `Some(var f)`, the owned inner array is taken with
  `mem.replace(&mut f.kids, <typed empty>)`, and moved into the
  parent with `move`.  Every rejection encountered was an EXPECTED
  v1 rule with a documented remedy — no-partial-moves
  (`std.mem.replace` is the canonical move-field-out; see the
  no-partial-moves language decision) and explicit `move` of a
  non-Copy value — NOT a compiler defect.

**Decision-1 gate result: the take/pop shape exposed NO compiler
defect; the regression-first LANGUAGE_BUG path does NOT fire, and no
stdlib reshaping-to-mask is involved.  The iterative parser is
FEASIBLE in v1 as designed.**  (Prototype omits spans, the throwing
HashMap object path, and duplicate-key timing — those are
mechanical extensions of the same proven shape, exercised by the §7
parity + memcheck gates, not fresh ownership questions.)

## 9. Binding decisions (maintainer, 2026-07-27) — supersedes the §8 "future slice" conclusion

1. Prototype first (DONE, §8a); LANGUAGE_BUG regression-first if the
   shape exposes a defect — it did not.
2. Land the iterative parser AND a FINITE default `max_depth`
   together; limit chosen from measured memory + compatibility data;
   explicit unbounded stays only as a documented trusted-input
   opt-in.
3. Scalars, errors+offsets, duplicate-key timing, spans, public
   signatures unchanged except the documented default-depth
   hardening.
4. Recursive parser is a DEV ORACLE on non-overflowing depths only;
   deep-input tests exercise the iterative parser INDEPENDENTLY.
5. Exact value/error/span parity across all duplicate-key modes,
   malformed-input families, and BOTH document + parser surfaces.
6. Prove deep input returns the depth ERROR — not a signal — on the
   default 256 KiB executor, DEBUG and RELEASE, including the
   workflows crash shape WITHOUT relying on the 2 MiB mitigation.
7. Primary perf gate: shallow request-sized JSON must not materially
   regress; also measure deep + large inputs, allocations, memcheck,
   ASAN.
8. Stays in the combined 0.33.89/ABI-22 candidate; no ABI bump
   indicated; record the default-depth behavior correction and
   measure/approve any corpus delta before the single final
   certification.

The workflows 2 MiB stack may remain as defense-in-depth but is NOT
acceptance evidence.

## STOP

Research + ownership prototype complete; feasibility confirmed with
no compiler defect.  Design is GO for implementation in the combined
0.33.89 candidate per the §9 binding decisions.  Implementation plan
(next, on approval of this design): iterative parser + finite default
max_depth (§6 option A, cap value measured against corpus +
downstream), the §7 differential/deep-input/parity/memcheck/ASAN
gates, the §7 perf gate (shallow primary), corpus measure→attribute→
promote via the governed tool, one final certification.  This
checkpoint remains report-only — no stdlib code changed yet; awaiting
the go-ahead to implement.

## 10. Default cap DECISION (binding decision 3 — measured)

**Default `max_depth` = 128.**  Basis:
- Depth distribution across 6,055 committed .json in all repos: 99.99%
  ≤ 6; deepest Drift-ecosystem application data = 8 (pushcoin
  bookkeeper scenarios); deepest anywhere = 10 (a Python tooling
  schema, not Drift); drift-workflows SP-result docs = 3.  128 is
  ~16x the deepest real Drift data.
- std.json's own test suite parses nothing deeper than ~3 by default
  (the only deep test sets max_depth=Some(1) to assert rejection).
- Measured per-level heap ≈ 1.3 KB (RSS delta depth-1 vs depth-1000,
  certified parser): a cap of 128 bounds the parse working set to
  ~170 KB, entirely on the HEAP — the iterative parser never spends
  the 256 KiB fiber stack on depth.
- Aligns with serde_json's well-known default (128); .NET uses 64.
`None` (unbounded) is retained ONLY as a documented trusted-input
opt-in.  Both container-frame ownership shapes needed by the
implementation are proven on the certified compiler:
mutable-match on an indexed sum-type frame (`match &mut stack[top]`)
and pop-move via `Some(var f)` + `mem.replace` (frame_stack_ownership
_probe.drift + mutmatch probe).

## 11. IMPLEMENTATION HALTED at a failed invariant (compiler ICE)

The iterative parser was written and spliced into std.json; the
frame-stack ownership shapes all compiled (mutable-match advance,
pop-mutate-push hand-off, owned-destructure close).  But the
hand-off's `pending_span` reassignment surfaced an INTERNAL COMPILER
ICE, minimally isolated (issues/json-destructible-plan-pathdependent-
ice/):

  destructible-plan tripwire (destructible_authority.py:279) ICEs
  whenever the ownership ledger returns PathDependent at a
  drop_before_overwrite point — reachable from VALID source by a
  conditionally-moved destructible local that is overwritten
  afterward.  ICEs on BOTH the tree AND certified 0.33.87 (long-
  standing latent defect, not a regression); independent of throwing.

Per binding decision 1 (fix the compiler regression-first; do NOT
reshape stdlib to mask), the parser is VALID and must land on a fixed
compiler — a consume-once-local reformulation that dodges the ICE
would be masking.  The fix (restore a flag-guarded PathDependent drop
at drop_before_overwrite, or tighten the lattice) is in the gated
ownership-lattice area and is a scope expansion.

RETURNING at this failed invariant.  json.drift is restored to clean
(no partial parser landed); the iterative-parser block is preserved
at issues/.../iterative-parser-block.drift.wip to re-land once the
compiler is fixed.

---

## CLOSURE — IMPLEMENTED & SUPERSEDED (2026-07-27)

The "IMPLEMENTATION HALTED / RETURNING at this failed invariant" status
above is SUPERSEDED. The blocking compiler defect (site-4
drop-before-overwrite ICE on a conditionally-moved-then-overwritten
destructible, and its match-binder variant) was FIXED in-tree, and the
iterative parser was LANDED in `stdlib/std/json/json.drift` — with the
completed-value hand-off encoded as `Optional<_Completed>` (no unowned
MAYBE_UNINIT consumer move), a finite default `max_depth = 128`, and the
recursive parser preserved OUT of production stdlib as a differential /
performance oracle (`lang/tests/fixtures/json_recursive_oracle.drift.frag`).

Authoritative record: the 2026-07-27 entry in `doc/history.md`. This
`work/` file is a superseded ephemeral research log; do not treat its
"HALTED" ending as the current state.
