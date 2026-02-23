Spec sweep findings (`docs/design/drift-lang-spec.md` vs compiler/tests/stdlib).

Previous batch — all RESOLVED (kept for context):
- Fn1-bounded borrowed capture stale limitation
- Statement-terminator bare block statements
- Void bindability semantics
- Iterator throw/invalidation contract
- Chapter 13 internal contradiction (trait desugaring vs direct)
- Map literal target-directed wording
- Smart quotes in code samples
- Heading numbering (§3.2 duplicate, Comments unnumbered)

---

## Current batch — spec-to-implementation sweep — all RESOLVED

- Tuple types: removed §3.5 from spec; added to TODO.md Post MVP `[Types]`.
- Range syntax: removed `start..end` sentence from §8.3; added to TODO.md Post MVP `[Loops]`.
- Send/Sync: added "spec-defined but not compiler-enforced in v1" notes to §5.14, §19.6, §19.9; added to TODO.md Post MVP `[Traits]`.
- Unborrowed: added "spec-defined but not compiler-enforced in v1" note to §5.14.1; added to TODO.md Post MVP `[Traits]`.
- Reserved keywords: rewrote §9 with 5 subsections (language keywords, operator keywords, contextual keywords, reserved type names, operator tokens) derived from grammar.lark terminals.
- "in MVP" diagnostics: replaced all occurrences across `lang/driftc/` (parser, type_checker, checker, call_resolver, stage0-2, borrow_checker, macro_expander) with "in v1".

---

## Verified — high-signal claims with evidence

Spec claims checked and confirmed to match the compiler. Each entry cites one concrete test or code reference for reproducibility.

§3-4 (types, ownership, move):
- Borrow/BorrowMut: traits in `stdlib/std/core/copy.drift:73-79`; coercion in `type_checker.py:_apply_autoborrow_args()`
- Struct header form: `lang/tests/codegen/e2e/method_move_by_implicit_self/main.drift:3`
- Auto-borrowing: `lang/tests/driver/test_autoborrow_receiver_place.py:test_autoborrow_shared_receiver_allows_rvalue_place_chain`
- Const literal-only: `lang/tests/codegen/e2e/const_initializer_nonliteral_expr_rejected/`
- Move restriction: `lang/tests/driver/test_move_params.py:test_move_var_param_allowed`; rejection at `type_checker.py:6725`
- Copy restriction: `lang/tests/type_checker/test_type_checker_expressions.py:test_copy_rvalue_rejected`; rejection at `type_checker.py:6787`

§5-6 (traits, interfaces):
- Trait guards: `lang/tests/driver/test_trait_guard_scoping.py` (and/or/not all covered)
- Trait-level require conjunction-only: `stdlib/std/core/hash.drift:29` uses conjunctive require
- Destructible: `stage2/hir_to_mir.py:296` checks `Destructible::destroy`
- Interface vtables: `codegen/llvm/llvm_codegen.py:5520` (`_ensure_interface_vtable`)
- Interface receiver restriction: `codegen/llvm/llvm_codegen.py:5486` (comment: "interface method self param must be &Self or &mut Self")

§7-9 (imports, control flow):
- Export non-pub rejected: `lang/tests/driver/test_export_star_resolution.py:test_export_non_pub_rejected`
- Field visibility: `type_checker.py:4903` (E-PRIVATE-FIELD)
- Import aliasing: `lang/tests/driver/test_use_trait_resolution.py` uses `import m_traits as t`
- Export ABI boundary: `codegen/llvm/llvm_codegen.py:336-383` (wrapper generation)
- Try-on-nothrow: `lang/tests/checker/test_try_expr_semantics.py:test_try_expr_rejects_nothrow_ternary_attempt`

§10-13 (variants, optional, arrays, collections):
- @tombstone: `lang/tests/core/test_variant_tombstone_requirement.py:8-26` (implicit synthesis)
- Qualified constructors: `lang/tests/driver/test_qualified_variant_ctor_alias.py:9-38`
- Match expr+stmt: `lang/tests/driver/test_match_stmt_all_arms_return.py` (both forms)
- Map literal default: `type_checker.py:7953-7958` (HashMapCore + DefaultBuildHasher)
- TreeMap target: `stage2/hir_to_mir.py:2024` (explicit support for HashMapCore/TreeMap)

§14-17 (exceptions, memory, ABI):
- Event code xxhash64: `lang/driftc/core/event_codes.py:19-31`; `lang/driftc/core/xxhash64.py`
- throw() parens: `lang/driftc/parser/grammar.lark:542-543` (exception_ctor rule)
- ^ Diagnostic enforcement: `lang/driftc/checker/__init__.py:3257-3265`
- Nothrow error-construction ban: `lang/driftc/stage4/throw_checks.py:33-46` (constructs_error flag)
- Pipeline: `lang/driftc/parser/grammar.lark:396-397` (pipeline_tail rule)
- Array layout: `codegen/llvm/llvm_codegen.py:53-56` (LEN=0, CAP=1, GEN=2, PTR=3)

§18-22 (IO, concurrency, closures):
- I/O handles: `stdlib/std/io/io.drift:198-226` (stdin/stdout/stderr + builders)
- IoError: `stdlib/std/io/io.drift:69-71` (variant IoError { Errno(code: Int) })
- spawn/join/scope: `stdlib/std/concurrent/concurrent.drift:416,543,756`
- lang.thread intrinsics: `stdlib/lang/thread.drift:71-94`
- DMIR-PKG: `lang/driftc/packages/dmir_pkg_v0.py:24-40` (MAGIC = b"DMIRPKG\0")
- Closure captures: `lang/driftc/parser/grammar.lark` (lambda_capture_item allows NAME only)
- FnN traits: `stdlib/std/core/copy.drift:158-168` (Fn0/Fn1/Fn2)
- CallbackN: `stdlib/std/core/copy.drift:195-212` (Callback0/1/2 + intrinsic converters)
