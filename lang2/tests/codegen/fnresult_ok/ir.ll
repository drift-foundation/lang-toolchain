%DriftError = type { i64, ptr, ptr, ptr }
%FnResult_Int_Error = type { i1, i64, %DriftError }

define %FnResult_Int_Error @callee() {
entry:
  %c0 = add i64 0, 1
  %ok0 = insertvalue %FnResult_Int_Error undef, i1 0, 0
  %ok1 = insertvalue %FnResult_Int_Error %ok0, i64 %c0, 1
  %cres = insertvalue %FnResult_Int_Error %ok1, %DriftError zeroinitializer, 2
  ret %FnResult_Int_Error %cres
}

define i64 @drift_main() {
entry:
  %call = call %FnResult_Int_Error @callee()
  %m0 = extractvalue %FnResult_Int_Error %call, 1
  ret i64 %m0
}

define i32 @main() {
entry:
  %ret = call i64 @drift_main()
  %trunc = trunc i64 %ret to i32
  ret i32 %trunc
}
