# regex-engine-allocation-removal: faithful Python replica of the
# std.regex NFA engine (stdlib/std/regex/regex.drift @ 0.33.88) with
# exact work counters.  Purpose: PREDICT candidate starts, bytes
# processed, NFA work, and alloc/free counts for every count-window
# op; run_bench.py reconciles these against the wrap-counter
# observations (residual must be ZERO) and against the real binary's
# CHECK lines (match-result fidelity).
#
# Fidelity notes (each mirrors a specific regex.drift construct):
#   * parser: straight port of _parse_alternation/_parse_sequence/
#     _parse_quantified/_parse_atom/_parse_char_class/_parse_escape
#     for the constructs the bench patterns use;
#   * _node_size/_emit_node/_nfa_compile: literal ports (absolute
#     jump targets, no backpatching);
#   * executor: literal port of _add_state/_byte_matches/
#     _try_match_at_range/_find_from_range/is_match;
#   * allocation model (regex.drift:944-982): per anchored attempt
#     2 allocations (bitmap :944, initial clist :945); per consumed
#     byte 2 allocations (seeds :958, replacement clist :969); every
#     one of them dies inside the attempt, so frees == allocs.
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- AST

# nodes: ("lit", byte) ("dot",) ("anchor", "start"|"end")
#        ("class", negated, [(lo,hi)...]) ("group", [children])
#        ("alt", [branches]) ("repeat", child, "star"|"plus"|"question")


def parse(pattern: str):
	data = pattern.encode()
	pos = [0]

	def peek():
		return data[pos[0]] if pos[0] < len(data) else -1

	def at_end():
		return pos[0] >= len(data)

	def parse_alternation():
		first = parse_sequence()
		if peek() != 124:
			return first
		branches = [first]
		while peek() == 124:
			pos[0] += 1
			branches.append(parse_sequence())
		return ("alt", branches)

	def parse_sequence():
		children = []
		while not at_end():
			ch = peek()
			if ch in (124, 41):
				break
			children.append(parse_quantified())
		if len(children) == 1:
			return children[0]
		return ("group", children)

	def parse_quantified():
		atom = parse_atom()
		if at_end():
			return atom
		ch = peek()
		if ch == 42:
			pos[0] += 1
			return ("repeat", atom, "star")
		if ch == 43:
			pos[0] += 1
			return ("repeat", atom, "plus")
		if ch == 63:
			pos[0] += 1
			return ("repeat", atom, "question")
		return atom

	def parse_atom():
		ch = peek()
		if ch == 46:
			pos[0] += 1
			return ("dot",)
		if ch == 94:
			pos[0] += 1
			return ("anchor", "start")
		if ch == 36:
			pos[0] += 1
			return ("anchor", "end")
		if ch == 40:
			pos[0] += 1
			inner = parse_alternation()
			assert peek() == 41, "unclosed-group"
			pos[0] += 1
			return inner
		if ch == 91:
			return parse_char_class()
		if ch == 92:
			raise NotImplementedError("escapes not needed by bench patterns")
		assert ch not in (41, 42, 43, 63), "parse error"
		pos[0] += 1
		return ("lit", ch)

	def parse_char_class():
		pos[0] += 1
		negated = False
		if peek() == 94:
			negated = True
			pos[0] += 1
		ranges = []
		first = True
		while not at_end() and (peek() != 93 or first):
			first = False
			lo = data[pos[0]]
			pos[0] += 1
			if (peek() == 45 and pos[0] + 1 < len(data)
					and data[pos[0] + 1] != 93):
				pos[0] += 1
				hi = data[pos[0]]
				pos[0] += 1
				ranges.append((lo, hi))
			else:
				ranges.append((lo, lo))
		assert peek() == 93, "unclosed-class"
		pos[0] += 1
		return ("class", negated, ranges)

	node = parse_alternation()
	assert at_end(), "trailing input"
	return node


# --------------------------------------------------- NFA compilation

