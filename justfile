set shell := ["bash", "-lc"]
set quiet
CLANG_BIN := "clang-15"

test: test-ssa test-e2e
	@echo "test-ssa test-e2e: Success."

test-e2e:
	./.venv/bin/python3 tests/e2e_runner.py

test-ssa:
	PYTHONPATH=. ./.venv/bin/python3 -m lang.mir_ssa_tests
	./.venv/bin/python3 tests/ssa_check_smoke.py
	./.venv/bin/python3 tests/ssa_programs_test.py
	if ! command -v {{CLANG_BIN}} >/dev/null 2>&1; then \
	  echo "{{CLANG_BIN}} is missing"; \
	  exit 1; \
	fi
	{{CLANG_BIN}} -Ilang -Ilang/runtime -o /tmp/runtime_error_dummy_raw tests/runtime_error_dummy_raw.c lang/runtime/error_dummy.c lang/runtime/string_runtime.c && /tmp/runtime_error_dummy_raw
	{{CLANG_BIN}} -Ilang -Ilang/runtime -o /tmp/runtime_error_args_none tests/runtime_error_args_none.c lang/runtime/error_dummy.c lang/runtime/string_runtime.c && /tmp/runtime_error_args_none

test-runtime-c:
	#!/usr/bin/env bash
	if ! command -v {{CLANG_BIN}} >/dev/null 2>&1; then
	  echo "{{CLANG_BIN}} is missing"
	  exit 1
	fi
	{{CLANG_BIN}} -Ilang -Ilang/runtime -o /tmp/runtime_error_dummy_raw tests/runtime_error_dummy_raw.c lang/runtime/error_dummy.c lang/runtime/string_runtime.c && /tmp/runtime_error_dummy_raw
	{{CLANG_BIN}} -Ilang -Ilang/runtime -o /tmp/runtime_error_args_none tests/runtime_error_args_none.c lang/runtime/error_dummy.c lang/runtime/string_runtime.c && /tmp/runtime_error_args_none
	echo "test-runtime-c OK"

test-e2e-ssa-subset:
	PYTHONPATH=. ./.venv/bin/python3 tests/e2e_runner.py hello throw_try try_catch try_call_error try_event_catch_stmt try_event_catch_expr try_event_catch_no_fallback array_string for_array console_hello ref_struct_mutation

parse-all: parse-examples
	@echo "parse-examples: Success."

# Parse every Drift example under examples/
parse-examples:
	./.venv/bin/python3 tools/draft_linter.py examples

stage-for-review:
	rm -rf staged
	mkdir staged
	git ls-files -m -o --exclude-standard | while read f; do mkdir -p "staged/$(dirname "$f")"; cp "$f" "staged/$f"; done 
