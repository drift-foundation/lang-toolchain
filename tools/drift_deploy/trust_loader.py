# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Trust-store loading helper for drift deploy-side tools.

`verify_lock_compatibility` and `_compare_locks_for_check` require a
resolved `TrustStore` in `VERIFY_MODE_SOURCE_REBUILD` so the disk's
artifact-signer and source-attestation-signer kids can be verified
against the package's namespace allowlist (and against the revocation
set).  This helper packages the same layering driftc itself uses
(core + project + optional user), so every caller of the source-
rebuild lane applies the same trust policy.

Lookup order (merged, with precedence):

  1. core trust store (toolchain-shipped; authoritative for reserved
     namespaces like `lang.*`, `std.*`, `drift.*`)
  2. project trust store (`<manifest_dir>/trust.json`, typically
     `drift/trust.json`)
  3. user trust store (`~/.config/drift/trust.json`), unless
     `include_user_trust=False`

Keys in later stores override earlier ones on kid conflicts, and
namespace allowlists are unioned across all three.  Revocations are
unioned too (a revocation in ANY layer revokes the kid).
"""

from __future__ import annotations

from pathlib import Path

from lang.driftc.packages.trust_v1 import (
	TrustStore,
	load_core_trust_store,
	load_trust_store_json,
	merge_trust_stores,
)


def load_merged_trust_store(
	manifest_dir: Path,
	*,
	include_user_trust: bool = True,
) -> TrustStore:
	"""Return a merged trust store suitable for source-rebuild verify.

	Layered per module docstring.  Missing project or user trust
	files are treated as empty (not an error) — the core trust store
	alone is enough for toolchain-namespace packages, and downstream
	projects that haven't added trust entries inherit only whatever
	their user layer supplies.  A missing core trust store IS an
	error (raised by `load_core_trust_store`).
	"""
	core = load_core_trust_store()

	project_path = manifest_dir / "trust.json"
	if project_path.exists():
		project = load_trust_store_json(project_path)
		merged = merge_trust_stores(project, core)
	else:
		merged = core

	if include_user_trust:
		user_path = Path.home() / ".config" / "drift" / "trust.json"
		if user_path.exists():
			user = load_trust_store_json(user_path)
			merged = merge_trust_stores(merged, user)

	return merged
