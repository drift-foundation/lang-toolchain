# Repo Agent Rules

## Git usage (strict)

- Use `git` **only** for reviewing history or diffing (e.g. `git diff`, `git log`, `git show`, `git blame`).
- **Do not** stage or unstage changes (`git add`, `git restore --staged`, etc.) without explicit permission.
- **Do not** perform any mutating git operations without explicit permission (including `git commit`, `git merge`, `git rebase`, `git cherry-pick`, `git reset`, `git checkout/switch`, `git stash`, and tag/branch operations).
- **Do not** wrap long lines (calls with many arguments, long expressions) for readability; avoid indentation churn, specially if code is deeply nested.
- **Do not** edit exisit tests without a clear confirmation it's OK. No bending around tests to patch compiler/infra deficiencies.

## Announcements
- Read and publish cross-team announcements from/to /tmp/drift-announce/<iso-utc-datetime>-<repo>-release-notes.md

## Language Specification Authority (strict)

- Do **not** edit `doc/design/drift-lang-spec.md`, `doc/design/spec-change-requests/`, or any other document that defines or proposes Drift language semantics without explicit, case-specific approval from Slawomir.
- A general request to fix, review, reconcile, migrate, or document behavior is not approval to change the language specification. Approval must identify the proposed semantic/spec change closely enough that its compatibility and implementation consequences are understood.
- The checked-in language specification is authoritative over compiler behavior, tests, reviewer proposals, implementer assumptions, and progress-file claims. Do not silently reinterpret the specification or modify it to match an implementation.
- If implementation or tests disagree with the specification, treat the mismatch as a likely `LANGUAGE_BUG`: stop semantic implementation changes, preserve the spec text, and report the exact conflicting sections, minimal repro, current behavior, and proposed resolution to Slawomir.
- If a correct fix appears to require changing the language contract, pause and request Slawomir's approval before editing the spec or implementing the new contract. Until approval is explicit, fixes and reviews must target conformance to the current checked-in specification.

## Compiler/Runtime Bug Policy (strict)

  - If behavior indicates a language/toolchain defect (parser, checker, lowering, codegen, runtime semantics), classify it immediately as LANGUAGE_BUG.
  - Do not patch stdlib/ or user-facing code to avoid triggering a suspected LANGUAGE_BUG unless explicitly approved by the user for a temporary workaround.

### Regression-first requirement (mandatory)

- For every suspected LANGUAGE_BUG, do this in order:
		1. Add a minimal failing regression test (prefer e2e; use driver/unit as appropriate).
		2. Confirm it fails on current mainline behavior.
		3. Fix compiler/runtime/toolchain root cause.
		4. Confirm regression passes.
		5. Only then consider any stdlib/app refactor.

### No semantic masking

- Forbidden without explicit approval:
		- Rewriting conditionals/control flow to “work around” short-circuit, borrow, move, drop, or exception semantics bugs.
		- Rewriting ownership patterns in stdlib/ to hide checker/lowering/runtime defects.
		- Any source-level change whose primary purpose is to bypass a compiler/runtime bug.

### Stop-and-confirm gate

- On first detection of a likely LANGUAGE_BUG, stop implementation changes and notify user with:
		- minimal repro
		- failing test path
		- suspected subsystem
- Continue with compiler/runtime fix by default; ask before applying any temporary workaround.

### Temporary workaround protocol (opt-in only)

- If user explicitly requests a temporary workaround:
		- Keep it minimal and localized.
		- Add a work-progress.md item or if file is missing, add to TODO.md, referencing the regression test and bug label.
		- Do not mark task complete until root-cause fix is landed or explicitly deferred by user.

### Completion criteria for language bugs

- A LANGUAGE_BUG is not “done” unless both are present:
		- pinned regression test
		- root-cause compiler/runtime fix
- “Workaround-only” changes must be reported as partial and not final resolution.

## Boundary Contract Guardrails (strict)

