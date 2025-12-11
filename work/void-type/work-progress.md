# Design and implementation plan for `Void` type in Drift

## 1. Goal

Replace the current hack of using `Int` as a stand‑in for “no useful return value” and introduce a proper `Void` type that:

- Is a first‑class type in the type system.
- Can be used as a function return type.
- Has no runtime/SSA value.
- Is not allowed as a stored type (locals, fields, arrays).
- Maps cleanly to LLVM `void`.

This change must be wired through: parser → type system → checker → MIR/SSA → LLVM codegen → prelude.

---

## 2. Language semantics

- `Void` means: “this function returns no meaningful value.”
- It is **not** the same as a “never returns” type; if we want that later, we’ll add a separate `Never`/`!` type.
- Expressions whose type is `Void`:
  - Are allowed only in statement position (expression statements).
  - Are not usable as values in assignments, arithmetic, comparisons, or returns of non‑Void functions.
- A function declared with `returns Void` may:
  - Use `return;` (no expression).
  - Omit `return` entirely and just fall off the end.
  - Must **not** use `return expr;`.

---

## 3. Type system changes

### 3.1. TypeKind and TypeTable

- Add a new kind:

  ```python
  class TypeKind(Enum):
      INT = auto()
      BOOL = auto()
      STRING = auto()
      ARRAY = auto()
      FN_RESULT = auto()
      VOID = auto()  # new
  ```

- Seed a canonical `Void` instance in `TypeTable`:

  ```python
  class TypeTable:
      def __init__(self):
          self._types: list[TypeInfo] = []
          self._int_type_id = self._add_builtin(TypeKind.INT, "Int")
          self._bool_type_id = self._add_builtin(TypeKind.BOOL, "Bool")
          self._string_type_id = self._add_builtin(TypeKind.STRING, "String")
          self._void_type_id = self._add_builtin(TypeKind.VOID, "Void")  # new

      def ensure_void(self) -> TypeId:
          return self._void_type_id

      def is_void(self, type_id: TypeId) -> bool:
          return self._types[type_id].kind is TypeKind.VOID
  ```

### 3.2. ABI helpers

- Size and scalar‑ness:

  ```python
  def type_size_in_bytes(self, tid: TypeId) -> int:
      kind = self._types[tid].kind
      if kind is TypeKind.VOID:
          return 0  # zero-sized
      ...
  ```

  ```python
  def is_scalar_type(self, tid: TypeId) -> bool:
      kind = self._types[tid].kind
      if kind is TypeKind.VOID:
          return False
      ...
  ```

- Storage rule:

  ```python
  def can_be_stored(self, tid: TypeId) -> bool:
      kind = self._types[tid].kind
      if kind is TypeKind.VOID:
          return False
      return True
  ```

  This will be enforced at the checker level so that you can't declare locals/fields/arrays of `Void`.

---

## 4. Parser and type resolution

### 4.1. Parsing the `Void` type

- Extend type‑name parsing to recognize `Void`:

  ```python
  def parse_type_name(self) -> TypeRef:
      ident = self.expect_ident()
      if ident == "Int":
          return TypeRef.builtin_int()
      if ident == "Bool":
          return TypeRef.builtin_bool()
      if ident == "String":
          return TypeRef.builtin_string()
      if ident == "Void":
          return TypeRef.builtin_void()  # new
      ...
  ```

- Add the constructor:

  ```python
  @dataclass
  class TypeRef:
      kind: TypeRefKind
      name: str | None = None
      ...

      @staticmethod
      def builtin_void() -> "TypeRef":
          return TypeRef(kind=TypeRefKind.BUILTIN, name="Void")
  ```

### 4.2. Resolving `Void` to a TypeId

- In the `TypeRef` → `TypeId` resolver:

  ```python
  def resolve_type_ref(self, tref: TypeRef) -> TypeId:
      if tref.kind is TypeRefKind.BUILTIN:
          if tref.name == "Int":
              return self.types.ensure_int()
          if tref.name == "Bool":
              return self.types.ensure_bool()
          if tref.name == "String":
              return self.types.ensure_string()
          if tref.name == "Void":
              return self.types.ensure_void()  # new
      ...
  ```

### 4.3. Function signatures

- Function declaration resolution remains mostly unchanged: the `return_type_id` now may be `Void`:

  ```python
  ret_type_id = self.resolve_type_ref(ast_fn_decl.return_type_ref)
  fn_sig.return_type_id = ret_type_id
  ```

- No implicit default return type for now:
  - Every function must declare `returns Int` / `Bool` / `String` / `Void`.
  - `main` remains `returns Int`.

---

## 5. Type checker rules

### 5.1. Return statements

