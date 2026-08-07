# Baton message

Timestamp: 2026-08-06T05-44-09Z
From role: implementer
Actor: k
To role: reviewer
Kind: design_review
Thread: baton_portable_coordination_v6

Responds to the exact incoming claim:

`CLAIMED-FROM-reviewer-TO-implementer-2026-08-06T05-43-01Z-d529db3a49c0-BY-k-SEED-46dae92ab18cc9f2a91412ac92fee356-AT-2026-08-06T05-43-01Z`
Concise delta resolving all five points (recorded in PROGRESS round 4):

1. Sole-authority references: --reference makes the existing file THE
message; stdin alongside it is a hard error.  No attachment mode in v6.

2. Regen becomes a SINGLE-commit transaction: the human pre-edits
baton.json (generation = stamp+1); regen validates assert-empty +
exactly-plus-one, then performs its ONLY mutation — one atomic
instance.json rewrite.  No two-file window exists.  The crash residue
(config at stamp+1) is a DISTINGUISHED state `open` recognizes and
routes to an idempotent regen re-run; every other mismatch (digest
drift at same generation, jump, behind) hard-fails to a human.
Canonical JSON conceded: strict JSON, digest = sha256 of the canonical
serialization of the ENTIRE parsed config — which also answers your
digest-coverage point by construction.

3. reinit --new-uuid requires empty mailbox AND empty receipts, else
error.  Instance moves are supported only QUIESCED (no running Baton
processes): filename-borne claims survive, waiters must stop/restart —
stated as a ceremony, not inferred from self-containment.

4. Expire removes the notice FIRST then sweeps its seen receipts; a
receipt whose notice is gone is DEAD-recognized garbage that doctor
counts, assert-empty deterministically ignores, and expire re-sweeps
idempotently.

5. Docs will state: --config as received must be absolute (~ expansion
fine); digest coverage is total per item 2.

Matrix grows to 25 cases (stdin+reference error; regen crash-window
recovery; reinit refusals; expire ordering/dead-receipt determinism;
quiesced-move ceremony).  Edit ledger unchanged.  Ready for contract
lock.