def node_size(node) -> int:
	kind = node[0]
	if kind in ("lit", "dot", "anchor", "class"):
		return 1
	if kind == "group":
		return sum(node_size(c) for c in node[1])
	if kind == "alt":
		branches = node[1]
		if not branches:
			return 0
		total = sum(node_size(b) for b in branches)
		if len(branches) > 1:
			total += 2 * (len(branches) - 1)
		return total
	if kind == "repeat":
		cs = node_size(node[1])
		return cs + 2 if node[2] == "star" else cs + 1
	raise AssertionError(kind)


def emit_node(node, ops, ranges):
	kind = node[0]
	if kind == "lit":
		ops.append(("byte", node[1]))
	elif kind == "dot":
		ops.append(("any",))
	elif kind == "anchor":
		ops.append(("assert_start",) if node[1] == "start" else ("assert_end",))
	elif kind == "class":
		rs = len(ranges)
		ranges.extend(node[2])
		ops.append(("ranges", rs, len(node[2]), node[1]))
	elif kind == "group":
		for c in node[1]:
			emit_node(c, ops, ranges)
	elif kind == "alt":
		branches = node[1]
		if len(branches) == 1:
			emit_node(branches[0], ops, ranges)
			return
		sizes = [node_size(b) for b in branches]
		end_pos = len(ops) + sum(sizes) + 2 * (len(branches) - 1)
		for bi in range(len(branches) - 1):
			body_start = len(ops) + 1
			after_jump = body_start + sizes[bi] + 1
			ops.append(("split", body_start, after_jump))
			emit_node(branches[bi], ops, ranges)
			ops.append(("jump", end_pos))
		emit_node(branches[-1], ops, ranges)
	elif kind == "repeat":
		child, quant = node[1], node[2]
		child_size = node_size(child)
		if quant == "star":
			split_pos = len(ops)
			ops.append(("split", split_pos + 1, split_pos + 1 + child_size + 1))
			emit_node(child, ops, ranges)
			ops.append(("jump", split_pos))
		elif quant == "plus":
			body_start = len(ops)
			emit_node(child, ops, ranges)
			ops.append(("split", body_start, len(ops) + 1))
		else:  # question
			body_start = len(ops) + 1
			ops.append(("split", body_start, body_start + child_size))
			emit_node(child, ops, ranges)
	else:
		raise AssertionError(kind)


def nfa_compile(pattern: str):
	root = parse(pattern)
	ops: list = []
	ranges: list = []
	emit_node(root, ops, ranges)
	ops.append(("accept",))
	return ops, ranges


# --------------------------------------------------------- executor

@dataclass
class Counters:
	attempts: int = 0        # _try_match_at_range calls (candidate starts)
	bytes: int = 0           # byte-loop iterations (bytes consumed)
	add_state_calls: int = 0  # _add_state invocations (incl. guarded-out)
	byte_tests: int = 0      # _byte_matches evaluations
	allocs: int = 0          # drift_alloc_array calls (2/attempt + 2/byte)
	frees: int = 0           # == allocs (all attempt-local)

	def add(self, other: "Counters"):
		for f in ("attempts", "bytes", "add_state_calls", "byte_tests",
		          "allocs", "frees"):
			setattr(self, f, getattr(self, f) + getattr(other, f))

	def scaled(self, k: int) -> "Counters":
		c = Counters()
		for f in ("attempts", "bytes", "add_state_calls", "byte_tests",
		          "allocs", "frees"):
			setattr(c, f, getattr(self, f) * k)
		return c

	def minus(self, other: "Counters") -> "Counters":
		c = Counters()
		for f in ("attempts", "bytes", "add_state_calls", "byte_tests",
		          "allocs", "frees"):
			setattr(c, f, getattr(self, f) - getattr(other, f))
		return c

	def as_dict(self):
		return dict(attempts=self.attempts, bytes=self.bytes,
		            add_state_calls=self.add_state_calls,
		            byte_tests=self.byte_tests,
		            allocs=self.allocs, frees=self.frees)

	def call_counts(self):
		"""Wrap-layer CALL predictions, calibrated by probe.drift
		(rev 2, classified counters + live-pointer set):
		  * Array::with_capacity emits 2 drift_alloc_array CALLS —
		    ONE real posix_memalign (the reserve) and ONE sentinel
		    no-op (the zero-capacity empty-init returns the runtime
		    sentinel);
		  * dropping an array performs exactly ONE real free (its
		    storage); every other drift_free_array call in the shape
		    is a no-op (sentinel/NULL tombstone) — for the engine's
		    exact shape the no-op frees total arrays + 5/attempt +
		    3/byte;
		  * so REAL allocator work is 1 malloc + 1 free per array.
		Verified residual-zero across 10 independent windows (4
		patterns x 3 input families x 2 sizes)."""
		arrays = self.allocs  # 2/attempt + 2/byte constructions
		return {
			"arrays": arrays,
			"alloc_calls": 2 * arrays,
			"alloc_real": arrays,
			"alloc_sentinel": arrays,
			"free_calls": 2 * arrays + 5 * self.attempts + 3 * self.bytes,
			"free_real": arrays,
			"free_noop": arrays + 5 * self.attempts + 3 * self.bytes,
		}


