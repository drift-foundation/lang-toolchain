# 🚨 **Global Architectural Change Summary**

> **HISTORICAL — RETIRED (Slice 7c-3, ABI 14, 2026-05-06).**  This
> change request introduced the `DiagnosticValue` variant + the
> `Error.attrs: Map<String, DiagnosticValue>` shape.  Both were
> implemented, shipped through ABI 12, and **fully retired** by the
> DV→JSON migration: Slice 7a removed the public DV surface, Slice
> 7b retired the throw-side DV-attachment, and Slices 7c-1/7c-2/7c-3
> deleted the runtime exports, compiler-internal substrate, and
> residual `TypeKind.DIAGNOSTICVALUE` type identity.  The canonical
> ABI 14 surface is `params_json` + `context_json` (canonical JSON
> text), with diagnostic projection owned by
> `core.Diagnostic.to_json_text(&E) -> String`.  See
> `doc/design/drift-lang-abi.md` §2 for the live error shape and
> `doc/design/drift-lang-spec.md` §5.13.8 for the public diagnostic
> surface.  This document is preserved as the historical design
> record; do not implement against it.

We are committing the following core changes:

### **A. Replace string-based diagnostics with tree-shaped `DiagnosticValue`**

- Introduce `DiagnosticValue` variant (JSON-compatible), non-throwing navigation.
- All diagnostics (exception attrs + `^` captures) lower to `DiagnosticValue`.

### **B. Replace `Error.args: Map<String,String>` with `Error.attrs: Map<String, DiagnosticValue>`**

- Remove args-view, `.field` sugar, typed arg keys.
- Uniform access: `e.attrs["sql_code"].as_int()`.

### **C. Replace exception declaration syntax**

From:

```
exception InvalidOrder(order_id: Int, code: String)
```

To:

```
exception InvalidOrder {
    order_id: Int,
    code: String,
}
```

### **D. Remove dot-notation flattening as a mandatory mechanism**

- It becomes *optional serialization*, not internal representation.

### **E. Update ABI spec**

- ABI stores attributes as JSON-like values (stringified form for cross-boundary C ABI).
- Attribute list is no longer key→string; it’s key→JSON-encoded attribute.

### **F. Update DMIR**

- DMIR now represents exception attrs as literal `DiagnosticValue` variant expressions.
- DMIR will serialize attrs deterministically and in canonical order.

---

# ✳️ **SPEC UPDATE STEPS (drift-lang-spec.md)**

## **1. Add a new chapter: “DiagnosticValue type”**

Insert after §5.13 “Diagnostics”:

---

### **§5.13.8 DiagnosticValue: structured diagnostics**

Define the variant:

```drift
variant DiagnosticValue {
    Missing
    Null
    Bool(value: Bool)
    Int(value: Int64)
    Float(value: Float64)
    String(value: String)
    Array(items: Array<DiagnosticValue>)
    Object(fields: Map<String, DiagnosticValue>)
}
```

### **Helper methods (library-provided, no new syntax)**

```drift
implement DiagnosticValue {
    fn kind(self: &DiagnosticValue) -> String   // optional helper

    fn get(self: &DiagnosticValue, field: String) -> DiagnosticValue
    fn index(self: &DiagnosticValue, idx: Int) -> DiagnosticValue

    fn as_string(self: &DiagnosticValue) -> Optional<String>
    fn as_int(self: &DiagnosticValue) -> Optional<Int64>
    fn as_bool(self: &DiagnosticValue) -> Optional<Bool>
    fn as_float(self: &DiagnosticValue) -> Optional<Float64>
}
```

Rules:

- **Never throws**.
- Wrong type, absent field, out-of-bounds → returns `DiagnosticValue::Missing`.
- Any `.as_*()` on `Missing` returns `Optional.none`.

---

## **2. Replace DiagnosticCtx / Diagnostic trait semantics**

Rewrite §5.13.7:

- Delete `DiagnosticCtx` completely.
- Replace with:

```drift
trait Diagnostic {
    fn to_diag(self) -> DiagnosticValue
}
```

