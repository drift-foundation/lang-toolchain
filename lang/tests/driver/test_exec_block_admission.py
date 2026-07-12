# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Block(timeout) bounded-admission pins (2026-07-11 design, DriftQuery-
approved; runtime `drift_exec_submit` + stdlib `BlockingExecutor`).

Every saturation scenario here is REAL — a 1-worker/1-slot executor
occupied by an actual task — never `exec_submit_test_override`.

Semantics pinned:
- Block success-after-wait: a submission to a full executor parks and
  is ADMITTED (capacity transfer, FIFO) when the occupant finishes.
- Block timeout: no capacity by the deadline → `Err(TIMEOUT)`; the
  timed-out submission's Destructible capture is destroyed EXACTLY
  once via the existing vt_drop path (0.33.79 contract).
- ReturnBusy: byte-identical immediate `Err(BUSY)`.
- No carrier pinning: the waiting submitter parks; cooperative VTs on
  the same single carrier keep progressing for the whole wait.
- Cancel-while-waiting → `Err(CANCELLED)` (new explicit code-3 arm).
- Yield/requeue transient: `drift_thread_yield` re-enqueues BEFORE the
  worker's running-- — conditional admission must NOT over-admit past
  the queue limit during the occupant's yields.
- Process exit with a waiter still parked: clean teardown (the
  executor shutdown drain abandons waiters; no hang, no leak).

Occupants use bounded time-based BUSY spins (never park) where the pin
requires capacity to stay held — a parked occupant legitimately frees
its slot (queue_limit bounds queued+running only; parked VTs are
outside the bound, a pre-existing semantic this slice preserves).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import asan_active, sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

_PRELUDE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;
import std.sync as sync;
import std.time as time;

fn busy_ms(ms: Int) nothrow -> Int {
	val start = time.now_monotonic();
	var spins = 0;
	while time.elapsed_ms(&start) < ms {
		spins = spins + 1;
	}
	return spins;
}
"""

# (1) Block success-after-wait + FIFO admission order.
_FIFO_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	b.queue_limit(1);
	b.timeout(conc.Duration(millis = 8000));
	val ex = conc.build_blocking_executor(b.build(), "test-exec");
	val order = conc.arc(sync.atomic_int(0));

	// Occupy the single slot with a busy (non-parking) task.
	val o1 = order.clone();
	var code = 0;
	match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| captures(move o1) => {
		val _ = busy_ms(400);
		val _ = o1.get().fetch_add(1, sync.MemoryOrder::AcqRel());
		return 0;
	})) {
		core.Result::Ok(first) => {
			var f = move first;
			// Two waiters, submitted in a known order from two spawned
			// VTs (each Block-parks inside spawn_blocking_on).
			val exa = ex;
			val o2 = order.clone();
			var wa = conc.spawn_cb(|| captures(copy exa, move o2) => {
				match conc.spawn_blocking_on(&exa, "test.op", core.callback0(|| captures(move o2) => {
					val _ = o2.get().fetch_add(1, sync.MemoryOrder::AcqRel());
					return 0;
				})) {
					core.Result::Ok(h) => { var hh = move h; match hh.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { return 1; } } },
					core.Result::Err(_) => { return 2; },
				}
				return 0;
			});
			val _ = conc.sleep(conc.Duration(millis = 150));
			val exb = ex;
			val o3 = order.clone();
			var wb = conc.spawn_cb(|| captures(copy exb, move o3) => {
				match conc.spawn_blocking_on(&exb, "test.op", core.callback0(|| captures(move o3) => {
					// FIFO: waiter A must have been admitted (and run)
					// before waiter B; both after the occupant.
					val seen = o3.get().load(sync.MemoryOrder::Acquire());
					match seen == 2 { true => {}, false => { return 0; } }
					val _ = o3.get().fetch_add(1, sync.MemoryOrder::AcqRel());
					return 0;
				})) {
					core.Result::Ok(h) => { var hh = move h; match hh.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { return 1; } } },
					core.Result::Err(_) => { return 2; },
				}
				return 0;
			});
			match wa.join() { core.Result::Ok(c) => { match c == 0 { true => {}, false => { code = 3; } } }, core.Result::Err(_) => { code = 4; } }
			match wb.join() { core.Result::Ok(c) => { match c == 0 { true => {}, false => { code = 5; } } }, core.Result::Err(_) => { code = 6; } }
			match f.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { code = 7; } }
		},
		core.Result::Err(_) => { code = 1; },
	}
	if code == 0 {
		val final_order = order.get().load(sync.MemoryOrder::Acquire());
		if final_order == 3 {
			console.println("fifo-ok");
			return 0;
		}
		return 8;
	}
	return code;
}
"""

