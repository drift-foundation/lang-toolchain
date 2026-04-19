# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Minimal semver parsing and constraint matching.

Constraint forms accepted by v2-manifest authors (the "declared
acceptable range" the package owner chooses):

- **`"major"`** — owner accepts any `major.x.x` release.
  Example: `"1"` matches `1.0.0`, `1.4.7`, `1.99.0`.
- **`"major.minor"`** — owner accepts any `major.minor.x` release.
  Example: `"0.3"` matches `0.3.0`, `0.3.14`.

Additional forms the parser accepts for **internal, non-manifest**
use — explicitly NOT a compatibility shim for pre-cut packages,
which are clean-break rejected at load time:

- Exact: `"1.2.3"` — used by lock-v3 entries (exact resolved
  artifacts) and by the v1→v2 manifest migration path.
- Caret: `"^1.2.3"` — unit-test vocabulary in resolver/conflict
  tests that exercise the matching algorithm.
- Tilde: `"~1.2.3"` — same: unit-test vocabulary only.

These forms have no path from `.dmp`-carried `required_deps` into
the resolver — pre-cut packages without v2 `required_deps` are
rejected at consume time (Phase 4).

Drift's resolver does not decide semantic compatibility.  Given the
owner-declared range, it simply picks the highest trusted candidate
whose exact version satisfies the range.  The owner decides how
permissive that range is.

No pre-release or build metadata in MVP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering


_VER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_RANGE_MN_RE = re.compile(r"^(\d+)\.(\d+)$")
_RANGE_M_RE = re.compile(r"^(\d+)$")


@total_ordering
@dataclass(frozen=True)
class SemVer:
	major: int
	minor: int
	patch: int

	def __str__(self) -> str:
		return f"{self.major}.{self.minor}.{self.patch}"

	def __lt__(self, other: object) -> bool:
		if not isinstance(other, SemVer):
			return NotImplemented
		return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)

	def __eq__(self, other: object) -> bool:
		if not isinstance(other, SemVer):
			return NotImplemented
		return (self.major, self.minor, self.patch) == (other.major, other.minor, other.patch)

	def __hash__(self) -> int:
		return hash((self.major, self.minor, self.patch))


def parse_version(s: str) -> SemVer:
	m = _VER_RE.match(s.strip())
	if not m:
		raise ValueError(f"invalid semver: {s!r}")
	return SemVer(int(m.group(1)), int(m.group(2)), int(m.group(3)))


@dataclass(frozen=True)
class Constraint:
	"""A semver range: the set of concrete versions an owner-declared
	range string accepts.

	`kind` values:

	- `"major_range"` — constraint string is `"M"`.  Satisfied by any
	  `M.x.x`.  `base.minor` / `base.patch` are 0 but not used in
	  matching; only `major` is consulted.
	- `"minor_range"` — constraint string is `"M.N"`.  Satisfied by
	  any `M.N.x`.  `base.patch` is 0 and unused.
	- `"exact"`, `"caret"`, `"tilde"` — internal use only (lock-v3
	  exact entries, v1→v2 manifest migration, resolver unit tests).
	  Not accepted in authored v2 manifests; no path from `.dmp`
	  `required_deps` into these forms (pre-cut packages are
	  rejected at consume time).
	"""
	raw: str
	kind: str  # "major_range", "minor_range", "exact", "caret", "tilde"
	base: SemVer

	def __str__(self) -> str:
		return self.raw

	def satisfies(self, ver: SemVer) -> bool:
		if self.kind == "major_range":
			return ver.major == self.base.major
		if self.kind == "minor_range":
			return ver.major == self.base.major and ver.minor == self.base.minor
		if self.kind == "exact":
			return ver == self.base
		if self.kind == "caret":
			if ver < self.base:
				return False
			if self.base.major == 0:
				# ^0.y.z: >=0.y.z, <0.(y+1).0
				return ver.major == 0 and ver.minor == self.base.minor and ver.patch >= self.base.patch
			return ver.major == self.base.major
		if self.kind == "tilde":
			if ver < self.base:
				return False
			return ver.major == self.base.major and ver.minor == self.base.minor
		raise ValueError(f"unknown constraint kind: {self.kind}")


def parse_constraint(s: str) -> Constraint:
	s = s.strip()
	if s.startswith("^"):
		base = parse_version(s[1:])
		return Constraint(raw=s, kind="caret", base=base)
	if s.startswith("~"):
		base = parse_version(s[1:])
		return Constraint(raw=s, kind="tilde", base=base)
	# Canonical v2-manifest forms: `"M"` → major_range, `"M.N"` →
	# minor_range.  Prefer these matches over the 3-part exact form
	# so `"0"` and `"0.3"` are unambiguously ranges, not attempts at
	# exact semvers.
	m_mn = _RANGE_MN_RE.match(s)
	if m_mn:
		base = SemVer(int(m_mn.group(1)), int(m_mn.group(2)), 0)
		return Constraint(raw=s, kind="minor_range", base=base)
	m_m = _RANGE_M_RE.match(s)
	if m_m:
		base = SemVer(int(m_m.group(1)), 0, 0)
		return Constraint(raw=s, kind="major_range", base=base)
	base = parse_version(s)
	return Constraint(raw=s, kind="exact", base=base)
