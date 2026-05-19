# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Resolved-closure walker for v1 trust verification.

Given a parent package's `required_deps` and a map of (package_id,
version) -> (LoadedPackage-like, ResolvedDep|None) pre-pass entries,
this module walks the transitive dependency graph and returns the
parent's `resolved_closure` -- the `list[ResolvedDep]` the v1 cert
claim verifier compares against the certifier's `dep_graph` (O3).

Why a separate module: this walker is security-critical (HIGH #4
gap: silent drops let a parent escape O3 when a dep is loaded
without an SCI stamp).  Pulling it out of `driftc.py`'s `main()`
lets the regression test exercise the exact code path the production
loader uses, instead of mocking the result.

Fail-closed contract:
  - missing `--dep` pin for a declared `required_deps` entry → raise
  - pinned dep not in pre-pass (corrupt/missing artifact) → raise
  - pinned dep in pre-pass with `identity=None` (loaded without
    source_content_id, e.g. under `allow_unverified_roots`) → raise

Callers (driftc.py) catch the ValueError in the same try block that
wraps `load_package_v1_with_policy`, so the user sees one consistent
package-error diagnostic.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Protocol

from lang.driftc.packages.cert_claim_v1 import ResolvedDep


class _HasRequiredDeps(Protocol):
	@property
	def name(self) -> str: ...


class _PreEntry(Protocol):
	"""Structural shape of a pre-pass entry: (loaded_pkg, bytes, identity).

	`identity` is None when the dep was loaded without a
	`source_content_id` stamp (typically through
	`allow_unverified_roots`).
	"""


def build_resolved_closure(
	*,
	start_pkg_id: str,
	start_required_deps: Iterable[_HasRequiredDeps],
	prepass: Mapping[tuple[str, str], tuple[object, object, Optional[ResolvedDep]]],
	version_pins: Mapping[str, str],
	self_pkg_id: Optional[str] = None,
) -> list[ResolvedDep]:
	"""Walk `start_required_deps` transitively and return the
	resolved closure.

	`prepass` and `version_pins` mirror the structures driftc.py
	maintains during its two-pass package load; passing them
	explicitly keeps this walker pure and testable.

	`self_pkg_id`, when set, is skipped during the walk -- the
	consumer's own package being compiled is intentionally excluded
	from the closure of its own deps.
	"""
	closure: list[ResolvedDep] = []
	seen: set[tuple[str, str]] = set()
	queue: list = list(start_required_deps)
	while queue:
		rd_entry = queue.pop()
		name = getattr(rd_entry, "name", None)
		if not isinstance(name, str) or not name:
			continue
		if self_pkg_id and name == self_pkg_id:
			continue
		pin = version_pins.get(name)
		if pin is None:
			raise ValueError(
				f"resolved closure for {start_pkg_id!r} cannot be "
				f"computed: declared required_deps entry {name!r} "
				f"has no --dep pin.  Run `drift prepare` / `drift "
				f"build` so the complete transitive graph reaches "
				f"driftc as exact --dep pins."
			)
		key = (name, pin)
		if key in seen:
			continue
		seen.add(key)
		pre_entry = prepass.get(key)
		if pre_entry is None:
			raise ValueError(
				f"resolved closure for {start_pkg_id!r} cannot be "
				f"computed: required dep {name!r}@{pin!r} was "
				f"not loaded in the pre-pass (the artifact may be "
				f"corrupt or missing under the package roots).  "
				f"Without the dep's identity the parent's "
				f"cert-claim dep_graph cover check (O3) cannot "
				f"run; refusing to verify the parent."
			)
		pre_pkg_obj, _, pre_identity = pre_entry
		if pre_identity is None:
			raise ValueError(
				f"resolved closure for {start_pkg_id!r} cannot be "
				f"computed: required dep {name!r}@{pin!r} loaded "
				f"without a source_content_id stamp (typically "
				f"because the dep sits under an "
				f"allow_unverified_roots prefix).  Certifier-shortcut "
				f"verification requires every transitive dep to "
				f"carry a v1 identity -- the parent's cert claim "
				f"cannot honestly attest a graph that includes an "
				f"unstamped dep.  Either provide v1 sidecars for "
				f"{name!r}@{pin!r}, or move both parent and dep "
				f"onto the verified path together."
			)
		closure.append(pre_identity)
		for sub_rd in getattr(pre_pkg_obj, "required_deps", []) or []:
			queue.append(sub_rd)
	return closure