# (2) Block timeout + Destructible capture exactly-once.
_TIMEOUT_SOURCE = _PRELUDE + """\

struct Token { tag: Int }

implement core.Destructible for Token {
	pub fn destroy(var self: Token) nothrow -> Void {
		match self.tag == 42 {
			true => { console.println("destroy-live"); },
			false => { console.println("destroy-zeroed"); },
		}
	}
}

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	b.queue_limit(1);
	b.timeout(conc.Duration(millis = 200));
	val ex = conc.build_blocking_executor(b.build(), "test-exec");

	var code = 1;
	match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| => { val _ = busy_ms(1200); return 0; })) {
		core.Result::Ok(first) => {
			var f = move first;
			val t = Token(tag = 42);
			match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| captures(move t) => {
				var mine = move t;
				return mine.tag;
			})) {
				core.Result::Ok(_) => { code = 2; },
				core.Result::Err(e) => {
					match e.kind == conc.CONCURRENCY_KIND_TIMEOUT {
						true => { console.println("timed-out"); code = 0; },
						false => { code = 3; },
					}
				},
			}
			match f.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { code = 4; } }
		},
		core.Result::Err(_) => {},
	}
	return code;
}
"""

# (3) ReturnBusy unchanged: immediate Err(BUSY).
_BUSY_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	b.queue_limit(1);
	b.on_saturation(conc.SaturationPolicy::ReturnBusy());
	val ex = conc.build_blocking_executor(b.build(), "test-exec");

	var code = 1;
	match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| => { val _ = busy_ms(600); return 0; })) {
		core.Result::Ok(first) => {
			var f = move first;
			val start = time.now_monotonic();
			match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| => { return 0; })) {
				core.Result::Ok(_) => { code = 2; },
				core.Result::Err(e) => {
					val waited = time.elapsed_ms(&start);
					match e.kind == conc.CONCURRENCY_KIND_BUSY {
						true => {
							// Immediate, not blocked-then-failed.
							if waited < 200 { console.println("busy-immediate"); code = 0; }
							else { code = 5; }
						},
						false => { code = 3; },
					}
				},
			}
			match f.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { code = 4; } }
		},
		core.Result::Err(_) => {},
	}
	return code;
}
"""

# (4) No carrier pinning: the default executor has ONE carrier; a chatty
# cooperative VT must keep progressing while main Block-waits.
_NO_PIN_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	b.queue_limit(1);
	b.timeout(conc.Duration(millis = 8000));
	val ex = conc.build_blocking_executor(b.build(), "test-exec");
	val ticks = conc.arc(sync.atomic_int(0));
	val stop = conc.arc(sync.atomic_bool(false));

	val t1 = ticks.clone();
	val s1 = stop.clone();
	var chatty = conc.spawn_cb(|| captures(move t1, move s1) => {
		// Bounded cooperative loop (deadline + stop flag).
		val start = time.now_monotonic();
		while time.elapsed_ms(&start) < 6000 {
			match s1.get().load(sync.MemoryOrder::Acquire()) {
				true => { return 0; },
				false => {},
			}
			val _ = t1.get().fetch_add(1, sync.MemoryOrder::AcqRel());
			conc.yield_now();
		}
		return 0;
	});

	var code = 1;
	match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| => { val _ = busy_ms(500); return 0; })) {
		core.Result::Ok(first) => {
			var f = move first;
			val before = ticks.get().load(sync.MemoryOrder::Acquire());
			// This submission Block-waits ~500ms.  Main's carrier must
			// stay free: chatty runs on the SAME single default carrier.
			match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| => { return 0; })) {
				core.Result::Ok(second) => {
					var sec = move second;
					val after = ticks.get().load(sync.MemoryOrder::Acquire());
					match sec.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { code = 5; } }
					if after > before + 10 { code = 0; }
					else { code = 2; }
				},
				core.Result::Err(_) => { code = 3; },
			}
			match f.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { code = 4; } }
		},
		core.Result::Err(_) => {},
	}
	stop.get().store(true, sync.MemoryOrder::Release());
	match chatty.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { return 6; } }
	if code == 0 { console.println("carrier-free"); }
	return code;
}
"""