- Any change that expands/changes type support across stage boundaries (checker -> stage2 -> MIR validate -> LLVM lowering) must update boundary expectations explicitly.
- Mandatory for boundary-shape changes (e.g. FnResult ok payload support changes):
		1. Add/adjust a positive regression proving the new shape works end-to-end (driver or e2e).
		2. Add/adjust a negative regression proving unsupported shapes still fail with clear contract diagnostics.
		3. Update stale contract comments/messages/tests that describe supported boundary shapes.
- Do not leave contradictory tests/comments (example forbidden state: behavior supports `Array<T>` but negative tests/docs still claim arrays are unsupported).
- Prefer central boundary checks and diagnostics over scattered ad-hoc guards.
- ABI boundary changes (runtime-exported helper signatures, data layouts crossing the compiler/runtime boundary, calling conventions, ownership/drop contracts) require bumping `DRIFT_RT_ABI_VERSION` in `lang/driftc/driftc_versions.py` and adding/updating the mismatch regression in `lang/tests/driver/test_abi_version_stamp.py`.
- ABI bump decision rule:
		- If a fix changes only internal lowering/analysis behavior (no boundary signature/layout/call-convention change), do **not** bump ABI.
		- If any compiler/runtime boundary shape changes, bump ABI in the same patch and update the mismatch regression.
- Compiler versioning rule:
		- Any actual **or suspected user-visible impact** requires a SemVer **minor** bump of `DRIFTC_VERSION`: `0.Y.Z` → `0.(Y+1).0`. User-visible impact includes language acceptance or semantics, stdlib API/behavior, compiler or `drift` CLI output/flags, tooling contracts, package/metadata/serialized formats, and migration requirements.
		- A SemVer **patch** bump (`0.Y.Z` → `0.Y.(Z+1)`) is permitted only when the change is proven user-neutral. Do not use a patch bump merely because the runtime ABI is unchanged.
		- If user impact is uncertain, fail toward the minor bump. This decision must be made before staging so downstreams never synchronize to a patch-numbered compatibility break.
		- ABI shape changes require both: a compiler minor-version bump and an ABI-version bump.

## Pre-1.0 Compatibility Policy (strict)

- While `DRIFTC_VERSION` has major version `0`, Drift does **not** retain backward compatibility for replaced language, stdlib, compiler, CLI, tooling, package, metadata, or serialized-format contracts.
- When a contract changes, keep exactly one current contract: delete the replaced API/format/parser and migrate all in-tree callers, tests, fixtures, examples, and documentation in the same change.
- Do **not** add or retain compatibility aliases, deprecated entry points, shims, fallbacks, dual readers/writers, legacy parser modes, or old-format acceptance unless the user explicitly approves a narrowly-scoped exception.
- Clean breaks still require the normal compiler/ABI version decisions, regression coverage, corpus enumeration where applicable, and explicit migration notes in history/release announcements.
- Once Drift reaches major version `1`, this pre-1.0 rule no longer applies; a public compatibility and deprecation policy must be established before making 1.x compatibility decisions.

## Checker / Lowering Contract (strict)

Surfaced by the G3 incident (0.31.34): a type-checker change accepted programs the lowering pipeline could not actually emit (`integer binop requires matching Int/Uint operands (have ptr, drift.int)`). These rules close that process gap.

### Rule 1 — No checker-only semantic coercions

- Any type-checker decision that changes the apparent type or value category of an expression in a way that affects lowering must be represented in the post-check IR, either by rewriting HIR or by recording a node-level coercion consumed by downstream passes.
- It must NOT exist only in transient checker locals (e.g. `left_ty` / `right_ty` / similar local rebindings).
- Test: "if I deleted the checker pass and re-ran lowering against the recorded HIR + node-level marks, would the result match the checker's accept verdict?" If no, the rule is violated.

Examples (OK):

