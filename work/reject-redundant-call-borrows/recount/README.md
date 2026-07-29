# Scoped recount — method, dataset, reproduction

Dataset: `results3.json` — one record per borrow-expression argument found in call
position, repo-wide (5,123 records; 5,091 classified as firing under the exact rule).
Schema per record:

```
path/area/kind/off/line   source unit and location ("py-embed" kinds carry the extracted
                          program's offset; line attribution is exact for .drift, off for
                          ~34% of py-embed sites whose source uses \n escapes)
op                        "&" | "&mut" (source-written)
opkind / opsub            operand class: lvalue | rvalue (literal, call, container-literal,
                          binary, other-expr)
formal / inner / tv       resolved formal's outer mode, inner type name, and whether the
                          inner is a typevar of the callee ("&-over-typevar")
fam                       callee family: free | method | assoc | mem | iface | iife
callee / decl_*           resolved declaration and its location
conf / ambiguous          resolution confidence tier; overload-ambiguous flag (82 records
                          resolved toward "fires" — the dataset's main upward bias)
```

Method (see PLAN.md §3 for the numbers): every source unit is parsed with the repo's own
Lark parser (`lang/driftc/parser`); embedded Drift in `lang/tests/**/*.py` is extracted
via Python's `ast` module (evaluated string constants, so escape sequences decode
correctly); doc fences parsed with a wrap-retry. A declaration index (every `fn`,
`implement` method, `interface`/`trait` signature) maps each borrow-arg's call position
to its formal; the site fires iff the formal's declared type is `&`/`&mut`-rooted.

Scripts: the iterative collection/resolution pipeline as left by the survey
(`collect2.py` → `resolve3.py`/`final2.py` and satellites; `units*.pkl` parse caches are
NOT copied here — regenerate by rerunning collection from the repo root with
`.venv/bin/python`). `overload_mode_erasure_scan.py` is the clean, self-contained scan
behind the R2 numbers (3 T-vs-& pairs, 0 &-vs-&mut pairs). The dataset
(`results3.json`) is the authoritative artifact the plan's tables derive from; totals
are estimates with stated error bars (±60), not exact counts — resolution is name-based
(88% high-confidence), not a full typecheck.