# (5) Cancel while waiting -> Err(CANCELLED).
_CANCEL_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	b.queue_limit(1);
	b.timeout(conc.Duration(millis = 10000));
	val ex = conc.build_blocking_executor(b.build(), "test-exec");
	val outcome = conc.arc(sync.atomic_int(0));

	var code = 1;
	match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| => { val _ = busy_ms(2500); return 0; })) {
		core.Result::Ok(first) => {
			var f = move first;
			val exw = ex;
			val oc = outcome.clone();
			var waiter = conc.spawn_cb(|| captures(copy exw, move oc) => {
				match conc.spawn_blocking_on(&exw, "test.op", core.callback0(|| => { return 0; })) {
					core.Result::Ok(_) => { oc.get().store(3, sync.MemoryOrder::Release()); },
					core.Result::Err(e) => {
						match e.kind == conc.CONCURRENCY_KIND_CANCELLED {
							true => { oc.get().store(1, sync.MemoryOrder::Release()); },
							false => { oc.get().store(2, sync.MemoryOrder::Release()); },
						}
					},
				}
				return 0;
			});
			val _ = conc.sleep(conc.Duration(millis = 300));
			waiter.cancel();
			// Poll the outcome with a deadline (the cancelled VT still
			// runs its post-submit code cooperatively).
			val start = time.now_monotonic();
			var seen = 0;
			while time.elapsed_ms(&start) < 5000 {
				seen = outcome.get().load(sync.MemoryOrder::Acquire());
				if seen != 0 { break; }
				val _ = conc.sleep(conc.Duration(millis = 20));
			}
			match seen == 1 { true => { console.println("cancelled-mapped"); code = 0; }, false => { code = 10 + seen; } }
			match f.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { code = 4; } }
		},
		core.Result::Err(_) => {},
	}
	return code;
}
"""

# (6) Yield/requeue transient: occupant yields repeatedly; the waiter
# must be admitted ONLY after the occupant truly finishes.
_YIELD_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	b.queue_limit(1);
	b.timeout(conc.Duration(millis = 8000));
	val ex = conc.build_blocking_executor(b.build(), "test-exec");
	val done = conc.arc(sync.atomic_bool(false));

	var code = 1;
	val d1 = done.clone();
	match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| captures(move d1) => {
		// Yield storm: every yield re-enqueues this VT BEFORE the
		// worker's running-- — the naive-transfer over-admission shape.
		val start = time.now_monotonic();
		while time.elapsed_ms(&start) < 400 {
			conc.yield_now();
		}
		d1.get().store(true, sync.MemoryOrder::Release());
		return 0;
	})) {
		core.Result::Ok(first) => {
			var f = move first;
			val d2 = done.clone();
			match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| captures(move d2) => {
				// First observable action: the occupant must ALREADY be
				// done — admission during its yields would run us early.
				match d2.get().load(sync.MemoryOrder::Acquire()) {
					true => { return 0; },
					false => { return 1; },
				}
			})) {
				core.Result::Ok(second) => {
					var sec = move second;
					match sec.join() {
						core.Result::Ok(flag) => {
							match flag == 0 { true => { console.println("no-over-admission"); code = 0; }, false => { code = 2; } }
						},
						core.Result::Err(_) => { code = 3; },
					}
				},
				core.Result::Err(_) => { code = 5; },
			}
			match f.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { code = 4; } }
		},
		core.Result::Err(_) => {},
	}
	return code;
}
"""