- `record_iface_coercion(arg, param_ty)` — adds a node-level coercion mark the lowering pass reads.
- Implicit `core.callbackN(...)` wrap — inserts a real HIR node the lowering pass sees.
- G3 v2 `HUnary(DEREF, HVar)` rewrite at HBinary / HTernary cond / match arm result — HIR node insertion; HIR→MIR's existing DEREF lowering emits `LoadRef`.

Examples (NOT OK):

- G3 v1 `_autodref_copy` adjusting `left_ty` / `right_ty` in HBinary type-check without rewriting the HIR. Lowering still saw a `Ref<Int>` operand and crashed at LLVM IR generation.

This rule does NOT apply to:

- Borrow-checker liveness / aliasing decisions (those live in their own state machine and don't change the HIR's apparent type).
- FORWARD_NOMINAL canonicalization (same bit pattern, different TypeId — no lowering change).
- Generic instantiation via `CallInfo` (the substitution IS recorded at the call site).

### Rule 2 — MIR / lowering contract failures must not surface as user diagnostics

- The internal-form `(checker bug)` / `MIR contract failure` messages must never reach end users.
- In dev / test builds, MIR / lowering contracts may `assert` — that's a useful safety net.
- In user-facing builds, a contract failure must be surfaced as an internal compiler error with full context (file, span, expression shape, suggested issue link), not as a confusing diagnostic that reads like a user error.
- A contract-fail diagnostic reaching a user is itself a process bug — the type-checker should have rejected upstream.

### Rule 3 — Acceptance tests for lowering-visible changes need a full compile/run companion

- Any new acceptance test (`rc == 0` expected) for a value-shape change that affects lowering — type-coercion, autoderef, escape rules, place-mutability — must include at least one full-compile-and-run companion test that proves the program actually lowers and executes.
- Checker-only `--test-build-only` coverage is not enough for this class of change. G3 v1 passed `--test-build-only` while failing at LLVM IR generation; reviewer caught it because they ran the full compile manually.
- Companion test format: spawn the driver as a subprocess, link the binary, run it, assert the exit code matches the expected semantic outcome. Cf. `_compile_and_run` in `lang/tests/driver/test_match_by_ref_variant.py`.

## Review findings tracking (work/finding-* subfolders)

Review findings are tracked as dedicated subfolders under `work/`, one per finding: `work/finding-<slug>/`. Two goals: no finding discovered at any point is ever lost, and the agent and reviewer can work **concurrently** — while the agent is heads-down (or stuck) on finding-1, the reviewer keeps piling tests, evidence, repros, and proposed solutions into finding-2, finding-3, … without interrupting the work in flight.

**Process:**