- Enforce consistency between function return type and `return` statements:

  ```python
  def check_return_stmt(self, fn_sig: FnSignature, stmt: ReturnStmt):
      ret_tid = fn_sig.return_type_id
      is_void = self.types.is_void(ret_tid)

      if stmt.expr is None:
          if not is_void:
              self.error(stmt.span, "non-void function must return a value")
          return

      # stmt.expr is present
      expr_tid = self.check_expr(stmt.expr)
      if is_void:
          self.error(stmt.span, "cannot return a value from a Void function")
      else:
          self.require_assignable(expr_tid, ret_tid, stmt.span)
  ```

### 5.2. Expression statements

- Expression statements are allowed whether the expression is `Void` or not:

  ```python
  def check_expr_stmt(self, stmt: ExprStmt):
      expr_tid = self.check_expr(stmt.expr)
      # If expr_tid is Void, that's fine: result is discarded.
      return
  ```

### 5.3. Forbid `Void` where a value is required

- Add a helper:

  ```python
  def ensure_non_void(self, tid: TypeId, span: Span, context: str):
      if self.types.is_void(tid):
          self.error(span, f"Void value is not allowed in {context}")
  ```

- Use it in:
  - Assignments:

    ```python
    def check_assign(self, stmt: AssignStmt):
        value_tid = self.check_expr(stmt.value)
        self.ensure_non_void(value_tid, stmt.value.span, "assignment")
        ...
    ```

  - Binary expressions:

    ```python
    def check_binary_expr(self, expr: BinaryExpr) -> TypeId:
        left_tid = self.check_expr(expr.left)
        right_tid = self.check_expr(expr.right)
        self.ensure_non_void(left_tid, expr.left.span, "binary operation")
        self.ensure_non_void(right_tid, expr.right.span, "binary operation")
        ...
    ```

  - Function arguments (for now, disallow `Void` params):

    ```python
    def check_call_expr(self, expr: CallExpr) -> TypeId:
        callee_sig = self.lookup_fn(expr.callee)
        ...
        for arg_ast, param_tid in zip(expr.args, callee_sig.param_type_ids):
            arg_tid = self.check_expr(arg_ast)
            self.ensure_non_void(arg_tid, arg_ast.span, "function argument")
            ...
        return callee_sig.return_type_id
    ```

### 5.4. Call expression type

- The type of a call expression is simply the callee's declared return type:

  ```python
  def check_call_expr(self, expr: CallExpr) -> TypeId:
      ...
      return callee_sig.return_type_id  # may be Void
  ```

  Any misuse of a `Void` typed call outside statement context will be caught by the rules above.

---

## 6. MIR / SSA changes

### 6.1. Function metadata

- Keep MIR function metadata unchanged; it just needs to support `Void` as a `return_type_id`:

  ```python
  @dataclass
  class MirFn:
      name: str
      return_type_id: TypeId
      params: list[MirLocal]
      locals: list[MirLocal]
      blocks: list[MirBlock]
  ```

### 6.2. Call and return instructions

- Define MIR nodes with optional result:

  ```python
  @dataclass
  class MirCall:
      dest: MirValue | None  # None for Void-returning calls
      fn: MirValue
      args: list[MirValue]

  @dataclass
  class MirReturn:
      value: MirValue | None  # None for Void functions
  ```

- In HIR → MIR lowering:

  - For calls in expression statement position and/or calls to a `Void` function:
    - Emit `MirCall(dest=None, ...)`.
  - For returns in a `Void` function:
    - Emit `MirReturn(value=None)`.

- Invariants:
  - If the callee's return type is `Void`, `dest` **must** be `None`.
  - Checker guarantees you never have “Void call assigned to variable”, so MIR doesn't need to model that case.

---

## 7. LLVM codegen

### 7.1. Type mapping

- Map `Void` to LLVM `void`:

  ```python
  from llvmlite import ir

  def llvm_type_for(self, tid: TypeId) -> ir.Type:
      if self.types.is_void(tid):
          return ir.VoidType()
      kind = self.types[tid].kind
      if kind is TypeKind.INT:
          return ir.IntType(64)
      ...
  ```

### 7.2. Function declarations

- Use the mapped type for return types:

  ```python
  def declare_function(self, fn_info: FnInfo):
      ret_ty = self.llvm_type_for(fn_info.return_type_id)
      param_tys = [self.llvm_type_for(tid) for tid in fn_info.param_type_ids]
      fn_ty = ir.FunctionType(ret_ty, param_tys)
      fn = ir.Function(self.module, fn_ty, name=fn_info.mangled_name)
      ...
  ```

### 7.3. Return emission

