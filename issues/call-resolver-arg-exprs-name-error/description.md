Summary: `NameError: arg_exprs` in call_resolver overload-narrowing path

Classification
- Compiler bug (correctness, fail-fast crash)
- Priority: medium (only one test exercises it; depends on whether the path is reachable from real user code)
- Pre-existing on `main` (verified via `git stash` during the robustness work session, 2026-04-07)

Symptom
- `lang/tests/traits/test_trait_overload_require.py::test_require_filters_out_unmet_overload` fails with:

  ```
  lang/driftc/checker/call_resolver.py:4980: NameError
  E   NameError: cannot access free variable 'arg_exprs' where it is not associated with a value in enclosing scope
  ```

- The failing line is `for _di, _da in enumerate(arg_exprs):` inside an inner closure of an overload-narrowing branch in `call_resolver.py`. The variable `arg_exprs` is referenced but is not in any enclosing scope at that point — Python raises `NameError` at runtime the moment the inner loop is entered.

- Reproduction: any test that exercises this overload-narrowing branch will hit it. The known reproducer is the trait-overload-require test cited above.

Why this is a real bug, not test infrastructure
- The error is a Python `NameError` raised by the compiler itself, not a test assertion failure.
- It is a fail-fast crash inside the type checker, so any user program that takes this code path gets an opaque internal compiler error with no source pointer.
- The failure is deterministic on `main` — it is not a flake.

Verification
- Confirmed pre-existing on `main` by stashing the robustness work and running the test in isolation. The same `NameError` reproduces with no robustness changes applied.
- All robustness sanity sweeps in this work session (rows #1–#15, Tier 2 cycle detector) deselected this test specifically with a comment noting it is pre-existing and out of scope for the robustness pass.

Likely cause
- A refactor of the overload-resolution path that introduced an inner closure or restructured a branch and lost the `arg_exprs` binding. The code shape (`for _di, _da in enumerate(arg_exprs)`) suggests it was originally inside a function that took `arg_exprs` as a parameter or local, and got pulled into a context that no longer has that name.

Pointers for fix
- `lang/driftc/checker/call_resolver.py:4980` is the failing line.
- Walk up the enclosing function until you find where `arg_exprs` *should* have been defined; either pass it in as a parameter, capture it from a parent scope by adding the binding, or rename to whatever the correct local is in the new context.
- Add a regression test if the existing one isn't sufficient (it currently *does* catch the bug, just hasn't been fixed yet).

Test plan
- The existing `test_require_filters_out_unmet_overload` is the regression. After the fix, the deselect-comment in any sanity-sweep call sites that mention this test should be removed.

Owner
- Unassigned. Slot into the type-checker / call-resolver queue.

Cross-references
- Surfaced repeatedly during the robustness work session 2026-04-07 (see `history.md` entries from 0.27.166 onward; each sanity sweep deselected this test).
