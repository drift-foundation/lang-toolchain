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

