# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Direct regression for HIGH #4: the resolved-closure walker must
fail closed when a transitive dep can't contribute a complete
identity to the parent's closure.

Scenario K flagged:

  - Parent package A is certified (verified through the v1 trust
    path; would consume a cert claim that attests A's full dep
    graph).
  - A depends on B@1.0.
  - The consumer pins B@1.0 from a path that is under
    `allow_unverified_roots`; B loads without an SCI stamp so its
    pre-pass identity is None.
  - The closure walker MUST raise a ValueError instead of silently
    dropping B.  Silently dropping would let A's cert claim
    dep_graph omit B and still "cover" the (incomplete) closure --
    that's the O3 escape hatch.

Why a unit test rather than an end-to-end driftc invocation:
the walker is local to `driftc.main()` and runs over an in-memory
pre-pass dict.  Building a full v1-certified `.dmp` fixture in a
unit test would require a populated `core_trust_v1.json` and v1
sidecars, which are out of scope until slice 4 part D.  Exercising
the walker directly against synthetic prepass entries pins the
exact code path the production loader uses, with no mocking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from lang.driftc.packages.cert_claim_v1 import ResolvedDep
from lang.driftc.packages.closure_walk import build_resolved_closure


@dataclass(frozen=True)
class _RD:
	"""Minimal stand-in for `RequiredDepEntry` -- only `.name` is read
	by the walker."""
	name: str
	version: str = "1"


@dataclass(frozen=True)
class _FakePkg:
	"""Minimal stand-in for `LoadedPackage` -- only `.required_deps`
	is read by the walker on transitive descent."""
	required_deps: tuple[_RD, ...] = ()


def _identity(pid: str, ver: str) -> ResolvedDep:
	return ResolvedDep(
		package_id=pid,
		version=ver,
		artifact_sha256=f"sha256:{'a' * 64}",
		source_content_id=f"sha256:{'b' * 64}",
	)


# ── Happy paths ────────────────────────────────────────────────────


def test_leaf_package_returns_empty_closure() -> None:
	"""Package with no required_deps -> empty closure, no walk needed."""
	out = build_resolved_closure(
		start_pkg_id="leaf",
		start_required_deps=(),
		prepass={},
		version_pins={},
	)
	assert out == []


def test_single_direct_dep_with_full_identity_succeeds() -> None:
	"""Parent A -> B, B has identity, walk emits one ResolvedDep."""
	rd_b = _identity("B", "1.0")
	out = build_resolved_closure(
		start_pkg_id="A",
		start_required_deps=(_RD(name="B"),),
		prepass={
			("B", "1.0"): (_FakePkg(), b"", rd_b),
		},
		version_pins={"B": "1.0"},
	)
	assert out == [rd_b]


def test_transitive_chain_walks_to_completion() -> None:
	"""A -> B -> C with all identities present.  Walker emits both."""
	rd_b = _identity("B", "1.0")
	rd_c = _identity("C", "2.0")
	out = build_resolved_closure(
		start_pkg_id="A",
		start_required_deps=(_RD(name="B"),),
		prepass={
			("B", "1.0"): (_FakePkg(required_deps=(_RD(name="C"),)), b"", rd_b),
			("C", "2.0"): (_FakePkg(), b"", rd_c),
		},
		version_pins={"B": "1.0", "C": "2.0"},
	)
	assert sorted(d.package_id for d in out) == ["B", "C"]


def test_self_pkg_id_is_excluded_from_closure() -> None:
	"""When the parent declares a required_dep matching its own
	package_id (self-exclusion case), the walker skips it instead
	of recursing infinitely."""
	rd_b = _identity("B", "1.0")
	out = build_resolved_closure(
		start_pkg_id="A",
		start_required_deps=(_RD(name="A"), _RD(name="B")),
		prepass={
			("A", "1.0"): (_FakePkg(), b"", _identity("A", "1.0")),
			("B", "1.0"): (_FakePkg(), b"", rd_b),
		},
		version_pins={"A": "1.0", "B": "1.0"},
		self_pkg_id="A",
	)
	assert [d.package_id for d in out] == ["B"]


# ── Fail-closed gates (HIGH #4 regressions) ────────────────────────


def test_missing_pin_raises_with_dep_name_in_message() -> None:
	"""Declared required_dep has no --dep pin: walker must raise.
	Caller (driftc.py) treats this as a per-package error so the
	user sees a pointer to `drift prepare`."""
	with pytest.raises(ValueError) as exc:
		build_resolved_closure(
			start_pkg_id="A",
			start_required_deps=(_RD(name="B"),),
			prepass={},
			version_pins={},
		)
	msg = str(exc.value)
	assert "resolved closure for 'A'" in msg
	assert "'B'" in msg
	assert "no --dep pin" in msg


