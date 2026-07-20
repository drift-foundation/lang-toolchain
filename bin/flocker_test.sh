#!/usr/bin/env bash
# Acceptance tests for flocker. Run with:
#   bash bin/flocker_test.sh
# Exits 0 on success, nonzero on first failed assertion.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLOCKER="${SCRIPT_DIR}/flocker"

[[ -x "${FLOCKER}" ]] || { echo "FAIL: ${FLOCKER} not executable" >&2; exit 1; }

WORK="$(mktemp -d -t flocker_test.XXXXXX)"
export FLOCKER_DIR="${WORK}/pools"
trap 'rm -rf "${WORK}"' EXIT

pass() { printf '  ok  %s\n' "$1"; }
fail() { printf '  FAIL  %s: %s\n' "$1" "$2" >&2; exit 1; }

# ── 1: stdio passthrough + exit code preservation ────────────────────────
echo "test 1: stdio + exit code"
out=$("${FLOCKER}" -k t1 -j 1 -- bash -c 'echo out; echo err >&2; exit 7' 2>/dev/null) || rc=$?
[[ "${out}" == "out" ]] || fail "1" "stdout: got '${out}'"
[[ "${rc:-0}" == "7" ]] || fail "1" "exit: got '${rc:-0}'"
err=$("${FLOCKER}" -k t1 -j 1 -- bash -c 'echo err >&2' 2>&1 >/dev/null)
[[ "${err}" == "err" ]] || fail "1" "stderr: got '${err}'"
pass "stdio passthrough + exit code"

# ── 2: -j 2 caps concurrency at 2 ────────────────────────────────────────
echo "test 2: -j 2 caps at 2 concurrent"
counter="${WORK}/counter"
echo 0 > "${counter}"
peak="${WORK}/peak"
echo 0 > "${peak}"
job() {
	(
		flock -x 200
		c=$(< "${counter}")
		c=$((c + 1))
		echo "${c}" > "${counter}"
		p=$(< "${peak}")
		if (( c > p )); then echo "${c}" > "${peak}"; fi
	) 200>"${WORK}/counter.lock"
	sleep 0.3
	(
		flock -x 200
		c=$(< "${counter}")
		echo $((c - 1)) > "${counter}"
	) 200>"${WORK}/counter.lock"
}
export -f job
export counter peak WORK
for _ in 1 2 3 4 5; do
	"${FLOCKER}" -k t2 -j 2 -- bash -c 'job' &
done
wait
peak_val=$(< "${peak}")
[[ "${peak_val}" == "2" ]] || fail "2" "peak concurrency: got ${peak_val}, expected 2"
pass "-j 2 enforced (peak=${peak_val})"

# ── 3: lock survives exec — wrapped child holds the slot ─────────────────
echo "test 3: lock survives exec"
"${FLOCKER}" -k t3 -j 1 -- sleep 3 &
parent_pid=$!
sleep 0.4
# Find the sleep child whose parent fd table inherits a flock-held token.
# Simpler proof: a second caller with -j 1 must block until sleep exits.
start=$(date +%s%N)
"${FLOCKER}" -k t3 -j 1 -- true
end=$(date +%s%N)
elapsed_ms=$(( (end - start) / 1000000 ))
wait "${parent_pid}" 2>/dev/null || true
(( elapsed_ms >= 1500 )) || fail "3" "second caller did not block (elapsed ${elapsed_ms}ms; expected >=1500)"
# Also confirm: by the time it acquires, the original token has been
# released — the second call completed in well under 3000ms past start.
pass "lock survives exec (second caller blocked ${elapsed_ms}ms)"

