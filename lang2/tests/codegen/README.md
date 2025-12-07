# Codegen E2E Tests (lang2)

Each codegen end-to-end test should live under `lang2/tests/codegen/<case>/`
and include at least:

- `main.drift`: source stub for the case (placeholder until parser/frontend
  lands; tests may construct MIR/SSA directly in Python for now).
- `expected.json`: exit_code/stdout/stderr expectations.

Build artifacts should go under `build/tests/lang2/<case>/` so lang2 tests stay
isolated from lang/ tests. The current e2e harnesses construct MIR/SSA directly
in pytest; once a parser is available, tests can consume `main.drift` inputs
using the shared runner pattern.