def test_missing_prepass_entry_raises() -> None:
	"""Pinned dep wasn't loaded in the pre-pass (corrupt/missing
	artifact).  Walker fails closed."""
	with pytest.raises(ValueError) as exc:
		build_resolved_closure(
			start_pkg_id="A",
			start_required_deps=(_RD(name="B"),),
			prepass={},
			version_pins={"B": "1.0"},
		)
	msg = str(exc.value)
	assert "resolved closure for 'A'" in msg
	assert "'B'@'1.0'" in msg
	assert "not loaded in the pre-pass" in msg


def test_dep_loaded_without_sci_raises_o3_diagnostic() -> None:
	"""K's exact HIGH #4 scenario: certified parent A depends on
	pinned child B@1.0; B loads through `allow_unverified_roots`
	without an SCI stamp so its pre-pass identity is None.

	Old behavior (silent drop): walker emitted [], A's cert claim
	dep_graph was unable to require an attestation for B because
	B never reached the cover check -- A could pass certifier
	verification on an incomplete graph.

	New behavior (this regression pins it): walker raises with a
	diagnostic that:
	  - names the parent and the dep at issue,
	  - mentions the missing source_content_id stamp,
	  - mentions the `allow_unverified_roots` mechanism so the
	    user can locate the cause,
	  - tells them how to fix it (either stamp the dep, or move
	    both onto the verified path together).
	"""
	with pytest.raises(ValueError) as exc:
		build_resolved_closure(
			start_pkg_id="A",
			start_required_deps=(_RD(name="B"),),
			prepass={
				# IDENTITY IS None -- this is the production signal
				# that B was loaded under allow_unverified_roots
				# without an SCI stamp.
				("B", "1.0"): (_FakePkg(), b"", None),
			},
			version_pins={"B": "1.0"},
		)
	msg = str(exc.value)
	assert "resolved closure for 'A'" in msg
	assert "'B'@'1.0'" in msg
	assert "source_content_id" in msg
	assert "allow_unverified_roots" in msg
	# Anti-regression: the diagnostic must NOT pretend the closure
	# was computed successfully.  An empty-closure response would be
	# the bug K identified.
	assert "cannot be computed" in msg


def test_transitive_dep_without_sci_raises_named() -> None:
	"""SCI-missing failure surfaces even when the failing dep is
	transitive (A -> B -> C; C has no identity).  Catches a
	regression where the walker only checked direct deps."""
	rd_b = _identity("B", "1.0")
	with pytest.raises(ValueError) as exc:
		build_resolved_closure(
			start_pkg_id="A",
			start_required_deps=(_RD(name="B"),),
			prepass={
				("B", "1.0"): (_FakePkg(required_deps=(_RD(name="C"),)), b"", rd_b),
				# C is pinned and loaded -- but no SCI stamp.
				("C", "2.0"): (_FakePkg(), b"", None),
			},
			version_pins={"B": "1.0", "C": "2.0"},
		)
	msg = str(exc.value)
	assert "'C'@'2.0'" in msg
	assert "source_content_id" in msg


def test_walker_does_not_return_partial_closure_on_failure() -> None:
	"""Belt-and-suspenders: even if some deps have identity and one
	doesn't, the walker raises (not "return what we have").  A
	half-built closure would be exactly the O3 escape K flagged."""
	rd_b = _identity("B", "1.0")
	with pytest.raises(ValueError):
		build_resolved_closure(
			start_pkg_id="A",
			start_required_deps=(_RD(name="B"), _RD(name="C")),
			prepass={
				("B", "1.0"): (_FakePkg(), b"", rd_b),
				("C", "2.0"): (_FakePkg(), b"", None),  # missing identity
			},
			version_pins={"B": "1.0", "C": "2.0"},
		)


def test_diamond_dep_visits_each_node_once() -> None:
	"""A -> B, A -> C, B -> D, C -> D.  Walker emits D exactly once
	(via the `seen` set) and does not loop.  Confirms the
	deduplication path works alongside the fail-closed paths."""
	rd_b = _identity("B", "1.0")
	rd_c = _identity("C", "1.0")
	rd_d = _identity("D", "1.0")
	out = build_resolved_closure(
		start_pkg_id="A",
		start_required_deps=(_RD(name="B"), _RD(name="C")),
		prepass={
			("B", "1.0"): (_FakePkg(required_deps=(_RD(name="D"),)), b"", rd_b),
			("C", "1.0"): (_FakePkg(required_deps=(_RD(name="D"),)), b"", rd_c),
			("D", "1.0"): (_FakePkg(), b"", rd_d),
		},
		version_pins={"B": "1.0", "C": "1.0", "D": "1.0"},
	)
	pids = [d.package_id for d in out]
	assert sorted(pids) == ["B", "C", "D"]
	assert len(pids) == 3  # D visited once even though referenced twice
