" driftdark.vim
" Drift dark theme
" Background based on classic darkblue: #000040

hi clear
if exists("syntax_on")
	syntax reset
endif

let g:colors_name = "driftdark"
set background=dark

if has("termguicolors")
	set termguicolors
endif

" ------------------------------------------------------------------
" Base UI
" ------------------------------------------------------------------
hi Normal         guifg=#c0c0c0 guibg=#000040 gui=NONE       ctermfg=7  ctermbg=4
hi CursorLine     guifg=NONE    guibg=#101060 gui=NONE       cterm=NONE
hi CursorColumn   guifg=NONE    guibg=#101060 gui=NONE       cterm=NONE
hi ColorColumn    guifg=NONE    guibg=#101060 gui=NONE       cterm=NONE

hi LineNr         guifg=#6f78a8 guibg=#000040 gui=NONE       ctermfg=8  ctermbg=4
hi CursorLineNr   guifg=#ffd75f guibg=#101060 gui=bold       ctermfg=11 ctermbg=4
hi SignColumn     guifg=#b8b8c8 guibg=#000040 gui=NONE       ctermfg=7  ctermbg=4

hi VertSplit      guifg=#505a90 guibg=#000040 gui=NONE       ctermfg=8  ctermbg=4
hi WinSeparator   guifg=#505a90 guibg=#000040 gui=NONE       ctermfg=8  ctermbg=4

hi StatusLine     guifg=#d0d0d8 guibg=#1a1a70 gui=bold       ctermfg=7  ctermbg=4
hi StatusLineNC   guifg=#9098b8 guibg=#101050 gui=NONE       ctermfg=8  ctermbg=4

hi TabLine        guifg=#aeb6d0 guibg=#101050 gui=NONE       ctermfg=7  ctermbg=4
hi TabLineFill    guifg=#aeb6d0 guibg=#101050 gui=NONE       ctermfg=7  ctermbg=4
hi TabLineSel     guifg=#d8d8e0 guibg=#202070 gui=bold       ctermfg=15 ctermbg=4

hi Visual         guifg=NONE    guibg=#404a90 gui=NONE       cterm=reverse
hi Search         guifg=#000040 guibg=#ffe082 gui=NONE       ctermfg=4  ctermbg=11
hi IncSearch      guifg=#000040 guibg=#ffbe78 gui=bold       ctermfg=4  ctermbg=3
hi MatchParen     guifg=#000040 guibg=#a0c8ff gui=bold       ctermfg=4  ctermbg=14

hi Pmenu          guifg=#c8c8d0 guibg=#101060 gui=NONE       ctermfg=7  ctermbg=4
hi PmenuSel       guifg=#000040 guibg=#a0c8ff gui=bold       ctermfg=4  ctermbg=14
hi PmenuSbar      guifg=NONE    guibg=#202070 gui=NONE       cterm=NONE
hi PmenuThumb     guifg=NONE    guibg=#7078b0 gui=NONE       cterm=NONE

" ------------------------------------------------------------------
" Messages / diagnostics
" ------------------------------------------------------------------
hi ErrorMsg       guifg=#ffffff guibg=#800000 gui=bold       ctermfg=15 ctermbg=1
hi WarningMsg     guifg=#ffbe78 guibg=#000040 gui=bold       ctermfg=11 ctermbg=4
hi ModeMsg        guifg=#8fd7ff guibg=#000040 gui=bold       ctermfg=14 ctermbg=4
hi MoreMsg        guifg=#8fd7ff guibg=#000040 gui=bold       ctermfg=14 ctermbg=4
hi Question       guifg=#8fd7ff guibg=#000040 gui=bold       ctermfg=14 ctermbg=4

hi Error          guifg=#ff8f8f guibg=#000040 gui=bold       ctermfg=9  ctermbg=4
hi Todo           guifg=#000040 guibg=#ffd75f gui=bold       ctermfg=4  ctermbg=11

" ------------------------------------------------------------------
" Diff
" ------------------------------------------------------------------
hi DiffAdd        guifg=#d6ffd6 guibg=#204020 gui=NONE       ctermfg=10 ctermbg=2
hi DiffChange     guifg=#f0f0d0 guibg=#505000 gui=NONE       ctermfg=15 ctermbg=3
hi DiffDelete     guifg=#ffd6d6 guibg=#502020 gui=NONE       ctermfg=9  ctermbg=1
hi DiffText       guifg=#ffffff guibg=#707000 gui=bold       ctermfg=15 ctermbg=3

" ------------------------------------------------------------------
" Generic syntax groups
" ------------------------------------------------------------------
hi Comment        guifg=#7f8fbf guibg=NONE    gui=italic     ctermfg=8
hi Constant       guifg=#ffb870 guibg=NONE    gui=NONE       ctermfg=11
hi String         guifg=#ff9f8f guibg=NONE    gui=NONE       ctermfg=10
hi Character      guifg=#ff9f8f guibg=NONE    gui=NONE       ctermfg=10
hi Number         guifg=#ffbe78 guibg=NONE    gui=NONE       ctermfg=11
hi Boolean        guifg=#ffbe78 guibg=NONE    gui=bold       ctermfg=11
hi Float          guifg=#ffbe78 guibg=NONE    gui=NONE       ctermfg=11

