# Reviewed ownership-corpus baseline

The checked-in expectation for `just ownership-corpus-promote`, and the seed for
a clean clone's `just ownership-corpus-check`.

The exact universe and counters live in the machine files beside this note —
`manifest.json` (the fixture universe: inclusion rule, per-fixture content
hashes, and the compiled / failed partition), `aggregate.json` (the summed
ownership-authoring counters), `metadata.json`, `fingerprint.json` (the run
snapshot this baseline was produced under), and — once first promoted under the
current tool — `projections.json` (the per-fixture ownership projections used to
seed a clean clone's developer cache without recompiling). Current shape:
**942 compiled / 367 compile-failed / 49 rule-excluded**, 14 counter keys.

## How this baseline is produced

Only by `drift_corpus_check.py` promotion (`just ownership-corpus-promote`): a
fresh FULL compile that **exactly** reproduced the reviewed expectation — the
developer projection handoff (`build/tmp/ownership-corpus-projection.json`) when
present, otherwise this baseline itself on a clean tree — under a stable
start==end toolchain fingerprint, with every hard gate at zero, then installed
via staged writes with post-install validation. Exact agreement with the
existing baseline is a byte-preserving no-op.

The Git commit that lands these files **is** the approval; reviewer identity and
date come from Git history.

## Two recipes

- `just ownership-corpus-check [<dir>]` — fast developer lane. Records key on
  fixture content hash, so a compiler-fingerprint move keeps old observations as
  *projected* rather than forcing a full rebuild; only new / edited / `--select`ed
  fixtures recompile. It exports the reviewed expectation to the handoff.
- `just ownership-corpus-promote` — fresh exhaustive verification + install.
  Never reads developer records. Invoking it approves the projected expectation,
  verified by a full independent compile.

## Distinct from the ownership matrix

The 51-fixture ownership **matrix** (`just ownership-matrix-check`, inside
`just test`) and this full-corpus **audit** are separate gates. Earlier
version-by-version provenance is in `doc/history.md`; the process is documented
in `doc/ownership-corpus-gate.md`.