# ── 4: SIGTERM to wrapped child releases the lock ────────────────────────
echo "test 4: SIGTERM to wrapped child releases slot"
"${FLOCKER}" -k t4 -j 1 -- sleep 30 &
victim_pid=$!
sleep 0.3
# Find and kill the actual sleep process (exec'd inside flocker).
# Since flocker exec's, victim_pid IS the sleep process.
kill -TERM "${victim_pid}"
wait "${victim_pid}" 2>/dev/null || true
# Now a new caller should acquire immediately.
start=$(date +%s%N)
"${FLOCKER}" -k t4 -j 1 -- true
end=$(date +%s%N)
elapsed_ms=$(( (end - start) / 1000000 ))
(( elapsed_ms < 1000 )) || fail "4" "next caller did not acquire promptly after SIGTERM (${elapsed_ms}ms)"
pass "lock released on SIGTERM (next acquire ${elapsed_ms}ms)"

# ── 5: SIGTERM to flocker before slot acquired leaves no leaked state ───
echo "test 5: SIGTERM to flocker pre-acquire"
"${FLOCKER}" -k t5 -j 1 -- sleep 10 &
holder_pid=$!
sleep 0.3
"${FLOCKER}" -k t5 -j 1 -- sleep 10 &
waiter_pid=$!
sleep 0.3
kill -TERM "${waiter_pid}"
wait "${waiter_pid}" 2>/dev/null || true
# Holder still running; pool state should be clean.
ls "${FLOCKER_DIR}/t5"/ >/dev/null || fail "5" "slot dir corrupted after waiter SIGTERM"
kill -TERM "${holder_pid}" 2>/dev/null || true
wait "${holder_pid}" 2>/dev/null || true
# Pool should accept new callers fine.
"${FLOCKER}" -k t5 -j 1 -- true || fail "5" "pool unusable after SIGTERM"
pass "no leaked state after pre-acquire SIGTERM"

# ── 6: -j mismatch — first wins, second warns ────────────────────────────
echo "test 6: -j mismatch warning"
"${FLOCKER}" -k t6 -j 4 -- true
warn=$("${FLOCKER}" -k t6 -j 8 -- true 2>&1 >/dev/null)
[[ "${warn}" == *"pool sized at 4"* ]] || fail "6" "no mismatch warning: '${warn}'"
[[ "${warn}" == *"requested 8"* ]]    || fail "6" "no mismatch warning: '${warn}'"
# Should not warn when N matches.
nowarn=$("${FLOCKER}" -k t6 -j 4 -- true 2>&1 >/dev/null)
[[ -z "${nowarn}" ]] || fail "6" "spurious warning on matching N: '${nowarn}'"
pass "first -j wins, mismatch warned"

# ── 7: argument validation ───────────────────────────────────────────────
echo "test 7: argument validation"
"${FLOCKER}" -k t7 -- true 2>/dev/null && fail "7" "missing -j accepted"
"${FLOCKER}" -j 1 -- true 2>/dev/null && fail "7" "missing -k accepted"
"${FLOCKER}" -k t7 -j 0 -- true 2>/dev/null && fail "7" "-j 0 accepted"
"${FLOCKER}" -k t7 -j -1 -- true 2>/dev/null && fail "7" "-j -1 accepted"
"${FLOCKER}" -k 'bad/key' -j 1 -- true 2>/dev/null && fail "7" "bad key accepted"
"${FLOCKER}" -k t7 -j 1 2>/dev/null && fail "7" "missing COMMAND accepted"
"${FLOCKER}" --bogus 2>/dev/null && fail "7" "unknown option accepted"
pass "argument validation"

# ── 8: recovery from dead initializer ────────────────────────────────────
echo "test 8: dead-initializer recovery"
# Simulate a crashed init: create the dir and some half-state, but no .size.
mkdir -p "${FLOCKER_DIR}/t8"
: > "${FLOCKER_DIR}/t8/1"
: > "${FLOCKER_DIR}/t8/2"
# No .size → next caller should recover.
"${FLOCKER}" -k t8 -j 3 -- true || fail "8" "recovery failed"
[[ -f "${FLOCKER_DIR}/t8/.size" ]] || fail "8" "no .size after recovery"
sz=$(< "${FLOCKER_DIR}/t8/.size")
[[ "${sz}" == "3" ]] || fail "8" "recovered with wrong N: ${sz}"
[[ -f "${FLOCKER_DIR}/t8/3" ]] || fail "8" "token 3 not created during recovery"
pass "recovered from dead initializer (N=${sz})"

