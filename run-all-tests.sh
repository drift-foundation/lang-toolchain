#!/usr/bin/env bash
# Maintainer's private pre-handoff runner (untracked).  Contract:
# the ownership corpus EXACTLY ONCE, then the existing two-mode full
# suite.  `just certify` is a SEPARATE, independent certification
# workflow and never invokes this script.
set -euo pipefail
just ownership-corpus-check
echo "OWNERSHIP CORPUS OK"
sleep 5
DRIFT_MEMCHECK=1 just test
echo "MEMCHECK suite OK"
sleep 5
DRIFT_ASAN=1 just test
echo "ASAN suite OK"
