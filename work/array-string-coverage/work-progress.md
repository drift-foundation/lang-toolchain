Done
• Added Array<String> store IR coverage: new test constructs an Array<String>, stores a %DriftString element, and returns it; IR is
  checked for %DriftString element type, alloc/bounds checks, and the store.
• Fixed test setup to import ArrayIndexStore and return a String value to match the function signature.
• Cleaned up checker bitwise detection earlier; all tests (including new array test) pass.
• Added e2e `string_array_store_read`: writes into an Array<String>, reads back lengths, exits with expected total (Uint).
• Added e2e `string_array_store_len`: mutates Array<String> and sums element byte lengths, exits with expected Uint result.
• Implemented argv entry: C helper `drift_build_argv`, LLVM argv wrapper (sret), rename of user `main` -> `drift_main` for Array<String> signatures, runner now passes sample args and enforces single `main`. Added e2e `main_argv_len`/`main_argv_content`.
• Added checker negatives for Array<String> misuse (string index, mixed literal elems).
• Added checker diagnostics/tests for string misuse (String + Int, String used as if-condition).
• Parser now rejects duplicate function definitions; added test to ensure duplicates raise early.

TODO:
  - Missing negatives: add e2e/unit cases for other misuse (e.g., String + Int, if s where s: String).