# ── 9: corrupted .size produces a clear diagnostic ──────────────────────
echo "test 9: corrupted .size diagnostic"
"${FLOCKER}" -k t9 -j 2 -- true
echo "potato" > "${FLOCKER_DIR}/t9/.size"
out=$("${FLOCKER}" -k t9 -j 2 -- true 2>&1) && rc=0 || rc=$?
[[ "${rc}" == "2" ]]                 || fail "9" "expected exit 2 on corrupted .size, got ${rc}"
[[ "${out}" == *"invalid .size"* ]]  || fail "9" "diagnostic missing 'invalid .size': '${out}'"
[[ "${out}" == *"potato"* ]]         || fail "9" "diagnostic missing offending value: '${out}'"
# Also non-positive integers should be rejected.
echo "0" > "${FLOCKER_DIR}/t9/.size"
"${FLOCKER}" -k t9 -j 2 -- true 2>/dev/null && fail "9" ".size=0 accepted"
echo "-3" > "${FLOCKER_DIR}/t9/.size"
"${FLOCKER}" -k t9 -j 2 -- true 2>/dev/null && fail "9" ".size=-3 accepted"
pass "corrupted .size produces clear diagnostic"

# ── 10: --heartbeat off by default — no extra output, exit code preserved ─
echo "test 10: no --heartbeat ⇒ byte-identical (no ticks)"
rc=0
out=$("${FLOCKER}" -k t10 -j 1 -- bash -c 'echo only-this; exit 5' 2>/dev/null) || rc=$?
[[ "${out}" == "only-this" ]] || fail "10" "unexpected stdout without --heartbeat: '${out}'"
[[ "${rc}" == "5" ]]          || fail "10" "exit not preserved: got '${rc}'"
[[ "${out}" != *"[flocker]"* ]] || fail "10" "heartbeat line leaked with flag absent"
pass "default off: no ticks, exit code preserved"

# ── 11: --heartbeat emits ticks for a long job, preserves exit code ───────
echo "test 11: --heartbeat emits progress + preserves exit"
rc=0
out=$("${FLOCKER}" -k t11 -j 2 --heartbeat 1 -- bash -c 'sleep 2.5; exit 0' 2>/dev/null) || rc=$?
hb_count=$(grep -c "^\[flocker\] key=t11" <<< "${out}" || true)
(( hb_count >= 2 )) || fail "11" "expected >=2 heartbeat lines over ~2.5s at 1s cadence, got ${hb_count}: '${out}'"
[[ "${rc}" == "0" ]] || fail "11" "exit not preserved through heartbeat: '${rc}'"
# The status line reports real pool state (held includes our own slot).
[[ "${out}" == *"slot="*"/2"* ]] || fail "11" "status line missing slot field: '${out}'"
[[ "${out}" == *"held="* ]]      || fail "11" "status line missing held field: '${out}'"
# Nonzero exit must also propagate through the fork-wait path.
rc=0
"${FLOCKER}" -k t11 -j 2 --heartbeat 1 -- bash -c 'sleep 1.5; exit 13' >/dev/null 2>&1 || rc=$?
[[ "${rc}" == "13" ]] || fail "11" "nonzero exit not propagated through heartbeat: got '${rc}'"
pass "heartbeat ticks (${hb_count} lines) + exit preserved (0 and 13)"

