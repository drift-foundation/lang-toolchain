# Drift Playground Snippets

Each `.drift` file here is a focused syntax sample you can run with `./drift.py playground/<name>.drift`.

- `basics.drift` – module-level `let`, string concatenation, and a tiny `main`.
- `functions.drift` – multiple functions calling each other, returning `str`, and using `return`.
- `effects.drift` – `throws{domain}` declarations paired with `raise domain fs error(...)`.
- `mutable_bindings.drift` – shows `let mut` bindings at module scope plus a helper function.
- `logic.drift` – demonstrates boolean operators (`and`, `or`, comparison chains) inside a pure helper.

Feel free to duplicate and tweak these while iterating on syntax ideas.