def byte_matches(ops, ranges, pc, b, ctr: Counters) -> bool:
	ctr.byte_tests += 1
	op = ops[pc]
	k = op[0]
	if k == "byte":
		return b == op[1]
	if k == "any":
		return True
	if k == "ranges":
		rs, rc, neg = op[1], op[2], op[3]
		found = any(ranges[rs + i][0] <= b <= ranges[rs + i][1]
		            for i in range(rc))
		return (not found) if neg else found
	return False


def add_state(ops, bitmap, clist, pc, pos, input_len, ctr: Counters):
	ctr.add_state_calls += 1
	if 0 <= pc < len(ops) and not bitmap[pc]:
		bitmap[pc] = True
		k = ops[pc][0]
		if k == "split":
			add_state(ops, bitmap, clist, ops[pc][1], pos, input_len, ctr)
			add_state(ops, bitmap, clist, ops[pc][2], pos, input_len, ctr)
		elif k == "jump":
			add_state(ops, bitmap, clist, ops[pc][1], pos, input_len, ctr)
		elif k == "assert_start":
			if pos == 0:
				add_state(ops, bitmap, clist, pc + 1, pos, input_len, ctr)
		elif k == "assert_end":
			if pos == input_len:
				add_state(ops, bitmap, clist, pc + 1, pos, input_len, ctr)
		else:  # byte / any / ranges / accept
			clist.append(pc)


def try_match_at(ops, ranges, data, start, ctr: Counters) -> int:
	input_len = len(data)
	prog_len = len(ops)
	accept_pc = prog_len - 1
	ctr.attempts += 1
	ctr.allocs += 2          # _make_bitmap (:944) + initial clist (:945)
	ctr.frees += 2
	bitmap = [False] * prog_len
	clist: list = []
	add_state(ops, bitmap, clist, 0, start, input_len, ctr)
	best_end = start if bitmap[accept_pc] else -1
	pos = start
	while pos < input_len and clist:
		b = data[pos]
		ctr.bytes += 1
		ctr.allocs += 2      # seeds (:958) + replacement clist (:969)
		ctr.frees += 2
		seeds = [pc + 1 for pc in clist
		         if byte_matches(ops, ranges, pc, b, ctr)]
		bitmap = [False] * prog_len   # drift: _clear_bitmap (no alloc)
		clist = []
		for s in seeds:
			add_state(ops, bitmap, clist, s, pos + 1, input_len, ctr)
		if bitmap[accept_pc]:
			best_end = pos + 1
		pos += 1
	return best_end


def find_from(ops, ranges, data, frm, ctr: Counters):
	sublen = len(data)
	start = frm
	while start <= sublen:
		end = try_match_at(ops, ranges, data, start, ctr)
		if end >= 0:
			return (start, end)
		start += 1
	return None


def is_match(ops, ranges, data, ctr: Counters) -> bool:
	start = 0
	while start <= len(data):
		if try_match_at(ops, ranges, data, start, ctr) >= 0:
			return True
		start += 1
	return False


def scan_all(ops, ranges, data, ctr: Counters):
	count = 0
	last_end = 0
	cursor = 0
	n = len(data)
	while cursor <= n:
		m = find_from(ops, ranges, data, cursor, ctr)
		if m is None:
			break
		count += 1
		last_end = m[1]
		cursor = m[0] + 1 if m[1] == m[0] else m[1]
	return count, last_end