# ── 12: --heartbeat forwards SIGTERM ⇒ child dies, slot released ──────────
echo "test 12: --heartbeat SIGTERM teardown releases slot"
"${FLOCKER}" -k t12 -j 1 --heartbeat 1 -- sleep 30 &
hb_pid=$!
sleep 0.5
kill -TERM "${hb_pid}"
wait "${hb_pid}" 2>/dev/null || true
# Slot must be free immediately — a fresh -j 1 caller acquires promptly.
start=$(date +%s%N)
"${FLOCKER}" -k t12 -j 1 -- true
end=$(date +%s%N)
elapsed_ms=$(( (end - start) / 1000000 ))
(( elapsed_ms < 1000 )) || fail "12" "slot not released after heartbeat-mode SIGTERM (${elapsed_ms}ms)"
pass "heartbeat SIGTERM tears down child + releases slot (${elapsed_ms}ms)"

# ── 13: --heartbeat argument validation ──────────────────────────────────
echo "test 13: --heartbeat validation"
"${FLOCKER}" -k t13 -j 1 --heartbeat 0 -- true 2>/dev/null && fail "13" "--heartbeat 0 accepted"
"${FLOCKER}" -k t13 -j 1 --heartbeat -2 -- true 2>/dev/null && fail "13" "--heartbeat -2 accepted"
"${FLOCKER}" -k t13 -j 1 --heartbeat abc -- true 2>/dev/null && fail "13" "--heartbeat abc accepted"
"${FLOCKER}" -k t13 -j 1 --heartbeat -- true 2>/dev/null && fail "13" "--heartbeat consuming '--' accepted"
pass "--heartbeat validation"

# ── 14: corrupt completed pool fails closed (no silent repair) ──────────
# TEST_INFRA_BUG regression (certification runner, 2026-07-20): a pool
# with .size present but a token missing must FAIL CLOSED — exit 2, the
# wrapped command NOT executed, the token NOT recreated (a running
# caller may still hold the unlinked token's inode; recreating would
# open a second independent slot pool), and the diagnostic must name
# the key, the pool path, and the missing token.
echo "test 14: corrupt completed pool fails closed"
"${FLOCKER}" -k t14 -j 2 -- true
rm "${FLOCKER_DIR}/t14/1"
marker="${WORK}/t14.ran"
out=$("${FLOCKER}" -k t14 -j 2 -- touch "${marker}" 2>&1) && rc=0 || rc=$?
[[ "${rc}" == "2" ]] || fail "14" "expected exit 2 on corrupt pool, got ${rc}: '${out}'"
[[ ! -e "${marker}" ]] || fail "14" "wrapped command executed on corrupt pool"
[[ ! -e "${FLOCKER_DIR}/t14/1" ]] || fail "14" "missing token was silently recreated"
[[ "${out}" == *"'t14'"* ]] || fail "14" "diagnostic missing key: '${out}'"
[[ "${out}" == *"${FLOCKER_DIR}/t14"* ]] || fail "14" "diagnostic missing pool path: '${out}'"
[[ "${out}" == *"${FLOCKER_DIR}/t14/1"* ]] || fail "14" "diagnostic missing token path: '${out}'"
[[ "${out}" == *"corrupt"* ]] || fail "14" "diagnostic missing corruption statement: '${out}'"
[[ "${out}" == *"refused"* ]] || fail "14" "diagnostic missing repair refusal: '${out}'"
[[ "${out}" == *"FLOCKER_DIR"* ]] || fail "14" "diagnostic missing recovery guidance: '${out}'"
# A token that is not a regular file is equally corrupt — and must not
# be mutated.
"${FLOCKER}" -k t14b -j 1 -- true
rm "${FLOCKER_DIR}/t14b/1"
mkdir "${FLOCKER_DIR}/t14b/1"
"${FLOCKER}" -k t14b -j 1 -- true 2>/dev/null && fail "14" "non-regular-file token accepted"
[[ -d "${FLOCKER_DIR}/t14b/1" ]] || fail "14" "non-regular token was mutated"
pass "corrupt completed pool fails closed (no repair, no execution)"

echo
echo "all tests passed"
