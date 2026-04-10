" drift.vim

if exists("b:current_syntax")
	finish
endif

syntax case match

" ------------------------------------------------------------------
" Strings / chars
" ------------------------------------------------------------------
syntax region DriftString start=+"+ skip=+\\\\\|\\"+ end=+"+
syntax region DriftChar   start=+'+ skip=+\\\\\|\\'+ end=+'+

" ------------------------------------------------------------------
" Declaration-leading keywords with nextgroup help
" ------------------------------------------------------------------
syntax match DriftFnKeyword        /\<fn\>/                      nextgroup=DriftFunctionDecl skipwhite
syntax match DriftExceptionKeyword /\<exception\>/               nextgroup=DriftExceptionName skipwhite
syntax match DriftTypeDeclKeyword  /\<\(struct\|enum\|trait\)\>/ nextgroup=DriftTypeDeclName skipwhite

" ------------------------------------------------------------------
" 3 keyword groups
" ------------------------------------------------------------------
syntax keyword DriftDeclKeyword     module import export use fn struct enum trait implement exception var val
syntax keyword DriftControlKeyword  if else match while for break continue return try catch
syntax keyword DriftModifierKeyword pub mut as nothrow

" ------------------------------------------------------------------
" Built-in types / booleans
" ------------------------------------------------------------------
syntax keyword DriftBoolean true false
syntax keyword DriftType    void bool i8 i16 i32 i64 u8 u16 u32 u64 f32 f64 char str

" ------------------------------------------------------------------
" Declaration targets
" ------------------------------------------------------------------
syntax match DriftFunctionDecl  /\<[A-Za-z_][A-Za-z0-9_]*\>/ contained
syntax match DriftExceptionName /\<[A-Z][A-Za-z0-9_]*\>/     contained
syntax match DriftTypeDeclName  /\<[A-Z][A-Za-z0-9_]*\>/     contained

" ------------------------------------------------------------------
" Qualified names
" Do NOT use containedin=ALL here; that was the comment leak.
" ------------------------------------------------------------------
syntax match DriftNamespace /\<[a-z_][a-z0-9_]*\ze\./
syntax match DriftModule /\<[a-z_][a-z0-9_]*\(\.[a-z_][a-z0-9_]*\)\+\>\%(\s*(\)\@!/

" ------------------------------------------------------------------
" Type-ish names
" Broad rule kept intentionally: capitalized identifiers are type-like
" ------------------------------------------------------------------
syntax match DriftTypeName /\<[A-Z][A-Za-z0-9_]*\>/
syntax match DriftEnumVariant /::\zs[A-Z][A-Za-z0-9_]*/

" ------------------------------------------------------------------
" Function / method calls
" ------------------------------------------------------------------
syntax match DriftFunctionCall /\<[a-z_][A-Za-z0-9_]*\ze\s*(/

" ------------------------------------------------------------------
" Numbers / operators / punctuation
" Slash deliberately omitted so // comments do not fight the operator rule.
" ------------------------------------------------------------------
syntax match DriftNumber    /\v<\d+(_\d+)*(\.\d+(_\d+)*)?>/
syntax match DriftOperator  /\v[-+*%!=<>:&|?]+/
syntax match DriftDelimiter /[(){}\[\],.;]/

" ------------------------------------------------------------------
" Comments
" ------------------------------------------------------------------
syntax keyword DriftTodo TODO FIXME XXX NOTE contained
syntax match   DriftLineComment +//.*$+ contains=DriftTodo,@Spell
syntax region  DriftBlockComment start=+/\*+ end=+\*/+ keepend contains=DriftTodo,@Spell

" ------------------------------------------------------------------
" Highlight links
" ------------------------------------------------------------------
hi def link DriftFnKeyword         Statement
hi def link DriftExceptionKeyword  Statement
hi def link DriftTypeDeclKeyword   Statement

hi def link DriftDeclKeyword       Statement
hi def link DriftControlKeyword    Conditional
hi def link DriftModifierKeyword   PreProc

hi def link DriftBoolean           Boolean
hi def link DriftType              Type

hi def link DriftFunctionDecl      Function
hi def link DriftFunctionCall      Function

hi def link DriftExceptionName     Type
hi def link DriftTypeDeclName      Type
hi def link DriftTypeName          Type
hi def link DriftEnumVariant       Type

hi def link DriftNamespace         DriftNamespace
hi def link DriftModule            DriftModule

hi def link DriftString            String
hi def link DriftChar              Character
hi def link DriftNumber            Number
hi def link DriftOperator          Operator
hi def link DriftDelimiter         Delimiter

hi def link DriftLineComment       Comment
hi def link DriftBlockComment      Comment
hi def link DriftTodo              Todo

let b:current_syntax = "drift"
