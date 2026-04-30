# Repo Agent Rules

## Git usage (strict)

- Use `git` **only** for reviewing history or diffing (e.g. `git diff`, `git log`, `git show`, `git blame`).
- **Do not** stage or unstage changes (`git add`, `git restore --staged`, etc.) without explicit permission.
- **Do not** perform any mutating git operations without explicit permission (including `git commit`, `git merge`, `git rebase`, `git cherry-pick`, `git reset`, `git checkout/switch`, `git stash`, and tag/branch operations).
- **Do not** wrap long lines (calls with many arguments, long expressions) for readability; avoid indentation churn, specially if code is deeply nested.
- **Do not** edit exisit tests without a clear confirmation it's OK. No bending around tests to patch compiler/infra deficiencies.

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
		- Behavior-changing compiler/toolchain fixes that do not change ABI boundary shape must still bump compiler minor version (`DRIFTC_VERSION`).
		- ABI shape changes require both: compiler version bump and ABI version bump.

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