- When a review surfaces a finding that is not being fixed on the spot, it gets its own `work/finding-<slug>/` folder capturing the finding (repro, suspected subsystem, evidence) at the time of discovery.
- Finding folders are a live drop-box: the reviewer may add material to any queued folder at any time. The agent does not need to react to those additions mid-task — queued folders are read when their turn comes.
- Use reviewer capacity to make queued findings implementation-ready, not merely identifiable. Before the implementer picks one up, the reviewer should add as much verified research as practical: a minimal repro and observed baseline; exact producer/consumer code paths and symbols; root-cause evidence with hypotheses clearly separated from facts; a recommended patch shape and affected-file boundary; semantic edge cases and interactions with current work; positive, negative, boundary, and compile/run regression cases; focused verification commands; and the refactor-trigger result for a LANGUAGE_BUG. Prefer leaving the implementer a bounded execution/verification task over asking them to rediscover the defect. Research artifacts and proposed tests belong in the finding folder until implementation; do not edit implementer-owned `PROGRESS.md` or in-tree source/tests as part of reviewer-only research.
- Reviewer research is decision support, not an implementation specification. Label material by epistemic status where ambiguity is possible: **Observed** (reproduced evidence), **Confirmed** (code-path fact), **Inferred** (best current explanation), **Proposed** (one patch/test design), or **Open** (unresolved question). Use directive language only for repository/user contracts and verified acceptance criteria; phrase diagnoses and patch shapes as falsifiable claims or recommendations. The implementer must revalidate the current tree and is explicitly free to disprove, narrow, or replace the reviewer's diagnosis or proposal; record the contrary evidence and resulting decision in implementer-owned `PROGRESS.md` rather than following a reviewer theory that does not fit the code.
- Fallibility is symmetric: neither an implementer's `PROGRESS.md` claim nor a review report is authoritative merely because of its author or channel. The implementer independently checks reviewer evidence and assumptions; the reviewer independently checks implementation claims against the current diff, relevant code paths, repros, and regression coverage rather than accepting the status summary. Treat doubt, counterexamples, and corrections as required engineering inputs, not friction. Resolve disagreements with reproducible evidence and repository contracts, state remaining uncertainty plainly, and do not sign off while a material claim has only been asserted by the other role.
- Keep researching and enriching the next queued findings while implementation is the throughput bottleneck. Prioritize the next serial item first, then other queued items by likely implementation cost and risk. Re-check all captured evidence against the current tree when the item starts; implementation may have made earlier research stale.
- Findings are worked **serially** — one at a time, to completion (per the LANGUAGE_BUG completion criteria above when applicable), before picking up the next.
- When picking up a finding, read the WHOLE folder fresh — it has likely grown since it was filed — and re-verify its claims against the current tree: earlier work may have resolved it in full or in part. Captured text going stale is expected; the folder is the tracking unit, not a living spec. If the finding is already fully resolved, record that outcome in the folder rather than re-fixing.
- Do not silently delete a finding folder because it looks stale — close it out explicitly (resolved by <what>, superseded by <what>, or fixed directly).
- Finding discovery is recursive and role-neutral: at any time while a current finding is being researched, reviewed, implemented, or verified, either the reviewer or the implementer may file another defect as a top-level `work/finding-<slug>/` or as a nested child finding. Use a child when the new defect is causally tied to or naturally scoped under the current finding and is expected to close with it; use a top-level finding when it is independent, separately schedulable, or may outlive the current parent. Filing the discovery immediately does not interrupt the serial-work rule—the new finding remains queued unless it is required to close the current finding's contract.
- When work on one finding uncovers a distinct child finding, use filesystem nesting rather than dot-notation names:
	```text
	work/finding-<parent-slug>/
	├── FINDING.md
	├── PLAN.md
	├── PROGRESS.md
	└── findings/
	    └── finding-<child-slug>/
	        ├── FINDING.md
	        ├── PLAN.md
	        └── PROGRESS.md
	```
	- Every child is a complete finding with its own `FINDING.md`, `PLAN.md`, and `PROGRESS.md`.
	- The parent `PROGRESS.md` lists each child and its status; the child `FINDING.md` names its parent and discovery context.
	- Do not use dotted top-level names or numeric prefixes for hierarchy/order; priorities change and the directory tree is the authority.
	- Keep nesting to at most two child levels. If a deeper child appears, or a child becomes independently scheduled or may outlive its parent, promote it to `work/finding-<child-slug>/` and update all live references; do not leave an alias/stub behind.
	- A parent cannot be deleted while it contains an open child finding. Close every child first or promote the open children before parent cleanup.
