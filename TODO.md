# TODO

[String]
- Remaining surface/runtime work:
  - Add argv entry shim: `fn main(argv: Array<String>) returns Int` → C `main` builds `Array<String>` and calls Drift `main`.
  - Expose/route any user-facing string printing helper once available.
  - Keep expanding test coverage as features land (argv content/length, print, more negative cases).
