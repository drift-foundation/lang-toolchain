## 2025-12-08 – String params & array helper decls
- Fixed LLVM backend to type arguments using function signatures (Int → i64, String → %DriftString) and emit typed call sites; function headers now preload param types into value_types.
- Moved array runtime helper declarations to module scope (emit once per module), preventing invalid IR from function-local declares.
- Added LLVM IR tests for typed params: Int+Int headers/calls and mixed Int/String param plus String return; added String literal pass-through call test.
- Updated docs/comments: compile_to_llvm_ir_for_tests now mentions Int/String/FnResult returns; string work-progress reflects param support; TODO trimmed.
- All tests green (PYTHONPATH=.. ../.venv/bin/pytest).
