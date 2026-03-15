# Drift Language Grammar 1.0 (syntax-only, normative for parsing)
<!-- CANONICAL-GRAMMAR -->

This file defines the lexical rules, precedence, and productions for Drift. It is authoritative for **syntax**. The main language specification (`drift-lang-spec.md`) remains authoritative for **semantics** (ownership, types, runtime behavior). If syntax and semantics appear to conflict, parsing follows this document; meaning follows the main spec.

## 1. Lexical structure

- **Identifiers:** `Ident ::= [A-Za-z_][A-Za-z0-9_]*`  
  Keywords are not identifiers, except `move` and `copy`, which are permitted where `Ident` appears. Double-underscore names are reserved for the compiler.
- **Literals:** `IntLiteral`, `FloatLiteral`, `StringLiteral` (UTF-8), `FStringLiteral`, `BoolLiteral` (`true` / `false`).
- **Operators/punctuation:** `+ - * / % == != < <= > >= & | ^ ~ << >> and or not ! ? : += -= *= /= %= &= |= ^= <<= >>= . , : ; = -> => [ ] { } ( ) |> <|`.
- **`mut` token:** `mut` is a keyword token and is meaningful after `&` in types/expressions.
- **Statement terminators:** `TERMINATOR` is the `;` token only. Newlines are whitespace and never produce `TERMINATOR`.

## 2. Precedence and associativity (high → low)

1. Postfix: call `()`, index `[]`, member `.`, member-through-ref `->`
2. Unary: `move`, `-`, `~`, `!`, `not`, `&`, `*` (deref)
3. Multiplicative: `*`, `/`, `%`
4. Additive: `+`, `-`
5. Comparisons: `<`, `<=`, `>`, `>=`, `==`, `!=`
6. Bitwise: `&`
7. Bitwise: `^`
8. Bitwise: `|`
9. Boolean `and`
10. Boolean `or`
11. Pipeline `|>` (left-associative)
12. Ternary `?:` (right-associative)

## 3. Grammar (EBNF-style)

Top-level:
```
Program      ::= (ModuleDecl | Item | TERMINATOR)*
ModuleDecl   ::= "module" ModulePath TERMINATOR
ModulePath   ::= Ident ("." Ident)*
Ident        ::= NAME | "move" | "copy"
```

Declarations and items:
```
Item         ::= TestBuildOnly? PubItem | TestBuildOnly? FnDef | TestBuildOnly? ConstDef
              | TestBuildOnly? StructDef | TestBuildOnly? ExceptionDef | TestBuildOnly? VariantDef
              | TestBuildOnly? TraitDef | TestBuildOnly? ImplementDef
              | UseTraitStmt | ImportStmt | ExportStmt
PubItem      ::= "pub" (FnDef | ConstDef | StructDef | ExceptionDef | VariantDef | TraitDef | ImplementDef)

ConstDef     ::= "const" NAME ":" Ty "=" Expr TERMINATOR
FnDef        ::= "fn" Ident TypeParams? "(" Params? ")" ReturnSig RequireClause? Block
ReturnSig    ::= "nothrow"? "->" Ty
Params       ::= Param ("," Param)*
Param        ::= ("var" | "val")? Ident ":" Ty

StructDef    ::= "struct" NAME TypeParams? RequireClause? TupleStruct TERMINATOR
              | "struct" NAME TypeParams? RequireClause? BlockStruct TERMINATOR?
TupleStruct  ::= "(" StructFieldList? ")"
StructFieldList ::= StructField ("," StructField)*
StructField  ::= "pub"? NAME ":" Ty
BlockStruct  ::= "{" TERMINATOR* (StructField ","? TERMINATOR*)* "}"

ExceptionDef ::= "exception" NAME "(" ExceptionParams? ")"
ExceptionParams ::= ExceptionParam ("," ExceptionParam)*
ExceptionParam ::= NAME ":" Ty | "domain" "=" STRING

VariantDef   ::= "variant" NAME TypeParams? VariantBody
VariantBody  ::= "{" VariantArm ("," VariantArm)* ","? "}"
VariantArm   ::= VariantArmAttr* NAME VariantFields?
VariantArmAttr ::= "@" NAME
VariantFields ::= "(" VariantFieldList? ")"
VariantFieldList ::= VariantField ("," VariantField)*
VariantField ::= NAME ":" Ty

TraitDef     ::= "trait" NAME RequireClause? TraitBody
TraitBody    ::= "{" (TraitItem | TERMINATOR)* "}"
TraitItem    ::= TraitMethodSig TERMINATOR*
TraitMethodSig ::= "fn" Ident "(" Params? ")" ReturnSig

ImplementDef ::= "implement" TypeParams? Ty ("for" Ty)? RequireClause? ImplementBody
ImplementBody ::= "{" (ImplementItem | TERMINATOR)* "}"
ImplementItem ::= TestBuildOnly? "pub" FnDef | TestBuildOnly? FnDef

TestBuildOnly ::= "@test_build_only"

RequireClause ::= "require" TraitExpr ("," TraitExpr)*

UseTraitStmt ::= "use" "trait" ModulePath
ImportStmt   ::= "import" ModulePath ("as" NAME)?
ExportStmt   ::= "export" "{" ExportItems? "}"
ExportItems  ::= ExportItem ("," ExportItem)*
ExportItem   ::= NAME | ModulePath "." "*"
```