- Each review pass of a finding is journaled in that finding's root as `review-YYYY-MM-DDTHH-MM-SSZ.md`, using UTC (for example, `review-2026-08-03T14-00-33Z.md`). Reviews are append-only history: never edit or delete an earlier review file; the newest file is the review to answer. The agent records its response/outcome and the current awaiting-review, changes-requested, or signed-off state in `PROGRESS.md`, leaving the review file itself untouched. Child findings use the same convention in their own root.
- Ready-for-handoff state is signaled with timestamped empty token files in the finding root. Singleton token names are forbidden because a concurrent handoff could be removed as though it were an older signal.
	- After publishing a changes-requested `review-<timestamp>.md`, the reviewer creates the empty token `REVIEW-PENDING-<same-timestamp>`. The implementer removes that exact token only after addressing the review in `PROGRESS.md`. A signing-off review follows the terminal rule below and creates no such token.
	- When implementation and `PROGRESS.md` are ready for review, the implementer creates a uniquely timestamped empty token `IMPL-PENDING-<UTC-timestamp>`. The reviewer removes that exact token only after publishing the corresponding review. Use the same `YYYY-MM-DDTHH-MM-SSZ` UTC format as review files; never reuse an existing timestamp.
	- When the implementer is blocked on a decision only Slawomir can make (an existing-test edit, a language-contract/spec ruling, or any other explicit-approval gate), the implementer records the complete proposal and evidence in `PROGRESS.md` and creates the empty token `APPROVAL-PENDING-<UTC-timestamp>`. No implementation proceeds on the gated item while the token stands. Slawomir (not the reviewer) resolves it: the approval/ruling is delivered in his own words (chat or a file he authors), and the implementer then removes that exact token and proceeds — resuming the normal `IMPL-PENDING` handshake when ready. A blocked pass consumes the incoming `REVIEW-PENDING-*` token as usual (the block itself is the recorded response).
	- At the start of a pass, snapshot and inspect the exact incoming token names being consumed. After publishing the outgoing artifact/token, remove only that captured set. Never glob or bulk-delete pending tokens: a token created while the pass was in flight belongs to the next pass and must remain.
	- Both token types may coexist; this means each role published work before consuming the other's latest handoff. The empty tokens are notification state only: `PROGRESS.md` and immutable `review-*.md` files remain the content authorities.
	- A review requesting more changes creates a new `REVIEW-PENDING-*` token. A signing-off review consumes the reviewed `IMPL-PENDING-*` token(s), creates no new review-pending token (otherwise both roles can enter a deadlock/spin), and the reviewer ends the user-facing handoff with exactly `finished`. Intermediate work that is not ready for the other role does not create a token.
- `PROGRESS.md` has a single writer: the implementer. The reviewer never updates it — reviewer input goes exclusively into `review-*.md` files and other evidence/repro material. One owner per channel keeps the status trail unambiguous: `PROGRESS.md` is always the implementer's claim of where things stand; `review-*.md` is always the reviewer's.
- Finding folders are **ephemeral**: they are deleted after the branch merges to main and the resolution is closed. No permanent or residual reference may point at them — not from code comments, source, tests, runners, or tools. Anything a finding produces that must outlive the branch (regression tests, doc updates, refactor-trigger entries) lands in the tree proper before the folder is closed, phrased so it stands alone without the folder.

## Refactor triggers (registry of opportunistic uplifts)

Some compiler design improvements are deferred because the cost is not justified by current value — but they have specific bug shapes that, if they appear, become a natural forcing function. When that bug shape lands, the fix's budget is large enough to land the improvement as the deliverable.

**Process rule (mandatory):**

When starting any LANGUAGE_BUG fix, scan `doc/refactor_triggers.md`. For each registered entry, ask: does the current bug match its trigger condition? If yes, the bug fix's deliverable is the larger refactor, not the minimal patch. If no, proceed with the minimal fix.

This is an explicit "stop and consult" step parallel to the LANGUAGE_BUG stop-and-confirm rule above — both fire at the start of bug investigation.

**Adding entries:**

When a refactor is identified but not currently justified, append an entry to `doc/refactor_triggers.md` with:

1. The improvement (one paragraph).
2. Why deferred (cost vs current value, dated).
3. Trigger conditions — specific bug shapes that would justify the improvement.
4. Estimated scope when triggered.

Filing an entry without a real trigger condition is a smell — the discipline of writing the trigger forces honest assessment of whether the improvement is real or just nice-to-have. Entries with vague triggers should be challenged in review.

**Removing entries:**

When an entry is acted on (the trigger fired and the refactor landed), strike it through with the version it landed in, but keep it visible — a record of "this was opportunistically completed at version X" is useful future-you context.
