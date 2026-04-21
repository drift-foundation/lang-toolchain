# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Permissive trust-store shim for `tools/drift_deploy/` tests.

The 0.31.1 source-rebuild trust-anchor work made `verify_lock_
compatibility` and `_compare_locks_for_check` require a caller-
supplied `TrustStore` whenever source-rebuild mode is selected.
Most existing deploy-side tests fabricate kids (`ed25519:rebuilder`,
`ed25519:test`, ...) without wiring a real trust store.

`PermissiveTrustStore` is a duck-typed stand-in whose
`allowed_kids_for_module` returns a set-shaped sentinel that
contains every kid, and whose `revoked_kids` is an empty set — so
the trust gate becomes a pass-through for any disk kid.

Tests that specifically pin trust-gate rejection (untrusted kid,
revoked kid, etc.) must build their own `TrustStore` (or a stricter
shim) — do NOT use `PermissiveTrustStore` for those.
"""

from __future__ import annotations


class _EveryKidOkSet(set):
	"""Set-shaped object whose `in` membership is always True."""

	def __contains__(self, _item) -> bool:
		return True


_EVERY_KID_OK = _EveryKidOkSet()


class PermissiveTrustStore:
	"""Duck-typed `TrustStore` stand-in — allowlists every kid.

	Does NOT subclass the real frozen-dataclass `TrustStore`; the
	verifier only touches `.allowed_kids_for_module(pkg_id)` and
	`.revoked_kids`, so a duck-typed shim is sufficient.
	"""

	__slots__ = ("revoked_kids",)

	def __init__(self) -> None:
		self.revoked_kids: set[str] = set()

	def allowed_kids_for_module(self, _module_id: str) -> set[str]:
		return _EVERY_KID_OK