Types:
```
Ty           ::= RefType | FnType | BaseType
RefType      ::= "&" "mut"? Ty
FnType       ::= "Fn" "(" (Ty ("," Ty)*)? ")" FnReturn
FnReturn     ::= "nothrow"? "->" Ty
BaseType     ::= NAME "." NAME TypeArgs? | NAME TypeArgs?
TypeArgs     ::= "[" Ty ("," Ty)* "]" | "<" Ty ("," Ty)* ">"
TypeParams   ::= "<" NAME ("," NAME)* ">"
```

Notes:
- `nothrow`, when present, appears immediately before `->` in `ReturnSig` for `FnDef`. `-> T nothrow` is invalid.
- `nothrow`, when present, appears immediately before `->` in `FnReturn`. `-> T nothrow` is invalid.
- Function types are not chainable; nesting is in the return type. `Fn(A) -> Fn(B) -> C` parses as `Fn(A) -> (Fn(B) -> C)`, and the other grouping is `Fn(Fn(A) -> Fn(B)) -> C`.
- In MVP, trait-level `require` is restricted to conjunctions of `Self is Trait`
  (supertraits). `or`/`not` are only allowed in function/struct `require` and
  in trait guards.

Statements and blocks:
```
Block        ::= "{" Stmt* "}"
ValueBlock   ::= "{" Stmt* Expr "}"

Stmt         ::= CompoundStmt | SimpleStmt TERMINATOR
CompoundStmt ::= Block | IfStmt | WhileStmt | ForStmt | TryStmt | MatchStmt
SimpleStmt   ::= LetStmt | ReturnStmt | RethrowStmt | RaiseStmt | BreakStmt
              | ContinueStmt | AugAssignStmt | AssignStmt | ExprStmt

LetStmt      ::= ("val" | "var") BindingName TypeSpec? AliasClause? "=" Expr
BindingName  ::= "^"? Ident
AliasClause  ::= "as" STRING
TypeSpec     ::= ":" Ty

ReturnStmt   ::= "return" Expr?
RethrowStmt  ::= "rethrow"
RaiseStmt    ::= "raise" DomainClause? Expr | "throw" ExceptionCtor | "throw" Expr
DomainClause ::= "domain" NAME
BreakStmt    ::= "break"
ContinueStmt ::= "continue"
WhileStmt    ::= "while" Expr Block
ForStmt      ::= "for" Ident "in" Expr Block

IfStmt       ::= "if" IfCond Block ElseClause?
IfCond       ::= TraitExpr | Expr
ElseClause   ::= "else" Block
TryStmt      ::= "try" (Block | Expr) CatchClause (CatchClause)*
CatchClause  ::= "catch" CatchPattern Block
CatchPattern ::= EventFqn "(" Ident ")" | Ident | (empty)

AssignStmt   ::= AssignTarget "=" Expr
AugAssignStmt ::= AssignTarget ("+=" | "-=" | "*=" | "/=" | "%=" | "&=" | "|=" | "^=" | "<<=" | ">>=") Expr
AssignTarget ::= PostfixExpr | "*" AssignTarget
ExprStmt     ::= PostfixExpr
```

