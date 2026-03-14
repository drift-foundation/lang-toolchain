# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Minimal semver parsing and constraint matching.

Supports:
- Exact: "1.2.3"
- Caret: "^1.2.3" (>=1.2.3, <2.0.0)
- Tilde: "~1.2.3" (>=1.2.3, <1.3.0)

No pre-release or build metadata in MVP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering


_VER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


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
	"""A semver range constraint."""
	raw: str
	kind: str  # "exact", "caret", "tilde"
	base: SemVer

	def __str__(self) -> str:
		return self.raw

	def satisfies(self, ver: SemVer) -> bool:
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
	base = parse_version(s)
	return Constraint(raw=s, kind="exact", base=base)
