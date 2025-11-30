# Drift Language Grammar (syntax-only, normative for parsing)

This file defines the lexical rules, precedence, and productions for Drift. It is authoritative for **syntax**. The main language specification (`drift-lang-spec.md`) remains authoritative for **semantics** (ownership, types, runtime behavior). If syntax and semantics appear to conflict, parsing follows this document; meaning follows the main spec.

## 1. Lexical structure

- **Identifiers:** `Ident ::= [A-Za-z_][A-Za-z0-9_]*`  
  Keywords (see reserved list in the main spec) are not identifiers.
- **Literals:** `IntLiteral`, `FloatLiteral`, `StringLiteral` (UTF-8), `BoolLiteral` (`true` / `false`).
- **Operators/punctuation:** `+ - * / % == != < <= > >= and or not ! ? : >> << |> <| . , : ; = -> => [ ] { } ( ) &`.
- **`mut` token:** `mut` is not reserved; it is treated as an identifier token and is meaningful after `&` in types/expressions.
- **Newlines / terminators:** The lexer may emit a `TERMINATOR` token on newline (`\n`) when **all** hold:
  1. Parenthesis/brace/bracket depth is zero.
  2. The previous token is “terminable” (identifiers, literals, `)`, `]`, `}`, `return`, `break`, etc.).
  3. The previous token is **not** an operator or separator that requires a follower (`+`, `*`, `>>`, `.`, `,`, `:`, `?`, etc.).
  Parsers may treat `TERMINATOR` like a semicolon; an explicit `;` is also allowed anywhere a `TERMINATOR` could appear.

## 2. Precedence and associativity (high → low)

1. Postfix: call `()`, index `[]`, member `.`, move `->`
2. Unary: `-`, `!`, `not`, `&`
3. Multiplicative: `*`, `/`, `%`
4. Additive: `+`, `-`
5. Comparisons: `<`, `<=`, `>`, `>=`, `==`, `!=`
6. Boolean `and`
7. Boolean `or`
8. Pipeline `>>` (left-associative)
9. Ternary `?:` (right-associative)

## 3. Grammar (EBNF-style)

Top-level:
```
Program      ::= ModuleDecl? ImportDecl* TopDecl*
ModuleDecl   ::= "module" ModulePath TERMINATOR?
ModulePath   ::= Ident ("." Ident)*
ImportDecl   ::= "import" ImportItem ("," ImportItem)* TERMINATOR
ImportItem   ::= ModulePath ("as" Ident)?
```

Declarations:
```
TopDecl      ::= FnDef | StructDef | TraitDef | Implement | VariantDef
               | ExceptionDef | InterfaceDef | TypeDef

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

ExceptionDef ::= "exception" Ident TraitParams? "(" Fields? ")" TraitReq?

InterfaceDef ::= "interface" Ident TraitParams? "{" InterfaceMember+ "}"
InterfaceMember ::= FnSig TERMINATOR?

TypeDef      ::= "type" Ident "=" Ty TERMINATOR?
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
ThrowStmt    ::= "throw" Expr TERMINATOR
```

Expressions:
```
Expr         ::= TernaryExpr
TernaryExpr  ::= PipelineExpr ("?" Expr ":" Expr)?
PipelineExpr ::= OrExpr ("<<" OrExpr)*   // reserved; no current semantics
               | OrExpr ("|>" OrExpr)*   // reserved; no current semantics
               | OrExpr (" >> " PipeStage)*
PipeStage    ::= PostfixExpr | CallExpr

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
              | "[" Expr "]"
              | "(" Args? ")"
              | "->"

CallExpr     ::= PostfixExpr  // for pipeline staging clarity

Args         ::= Expr ("," Expr)*

PrimaryExpr  ::= Literal
              | Ident
              | "(" Expr ")"
              | TupleExpr
              | ArrayLiteral
              | MapLiteral
              | MatchExpr
              | TryCatchExpr
              | LambdaExpr

TupleExpr    ::= "(" Expr ("," Expr)+ ")"
ArrayLiteral ::= "[" (Expr ("," Expr)*)? "]"
MapLiteral   ::= "{" (MapEntry ("," MapEntry)*)? "}"
MapEntry     ::= Expr ":" Expr
MatchExpr    ::= "match" Expr "{" MatchArm+ "}"
MatchArm     ::= Pattern "=>" Expr TERMINATOR?
Pattern      ::= Ident | Literal | "(" Pattern ("," Pattern)+ ")"
TryCatchExpr ::= "try" Expr "catch" CatchExprArm
CatchExprArm ::= Ident "(" Ident ")" Block
               | Ident Block
               | Block
LambdaExpr   ::= "|" LambdaParams? "|" "=>" (Expr | Block)
LambdaParams ::= LambdaParam ("," LambdaParam)*

LambdaParam  ::= Capture? Ident (":" Ty)?
Capture      ::= "copy"
Literal      ::= IntLiteral | FloatLiteral | StringLiteral | BoolLiteral
```

### Notes

- Tuple types require at least two elements; `(T)` is just `T`.
- Pipelines use `>>` and are left-associative; `<<`, `|>`, `<|` are reserved tokens without current semantics.
- Zero-argument callables use `Void` as their argument type and are called with `f.call()`.
- This grammar is a reference for parsers; semantic rules (ownership, moves, errors) are defined in `drift-lang-spec.md`.
