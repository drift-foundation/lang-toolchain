#!/usr/bin/env bash
# Acceptance tests for tools/drift_test_run.py — the shared job executor.
# Hermetic: plans use shell builtins (echo/sleep/true/false), no driftc needed.
# Run:  bash tools/drift_test_run_test.sh    (exit 0 = all pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNNER="${SCRIPT_DIR}/drift_test_run.py"
PY="${REPO_ROOT}/.venv/bin/python3"
[[ -x "${PY}" ]] || PY="python3"

[[ -f "${RUNNER}" ]] || { echo "FAIL: ${RUNNER} not found" >&2; exit 1; }

WORK="$(mktemp -d -t dtr_test.XXXXXX)"
export FLOCKER_DIR="${WORK}/flocker"
trap 'rm -rf "${WORK}"' EXIT

pass() { printf '  ok  %s\n' "$1"; }
fail() { printf '  FAIL  %s: %s\n' "$1" "$2" >&2; exit 1; }

run() { "${PY}" "${RUNNER}" "$@"; }

# ── 1: happy path — two phases, parallel + dedup + needs ────────────────
echo "test 1: happy path (phases, parallel, dedup, needs)"
plan="${WORK}/p1.json"
cat > "${plan}" <<EOF
{
  "name": "t1",
  "phases": [
    {"name": "build", "jobs": [
      {"id": "b1", "cmd": ["bash","-c","echo build1 > {work}/b1.out"], "out": "{work}/b1.out"},
      {"id": "b2", "cmd": ["bash","-c","echo build2 > {work}/b2.out"], "out": "{work}/b2.out"},
      {"id": "b1-dup", "cmd": ["bash","-c","echo SHOULD-NOT-RUN > {work}/b1.out"], "out": "{work}/b1.out"}
    ]},
    {"name": "check", "jobs": [
      {"id": "r1", "cmd": ["true"], "needs": ["b1"]},
      {"id": "r2", "cmd": ["true"], "needs": ["b2"]}
    ]}
  ]
}
EOF
out=$(run --plan "${plan}" --work-dir "${WORK}/w1" --jobs 2 2>&1); rc=$?
[[ "${rc}" == "0" ]] || fail "1" "expected exit 0, got ${rc}: ${out}"
[[ "${out}" == *"skip  b1-dup"* ]] || fail "1" "dedup did not skip b1-dup: ${out}"
[[ "$(cat "${WORK}/w1/b1.out" 2>/dev/null)" == "build1" ]] || fail "1" "dedup let the dup overwrite b1.out"
[[ "${out}" == *"4 ok, 0 failed, 1 skipped"* ]] || fail "1" "summary wrong: ${out}"
pass "happy path + dedup + skip count"

# ── 2: failure propagation + stop-on-phase-failure ──────────────────────
echo "test 2: failure stops subsequent phases"
plan="${WORK}/p2.json"
cat > "${plan}" <<EOF
{
  "name": "t2",
  "phases": [
    {"name": "build", "jobs": [
      {"id": "ok1", "cmd": ["true"]},
      {"id": "bad", "cmd": ["bash","-c","echo boom >&2; exit 3"]}
    ]},
    {"name": "check", "jobs": [
      {"id": "should-not-run", "cmd": ["bash","-c","echo RAN > {work}/ran.marker"]}
    ]}
  ]
}
EOF
out=$(run --plan "${plan}" --work-dir "${WORK}/w2" --jobs 2 2>&1); rc=$?
[[ "${rc}" == "1" ]] || fail "2" "expected exit 1 on failure, got ${rc}"
[[ "${out}" == *"stopping"* ]] || fail "2" "did not stop after failed phase: ${out}"
[[ ! -f "${WORK}/w2/ran.marker" ]] || fail "2" "second phase ran despite failure"
[[ "${out}" == *"boom"* ]] || fail "2" "failed-job log tail not surfaced: ${out}"
pass "failure ⇒ exit 1, later phase skipped, log tail shown"