- Emit `ret void` for Void functions:

  ```python
  def emit_return(self, mir_ret: MirReturn, fn_ret_tid: TypeId):
      if self.types.is_void(fn_ret_tid):
          self.builder.ret_void()
      else:
          value = self.get_llvm_value(mir_ret.value)
          self.builder.ret(value)
  ```

### 7.4. Call emission

- Omit result on `void` calls:

  ```python
  def emit_call(self, mir_call: MirCall, callee_ret_tid: TypeId) -> ir.Value | None:
      callee_llvm = self.get_llvm_value(mir_call.fn)
      arg_values = [self.get_llvm_value(a) for a in mir_call.args]

      if self.types.is_void(callee_ret_tid):
          self.builder.call(callee_llvm, arg_values)
          return None
      else:
          return self.builder.call(callee_llvm, arg_values, name="calltmp")
  ```

- The MIR→LLVM layer must respect the invariant:
  - If `mir_call.dest is None`, it must not try to store the call result anywhere.

---

## 8. Prelude updates

Once all the above is hooked up and tested:

1. Change prelude function signatures that conceptually return nothing (e.g. console I/O):

   - Before:

     ```python
     return_type_id = types.ensure_int()
     ```

   - After:

     ```python
     return_type_id = types.ensure_void()
     ```

2. Confirm that:
   - Their LLVM signatures now use `void` return type.
   - Call sites no longer expect a result in the IR/SSA.

`main` stays `returns Int` for now.

---

## 9. Testing checklist

Add tests that exercise the new behavior:

1. **Valid Void function:**

   ```drift
   fn log(msg: String) returns Void {
       std.console.out.writeln(msg);
   }
   ```

2. **Void function returning a value (error):**

   ```drift
   fn bad() returns Void {
       return 123; // error
   }
   ```

3. **Non-void function without value (error):**

   ```drift
   fn bad2() returns Int {
       return; // error
   }
   ```

4. **Using Void call as value (error):**

   ```drift
   fn f() returns Void { ... }

   fn g() returns Int {
       let x = f();  // error: cannot use Void in assignment
       return 0;
   }
   ```

5. **Expression statement with Void call (ok):**

   ```drift
   fn g() returns Int {
       std.console.out.writeln("hello"); // returns Void, used as stmt
       return 0;
   }
   ```

6. **Prelude integration:**
   - Call prelude console functions from `main`.
   - Verify generated LLVM uses `void` for them, and `main` still returns `Int`.

---

## 10. Future extensions

This design intentionally keeps `Void` simple:

- It is non‑storable and non‑generic.
- It has no runtime representation.
- It is purely a “no meaningful return” type.

If/when you add generics or need a proper unit type, you can:

- Introduce `Unit` or `()` as a storable, zero‑sized type distinct from `Void`.
- Introduce `Never`/`!` for non‑returning functions and integrate it into control‑flow and exhaustiveness analysis.

For now, this plan gets you a correct, clean `Void` type wired end‑to‑end through the compiler and ABI.

---

## Progress log

- [x] Audit lang2 type core and resolver: `TypeKind` only has SCALAR/ERROR/FNRESULT/FUNCTION/ARRAY/REF/UNKNOWN; `TypeTable` seeds Int/Bool/String/Uint/Error/Unknown only. `_resolve_type` maps Int/Bool/String/Uint/Error/FnResult/Array, falls back to Unknown; no `Void` mapping and `None` -> Unknown. Parser builds `TypeExpr` names directly (no special-casing), so adding `Void` means updating resolver + type table to seed a builtin.
- [x] Add `Void` to `TypeKind`/`TypeTable` with helpers (`ensure_void`, `is_void`, storage rules, size/scalar helpers).
- [x] Thread `Void` through resolver/parser builtin mapping so `returns Void` resolves to the canonical TypeId, and stub checker helper now understands `Void` in declared/opaque types. Added unit test for stable `Void` TypeId seeding.
- [x] Centralize raw→TypeId mapping via `resolve_opaque_type` and use it in resolver + checker; added canonical `ensure_error` to avoid duplicate `Error` TypeIds and covered both with tests.
- [x] Extend checker rules for `Void` (return statement validation, forbid use as value, expression statements ok).
- [x] Update MIR/LLVM lowering to treat `Void` as no SSA value and emit `ret void`/void calls.
- [x] Update prelude signatures to use `Void` where appropriate and add tests from checklist.
- [x] Broaden type inference/diagnostics for `Void` in more contexts (ternary, array literals, try operands) so misuse is caught uniformly.
- [x] Extend SSA/type_env to carry `Void`-aware facts to tighten codegen/assertions (skip void call dest typing, void return seeding).
- [x] Hooked the driftc CLI to surface checker diagnostics (e.g., Void return misuse) and added codegen e2e coverage (`void_basic`, `void_return_value_error`) with corrected expectations.
