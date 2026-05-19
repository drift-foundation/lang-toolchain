# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Author-side claim emit (trust-v1).

This package is intentionally separate from `tools/drift_deploy/` so
the author-key-out-of-orch invariant has a structural enforcement
point.  The orch / certifier pipeline (`tools/drift_deploy/`) MUST
NOT import anything under `tools/drift_author/` -- a static import
boundary check (`lang/tests/packages/test_author_key_boundary.py`)
enforces this.  See `work/drift-trust-model-audit/plan.md` for the
role separation rationale.

Author claim semantics (per O8 / slice 2):
  - Per-release singleton: exactly one `<pkg>.author-claim` sidecar
    file per (package_id, version).
  - Multi-author releases use the `signatures: [...]` array INSIDE
    that one file; co-authors call `add_signature_to_claim_file()`.
  - Body binds: namespace, package_id, version, source_content_id,
    declared deps, release intent.
  - Body NEVER binds artifact bytes (G3).  Artifact binding is the
    cert claim's job, signed by a separate key role.
"""

from tools.drift_author.author_publish import (
	SignAuthorClaimOptions,
	add_signature_to_claim_file,
	sign_and_write_author_claim,
)
from tools.drift_author.key_loader import (
	decode_author_seed32,
	load_author_seed32,
)


__all__ = [
	"SignAuthorClaimOptions",
	"add_signature_to_claim_file",
	"decode_author_seed32",
	"load_author_seed32",
	"sign_and_write_author_claim",
]
