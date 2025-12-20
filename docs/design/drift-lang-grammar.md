# Drift Language Grammar 1.0 (syntax-only, normative for parsing)

This file defines the lexical rules, precedence, and productions for Drift. It is authoritative for **syntax**. The main language specification (`drift-lang-spec.md`) remains authoritative for **semantics** (ownership, types, runtime behavior). If syntax and semantics appear to conflict, parsing follows this document; meaning follows the main spec.

## 1. Lexical structure

- **Identifiers:** `Ident ::= [A-Za-z_][A-Za-z0-9_]*`  
  Keywords (see reserved list in the main spec) are not identifiers.
- **Literals:** `IntLiteral`, `FloatLiteral`, `StringLiteral` (UTF-8), `BoolLiteral` (`true` / `false`).
- **Operators/punctuation:** `+ - * / % == != < <= > >= & | ^ ~ << >> and or not ! ? : += -= *= /= %= &= |= ^= <<= >>= . , : ; = -> => [ ] { } ( )`.
- **`mut` token:** `mut` is not reserved; it is treated as an identifier token and is meaningful after `&` in types/expressions.
- **Newlines / terminators:** The lexer may emit a `TERMINATOR` token on newline (`\n`) when **all** hold:
  1. Parenthesis/brace/bracket depth is zero.
  2. The previous token is “terminable” (identifiers, literals, `)`, `]`, `}`, `return`, `break`, etc.).
  3. The previous token is **not** an operator or separator that requires a follower (`+`, `*`, `>>`, `.`, `,`, `:`, `?`, etc.).
  Parsers may treat `TERMINATOR` like a semicolon; an explicit `;` is also allowed anywhere a `TERMINATOR` could appear.

## 2. Precedence and associativity (high → low)

1. Postfix: call `()`, index `[]`, member `.`, member-through-ref `->`
2. Unary: `move`, `copy`, `-`, `~`, `!`, `not`, `&`, `*` (deref)
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
Program      ::= ModuleDecl? ExportDecl* ImportDecl* TopDecl*
ModuleDecl   ::= "module" ModulePath TERMINATOR?
ModulePath   ::= Ident ("." Ident)*
ExportDecl   ::= "export" "{" ExportItems? "}" TERMINATOR
ExportItems  ::= Ident ("," Ident)*
ImportDecl   ::= "import" ImportItem ("," ImportItem)* TERMINATOR
ImportItem   ::= ModulePath ("as" Ident)?
```

Declarations:
```
TopDecl      ::= FnDef | StructDef | TraitDef | Implement | VariantDef
               | ExceptionDef | InterfaceDef | TypeDef | ConstDef

FnDef        ::= "fn" Ident "(" Params? ")" Returns? TraitReq? Block
Params       ::= Param ("," Param)*
Param        ::= ("^")? Ident ":" Ty
Returns      ::= "returns" Ty | ":" Ty

StructDef    ::= "struct" Ident StructParams? StructBody
StructParams ::= "<" Ident ("," Ident)* ">"
StructBody   ::= "(" Fields? ")" | "{" Fields? "}"
Fields       ::= Field ("," Field)*
Field        ::= Ident ":" Ty

TraitDef     ::= "trait" Ident TraitParams? TraitReq? TraitBody
TraitParams  ::= "<" Ident ("," Ident)* ">"
TraitReq     ::= "require" TraitReqClause ("," TraitReqClause)*
TraitReqClause ::= ("Self" | Ident) "is" TraitExpr
TraitExpr    ::= TraitTerm (("and" | "or") TraitTerm)*
TraitTerm    ::= "not"? Ident | "(" TraitExpr ")"
TraitBody    ::= "{" TraitMember* "}"
TraitMember  ::= FnSig TERMINATOR?

Implement    ::= "implement" Ty ("for" Ty)? TraitReq? TraitBody

VariantDef   ::= "variant" Ident TraitParams? "{" VariantItem+ "}"
VariantItem  ::= Ident ("(" Fields? ")")?

ExceptionDef ::= "exception" Ident TraitParams? "{" Fields? "}" TraitReq?

InterfaceDef ::= "interface" Ident TraitParams? "{" InterfaceMember+ "}"
InterfaceMember ::= FnSig TERMINATOR?

TypeDef      ::= "type" Ident "=" Ty TERMINATOR?

ConstDef     ::= "const" Ident ":" Ty "=" Expr TERMINATOR
```

Functions and types:
```
FnSig        ::= "fn" Ident "(" Params? ")" Returns? TraitReq?
Ty           ::= "&" "mut"? Ty
              | Ident TraitParams?
              | Ty "[" "]"                // array type
              | "(" Ty ("," Ty)+ ")"      // tuple type (n >= 2)
              | VariantType | InterfaceType | TraitType

VariantType  ::= Ident TraitParams?
InterfaceType::= Ident TraitParams?
TraitType    ::= Ident TraitParams?
```

Statements and blocks:
```
Block        ::= "{" Stmt* "}"
Stmt         ::= ValDecl | VarDecl | ExprStmt | IfStmt | WhileStmt | ForStmt
               | ReturnStmt | BreakStmt | ContinueStmt | TryStmt | ThrowStmt