# ── 3: --keep-going runs later phases despite failure ───────────────────
echo "test 3: --keep-going"
out=$(run --plan "${plan}" --work-dir "${WORK}/w3" --jobs 2 --keep-going 2>&1); rc=$?
[[ "${rc}" == "1" ]] || fail "3" "expected exit 1 (a job still failed), got ${rc}"
[[ -f "${WORK}/w3/ran.marker" ]] || fail "3" "--keep-going did not run the later phase"
pass "--keep-going continues, still exits 1"

# ── 4: serial group runs one-at-a-time in order ─────────────────────────
echo "test 4: serial group ordering + exclusivity"
plan="${WORK}/p4.json"
cat > "${plan}" <<EOF
{
  "name": "t4",
  "phases": [
    {"name": "measure", "jobs": [
      {"id": "m0", "cmd": ["bash","-c","echo 0 >> {work}/seq.txt; sleep 0.2"], "mode": "serial", "group": "g", "order": 0},
      {"id": "m1", "cmd": ["bash","-c","echo 1 >> {work}/seq.txt; sleep 0.2"], "mode": "serial", "group": "g", "order": 1},
      {"id": "m2", "cmd": ["bash","-c","echo 2 >> {work}/seq.txt"], "mode": "serial", "group": "g", "order": 2}
    ]}
  ]
}
EOF
mkdir -p "${WORK}/w4"
out=$(run --plan "${plan}" --work-dir "${WORK}/w4" --jobs 4 2>&1); rc=$?
[[ "${rc}" == "0" ]] || fail "4" "expected exit 0, got ${rc}: ${out}"
seq_got=$(tr '\n' ' ' < "${WORK}/w4/seq.txt")
[[ "${seq_got}" == "0 1 2 " ]] || fail "4" "serial order wrong: '${seq_got}'"
pass "serial group ran in declared order"

# ── 5: --dry-run prints flocker argv, executes nothing ──────────────────
echo "test 5: --dry-run"
plan="${WORK}/p5.json"
cat > "${plan}" <<EOF
{
  "name": "t5",
  "phases": [{"name":"build","jobs":[
    {"id":"j","cmd":["bash","-c","echo SHOULD-NOT-RUN > {work}/dry.marker"]}
  ]}]
}
EOF
out=$(run --plan "${plan}" --work-dir "${WORK}/w5" --jobs 3 --dry-run 2>&1); rc=$?
[[ "${rc}" == "0" ]] || fail "5" "dry-run exit: ${rc}"
[[ "${out}" == *"--key drift-jobs -j 3 --"* ]] || fail "5" "dry-run missing flocker wrap: ${out}"
[[ ! -f "${WORK}/w5/dry.marker" ]] || fail "5" "dry-run executed a job"
pass "dry-run shows wrapped argv, runs nothing"

# ── 6: wrap:memcheck expands to canonical valgrind in the argv ──────────
echo "test 6: wrap → canonical valgrind"
plan="${WORK}/p6.json"
cat > "${plan}" <<EOF
{
  "name": "t6",
  "phases": [{"name":"check","jobs":[
    {"id":"mc","cmd":["{work}/bin"],"wrap":"memcheck"}
  ]}]
}
EOF
out=$(run --plan "${plan}" --work-dir "${WORK}/w6" --jobs 1 --dry-run 2>&1); rc=$?
[[ "${out}" == *"valgrind --tool=memcheck"* ]] || fail "6" "no memcheck expansion: ${out}"
[[ "${out}" == *"--error-exitcode=97"* ]]      || fail "6" "missing canonical --error-exitcode: ${out}"
[[ "${out}" == *"--fair-sched=yes"* ]]         || fail "6" "missing --fair-sched=yes: ${out}"
pass "wrap:memcheck → canonical valgrind incantation"

# ── 7: budget — default sources pytest_jobs.py; --jobs overrides ────────
echo "test 7: budget sourcing"
plan="${WORK}/p7.json"
cat > "${plan}" <<EOF
{"name":"t7","phases":[{"name":"b","jobs":[{"id":"j","cmd":["true"]}]}]}
EOF
out=$(DRIFT_TEST_JOBS=3 run --plan "${plan}" --work-dir "${WORK}/w7" --dry-run 2>&1)
[[ "${out}" == *"-j 3 pool="* ]] || fail "7" "did not source DRIFT_TEST_JOBS=3: ${out}"
out=$(DRIFT_TEST_JOBS=3 run --plan "${plan}" --work-dir "${WORK}/w7b" --jobs 7 --dry-run 2>&1)
[[ "${out}" == *"-j 7 pool="* ]] || fail "7" "--jobs did not override budget: ${out}"
pass "budget from pytest_jobs.py protocol; --jobs overrides"