- Primitive types implement `to_diag` as obvious scalars.
- Structs without custom impl default to:

```drift
Object({ field_name: field_value.to_diag(), ... })
```

---

## **3. Replace `Error.args` with `Error.attrs`**

In §14.2 replace:

```
args: Map<String, String>
```

with:

```
attrs: Map<String, DiagnosticValue>
```

And update entire chapter to use the term **attrs**.

---

## **4. Update JSON logging example (§14.8)**

Replace:

```
"args": { "order_id": "42", "code": "order.invalid" }
```

with:

```
"attrs": {
    "order_id": 42,
    "code": "order.invalid"
}
```

Nested example:

```
"attrs": {
    "order": {
        "id": 42,
        "customer": { "id": "c-123" }
    }
}
```

---

## **5. Replace exception declaration syntax in §14.3**

Old:

```
exception InvalidOrder(order_id: Int64, code: String)
```

New:

```drift
exception InvalidOrder {
    order_id: Int64,
    code: String,
}
```

Throw site unchanged.

---

## **6. Update catch usage**

Old:

```
val code = e.args[.sql_code]
```

New:

```drift
val code = e.attrs["sql_code"].as_int()
```

Nested diagnostic access:

```drift
val cust = e.attrs["order"]["customer"]["id"].as_string()
```

---

## **7. Update ^ capture semantics (§14.4)**

Replace:

```
locals: Map<String,String>
```

with:

```
locals: Map<String, DiagnosticValue>
```

Captured values must implement `Diagnostic` → runtime stores full `DiagnosticValue`.

---

## **8. Remove args-view (§14.5.4)**

Delete entire §14.5.4.

Replace with:

### **§14.5.4 Accessing attributes**

```drift
fn attrs(self: &Error) -> Map<String, DiagnosticValue]
```

Uniform model; no dot-shortcuts; no typed arg keys.

---

# ✳️ **GRAMMAR CHANGES (drift-lang-grammar.md)**

## **1. Update `ExceptionDef` grammar**

Replace:

```
ExceptionDef ::= "exception" Ident TraitParams? "(" Fields? ")" TraitReq?
```

with:

```
ExceptionDef ::= "exception" Ident TraitParams? "{" Fields? "}" TraitReq?
```

No further grammar changes needed.

---

# ✳️ **ABI CHANGES (drift-abi-exceptions.md)**

## **1. DriftErrorAttr**

Keep layout:

```c
struct DriftErrorAttr { const char* key; const char* value_json; };
```

But update semantics:

- `value_json` stores **canonical JSON encoding** of the `DiagnosticValue` tree.
- All values serialize as JSON scalars, arrays, or objects.

---

## **2. Error→ABI conversion text**

Replace:

> "(key, value) pairs of strings"

with:

> "(key, JSON-string) pairs, where the JSON-string is the canonical encoding of the DiagnosticValue tree".

---

# ✳️ **DMIR CHANGES (dmir-spec.md)**

## **1. Update Error lowering rules**

Replace “args lowered to string pairs” with:

> attrs lowered to key→DiagnosticValue variant literals.

DMIR constructors must include canonical forms:

- `DV_Missing`
- `DV_Null`
- `DV_Bool(true/false)`
- `DV_Int(…)`
- `DV_Float(…)`
- `DV_String(…)`
- `DV_Array([…])`
- `DV_Object([(key, dv), …])`

---

## **2. Update DMIR struct for Error**

```
attrs: Map<String, DiagnosticValue>
ctx_frames: Array<CtxFrame>  // locals: Map<String, DiagnosticValue>
```

---

## **3. Canonicalization rule**

- Internal DMIR preserves declaration order.
- JSON export sorts object keys lexicographically.

---

## **4. Exception constructor lowering**

Replace:

```
Invalid(code="bad") → Error(args…)
```

with:

```
Invalid {
    code: "bad"
}
→ Error {
    event: "Invalid",
    attrs: { "code": DV_String("bad") },
    ctx_frames: [],
    stack: capture_stack()
}
```