ValDecl      ::= "val" Ident (":" Ty)? "=" Expr TERMINATOR
VarDecl      ::= "var" Ident (":" Ty)? "=" Expr TERMINATOR
ExprStmt     ::= Expr TERMINATOR

IfStmt       ::= "if" Expr Block ("else" (Block | IfStmt))?
WhileStmt    ::= "while" Expr Block
ForStmt      ::= "for" "(" (ValDecl | VarDecl | ExprStmt)? Expr? TERMINATOR Expr? ")" Block
ReturnStmt   ::= "return" Expr? TERMINATOR
BreakStmt    ::= "break" TERMINATOR
ContinueStmt ::= "continue" TERMINATOR
TryStmt      ::= "try" Block (TryElse | TryCatch)?
TryElse      ::= "else" Block
TryCatch     ::= "catch" (Ident)? Block
ThrowStmt    ::= "throw" ExceptionCtor TERMINATOR
ExceptionCtor::= Ident "(" CallArgs? ")"
```

Expressions:
```
Expr         ::= TernaryExpr
TernaryExpr  ::= PipelineExpr ("?" Expr ":" Expr)?
PipelineExpr ::= OrExpr ("|>" OrExpr)*

OrExpr       ::= AndExpr ("or" AndExpr)*
AndExpr      ::= CmpExpr ("and" CmpExpr)*
CmpExpr      ::= AddExpr (CmpOp AddExpr)*
CmpOp        ::= "<" | "<=" | ">" | ">=" | "==" | "!="
AddExpr      ::= MulExpr (AddOp MulExpr)*
AddOp        ::= "+" | "-"
MulExpr      ::= UnaryExpr (MulOp UnaryExpr)*
MulOp        ::= "*" | "/" | "%"
UnaryExpr    ::= BorrowExpr | NormalUnary
BorrowExpr   ::= "&" "mut"? PostfixExpr
NormalUnary  ::= ("-" | "!" | "not")* PostfixExpr
UnaryOp      ::= "-" | "!" | "not" | "&"

PostfixExpr  ::= PrimaryExpr PostfixTail*
PostfixTail  ::= "." Ident
              | "[" ExprOrLeadingDot "]"
              | "(" ArgsOrLeadingDot? ")"
              | "->" Ident

CallExpr     ::= PostfixExpr  // for pipeline staging clarity

Args         ::= Expr ("," Expr)*
ArgsOrLeadingDot ::= ExprOrLeadingDot ("," ExprOrLeadingDot)*
ExprOrLeadingDot ::= Expr | LeadingDotExpr
LeadingDotExpr ::= "." Ident ("(" Args? ")")?

PrimaryExpr  ::= Literal
              | Ident
              | "(" Expr ")"
              | TupleExpr
              | ArrayLiteral
              | MapLiteral
              | TypeInitExpr
              | MatchExpr
              | TryCatchExpr
              | LambdaExpr

TupleExpr    ::= "(" Expr ("," Expr)+ ")"
ArrayLiteral ::= "[" (Expr ("," Expr)*)? "]"
MapLiteral   ::= "{" (MapEntry ("," MapEntry)*)? "}"
MapEntry     ::= Expr ":" Expr
TypeInitExpr ::= Ident "{" FieldAssignList? "}"
FieldAssignList ::= FieldAssign ("," FieldAssign)* (",")?
FieldAssign     ::= Ident "=" Expr
MatchExpr    ::= "match" Expr "{" MatchArm+ "}"
MatchArm     ::= Pattern "=>" Expr TERMINATOR?
Pattern      ::= Ident | Literal | "(" Pattern ("," Pattern)+ ")"
TryCatchExpr ::= "try" Expr ("catch" CatchExprArm)+
CatchExprArm ::= Ident "(" Ident ")" Block    // event-specific
               | Ident Block                  // catch-all with binder
               | Block                        // catch-all
LambdaExpr   ::= "|" LambdaParams? "|" "=>" (Expr | Block)
LambdaParams ::= LambdaParam ("," LambdaParam)*

LambdaParam  ::= Capture? Ident (":" Ty)?
Capture      ::= "copy"
Literal      ::= IntLiteral | FloatLiteral | StringLiteral | BoolLiteral
```

### Notes

- Tuple types require at least two elements; `(T)` is just `T`.
- Pipelines use `|>` and are left-associative; `<|` is reserved for a future reverse-pipeline form.
- Zero-argument callables use `Void` as their argument type and are called with `f.call()`.
- This grammar is a reference for parsers; semantic rules (ownership, moves, errors) are defined in `drift-lang-spec.md`.
- In blocks, a bare expression must appear as a statement (`ExprStmt`) with a terminator (`;` or newline), e.g., `catch { 0; }` or `catch { return 0; }`. A future ergonomics pass may allow `{ 0 }` in expression contexts, but it is not supported today.
- Leading-dot expressions (`.foo`, `.foo(...)`) are only valid inside indexing brackets or argument lists; they desugar to member access on the receiver value (see §2.x “Receiver placeholder” in `drift-lang-spec.md` for semantics).