# ── 8: plan validation errors (exit 2) ──────────────────────────────────
echo "test 8: validation errors"
bad="${WORK}/bad.json"
echo '{"name":"x"}' > "${bad}"
run --plan "${bad}" --work-dir "${WORK}/w8" 2>/dev/null && fail "8" "missing phases accepted"
echo '{"name":"x","phases":[{"name":"p","jobs":[{"id":"a","cmd":["true"]},{"id":"a","cmd":["true"]}]}]}' > "${bad}"
err=$(run --plan "${bad}" --work-dir "${WORK}/w8" 2>&1); rc=$?
[[ "${rc}" == "2" ]] || fail "8" "duplicate id not exit 2 (got ${rc})"
[[ "${err}" == *"duplicate job id 'a'"* ]] || fail "8" "no duplicate-id diagnostic: ${err}"
echo '{"name":"x","phases":[{"name":"p","jobs":[{"id":"a","cmd":["true"],"needs":["a"]}]}]}' > "${bad}"
err=$(run --plan "${bad}" --work-dir "${WORK}/w8" 2>&1); rc=$?
[[ "${rc}" == "2" ]] || fail "8" "same-phase needs not rejected"
[[ "${err}" == *"earlier phase"* ]] || fail "8" "no phase-barrier guidance: ${err}"
echo '{"name":"x","phases":[{"name":"p","jobs":[{"id":"a","cmd":["true"],"wrap":"tsan"}]}]}' > "${bad}"
run --plan "${bad}" --work-dir "${WORK}/w8" 2>/dev/null && fail "8" "bad wrap accepted"
pass "validation: missing phases / dup id / same-phase needs / bad wrap"

# ── 9: JSON report ──────────────────────────────────────────────────────
echo "test 9: --report JSON"
plan="${WORK}/p9.json"
cat > "${plan}" <<EOF
{"name":"t9","phases":[{"name":"b","jobs":[
  {"id":"good","cmd":["true"]},
  {"id":"bad","cmd":["false"]}
]}]}
EOF
rep="${WORK}/report.json"
run --plan "${plan}" --work-dir "${WORK}/w9" --jobs 2 --keep-going --report "${rep}" >/dev/null 2>&1
[[ -f "${rep}" ]] || fail "9" "no report written"
"${PY}" - "$rep" <<'PYEOF' || fail "9" "report content wrong"
import json,sys
r=json.load(open(sys.argv[1]))
assert r["plan"]=="t9", r
st={j["id"]:j["status"] for j in r["results"]}
assert st.get("good")=="ok" and st.get("bad")=="fail", st
PYEOF
pass "report JSON has per-job status"

# ── 10: env overlay applied + sanitizer defaults present ────────────────
echo "test 10: env overlay + sanitizer defaults"
plan="${WORK}/p10.json"
cat > "${plan}" <<EOF
{"name":"t10","phases":[{"name":"b","jobs":[
  {"id":"e","cmd":["bash","-c","echo MYVAR=\$MYVAR ASAN=\$ASAN_OPTIONS > {work}/env.txt"],"env":{"MYVAR":"hello"}}
]}]}
EOF
mkdir -p "${WORK}/w10"
run --plan "${plan}" --work-dir "${WORK}/w10" --jobs 1 >/dev/null 2>&1
got=$(cat "${WORK}/w10/env.txt" 2>/dev/null)
[[ "${got}" == *"MYVAR=hello"* ]] || fail "10" "env overlay not applied: ${got}"
[[ "${got}" == *"ASAN=detect_leaks=0"* ]] || fail "10" "sanitizer default not set: ${got}"
pass "per-job env overlay + sanitizer defaults"

echo
echo "all tests passed"
