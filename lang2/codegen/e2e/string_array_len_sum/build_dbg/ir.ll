%DriftError = type { i64, ptr, ptr, ptr }
%FnResult_Int_Error = type { i1, i64, %DriftError }
%DriftString = type { i64, i8* }

@.str0 = private unnamed_addr constant [2 x i8] c"a\00"
@.str1 = private unnamed_addr constant [3 x i8] c"bb\00"
@.str2 = private unnamed_addr constant [4 x i8] c"ccc\00"

declare ptr @drift_alloc_array(i64, i64, i64, i64)
declare void @drift_bounds_check_fail(i64, i64)

define i64 @main() {
entry:
  %strptr1 = getelementptr inbounds [2 x i8], [2 x i8]* @.str0, i32 0, i32 0
  %str02 = insertvalue %DriftString undef, i64 1, 0
  %t1 = insertvalue %DriftString %str02, i8* %strptr1, 1
  %strptr3 = getelementptr inbounds [3 x i8], [3 x i8]* @.str1, i32 0, i32 0
  %str04 = insertvalue %DriftString undef, i64 2, 0
  %t2 = insertvalue %DriftString %str04, i8* %strptr3, 1
  %strptr5 = getelementptr inbounds [4 x i8], [4 x i8]* @.str2, i32 0, i32 0
  %str06 = insertvalue %DriftString undef, i64 3, 0
  %t3 = insertvalue %DriftString %str06, i8* %strptr5, 1
  %arr7 = call ptr @drift_alloc_array(i64 16, i64 8, i64 3, i64 3)
  %data8 = bitcast ptr %arr7 to %DriftString*
  %eltptr9 = getelementptr inbounds %DriftString, %DriftString* %data8, i64 0
  store %DriftString %t1, %DriftString* %eltptr9
  %eltptr10 = getelementptr inbounds %DriftString, %DriftString* %data8, i64 1
  store %DriftString %t2, %DriftString* %eltptr10
  %eltptr11 = getelementptr inbounds %DriftString, %DriftString* %data8, i64 2
  store %DriftString %t3, %DriftString* %eltptr11
  %arrh012 = insertvalue { i64, i64, %DriftString* } undef, i64 3, 0
  %arrh113 = insertvalue { i64, i64, %DriftString* } %arrh012, i64 3, 1
  %t4 = insertvalue { i64, i64, %DriftString* } %arrh113, %DriftString* %data8, 2
  %t6 = add i64 0, 0
  %len14 = extractvalue { i64, i64, %DriftString* } %t4, 0
  %cap15 = extractvalue { i64, i64, %DriftString* } %t4, 1
  %data16 = extractvalue { i64, i64, %DriftString* } %t4, 2
  %negcmp17 = icmp slt i64 %t6, 0
  %oobcmp18 = icmp uge i64 %t6, %len14
  %oobor19 = or i1 %negcmp17, %oobcmp18
  br i1 %oobor19, label %array_oob21, label %array_ok20
array_oob21:
  call void @drift_bounds_check_fail(i64 %t6, i64 %len14)
  unreachable
array_ok20:
  %eltptr22 = getelementptr inbounds %DriftString, %DriftString* %data16, i64 %t6
  %t7 = load %DriftString, %DriftString* %eltptr22
  %t8 = extractvalue %DriftString %t7, 0
  %t10 = add i64 0, 1
  %len23 = extractvalue { i64, i64, %DriftString* } %t4, 0
  %cap24 = extractvalue { i64, i64, %DriftString* } %t4, 1
  %data25 = extractvalue { i64, i64, %DriftString* } %t4, 2
  %negcmp26 = icmp slt i64 %t10, 0
  %oobcmp27 = icmp uge i64 %t10, %len23
  %oobor28 = or i1 %negcmp26, %oobcmp27
  br i1 %oobor28, label %array_oob30, label %array_ok29
array_oob30:
  call void @drift_bounds_check_fail(i64 %t10, i64 %len23)
  unreachable
array_ok29:
  %eltptr31 = getelementptr inbounds %DriftString, %DriftString* %data25, i64 %t10
  %t11 = load %DriftString, %DriftString* %eltptr31
  %t12 = extractvalue %DriftString %t11, 0
  %t13 = add i64 %t8, %t12
  %t15 = add i64 0, 2
  %len32 = extractvalue { i64, i64, %DriftString* } %t4, 0
  %cap33 = extractvalue { i64, i64, %DriftString* } %t4, 1
  %data34 = extractvalue { i64, i64, %DriftString* } %t4, 2
  %negcmp35 = icmp slt i64 %t15, 0
  %oobcmp36 = icmp uge i64 %t15, %len32
  %oobor37 = or i1 %negcmp35, %oobcmp36
  br i1 %oobor37, label %array_oob39, label %array_ok38
array_oob39:
  call void @drift_bounds_check_fail(i64 %t15, i64 %len32)
  unreachable
array_ok38:
  %eltptr40 = getelementptr inbounds %DriftString, %DriftString* %data34, i64 %t15
  %t16 = load %DriftString, %DriftString* %eltptr40
  %t17 = extractvalue %DriftString %t16, 0
  %t18 = add i64 %t13, %t17
  ret i64 %t18
}
