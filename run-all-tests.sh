#!/usr/bin/env bash
# Maintainer's private pre-handoff runner (untracked).  Contract:
# the ownership corpus EXACTLY ONCE, then the existing two-mode full
# suite.  `just certify` is a SEPARATE, independent certification
# workflow and never invokes this script.
set -euo pipefail
start=$(date +%s)
report_total() {
	local end elapsed
	end=$(date +%s)
	elapsed=$((end - start))
	printf 'TOTAL TEST-RUN TIME: %dh %02dm %02ds (%ds)\n' \
		$((elapsed / 3600)) $((elapsed % 3600 / 60)) $((elapsed % 60)) "$elapsed"
}
trap report_total EXIT
just ownership-corpus-check
echo "OWNERSHIP CORPUS OK"
sleep 5
just perf-protocols
echo "PERF PROTOCOLS OK"
sleep 5
DRIFT_MEMCHECK=1 just test
echo "MEMCHECK suite OK"
sleep 5
DRIFT_ASAN=1 just test
echo "ASAN suite OK"
