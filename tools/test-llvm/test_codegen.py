#!/usr/bin/env python3
import sys
from llvmlite import ir, binding as llvm

# --- argument check ---
if len(sys.argv) != 2:
    print("usage: python test_codegen.py <output.o>")
    sys.exit(1)

out_path = sys.argv[1]

# --- LLVM init ---
llvm.initialize()
llvm.initialize_native_target()
llvm.initialize_native_asmprinter()

target = llvm.Target.from_default_triple()
tm = target.create_target_machine()

# --- IR: int add(int a, int b) { return a + b; } ---
module = ir.Module(name="add_module")
module.triple = llvm.get_default_triple()
module.data_layout = tm.target_data

int32 = ir.IntType(32)
fn_type = ir.FunctionType(int32, [int32, int32])
fn = ir.Function(module, fn_type, name="add_i32")

block = fn.append_basic_block(name="entry")
builder = ir.IRBuilder(block)
a, b = fn.args
res = builder.add(a, b, name="res")
builder.ret(res)

print("=== LLVM IR ===")
print(module)

# --- IR → object ---
llvm_module = llvm.parse_assembly(str(module))
llvm_module.verify()

obj = tm.emit_object(llvm_module)

with open(out_path, "wb") as f:
    f.write(obj)

print(f"Wrote object file to: {out_path}")
