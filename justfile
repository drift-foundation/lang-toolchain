set shell := ["bash", "-lc"]
set quiet
CLANG_BIN := "clang-15"

# Default task: run the staged lang2 compiler tests.
default: lang2-test
	@echo "lang2-test: Success."

test: test-ssa test-e2e
	@echo "test-ssa test-e2e: Success."

test-e2e:
	rm -rf build/tests/e2e
	./.venv/bin/python3 tests/e2e_runner.py

test-ssa:
	PYTHONPATH=. ./.venv/bin/python3 -m lang.mir_ssa_tests
	./.venv/bin/python3 tests/ssa_check_smoke.py
	./.venv/bin/python3 tests/ssa_programs_test.py
	if ! command -v {{CLANG_BIN}} >/dev/null 2>&1; then \
	  echo "{{CLANG_BIN}} is missing"; \
	  exit 1; \
	fi
	{{CLANG_BIN}} -Ilang -Ilang/runtime -o /tmp/runtime_error_dummy_raw tests/runtime_error_dummy_raw.c lang/runtime/error_dummy.c lang/runtime/string_runtime.c lang/runtime/diagnostic_runtime.c && /tmp/runtime_error_dummy_raw
	# legacy args test removed
	{{CLANG_BIN}} -Ilang -Ilang/runtime -o /tmp/runtime_diagnostic_value tests/runtime_diagnostic_value.c lang/runtime/diagnostic_runtime.c lang/runtime/string_runtime.c && /tmp/runtime_diagnostic_value

test-runtime-c:
	#!/usr/bin/env bash
	if ! command -v {{CLANG_BIN}} >/dev/null 2>&1; then
	  echo "{{CLANG_BIN}} is missing"
	  exit 1
	fi
	{{CLANG_BIN}} -Ilang -Ilang/runtime -o /tmp/runtime_error_dummy_raw tests/runtime_error_dummy_raw.c lang/runtime/error_dummy.c lang/runtime/string_runtime.c && /tmp/runtime_error_dummy_raw
	# legacy args test removed
	echo "test-runtime-c OK"

test-e2e-ssa-subset:
	rm -rf build/tests/e2e
	PYTHONPATH=. ./.venv/bin/python3 tests/e2e_runner.py hello throw_try try_catch try_call_error try_event_catch_stmt try_event_catch_expr try_event_catch_no_fallback array_string for_array console_hello ref_struct_mutation

# Loop a built e2e binary repeatedly to catch flakiness.
run-e2e-loop CASE="exception_args_dot" RUNS="1000":
	exe="build/tests/e2e/{{CASE}}/a.out"; \
	if [ ! -x "$exe" ]; then \
	  echo "missing $exe; build the case first (e.g., just test-e2e {{CASE}})" >&2; \
	  exit 1; \
	fi; \
	expect="tests/e2e/{{CASE}}/expected.json"; \
	./.venv/bin/python3 tools/run_loop.py --exe "$exe" --runs {{RUNS}} --expect-file "$expect"

parse-all: parse-examples
	@echo "parse-examples: Success."

# Parse every Drift example under examples/
parse-examples:
	./.venv/bin/python3 tools/draft_linter.py examples

# Lang2 staged compiler tests
lang2-test: lang2-stage1-test lang2-stage2-test lang2-stage3-test lang2-stage4-test lang2-parser-test lang2-core-test lang2-driver-test lang2-llvm-test lang2-codegen-test lang2-borrow-test lang2-type-checker-test lang2-method-registry-test
	@echo "lang2 tests: Success."

lang2-stage1-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/stage1

lang2-stage2-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/stage2

lang2-stage3-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/stage3

lang2-stage4-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/stage4

# Parser tests (lang2 parser copy + adapter).
lang2-parser-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/parser

# Core TypeEnv/TypeTable tests.
lang2-core-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/core

# Type checker tests (typed HIR + resolution).
lang2-type-checker-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/type_checker

# Method registry/resolver tests.
lang2-method-registry-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/method_registry

# Driver/integration tests (driftc pipeline, try sugar, declared events).
lang2-driver-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/driver

# Basic LLVM codegen smoke test (llvmlite), kept separate from pytest collection.
lang2-llvm-test:
	./.venv/bin/python3 tools/test-llvm/test_codegen.py /tmp/lang2_test_codegen.o

# LLVM textual codegen tests (SSA→LLVM IR).
lang2-codegen-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	# Clean codegen artifacts to keep cases isolated between runs.
	rm -rf build/tests/lang2
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/codegen/llvm/tests
	# Run clang-based IR cases (per-case dirs under lang2/codegen/ir_cases).
	PYTHONPATH=. ./.venv/bin/python3 lang2/codegen/ir_cases/e2e_runner.py
	# Run Drift-source e2e cases (per-case dirs under lang2/codegen/tests/e2e).
	PYTHONPATH=. ./.venv/bin/python3 lang2/codegen/tests/e2e/runner.py

# Lang2 e2e runner (lang2.driftc: json + run modes against tests/e2e)
lang2-e2e CASES="":
	PYTHONPATH=. ./.venv/bin/python3 lang2/codegen/codegen_runner.py {{CASES}}

# Borrow checker scaffolding tests.
lang2-borrow-test:
	# Ensure pytest is available in the venv
	if ! ./.venv/bin/python3 -m pytest --version >/dev/null 2>&1; then \
	  echo "pytest is missing in .venv; please install it (e.g., .venv/bin/python3 -m pip install pytest)"; \
	  exit 1; \
	fi
	PYTHONPATH=. ./.venv/bin/python3 -m pytest -v lang2/tests/borrow_checker

stage-for-review:
	#!/usr/bin/env bash
	staged_dir=staged
	rm -rf "$staged_dir"
	mkdir -p "$staged_dir"
	git ls-files -m -o --exclude-standard | while IFS= read -r f; do
		[ -f "$f" ] || continue
		mkdir -p "$staged_dir/$(dirname "$f")"
		cp -- "$f" "$staged_dir/$f"
	done
	# rm -f staged.tar.gz
	# zip -r staged.zip "$staged_dir"