# (7) Process exit with a waiter still parked (infinite admission wait):
# teardown must drain the waiter and exit cleanly.
_EXIT_WAITER_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	b.queue_limit(1);
	b.timeout(conc.Duration(millis = 0));
	val ex = conc.build_blocking_executor(b.build(), "test-exec");

	match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| => { val _ = busy_ms(1200); return 0; })) {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 1; },
	}
	val exw = ex;
	val _waiter = conc.spawn_cb(|| captures(copy exw) => {
		// Blocks in admission with NO deadline; only the shutdown drain
		// can release it.
		match conc.spawn_blocking_on(&exw, "test.op", core.callback0(|| => { return 0; })) {
			core.Result::Ok(_) => {},
			core.Result::Err(_) => {},
		}
		return 0;
	});
	val _ = conc.sleep(conc.Duration(millis = 200));
	console.println("exiting-with-waiter");
	return 0;
}
"""


# (8) Waiter starvation guard (FIFO-bypass regression, review finding):
# freed capacity must go to the oldest waiter, not to whichever direct
# submitter wins the race for ex->mu after a release.  A waiter with a
# bounded timeout must complete despite CONTINUOUS competing traffic.
# (The bypass window itself is sub-microsecond and not deterministically
# reachable from Drift; this pin is the observable starvation property,
# and the structural fix — submissions queue behind existing waiters —
# is pinned by ordering in the FIFO test above.)
_STARVATION_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	b.queue_limit(1);
	b.timeout(conc.Duration(millis = 4000));
	val ex = conc.build_blocking_executor(b.build(), "test-exec");
	val got = conc.arc(sync.atomic_bool(false));

	var code = 1;
	match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| => { val _ = busy_ms(200); return 0; })) {
		core.Result::Ok(first) => {
			var f = move first;
			// The waiter under test.
			val exw = ex;
			val g1 = got.clone();
			var waiter = conc.spawn_cb(|| captures(copy exw, move g1) => {
				match conc.spawn_blocking_on(&exw, "test.op", core.callback0(|| => { return 0; })) {
					core.Result::Ok(h) => {
						var hh = move h;
						match hh.join() { core.Result::Ok(_) => { g1.get().store(true, sync.MemoryOrder::Release()); }, core.Result::Err(_) => {} }
					},
					core.Result::Err(_) => {},
				}
				return 0;
			});
			val _ = conc.sleep(conc.Duration(millis = 50));
			// Continuous competing traffic: 12 sequential submissions,
			// each racing the admit path right after capacity frees.
			var i = 0;
			var traffic_fail = 0;
			while i < 12 {
				match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| => { val _ = busy_ms(30); return 0; })) {
					core.Result::Ok(h) => {
						var hh = move h;
						match hh.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { traffic_fail = 1; } }
					},
					core.Result::Err(_) => { traffic_fail = 1; },
				}
				i = i + 1;
			}
			match waiter.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { code = 3; } }
			match f.join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { code = 4; } }
			if code == 1 {
				match got.get().load(sync.MemoryOrder::Acquire()) {
					true => {
						if traffic_fail == 0 { console.println("no-starvation"); code = 0; }
						else { code = 5; }
					},
					false => { code = 2; },
				}
			}
		},
		core.Result::Err(_) => {},
	}
	return code;
}
"""


