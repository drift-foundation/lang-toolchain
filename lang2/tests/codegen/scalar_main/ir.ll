%DriftError = type { i64, ptr, ptr, ptr }
%FnResult_Int_Error = type { i1, i64, %DriftError }

define i64 @drift_main() {
entry:
  ret i64 42
}

define i32 @main() {
entry:
  %ret = call i64 @drift_main()
  %trunc = trunc i64 %ret to i32
  ret i32 %trunc
}
