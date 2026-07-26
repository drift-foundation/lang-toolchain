# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""string-hotpath-performance-recovery: trace-cache contract teeth
(rev 2 — review blocker 2).

DRIFT_STR_TRACE is read EXACTLY ONCE during process initialization
(constructor) and published as immutable state; the retain/release
hot paths branch on a plain int.  Two SEPARATE proofs:

test_trace_output_contract (small workload — bounded output):
  * exactly ONE getenv("DRIFT_STR_TRACE") lookup per process;
  * launch UNSET, then setenv in main  -> stays DISABLED (zero [str]
    events, zero FILTER lookups — the slow path is never entered);
  * launch SET,   then unsetenv in main -> stays ENABLED, proven by a
    POST-CHANGE-ONLY marker: a String created and churned only AFTER
    the unsetenv must appear in the trace output;
  * PRESENCE semantics: DRIFT_STR_TRACE=0 enables;
  * FILTER lookups occur only on the enabled slow path;
  * both the normal and debug runtime archives.

test_trace_concurrency_hammer (4 threads x 200k retain/release):
  * runs trace-DISABLED and trace-ENABLED-with-nonmatching-FILTER
    (slow path exercised per event, output suppressed — bounded);
  * EXACT final refcount asserted ATOMICALLY in-driver:
    shared.storage->strong must equal 1 before the final release;
  * crash-free under concurrency (atomics untouched by the fix).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.language_runtime import build_runtime_archive, runtime_archive_variant

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "lang" / "language_runtime"

C_DRIVER = r"""
#include "string_runtime.h"
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* counters are ATOMIC: the hammer calls getenv from 4 threads */
static _Atomic long n_trace_lookups, n_filter_lookups;
char *__real_getenv(const char *name);
char *__wrap_getenv(const char *name) {
	if (name && strcmp(name, "DRIFT_STR_TRACE") == 0)
		atomic_fetch_add_explicit(&n_trace_lookups, 1, memory_order_relaxed);
	else if (name && strcmp(name, "DRIFT_STR_TRACE_FILTER") == 0)
		atomic_fetch_add_explicit(&n_filter_lookups, 1, memory_order_relaxed);
	return __real_getenv(name);
}

static void churn(DriftString base, int n) {
	for (int i = 0; i < n; i++) {
		DriftString c = drift_string_retain(base);
		drift_string_release(c);
	}
}

static DriftString shared;
#define N_THREADS 4
#define PER_THREAD 200000
static void *hammer(void *arg) {
	(void)arg;
	for (int i = 0; i < PER_THREAD; i++) {
		DriftString c = drift_string_retain(shared);
		drift_string_release(c);
	}
	return NULL;
}

/* modes:
 *   contract-flip-on  : launched UNSET; setenv mid-run (stays disabled)
 *   contract-flip-off : launched SET; unsetenv mid-run (stays enabled,
 *                       post-change-only marker must trace)
 *   hammer            : concurrency + exact-refcount proof
 */
int main(int argc, char **argv) {
	const char *mode = argc > 1 ? argv[1] : "contract-flip-on";

	if (strcmp(mode, "hammer") == 0) {
		shared = drift_string_from_utf8_bytes("hammer-subject", 14);
		pthread_t th[N_THREADS];
		for (int i = 0; i < N_THREADS; i++)
			pthread_create(&th[i], NULL, hammer, NULL);
		for (int i = 0; i < N_THREADS; i++)
			pthread_join(th[i], NULL);
		/* EXACT final refcount, asserted atomically: every hammer
		 * pair balanced; only the construction stake remains */
		unsigned long long rc = (unsigned long long)atomic_load_explicit(
			&shared.storage->strong, memory_order_acquire);
		if (rc != 1) {
			fprintf(stdout, "REFCOUNT-FAIL rc=%llu\n", rc);
			return 71;
		}
		fprintf(stdout, "REFCOUNT-OK rc=1\n");
		drift_string_release(shared);
		fprintf(stdout, "LOOKUPS trace=%ld filter=%ld\n",
			atomic_load(&n_trace_lookups), atomic_load(&n_filter_lookups));
		fprintf(stdout, "DONE\n");
		return 0;
	}

	DriftString base = drift_string_from_utf8_bytes("pre-flip-subject", 16);
	churn(base, 20);
	if (strcmp(mode, "contract-flip-on") == 0) {
		setenv("DRIFT_STR_TRACE", "1", 1);
	} else {
		unsetenv("DRIFT_STR_TRACE");
	}
	/* POST-CHANGE-ONLY marker: created and churned strictly after the
	 * env change; its appearance (or required absence) in stderr is
	 * the immutability proof */
	DriftString marker = drift_string_from_utf8_bytes("post-flip-marker", 16);
	churn(marker, 20);
	drift_string_release(marker);
	drift_string_release(base);
	fprintf(stdout, "LOOKUPS trace=%ld filter=%ld\n",
		atomic_load(&n_trace_lookups), atomic_load(&n_filter_lookups));
	fprintf(stdout, "DONE\n");
	return 0;
}
"""