# (9) Many-waiter batch admission with INFINITE timeout (lost-wake
# regression): 12 simultaneous occupant finishes free up to 12 slots at
# once, so one admit call can admit >8 waiters — every admitted waiter
# must be woken even when the wake batch fills (pre-fix, waiters beyond
# the batch stayed parked FOREVER with timeout=0).
_MANY_WAITER_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(12);
	b.max_threads(12);
	b.queue_limit(12);
	b.timeout(conc.Duration(millis = 0));
	val ex = conc.build_blocking_executor(b.build(), "test-exec");
	val release = conc.arc(sync.atomic_bool(false));
	val done_count = conc.arc(sync.atomic_int(0));

	// Fill all 12 slots with occupants that spin until released
	// (bounded: 8s hard deadline on every exit path).
	var occupants: Array<conc.VirtualThread<Int>> = [];
	var i = 0;
	while i < 12 {
		val r1 = release.clone();
		match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| captures(move r1) => {
			val start = time.now_monotonic();
			while time.elapsed_ms(&start) < 8000 {
				match r1.get().load(sync.MemoryOrder::Acquire()) {
					true => { return 0; },
					false => {},
				}
			}
			return 1;
		})) {
			core.Result::Ok(h) => { occupants.push(move h); },
			core.Result::Err(_) => { return 1; },
		}
		i = i + 1;
	}
	val _ = conc.sleep(conc.Duration(millis = 150));
	// Pile 10 infinite-timeout waiters.
	var waiters: Array<conc.VirtualThread<Int>> = [];
	var j = 0;
	while j < 10 {
		val exw = ex;
		val d1 = done_count.clone();
		var wv = conc.spawn_cb(|| captures(copy exw, move d1) => {
			match conc.spawn_blocking_on(&exw, "test.op", core.callback0(|| => { return 0; })) {
				core.Result::Ok(h) => {
					var hh = move h;
					match hh.join() { core.Result::Ok(_) => { val _ = d1.get().fetch_add(1, sync.MemoryOrder::AcqRel()); }, core.Result::Err(_) => {} }
				},
				core.Result::Err(_) => {},
			}
			return 0;
		});
		waiters.push(move wv);
		j = j + 1;
	}
	val _ = conc.sleep(conc.Duration(millis = 200));
	// Simultaneous release: up to 12 slots free at once.
	release.get().store(true, sync.MemoryOrder::Release());
	var k = 0;
	var code = 0;
	while k < 10 {
		match waiters[k].join() { core.Result::Ok(_) => {}, core.Result::Err(_) => { code = 2; } }
		k = k + 1;
	}
	var m = 0;
	while m < 12 {
		match occupants[m].join() { core.Result::Ok(c) => { match c == 0 { true => {}, false => { code = 3; } } }, core.Result::Err(_) => { code = 4; } }
		m = m + 1;
	}
	if code == 0 {
		val n = done_count.get().load(sync.MemoryOrder::Acquire());
		if n == 10 { console.println("all-waiters-woken"); return 0; }
		return 5;
	}
	return code;
}
"""

# (10) >64 waiters parked at process exit: the shutdown drain must wake
# ALL of them in batches (pre-fix, waiters 65+ were abandoned parked and
# their submission callbacks leaked).
_MANY_WAITER_EXIT_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	b.queue_limit(1);
	b.timeout(conc.Duration(millis = 0));
	val ex = conc.build_blocking_executor(b.build(), "test-exec");

	match conc.spawn_blocking_on(&ex, "test.op", core.callback0(|| => { val _ = busy_ms(1500); return 0; })) {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 1; },
	}
	var j = 0;
	while j < 70 {
		val exw = ex;
		val _w = conc.spawn_cb(|| captures(copy exw) => {
			match conc.spawn_blocking_on(&exw, "test.op", core.callback0(|| => { return 0; })) {
				core.Result::Ok(_) => {},
				core.Result::Err(_) => {},
			}
			return 0;
		});
		j = j + 1;
	}
	val _ = conc.sleep(conc.Duration(millis = 400));
	console.println("exiting-with-70-waiters");
	return 0;
}
"""


# (11) Shutdown TOPOLOGY pin (review blocker): the waiter's HOME
# executor (A) is created AFTER the blocking executor (B), so registry
# newest-first destruction frees A before B's own drain would run.
# Pre-fix, B's drain unparked the waiter through freed A->mu (UAF).
# The global drain prepass must wake the waiter while BOTH are alive.
_TOPOLOGY_EXIT_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	// B first...
	var bb = conc.blocking_executor_builder();
	bb.min_threads(1);
	bb.max_threads(1);
	bb.queue_limit(1);
	bb.timeout(conc.Duration(millis = 0));
	val bex = conc.build_blocking_executor(bb.build(), "test-bex");
	// ...then A (destroyed FIRST at exit, newest-first).
	var ab = conc.executor_policy_builder();
	ab.min_threads(1);
	ab.max_threads(1);
	val aex = conc.build_executor(ab.build());

	match conc.spawn_blocking_on(&bex, "test.op", core.callback0(|| => { val _ = busy_ms(1500); return 0; })) {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 1; },
	}
	// The waiter VT HOMES on A and Block-waits (forever) on B.
	val bexw = bex;
	match conc.spawn_on(aex, core.callback0(|| captures(copy bexw) => {
		match conc.spawn_blocking_on(&bexw, "test.op", core.callback0(|| => { return 0; })) {
			core.Result::Ok(_) => {},
			core.Result::Err(_) => {},
		}
		return 0;
	})) {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 2; },
	}
	val _ = conc.sleep(conc.Duration(millis = 300));
	console.println("exiting-cross-exec-waiter");
	return 0;
}
"""


# (12) Full-admission-freeze pin: a shutdown-resumed waiter re-submits
# into a DIFFERENT, NOT-FULL blocking executor during teardown.  It must
# get Err(BUSY) from the prepass admission freeze — pre-fix, the
# free-capacity direct path enqueued (and the live workers could even
# RUN the task, printing the "ran" marker).
_SECOND_SUBMIT_SOURCE = _PRELUDE + """\