hi Identifier     guifg=#c0c0c0 guibg=NONE    gui=NONE       ctermfg=7
hi Function       guifg=#7fdfff guibg=NONE    gui=NONE       ctermfg=14

hi Statement      guifg=#ffd75f guibg=NONE    gui=bold       ctermfg=11
hi Conditional    guifg=#ffbe78 guibg=NONE    gui=bold       ctermfg=11
hi Repeat         guifg=#ffbe78 guibg=NONE    gui=bold       ctermfg=11
hi Label          guifg=#ffd75f guibg=NONE    gui=NONE       ctermfg=11
hi Operator       guifg=#d8d8a0 guibg=NONE    gui=NONE       ctermfg=11
hi Keyword        guifg=#ffd75f guibg=NONE    gui=bold       ctermfg=11
hi Exception      guifg=#ffbe78 guibg=NONE    gui=bold       ctermfg=11

hi PreProc        guifg=#ff8fd8 guibg=NONE    gui=NONE       ctermfg=13
hi Include        guifg=#ffd75f guibg=NONE    gui=bold       ctermfg=11
hi Define         guifg=#ffd75f guibg=NONE    gui=bold       ctermfg=11
hi Macro          guifg=#ff8fd8 guibg=NONE    gui=NONE       ctermfg=13
hi PreCondit      guifg=#ff8fd8 guibg=NONE    gui=NONE       ctermfg=13

hi Type           guifg=#66ff66 guibg=NONE    gui=bold       ctermfg=10
hi StorageClass   guifg=#66ff66 guibg=NONE    gui=bold       ctermfg=10
hi Structure      guifg=#66ff66 guibg=NONE    gui=bold       ctermfg=10
hi Typedef        guifg=#66ff66 guibg=NONE    gui=bold       ctermfg=10

hi Special        guifg=#ffbe78 guibg=NONE    gui=NONE       ctermfg=11
hi SpecialChar    guifg=#ffbe78 guibg=NONE    gui=NONE       ctermfg=11
hi Delimiter      guifg=#b8b8c8 guibg=NONE    gui=NONE       ctermfg=7
hi SpecialComment guifg=#90a8d8 guibg=NONE    gui=italic     ctermfg=12
hi Tag            guifg=#b0c4ff guibg=NONE    gui=NONE       ctermfg=12

hi Underlined     guifg=#8fd7ff guibg=NONE    gui=underline  cterm=underline
hi Ignore         guifg=#606890 guibg=NONE    gui=NONE       ctermfg=8

" ------------------------------------------------------------------
" Drift-specific groups
" ------------------------------------------------------------------
hi DriftDeclKeyword     guifg=#ffd75f guibg=NONE gui=bold
hi DriftControlKeyword  guifg=#ffbe78 guibg=NONE gui=bold
hi DriftModifierKeyword guifg=#ff8fd8 guibg=NONE gui=NONE

hi DriftType            guifg=#66ff66 guibg=NONE gui=bold
hi DriftBuiltinType     guifg=#66ff66 guibg=NONE gui=bold
hi DriftFunction        guifg=#7fdfff guibg=NONE gui=NONE
hi DriftBuiltinFunc     guifg=#7fdfff guibg=NONE gui=NONE

hi DriftNumber          guifg=#ffbe78 guibg=NONE gui=NONE
hi DriftString          guifg=#ff9f8f guibg=NONE gui=NONE
hi DriftChar            guifg=#ff9f8f guibg=NONE gui=NONE
hi DriftEscape          guifg=#ffbe78 guibg=NONE gui=NONE

hi DriftComment         guifg=#7f8fbf guibg=NONE gui=italic
hi DriftLineComment     guifg=#7f8fbf guibg=NONE gui=italic
hi DriftBlockComment    guifg=#7f8fbf guibg=NONE gui=italic
hi DriftDocComment      guifg=#90a8d8 guibg=NONE gui=italic
hi DriftTodo            guifg=#000040 guibg=#ffd75f gui=bold

hi DriftBoolean         guifg=#ffbe78 guibg=NONE gui=bold
hi DriftOperator        guifg=#d8d8a0 guibg=NONE gui=NONE
hi DriftDelimiter       guifg=#b8b8c8 guibg=NONE gui=NONE

hi DriftNamespace       guifg=#9db4e0 guibg=NONE gui=NONE
hi DriftModule          guifg=#b0c4ff guibg=NONE gui=NONE
hi DriftEnumVariant     guifg=#66ff66 guibg=NONE gui=bold
