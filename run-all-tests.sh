#!/usr/bin/env bash
# Maintainer's private pre-handoff runner (untracked).  Runs the existing
# two-mode full suite (perf, then `just test` under memcheck and ASAN).  The
# ownership corpus is NOT run here — verify/promote it separately.  `just
# certify` is a SEPARATE, independent certification workflow and never invokes
# this script.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
log_file="$script_dir/run_all_tests.log"
: > "$log_file"
exec > >(tee -a "$log_file") 2>&1
start=$(date +%s)
report_total() {
	local end elapsed
	end=$(date +%s)
	elapsed=$((end - start))
	printf 'TOTAL TEST-RUN TIME: %dh %02dm %02ds (%ds)\n' \
		$((elapsed / 3600)) $((elapsed % 3600 / 60)) $((elapsed % 60)) "$elapsed"
}
trap report_total EXIT
just perf-protocols
echo "PERF PROTOCOLS OK"
sleep 5
DRIFT_MEMCHECK=1 just test
echo "MEMCHECK suite OK"
sleep 5
DRIFT_ASAN=1 just test
echo "ASAN suite OK"

echo "Recommended next step: just ownership-corpus-verify"