pub fn main() nothrow -> Int {
	var b1 = conc.blocking_executor_builder();
	b1.min_threads(1);
	b1.max_threads(1);
	b1.queue_limit(1);
	b1.timeout(conc.Duration(millis = 0));
	val bex1 = conc.build_blocking_executor(b1.build(), "test-exec-1");
	var b2 = conc.blocking_executor_builder();
	b2.min_threads(1);
	b2.max_threads(1);
	b2.queue_limit(4);
	val bex2 = conc.build_blocking_executor(b2.build(), "test-exec-2");

	match conc.spawn_blocking_on(&bex1, "test.op", core.callback0(|| => { val _ = busy_ms(1500); return 0; })) {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 1; },
	}
	val b1w = bex1;
	val b2w = bex2;
	val _w = conc.spawn_cb(|| captures(copy b1w, copy b2w) => {
		match conc.spawn_blocking_on(&b1w, "test.op", core.callback0(|| => { return 0; })) {
			core.Result::Ok(_) => { console.println("first-submit-admitted"); },
			core.Result::Err(_) => {
				// Resumed by the shutdown drain.  B2 has FREE capacity,
				// but the prepass closed admission everywhere.
				match conc.spawn_blocking_on(&b2w, "test.op", core.callback0(|| => { console.println("second-task-ran"); return 0; })) {
					core.Result::Ok(_) => { console.println("second-submit-admitted"); },
					core.Result::Err(e) => {
						match e.kind == conc.CONCURRENCY_KIND_BUSY {
							true => { console.println("second-submit-busy"); },
							false => { console.println("second-submit-othererr"); },
						}
					},
				}
			},
		}
		return 0;
	});
	val _ = conc.sleep(conc.Duration(millis = 300));
	console.println("exiting");
	return 0;
}
"""


def _compile(tmp_path: Path, source: str, *extra: str) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 *extra, str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240), env=os.environ.copy(),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1800:]}"
	return out


def _run(out: Path, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
	return subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(timeout_s))


def _run_valgrind(out: Path, tmp_path: Path, timeout_s: int = 240) -> tuple[subprocess.CompletedProcess[str], str]:
	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97", "--fair-sched=yes",
			f"--log-file={vg_log}", str(out)),
		capture_output=True, text=True, timeout=sanitizer_timeout(timeout_s),
	)
	return vg, (vg_log.read_text() if vg_log.exists() else "")


def test_block_success_after_wait_fifo(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _FIFO_SOURCE))
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}\nstdout:\n{run.stdout}"
	assert "fifo-ok" in run.stdout


def test_block_timeout_destructible_exactly_once(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _TIMEOUT_SOURCE))
	assert run.returncode == 0, f"exit {run.returncode}; stdout:\n{run.stdout}\nstderr:\n{run.stderr[-800:]}"
	assert "timed-out" in run.stdout
	assert run.stdout.count("destroy-live") == 1, run.stdout
	assert "destroy-zeroed" not in run.stdout, run.stdout


def test_block_timeout_destructible_exactly_once_asan(tmp_path: Path) -> None:
	out = _compile(tmp_path, _TIMEOUT_SOURCE, "--sanitize=address,undefined")
	run = _run(out, timeout_s=60)
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-1200:]}"
	assert run.stdout.count("destroy-live") == 1
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-1200:]


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_block_timeout_destructible_exactly_once_valgrind(tmp_path: Path) -> None:
	out = _compile(tmp_path, _TIMEOUT_SOURCE)
	vg, log = _run_valgrind(out, tmp_path)
	assert vg.returncode == 0, f"valgrind errors:\n{log[-1500:]}\nstdout:\n{vg.stdout}"
	assert vg.stdout.count("destroy-live") == 1, vg.stdout


def test_return_busy_unchanged(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _BUSY_SOURCE))
	assert run.returncode == 0, f"exit {run.returncode}; stdout:\n{run.stdout}\nstderr:\n{run.stderr[-800:]}"
	assert "busy-immediate" in run.stdout


def test_no_carrier_pinning_during_admission_wait(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _NO_PIN_SOURCE))
	assert run.returncode == 0, f"exit {run.returncode}; stdout:\n{run.stdout}\nstderr:\n{run.stderr[-800:]}"
	assert "carrier-free" in run.stdout


def test_cancel_while_waiting_maps_to_cancelled(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _CANCEL_SOURCE))
	assert run.returncode == 0, f"exit {run.returncode}; stdout:\n{run.stdout}\nstderr:\n{run.stderr[-800:]}"
	assert "cancelled-mapped" in run.stdout


def test_yield_requeue_no_over_admission(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _YIELD_SOURCE))
	assert run.returncode == 0, f"exit {run.returncode}; stdout:\n{run.stdout}\nstderr:\n{run.stderr[-800:]}"
	assert "no-over-admission" in run.stdout


def test_exit_with_waiter_outstanding(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _EXIT_WAITER_SOURCE))
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "exiting-with-waiter" in run.stdout


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_exit_with_waiter_outstanding_valgrind(tmp_path: Path) -> None:
	out = _compile(tmp_path, _EXIT_WAITER_SOURCE)
	vg, log = _run_valgrind(out, tmp_path)
	assert vg.returncode == 0, f"valgrind errors:\n{log[-1500:]}"


def test_waiter_not_starved_by_direct_traffic(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _STARVATION_SOURCE), timeout_s=60)
	assert run.returncode == 0, f"exit {run.returncode}; stdout:\n{run.stdout}\nstderr:\n{run.stderr[-800:]}"
	assert "no-starvation" in run.stdout


def test_many_waiter_batch_admission_all_woken(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _MANY_WAITER_SOURCE), timeout_s=60)
	assert run.returncode == 0, f"exit {run.returncode}; stdout:\n{run.stdout}\nstderr:\n{run.stderr[-800:]}"
	assert "all-waiters-woken" in run.stdout


def test_exit_with_many_waiters_outstanding(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _MANY_WAITER_EXIT_SOURCE), timeout_s=60)
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "exiting-with-70-waiters" in run.stdout


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_exit_with_many_waiters_outstanding_valgrind(tmp_path: Path) -> None:
	out = _compile(tmp_path, _MANY_WAITER_EXIT_SOURCE)
	vg, log = _run_valgrind(out, tmp_path, timeout_s=360)
	assert vg.returncode == 0, f"valgrind errors:\n{log[-1500:]}"


def test_exit_cross_executor_waiter_topology(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _TOPOLOGY_EXIT_SOURCE), timeout_s=60)
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "exiting-cross-exec-waiter" in run.stdout


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_exit_cross_executor_waiter_topology_valgrind(tmp_path: Path) -> None:
	out = _compile(tmp_path, _TOPOLOGY_EXIT_SOURCE)
	vg, log = _run_valgrind(out, tmp_path, timeout_s=360)
	assert vg.returncode == 0, f"valgrind errors:\n{log[-1500:]}"


def test_shutdown_resumed_waiter_second_submit_frozen(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _SECOND_SUBMIT_SOURCE), timeout_s=60)
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "second-submit-busy" in run.stdout, run.stdout
	assert "second-submit-admitted" not in run.stdout, run.stdout
	assert "second-task-ran" not in run.stdout, run.stdout


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_shutdown_resumed_waiter_second_submit_frozen_valgrind(tmp_path: Path) -> None:
	out = _compile(tmp_path, _SECOND_SUBMIT_SOURCE)
	vg, log = _run_valgrind(out, tmp_path, timeout_s=360)
	assert vg.returncode == 0, f"valgrind errors:\n{log[-1500:]}"
	assert "second-submit-busy" in vg.stdout, vg.stdout