# --------------------------------------------- periodic prediction

CARRIER_CHUNK = b"alpha,bravo12,charlie345,dd,echo_echo_echo,f,"
NOMATCH_CHUNK = b"alpha,bravo,charlie,dd,echo_echo_echo,f,"


def chunks_for(chunk: bytes, target: int) -> int:
	"""build_from_chunk appends whole chunks until total >= target."""
	n = 0
	total = 0
	while total < target:
		n += 1
		total += len(chunk)
	return n


def periodic_counters(pattern: str, chunk: bytes, workload: str):
	"""Exact per-chunk decomposition: counters(k chunks) is affine in k
	(C*k + T) because for these patterns no NFA thread survives a ','
	(none of the bench patterns match ','), so every attempt's work is
	local to its token and chunks repeat verbatim.  Derive C and T from
	brute force at k=2,3; VERIFY at k=5; then predict any whole-chunk
	size exactly."""
	ops, ranges = nfa_compile(pattern)

	def brute(k: int):
		data = chunk * k
		ctr = Counters()
		if workload == "scan_all":
			result = scan_all(ops, ranges, data, ctr)
		elif workload == "find_nomatch":
			result = find_from(ops, ranges, data, 0, ctr)
			assert result is None
		else:
			raise AssertionError(workload)
		return ctr, result

	c2, _ = brute(2)
	c3, _ = brute(3)
	C = c3.minus(c2)
	T = c2.minus(C.scaled(2))
	c5, r5 = brute(5)
	pred5 = C.scaled(5)
	pred5.add(T)
	assert pred5.as_dict() == c5.as_dict(), (
		f"periodicity broken for {pattern}/{workload}: "
		f"{pred5.as_dict()} vs {c5.as_dict()}")
	return ops, ranges, C, T


def predict(pattern: str, chunk: bytes, target: int, workload: str):
	ops, ranges, C, T = periodic_counters(pattern, chunk, workload)
	k = chunks_for(chunk, target)
	out = C.scaled(k)
	out.add(T)
	result = None
	if workload == "scan_all":
		# match count / last_end are affine in k as well; derive the
		# same way (verified by the k=5 assert via counters already;
		# recompute counts directly at k=2,3 to build the affine form).
		ctr2 = Counters()
		n2, e2 = scan_all(ops, ranges, chunk * 2, ctr2)
		ctr3 = Counters()
		n3, e3 = scan_all(ops, ranges, chunk * 3, ctr3)
		dc = n3 - n2
		de = e3 - e2
		result = (n2 + dc * (k - 2), e2 + de * (k - 2))
	return out, result, k


# ------------------------------------------------------- op windows

