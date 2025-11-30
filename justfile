set shell := ["bash", "-lc"]

test: test-ssa test-e2e
	@echo "Success."

test-e2e:
	./.venv/bin/python3 tests/e2e_runner.py

test-ssa:
	PYTHONPATH=. ./.venv/bin/python3 -m lang.mir_ssa_tests
	./.venv/bin/python3 tests/ssa_check_smoke.py
	./.venv/bin/python3 tests/ssa_programs_test.py

test-e2e-ssa-subset:
	PYTHONPATH=. ./.venv/bin/python3 tests/e2e_runner.py hello throw_try try_catch try_call_error try_event_catch_stmt try_event_catch_expr try_event_catch_no_fallback array_string for_array console_hello ref_struct_mutation

parse-all: parse-examples
	@echo "Parsing successful."

# Parse every Drift example under examples/
parse-examples:
	./.venv/bin/python3 tools/draft_linter.py examples

stage-for-review:
	rm -rf staged
	mkdir staged
	git ls-files -m -o --exclude-standard | while read f; do mkdir -p "staged/$(dirname "$f")"; cp "$f" "staged/$f"; done 