def _build(tmp: Path, debug_style: bool) -> Path:
	driver = tmp / "driver.c"
	driver.write_text(C_DRIVER)
	archive = build_runtime_archive(
		ROOT, clang=shutil.which("clang"),
		variant=runtime_archive_variant(debug_style=debug_style,
		                                asan_enabled=False,
		                                alloc_track_enabled=False))
	out = tmp / f"trace_contract_{'debug' if debug_style else 'normal'}.bin"
	res = subprocess.run(
		["/usr/bin/clang", "-std=gnu11", "-pthread",
		 "-x", "c", str(driver), "-x", "none", str(archive),
		 "-Wl,--wrap=getenv", "-lz", "-Wl,--as-needed",
		 "-I", str(RUNTIME), "-o", str(out)],
		capture_output=True, text=True, timeout=sanitizer_timeout(240))
	assert res.returncode == 0, f"link failed:\n{res.stderr[:2000]}"
	return out


def _run(binary: Path, mode: str, env_trace: str | None,
         env_filter: str | None = None):
	env = dict(os.environ)
	env.pop("DRIFT_STR_TRACE", None)
	env.pop("DRIFT_STR_TRACE_FILTER", None)
	if env_trace is not None:
		env["DRIFT_STR_TRACE"] = env_trace
	if env_filter is not None:
		env["DRIFT_STR_TRACE_FILTER"] = env_filter
	r = subprocess.run([str(binary), mode], capture_output=True, text=True,
	                   timeout=sanitizer_timeout(300), env=env)
	assert r.returncode == 0, (mode, env_trace, r.returncode, r.stderr[-400:])
	assert "DONE" in r.stdout, r.stdout
	m = [l for l in r.stdout.splitlines() if l.startswith("LOOKUPS")][0]
	parts = dict(kv.split("=") for kv in m.split()[1:])
	return int(parts["trace"]), int(parts["filter"]), r.stdout, r.stderr


@pytest.mark.parametrize("debug_style", [False, True],
                         ids=["normal", "debug"])
def test_trace_output_contract(tmp_path: Path, debug_style: bool) -> None:
	binary = _build(tmp_path, debug_style)

	# launched UNSET, setenv mid-run: stays disabled; exactly ONE
	# trace lookup (the constructor); ZERO filter lookups; no [str]
	# events at all — including for the post-change marker
	trace, filt, _out, err = _run(binary, "contract-flip-on", None)
	assert trace == 1, f"expected exactly 1 init lookup, got {trace}"
	assert filt == 0, f"filter looked up while disabled: {filt}"
	assert "[str]" not in err, "tracing activated mid-run"
	assert "post-flip-marker" not in err

	# launched SET (=1), unsetenv mid-run: stays enabled; exactly ONE
	# trace lookup; the POST-CHANGE-ONLY marker must be traced
	trace, filt, _out, err = _run(binary, "contract-flip-off", "1")
	assert trace == 1, f"expected exactly 1 init lookup, got {trace}"
	assert "[str]" in err, "tracing did not activate when launched set"
	assert "post-flip-marker" in err, \
		"post-unsetenv churn was NOT traced — immutability broken"
	assert filt > 0, "enabled slow path must consult the filter"

	# PRESENCE semantics: DRIFT_STR_TRACE=0 still enables
	trace, _filt, _out, err = _run(binary, "contract-flip-off", "0")
	assert trace == 1
	assert "post-flip-marker" in err, \
		"presence semantics broken: '=0' must enable"


@pytest.mark.parametrize("debug_style", [False, True],
                         ids=["normal", "debug"])
def test_trace_concurrency_hammer(tmp_path: Path, debug_style: bool) -> None:
	binary = _build(tmp_path, debug_style)

	# disabled: no trace work at all on the hot path
	trace, filt, out, err = _run(binary, "hammer", None)
	assert "REFCOUNT-OK rc=1" in out, out
	assert trace == 1 and filt == 0
	assert "[str]" not in err

	# enabled with a NONMATCHING filter: the slow path runs per event
	# (filter lookups scale with traffic) but output stays suppressed;
	# refcount exactness must hold identically
	trace, filt, out, err = _run(binary, "hammer", "1", "zz-never-matches")
	assert "REFCOUNT-OK rc=1" in out, out
	assert trace == 1
	# EXACTLY one filter lookup per traced event: (retain + release)
	# x 4 threads x 200k iterations, PLUS the final release of the
	# construction stake (traced after the refcount assertion)
	assert filt == 2 * 4 * 200000 + 1, \
		f"enabled hammer must consult the filter EXACTLY per event: {filt}"
	assert "hammer-subject" not in err, "nonmatching filter leaked output"