Expressions:
```
Expr         ::= TryCatchExpr | MatchExpr | Ternary | Pipeline
MatchExpr    ::= "match" Expr "{" MatchExprArms "}"
MatchStmt    ::= "match" Expr "{" MatchStmtArms "}"
MatchExprArms ::= MatchExprArm ("," MatchExprArm)* ","?
MatchExprArm  ::= MatchPat "=>" MatchExprArmBody
MatchStmtArms ::= MatchStmtArm ("," MatchStmtArm)* ","?
MatchStmtArm  ::= MatchPat "=>" MatchStmtArmBody
MatchPat     ::= "default"
              | NAME "(" ")"
              | NAME "(" MatchNamedBinders ")"
              | NAME "(" MatchBinders ")"
              | NAME
MatchBinders ::= NAME ("," NAME)*
MatchNamedBinders ::= NAME "=" NAME ("," NAME "=" NAME)*
MatchExprArmBody ::= ValueBlock
MatchStmtArmBody ::= Block

TryCatchExpr ::= "try" Expr ("catch" CatchExprArm)+
CatchExprArm ::= EventFqn "(" Ident ")" ValueBlock
              | Ident ValueBlock
              | ValueBlock

Ternary      ::= Pipeline "?" Expr ":" Expr
Pipeline     ::= OrExpr ("|>" OrExpr)*

OrExpr       ::= AndExpr ("or" AndExpr)*
AndExpr      ::= BitOrExpr ("and" BitOrExpr)*
BitOrExpr    ::= BitXorExpr ("|" BitXorExpr)*
BitXorExpr   ::= BitAndExpr ("^" BitAndExpr)*
BitAndExpr   ::= EqExpr ("&" EqExpr)*
EqExpr       ::= CmpExpr (("==" | "!=") CmpExpr)*
CmpExpr      ::= ShiftExpr (("<" | "<=" | ">" | ">=") ShiftExpr)*
ShiftExpr    ::= AddExpr (("<<" | ">>") AddExpr)*
AddExpr      ::= MulExpr (("+" | "-") MulExpr)*
MulExpr      ::= UnaryExpr (("*" | "/" | "%") UnaryExpr)*

UnaryExpr    ::= PostfixExpr
              | "*" UnaryExpr
              | "move" UnaryExpr
              | "+" UnaryExpr
              | "-" UnaryExpr
              | "not" UnaryExpr
              | "!" UnaryExpr
              | "~" UnaryExpr
              | "&" "mut"? UnaryExpr

PostfixExpr  ::= PrimaryExpr PostfixSuffix*
PostfixSuffix ::= CallSuffix | AttrSuffix | ArrowSuffix | IndexSuffix | TypeAppSuffix | QualifiedSuffix
CallSuffix   ::= CallTypeArgs? "(" CallArgs? ")"
CallTypeArgs ::= "<" "type" Ty ("," Ty)* ">"
QualifiedSuffix ::= QualifiedPreTypeArgs? "::" NAME
QualifiedPreTypeArgs ::= "<" Ty ("," Ty)* ">"
AttrSuffix   ::= "." NAME
ArrowSuffix  ::= "->" NAME
IndexSuffix  ::= "[" (LeadingDotExpr | Expr) "]"
TypeAppSuffix ::= CallTypeArgs

EventFqn     ::= ModulePath ":" NAME

PrimaryExpr  ::= Literal
              | Ident
              | CastExpr
              | "(" Expr ")"
              | LambdaExpr
              | ArrayLiteral
              | LeadingDotExpr

LeadingDotExpr ::= "." NAME (CallSuffix | AttrSuffix | ArrowSuffix | IndexSuffix | TypeAppSuffix)*

ArrayLiteral ::= "[" ExprList? "]"
ExprList     ::= Expr ("," Expr)*

CastExpr     ::= "cast" "<" Ty ">" "(" Expr ")"

LambdaExpr   ::= "|" LambdaParams? "|" LambdaCaptures? LambdaReturns? "=>" LambdaBody
LambdaReturns ::= "->" Ty
LambdaParams ::= LambdaParam ("," LambdaParam)*
LambdaParam  ::= ("var" | "val")? NAME (":" Ty)?
LambdaCaptures ::= "captures" "(" (LambdaCaptureItem ("," LambdaCaptureItem)*)? ")"
LambdaCaptureItem ::= "copy" NAME | "move" NAME | "&" "mut"? NAME | NAME
LambdaBody   ::= Expr | ValueBlock | Block

TraitExpr    ::= TraitOr
TraitOr      ::= TraitAnd ("or" TraitAnd)*
TraitAnd     ::= TraitNot ("and" TraitNot)*
TraitNot     ::= "not" TraitNot | TraitAtom
TraitAtom    ::= TraitSubject "is" TraitName | "(" TraitExpr ")"
TraitSubject ::= NAME
TraitName    ::= BaseType

ExceptionCtor ::= NAME "(" CallArgs? ")"
CallArgs     ::= CallArg ("," CallArg)*
CallArg      ::= NAME "=" Expr | Expr

Literal      ::= IntLiteral | FloatLiteral | StringLiteral | BoolLiteral | FStringLiteral
```

### Notes

- Pipelines use `|>` and are left-associative; `<|` is reserved for a future reverse-pipeline form.
- Lambda captures are inferred by default; `captures(...)` opts into explicit capture mode.
- This grammar is a reference for parsers; semantic rules (ownership, moves, errors) are defined in `drift-lang-spec.md`.
- In normal blocks, a bare expression must appear as a statement (`ExprStmt`) and end with `;`. In a `ValueBlock` (`{ ... Expr }`), statements require `;`, but the final value expression must not.
- Examples: `try f() catch { 0 }`, `try f() catch { log("x"); 0 }`. A `{ x + 1 }` block is only legal where a value-producing block is expected (lambda bodies, match **expression** arms, try/catch expression arms).
- Leading-dot expressions (`.foo`, `.foo(...)`) are only valid inside indexing brackets or argument lists; they desugar to member access on the receiver value (see §2.x “Receiver placeholder” in `drift-lang-spec.md` for semantics).