def model_windows():
	"""Returns {op_label: {'counters': dict, 'result': ...}} for every
	count-window op in driver.c, matching subjects/sizes exactly."""
	out = {}

	# scan_all over the carrier (64 KiB and 2 MiB)
	for label, target in (("scan_all_64k", 64 * 1024),
	                      ("scan_all_2m", 2 * 1024 * 1024)):
		ctr, res, k = predict("[a-z]+[0-9]+", CARRIER_CHUNK, target, "scan_all")
		out[label] = {"counters": ctr.as_dict(), "calls": ctr.call_counts(),
		              "result": res[0] * 10000000 + res[1],
		              "chunks": k, "input_len": k * len(CARRIER_CHUNK)}

	# find_first over the no-match input (64 KiB and 2 MiB)
	for label, target in (("find_nomatch_64k", 64 * 1024),
	                      ("find_nomatch_2m", 2 * 1024 * 1024)):
		ctr, _res, k = predict("[a-z]+[0-9]+", NOMATCH_CHUNK, target, "find_nomatch")
		out[label] = {"counters": ctr.as_dict(), "calls": ctr.call_counts(),
		              "result": 1,
		              "chunks": k, "input_len": k * len(NOMATCH_CHUNK)}

	# view form: engine-identical to find_nomatch_64k (retain +1 is a
	# wrap-counter pin, not an engine-counter difference)
	out["find_nomatch_view_64k"] = dict(out["find_nomatch_64k"])

	# wide alternation over 64 KiB no-match input (brute force)
	ops, ranges = nfa_compile("(ab|ac|ad|ae|af|ag|ah|ai|aj|ak|al|am|an|ao|ap|aq)z")
	data = NOMATCH_CHUNK * chunks_for(NOMATCH_CHUNK, 64 * 1024)
	ctr = Counters()
	assert find_from(ops, ranges, data, 0, ctr) is None
	out["alt_64k"] = {"counters": ctr.as_dict(), "calls": ctr.call_counts(),
	                  "result": 1,
	                  "prog_len": len(ops), "input_len": len(data)}

	# zero-width: a* over 1 KiB of 'z', find_first x100
	ops, ranges = nfa_compile("a*")
	data = b"z" * 1024
	one = Counters()
	m = find_from(ops, ranges, data, 0, one)
	assert m == (0, 0)
	z100 = one.scaled(100)
	out["zw_x100"] = {"counters": z100.as_dict(), "calls": z100.call_counts(),
	                  "result": 100, "prog_len": len(ops)}

	# short token, hit: [a-z]+[0-9]+ on "user42", find_first x100
	ops, ranges = nfa_compile("[a-z]+[0-9]+")
	one = Counters()
	m = find_from(ops, ranges, b"user42", 0, one)
	assert m == (0, 6), m
	h100 = one.scaled(100)
	out["short_hit_x100"] = {"counters": h100.as_dict(), "calls": h100.call_counts(),
	                         "result": 600, "prog_len": len(ops)}

	# short token, miss: on "userxyz", find_first x100
	one = Counters()
	assert find_from(ops, ranges, b"userxyz", 0, one) is None
	m100 = one.scaled(100)
	out["short_miss_x100"] = {"counters": m100.as_dict(), "calls": m100.call_counts(),
	                          "result": 100}

	# anchored: ^[a-z]*$ on "alphabravo", is_match x100
	ops, ranges = nfa_compile("^[a-z]*$")
	one = Counters()
	assert is_match(ops, ranges, b"alphabravo", one)
	a100 = one.scaled(100)
	out["anchor_x100"] = {"counters": a100.as_dict(), "calls": a100.call_counts(),
	                      "result": 100, "prog_len": len(ops)}

	# timing-binary CHECK cross-validation values
	_ctr, res, _k = predict("[a-z]+[0-9]+", CARRIER_CHUNK,
	                        2 * 1024 * 1024, "scan_all")
	out["_check_scan_all_2m"] = res[0] * 10000000 + res[1]
	out["_check_short_hit"] = 6
	return out


if __name__ == "__main__":
	import json
	print(json.dumps(model_windows(), indent=2, default=str))


# ------------------------------------------- small-subject suite

import gen_small as gs  # single source of truth for the small suite


def small_windows():
	"""Predictions for every generated small-suite count window
	(labels match gen_small.py's counts_small.drift ops)."""
	out = {}

	def one_search(scen: str, n: int) -> tuple[Counters, int]:
		ops, ranges = nfa_compile(gs.pattern_for(scen))
		data = gs.py_input(scen, n)
		assert len(data) == n, (scen, n, len(data))
		ctr = Counters()
		if scen == "anchored":
			assert is_match(ops, ranges, data, ctr)
			return ctr, 1
		m = find_from(ops, ranges, data, 0, ctr)
		if scen == "early":
			assert m == (0, 4), m
		elif scen == "late":
			assert m == (n - 4, n), m
		elif scen == "alt":
			assert m == (n - 5, n), m
		else:
			assert m is None, m
		return ctr, (m[1] - m[0]) if m else 0

	for scen, sizes in gs.COUNT_SIZES.items():
		for n in sizes:
			ctr, contrib = one_search(scen, n)
			c = ctr.scaled(gs.COUNT_REPS)
			out[f"sc_{scen}_{n}"] = {
				"counters": c.as_dict(), "calls": c.call_counts(),
				"result": contrib * gs.COUNT_REPS,
			}
	for scen, sizes in gs.VIEW_COUNT.items():
		for n in sizes:
			out[f"sc_{scen}_view_{n}"] = dict(out[f"sc_{scen}_{n}"])
	return out
