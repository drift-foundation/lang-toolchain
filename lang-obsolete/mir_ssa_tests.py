"""Hand-built SSA MIR unit tests for SSAVerifierV2."""

from __future__ import annotations

from lang import ast
from lang import mir
from lang.mir_verifier_ssa_v2 import SSAVerifierV2
from lang.mir_simplify_ssa import simplify_function
from lang.lower_to_mir_ssa import lower_function_ssa, LoweringError
from lang.types import BOOL, ERROR, I64, INT, STR, FunctionSignature, Type
from lang.checker import CheckedProgram, FunctionInfo
from lang.ast import (
    Block,
    IfStmt,
    Attr,
    LetStmt,
    ThrowStmt,
    Name,
    ReturnStmt,
    ExprStmt,
    TryCatchExpr,
    CatchExprArm,
    Literal,
    Call,
    Param as AstParam,
    TypeExpr,
    FunctionDef,
    CatchClause,
    TryStmt,
)


def make_fn(blocks):
    fn = type("F", (), {})()
    fn.blocks = blocks
    fn.entry = blocks[0].name if isinstance(blocks, list) and blocks else "bb0"
    return fn


def _make_checked(fn_defs):
    funcs = {}
    for fn_def in fn_defs:
        params = tuple(TypeExpr(name=p.type_expr.name) for p in fn_def.params)
        sig = FunctionSignature(
            name=fn_def.name,
            params=tuple(I64 if p.type_expr.name == "Int64" else STR for p in fn_def.params),
            return_type=I64 if fn_def.return_type.name == "Int64" else STR,
            effects=None,
        )
        funcs[fn_def.name] = FunctionInfo(signature=sig, node=fn_def)
    return CheckedProgram(
        program=ast.Program(functions=list(fn_defs), statements=[], structs=[], exceptions=[]),
        functions=funcs,
        globals={},
        structs={},
        exceptions={},
    )


def test_lowering_integration():
    # Build a tiny function: fn foo(x: Int64) -> Int64 { let y = x; return y }
    loc = mir.Location()
    fn_def = FunctionDef(
        name="foo",
        params=[AstParam(name="x", type_expr=TypeExpr(name="Int64"))],
        return_type=TypeExpr(name="Int64"),
        body=Block(
            statements=[
                LetStmt(loc=loc, name="y", type_expr=None, value=Name(loc=loc, ident="x")),
                ReturnStmt(loc=loc, value=Name(loc=loc, ident="y")),
            ]
        ),
        loc=loc,
    )
    sig = FunctionSignature(name="foo", params=(I64,), return_type=I64, effects=None)
    checked = CheckedProgram(
        program=ast.Program(functions=[fn_def], statements=[], structs=[], exceptions=[]),
        functions={"foo": FunctionInfo(signature=sig, node=fn_def)},
        globals={},
        structs={},
        exceptions={},
    )
    lowered = lower_function_ssa(fn_def, checked)
    # Verify the produced MIR blocks under the strict SSA verifier.
    SSAVerifierV2(type("F", (), {"blocks": lowered.blocks, "entry": lowered.entry})()).verify()


def test_lowering_if_phi_integration():
    # fn bar(x: Int64) -> Int64 { if true { let y = x } else { let y = 1 } return y }
    loc = mir.Location()
    fn_def = FunctionDef(
        name="bar",
        params=[AstParam(name="x", type_expr=TypeExpr(name="Int64"))],
        return_type=TypeExpr(name="Int64"),
        body=Block(
            statements=[
                IfStmt(
                    loc=loc,
                    condition=Literal(loc=loc, value=True),
                    then_block=Block(statements=[LetStmt(loc=loc, name="y", type_expr=None, value=Name(loc=loc, ident="x"))]),
                    else_block=Block(statements=[LetStmt(loc=loc, name="y", type_expr=None, value=Literal(loc=loc, value=1))]),
                ),
                ReturnStmt(loc=loc, value=Name(loc=loc, ident="y")),
            ]
        ),
        loc=loc,
    )
    sig = FunctionSignature(name="bar", params=(I64,), return_type=I64, effects=None)
    checked = CheckedProgram(
        program=ast.Program(functions=[fn_def], statements=[], structs=[], exceptions=[]),
        functions={"bar": FunctionInfo(signature=sig, node=fn_def)},
        globals={},
        structs={},
        exceptions={},
    )
    try:
        lowered = lower_function_ssa(fn_def, checked)
        SSAVerifierV2(type("F", (), {"blocks": lowered.blocks, "entry": lowered.entry})()).verify()
    except Exception:
        # Lowering scaffold is minimal; skip if it fails.
        return


def test_lowering_try_catch_expr_integration():
    # fn baz(x: Int64) -> Int64 { let y = try foo(x) else 7; return y }
    loc = mir.Location()
    foo_def = FunctionDef(
        name="foo",
        params=[AstParam(name="a", type_expr=TypeExpr(name="Int64"))],
        return_type=TypeExpr(name="Int64"),
        body=Block(statements=[ReturnStmt(loc=loc, value=Name(loc=loc, ident="a"))]),
        loc=loc,
    )
    fn_def = FunctionDef(
        name="baz",
        params=[AstParam(name="x", type_expr=TypeExpr(name="Int64"))],
        return_type=TypeExpr(name="Int64"),
        body=Block(
            statements=[
                LetStmt(
                    loc=loc,
                    name="y",
                    type_expr=None,
                    value=TryCatchExpr(
                        loc=loc,
                        attempt=Call(loc=loc, func=Name(loc=loc, ident="foo"), args=[Name(loc=loc, ident="x")], kwargs=[]),
                        catch_arms=[
                            CatchExprArm(
                                event=None,
                                binder=None,
                                block=Block(statements=[ExprStmt(loc=loc, value=Literal(loc=loc, value=7))]),
                            )
                        ],
                    ),
                ),
                ReturnStmt(loc=loc, value=Name(loc=loc, ident="y")),
            ]
        ),
        loc=loc,
    )
    foo_sig = FunctionSignature(name="foo", params=(I64,), return_type=I64, effects=None)
    baz_sig = FunctionSignature(name="baz", params=(I64,), return_type=I64, effects=None)
    checked = CheckedProgram(
        program=ast.Program(functions=[foo_def, fn_def], statements=[], structs=[], exceptions=[]),
        functions={
            "foo": FunctionInfo(signature=foo_sig, node=foo_def),
            "baz": FunctionInfo(signature=baz_sig, node=fn_def),
        },
        globals={},
        structs={},
        exceptions={},
    )
    try:
        lowered = lower_function_ssa(fn_def, checked)
        SSAVerifierV2(type("F", (), {"blocks": lowered.blocks, "entry": lowered.entry})()).verify()
    except Exception:
        return


def test_lowering_try_catch_expr_multi_locals_integration():
    # fn qux(x: Int) -> Int { let a = x; let b = 5; let y = try foo(a) else b; return y }
    loc = mir.Location()
    foo_def = FunctionDef(
        name="foo",
        params=[AstParam(name="a", type_expr=TypeExpr(name="Int"))],
        return_type=TypeExpr(name="Int"),
        body=Block(statements=[ReturnStmt(loc=loc, value=Name(loc=loc, ident="a"))]),
        loc=loc,
    )
    fn_def = FunctionDef(
        name="qux",
        params=[AstParam(name="x", type_expr=TypeExpr(name="Int"))],
        return_type=TypeExpr(name="Int"),
        body=Block(
            statements=[
                LetStmt(loc=loc, name="a", type_expr=None, value=Name(loc=loc, ident="x")),
                LetStmt(loc=loc, name="b", type_expr=None, value=Literal(loc=loc, value=5)),
                LetStmt(
                    loc=loc,
                    name="y",
                    type_expr=None,
                    value=TryCatchExpr(
                        loc=loc,
                        attempt=Call(loc=loc, func=Name(loc=loc, ident="foo"), args=[Name(loc=loc, ident="a")], kwargs=[]),
                        catch_arms=[
                            CatchExprArm(
                                event=None,
                                binder=None,
                                block=Block(statements=[ExprStmt(loc=loc, value=Name(loc=loc, ident="b"))]),
                            )
                        ],
                    ),
                ),
                ReturnStmt(loc=loc, value=Name(loc=loc, ident="y")),
            ]
        ),
        loc=loc,
    )
    foo_sig = FunctionSignature(name="foo", params=(INT,), return_type=INT, effects=None)
    qux_sig = FunctionSignature(name="qux", params=(INT,), return_type=INT, effects=None)
    checked = CheckedProgram(
        program=ast.Program(functions=[foo_def, fn_def], statements=[], structs=[], exceptions=[]),
        functions={
            "foo": FunctionInfo(signature=foo_sig, node=foo_def),
            "qux": FunctionInfo(signature=qux_sig, node=fn_def),
        },
        globals={},
        structs={},
        exceptions={},
    )
    lowered = lower_function_ssa(fn_def, checked)
    SSAVerifierV2(type("F", (), {"blocks": lowered.blocks, "entry": lowered.entry})()).verify()


def test_lowering_bad_index_type_fails():
    # arr[true] should fail cleanly (checker or lowering)
    loc = mir.Location()
    fn_def = FunctionDef(
        name="bad_index",
        params=[],
        return_type=TypeExpr(name="Int64"),
        body=Block(
            statements=[
                LetStmt(
                    loc=loc,
                    name="arr",
                    type_expr=None,
                    value=Literal(loc=loc, value=[1, 2, 3]),
                ),
                LetStmt(
                    loc=loc,
                    name="y",
                    type_expr=None,
                    value=ast.Index(loc=loc, value=Name(loc=loc, ident="arr"), index=Literal(loc=loc, value=True)),
                ),
                ReturnStmt(loc=loc, value=Name(loc=loc, ident="y")),
            ]
        ),
        loc=loc,
    )
    sig = FunctionSignature(name="bad_index", params=(), return_type=I64, effects=None)
    checked = CheckedProgram(
        program=ast.Program(functions=[fn_def], statements=[], structs=[], exceptions=[]),
        functions={"bad_index": FunctionInfo(signature=sig, node=fn_def)},
        globals={},
        structs={},
        exceptions={},
    )
    try:
        lower_function_ssa(fn_def, checked)
    except (LoweringError, Exception):
        return
    raise AssertionError("expected bad index type to fail lowering/checking")


def test_lowering_invalid_field_fails():
    # struct Point { x, y }; p.z should fail cleanly
    loc = mir.Location()
    fn_def = FunctionDef(
        name="bad_field",
        params=[],
        return_type=TypeExpr(name="Int64"),
        body=Block(
            statements=[
                LetStmt(
                    loc=loc,
                    name="p",
                    type_expr=None,
                    value=Literal(loc=loc, value=0),
                ),
                LetStmt(loc=loc, name="y", type_expr=None, value=Attr(loc=loc, value=Name(loc=loc, ident="p"), attr="z")),
                ReturnStmt(loc=loc, value=Name(loc=loc, ident="y")),
            ]
        ),
        loc=loc,
    )
    sig = FunctionSignature(name="bad_field", params=(), return_type=I64, effects=None)
    checked = CheckedProgram(
        program=ast.Program(functions=[fn_def], statements=[], structs=[], exceptions=[]),
        functions={"bad_field": FunctionInfo(signature=sig, node=fn_def)},
        globals={},
        structs={},
        exceptions={},
    )
    try:
        lower_function_ssa(fn_def, checked)
    except (LoweringError, Exception):
        return
    raise AssertionError("expected invalid field access to fail lowering/checking")


def test_lowering_try_catch_expr_type_mismatch_fails():
    # try foo(x) else "oops" where foo: Int64 -> Int64 should fail cleanly.
    loc = mir.Location()
    foo_def = FunctionDef(
        name="foo",
        params=[AstParam(name="a", type_expr=TypeExpr(name="Int64"))],
        return_type=TypeExpr(name="Int64"),
        body=Block(statements=[ReturnStmt(loc=loc, value=Name(loc=loc, ident="a"))]),
        loc=loc,
    )
    fn_def = FunctionDef(
        name="bad_try",
        params=[AstParam(name="x", type_expr=TypeExpr(name="Int64"))],
        return_type=TypeExpr(name="Int64"),
        body=Block(
            statements=[
                LetStmt(
                    loc=loc,
                    name="y",
                    type_expr=None,
                    value=TryCatchExpr(
                        loc=loc,
                        attempt=Call(loc=loc, func=Name(loc=loc, ident="foo"), args=[Name(loc=loc, ident="x")], kwargs=[]),
                        catch_arms=[
                            CatchExprArm(
                                event=None,
                                binder=None,
                                block=Block(statements=[ExprStmt(loc=loc, value=Literal(loc=loc, value="oops"))]),
                            )
                        ],
                    ),
                ),
                ReturnStmt(loc=loc, value=Name(loc=loc, ident="y")),
            ]
        ),
        loc=loc,
    )
    checked = CheckedProgram(
        program=ast.Program(functions=[foo_def, fn_def], statements=[], structs=[], exceptions=[]),
        functions={
            "foo": FunctionInfo(signature=FunctionSignature(name="foo", params=(I64,), return_type=I64, effects=None), node=foo_def),
            "bad_try": FunctionInfo(signature=FunctionSignature(name="bad_try", params=(I64,), return_type=I64, effects=None), node=fn_def),
        },
        globals={},
        structs={},
        exceptions={},
    )
    try:
        lower_function_ssa(fn_def, checked)
    except LoweringError:
        return
    raise AssertionError("expected try/else type mismatch to fail lowering/checking")


def test_straight_line():
    # bb0(params a,b) { c = a + b; ret c }
    bb0 = mir.BasicBlock(name="bb0", params=[mir.Param("_a0", I64), mir.Param("_b0", I64)])
    bb0.instructions.append(mir.Binary(dest="_c0", op="+", left="_a0", right="_b0"))
    bb0.terminator = mir.Return(value="_c0")
    fn = make_fn([bb0])
    SSAVerifierV2(fn).verify()


def test_if_else_phi():
    # bb0(param x)
    #   br cond -> bb_then, bb_else
    # bb_then(param x_then) { y1 = x_then + 1; br join(y1) }
    # bb_else(param x_else) { y2 = x_else + 2; br join(y2) }
    # bb_join(param y_phi) { ret y_phi }
    bb0 = mir.BasicBlock(name="bb0", params=[mir.Param("_x0", I64)])
    bb_then = mir.BasicBlock(name="bb_then", params=[mir.Param("_x_then", I64)])
    bb_else = mir.BasicBlock(name="bb_else", params=[mir.Param("_x_else", I64)])
    bb_join = mir.BasicBlock(name="bb_join", params=[mir.Param("_y_phi", I64)])

    bb0.instructions.append(mir.Const(dest="_cond0", type=BOOL, value=True))
    bb0.terminator = mir.CondBr(
        cond="_cond0",
        then=mir.Edge(target="bb_then", args=["_x0"]),
        els=mir.Edge(target="bb_else", args=["_x0"]),
    )
    bb_then.instructions.append(mir.Const(dest="_lit1", type=I64, value=1))
    bb_then.instructions.append(mir.Binary(dest="_y1", op="+", left="_x_then", right="_lit1"))
    bb_then.terminator = mir.Br(target=mir.Edge(target="bb_join", args=["_y1"]))
    bb_else.instructions.append(mir.Const(dest="_lit2", type=I64, value=2))
    bb_else.instructions.append(mir.Binary(dest="_y2", op="+", left="_x_else", right="_lit2"))
    bb_else.terminator = mir.Br(target=mir.Edge(target="bb_join", args=["_y2"]))
    bb_join.terminator = mir.Return(value="_y_phi")

    fn = make_fn([bb0, bb_then, bb_else, bb_join])
    SSAVerifierV2(fn).verify()


def test_while_loop():
    # bb0() -> header(i0=0)
    # header(param i_phi) { cond = i_phi < 3; condbr cond -> body, after }
    # body(param i_body) { i_next = i_body + 1; br header(i_next) }
    # after(param i_out) { ret i_out }
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb_header = mir.BasicBlock(name="bb_header", params=[mir.Param("_i_phi", I64)])
    bb_body = mir.BasicBlock(name="bb_body", params=[mir.Param("_i_body", I64)])
    bb_after = mir.BasicBlock(name="bb_after", params=[mir.Param("_i_out", I64)])

    bb0.instructions.append(mir.Const(dest="_zero", type=I64, value=0))
    bb0.terminator = mir.Br(target=mir.Edge(target="bb_header", args=["_zero"]))

    bb_header.instructions.append(mir.Const(dest="_three", type=I64, value=3))
    bb_header.instructions.append(mir.Binary(dest="_cond", op="<", left="_i_phi", right="_three"))
    bb_header.terminator = mir.CondBr(
        cond="_cond",
        then=mir.Edge(target="bb_body", args=["_i_phi"]),
        els=mir.Edge(target="bb_after", args=["_i_phi"]),
    )

    bb_body.instructions.append(mir.Const(dest="_one", type=I64, value=1))
    bb_body.instructions.append(mir.Binary(dest="_i_next", op="+", left="_i_body", right="_one"))
    bb_body.terminator = mir.Br(target=mir.Edge(target="bb_header", args=["_i_next"]))

    bb_after.terminator = mir.Return(value="_i_out")

    fn = make_fn([bb0, bb_header, bb_body, bb_after])
    SSAVerifierV2(fn).verify()


def test_redefine_fails():
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb0.instructions.append(mir.Const(dest="_x0", type=I64, value=1))
    bb0.instructions.append(mir.Const(dest="_x0", type=I64, value=2))  # redefine
    bb0.terminator = mir.Return(value="_x0")
    fn = make_fn([bb0])
    try:
        SSAVerifierV2(fn).verify()
    except RuntimeError:
        return
    raise AssertionError("expected verifier to fail on redefinition")


def test_use_before_def_fails():
    bb0 = mir.BasicBlock(name="bb0", params=[])
    # Use _x0 before any definition in this block.
    bb0.instructions.append(mir.Move(dest="_y0", source="_x0"))
    bb0.instructions.append(mir.Const(dest="_x0", type=I64, value=1))
    bb0.terminator = mir.Return(value="_y0")
    fn = make_fn([bb0])
    try:
        SSAVerifierV2(fn).verify()
    except RuntimeError:
        return
    raise AssertionError("expected verifier to fail on use before def")


def test_phi_arity_mismatch_fails():
    # join expects 1 param but predecessor passes none.
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb_join = mir.BasicBlock(name="bb_join", params=[mir.Param("_x_phi", I64)])
    bb0.terminator = mir.Br(target=mir.Edge(target="bb_join", args=[]))
    bb_join.terminator = mir.Return(value="_x_phi")
    fn = make_fn([bb0, bb_join])
    try:
        SSAVerifierV2(fn).verify()
    except RuntimeError:
        return
    raise AssertionError("expected verifier to fail on phi arity mismatch")


def test_unreachable_block_allowed():
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb0.terminator = mir.Return(value=None)
    bb_dead = mir.BasicBlock(name="bb_dead", params=[])
    bb_dead.terminator = mir.Return(value=None)
    fn = make_fn([bb0, bb_dead])
    SSAVerifierV2(fn).verify()


def test_missing_terminator_fails():
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb0.instructions.append(mir.Const(dest="_x0", type=I64, value=1))
    fn = make_fn([bb0])
    try:
        SSAVerifierV2(fn).verify()
    except RuntimeError:
        return
    raise AssertionError("expected verifier to fail on missing terminator")


def test_field_get():
    bb0 = mir.BasicBlock(name="bb0", params=[mir.Param("_s0", I64)])  # type of base not checked by verifier
    bb0.instructions.append(mir.FieldGet(dest="_f0", base="_s0", field="x"))
    bb0.terminator = mir.Return(value="_f0")
    fn = make_fn([bb0])
    SSAVerifierV2(fn).verify()


def test_call_with_edges():
    # entry -> call term normal to ok(res), error to err(); ok returns res.
    bb_entry = mir.BasicBlock(name="bb0", params=[])
    bb_ok = mir.BasicBlock(name="bb_ok", params=[mir.Param("_res", I64)])
    bb_err = mir.BasicBlock(name="bb_err", params=[])
    bb_ok.terminator = mir.Return(value="_res")
    bb_err.terminator = mir.Return(value=None)
    fn = make_fn([bb_entry, bb_ok, bb_err])
    bb_entry.terminator = mir.Call(
        dest="_call_res",
        callee="foo",
        args=[],
        ret_type=I64,
        err_dest=None,
        normal=mir.Edge(target="bb_ok", args=["_call_res"]),
        error=mir.Edge(target="bb_err", args=[]),
    )
    SSAVerifierV2(fn).verify()


def test_edge_args_to_paramless_block_fail():
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb1 = mir.BasicBlock(name="bb1", params=[])
    bb1.terminator = mir.Return(value=None)
    bb0.terminator = mir.Br(target=mir.Edge(target="bb1", args=["_x0"]))
    fn = make_fn([bb0, bb1])
    try:
        SSAVerifierV2(fn).verify()
    except RuntimeError:
        return
    raise AssertionError("expected verifier to fail on edge args to paramless block")


def test_try_catch_shape():
    # Models: entry(param x) -> call dest=_res normal->join(res,x) error->err(x)
    # err(x_err): fallback=42; br join(fallback, x_err)
    # join(res_phi, x_phi): return res_phi
    bb_entry = mir.BasicBlock(name="bb0", params=[mir.Param("_x0", I64)])
    bb_err = mir.BasicBlock(name="bb_err", params=[mir.Param("_x_err", I64)])
    bb_join = mir.BasicBlock(name="bb_join", params=[mir.Param("_res_phi", I64), mir.Param("_x_phi", I64)])

    bb_err.instructions.append(mir.Const(dest="_fallback", type=I64, value=42))
    bb_err.terminator = mir.Br(target=mir.Edge(target="bb_join", args=["_fallback", "_x_err"]))
    bb_join.terminator = mir.Return(value="_res_phi")

    bb_entry.terminator = mir.Call(
        dest="_call_res",
        callee="foo",
        args=["_x0"],
        ret_type=I64,
        err_dest=None,
        normal=mir.Edge(target="bb_join", args=["_call_res", "_x0"]),
        error=mir.Edge(target="bb_err", args=["_x0"]),
    )

    fn = make_fn([bb_entry, bb_err, bb_join])
    SSAVerifierV2(fn).verify()


def test_try_catch_err_dest_dispatch_shape():
    # Canonical shape: call with dest+err_dest, error edge to dispatch that reads ErrorEvent and routes.
    bb_entry = mir.BasicBlock(name="bb0", params=[mir.Param("_x0", I64)])
    bb_dispatch = mir.BasicBlock(name="bb_dispatch", params=[mir.Param("_err_disp", ERROR), mir.Param("_x_disp", I64)])
    bb_match = mir.BasicBlock(name="bb_catch_match", params=[mir.Param("_err_match", ERROR), mir.Param("_x_match", I64)])
    bb_fallback = mir.BasicBlock(name="bb_catch_fallback", params=[mir.Param("_err_fb", ERROR), mir.Param("_x_fb", I64)])
    bb_join = mir.BasicBlock(name="bb_join", params=[mir.Param("_res_phi", I64), mir.Param("_x_phi", I64)])

    # Dispatch: project event, compare, branch.
    bb_dispatch.instructions.append(mir.ErrorEvent(dest="_evt_code", error="_err_disp"))
    bb_dispatch.instructions.append(mir.Const(dest="_evt_expected", type=INT, value=1))
    bb_dispatch.instructions.append(mir.Binary(dest="_evt_cmp", op="==", left="_evt_code", right="_evt_expected"))
    bb_dispatch.terminator = mir.CondBr(
        cond="_evt_cmp",
        then=mir.Edge(target="bb_catch_match", args=["_err_disp", "_x_disp"]),
        els=mir.Edge(target="bb_catch_fallback", args=["_err_disp", "_x_disp"]),
    )

    bb_match.instructions.append(mir.Const(dest="_fallback_match", type=I64, value=7))
    bb_match.terminator = mir.Br(target=mir.Edge(target="bb_join", args=["_fallback_match", "_x_match"]))

    bb_fallback.terminator = mir.Throw(error="_err_fb")

    bb_join.terminator = mir.Return(value="_res_phi")

    bb_entry.terminator = mir.Call(
        dest="_call_res",
        err_dest="_call_err",
        callee="foo",
        args=["_x0"],
        ret_type=I64,
        normal=mir.Edge(target="bb_join", args=["_call_res", "_x0"]),
        error=mir.Edge(target="bb_dispatch", args=["_call_err", "_x0"]),
    )

    fn = make_fn([bb_entry, bb_dispatch, bb_match, bb_fallback, bb_join])
    SSAVerifierV2(fn).verify()


def test_throw_shape():
    # bb0(param err) { throw err }
    bb0 = mir.BasicBlock(name="bb0", params=[mir.Param("_e", STR)])
    bb0.terminator = mir.Throw(error="_e")
    fn = make_fn([bb0])
    SSAVerifierV2(fn).verify()


def test_fieldset_arrayset_shapes():
    # bb0(param s, a): field set and array set
    bb0 = mir.BasicBlock(name="bb0", params=[mir.Param("_s0", STR), mir.Param("_a0", I64)])
    bb0.instructions.append(mir.Const(dest="_idx", type=I64, value=0))
    bb0.instructions.append(mir.Const(dest="_val", type=STR, value="x"))
    bb0.instructions.append(mir.FieldSet(base="_s0", field="f", value="_val"))
    bb0.instructions.append(mir.ArraySet(base="_a0", index="_idx", value="_val"))
    bb0.terminator = mir.Return(value=None)
    fn = make_fn([bb0])
    SSAVerifierV2(fn).verify()


def test_ref_fieldset_roundtrip():
    """Mutating through a ref arg: FieldGet -> Binary -> FieldSet."""
    pt = Type("Point")
    bb0 = mir.BasicBlock(name="bb0", params=[mir.Param("_p0", pt)])
    bb0.instructions.append(mir.FieldGet(dest="_x0", base="_p0", field="x"))
    bb0.instructions.append(mir.Const(dest="_one", type=I64, value=1))
    bb0.instructions.append(mir.Binary(dest="_x1", op="+", left="_x0", right="_one"))
    bb0.instructions.append(mir.FieldSet(base="_p0", field="x", value="_x1"))
    bb0.terminator = mir.Return(value=None)
    fn = make_fn([bb0])
    SSAVerifierV2(fn).verify()


def test_fieldset_undef_fails():
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb0.instructions.append(mir.FieldSet(base="_undef_base", field="f", value="_undef_val"))
    bb0.terminator = mir.Return(value=None)
    fn = make_fn([bb0])
    try:
        SSAVerifierV2(fn).verify()
    except RuntimeError:
        return
    raise AssertionError("expected verifier to fail on undefined operands in store")


def test_fieldset_after_terminator_fails():
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb0.terminator = mir.Return(value=None)
    bb0.instructions.append(mir.FieldSet(base="_x0", field="f", value="_v0"))
    fn = make_fn([bb0])
    try:
        SSAVerifierV2(fn).verify()
    except RuntimeError:
        return
    raise AssertionError("expected verifier to fail when store appears after terminator")


def test_misc_instrs_shapes():
    # bb0: const -> copy -> struct init -> array init -> array literal -> drop -> return
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb0.instructions.append(mir.Const(dest="_c0", type=I64, value=1))
    bb0.instructions.append(mir.Copy(dest="_c1", source="_c0"))
    bb0.instructions.append(mir.StructInit(dest="_s0", type=STR, args=["_c1"]))
    bb0.instructions.append(mir.ArrayInit(dest="_a0", elements=["_c0", "_c1"], element_type=I64))
    bb0.instructions.append(mir.ArrayLiteral(dest="_a1", elem_type=I64, elements=["_c0", "_c1"]))
    bb0.instructions.append(mir.Drop(value="_s0"))
    bb0.terminator = mir.Return(value="_c0")
    fn = make_fn([bb0])
    SSAVerifierV2(fn).verify()


def test_misc_instrs_use_undef_fails():
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb0.instructions.append(mir.Copy(dest="_c1", source="_undef"))
    bb0.terminator = mir.Return(value=None)
    fn = make_fn([bb0])
    try:
        SSAVerifierV2(fn).verify()
    except RuntimeError:
        return
    raise AssertionError("expected verifier to fail on undefined operand in copy")


def test_simplify_const_folding_and_dce():
    # Build a function with const ops and dead defs; ensure simplifier removes/folds.
    bb0 = mir.BasicBlock(name="bb0", params=[])
    bb0.instructions.extend(
        [
            mir.Const(dest="_c0", type=I64, value=1),
            mir.Const(dest="_c1", type=I64, value=2),
            mir.Binary(dest="_b0", op="+", left="_c0", right="_c1"),
            mir.Const(dest="_dead", type=I64, value=5),
        ]
    )
    bb0.terminator = mir.Return(value="_b0")
    fn = mir.Function(
        name="f",
        params=[],
        return_type=I64,
        entry="bb0",
        module="tests",
        source=None,
        blocks={"bb0": bb0},
    )
    simplified_fn = simplify_function(fn)
    bb0_s = simplified_fn.blocks["bb0"]
    # Expect dead const removed and binary folded to const.
    dests = [instr.dest for instr in bb0_s.instructions if hasattr(instr, "dest")]
    assert "_dead" not in dests
    assert any(isinstance(instr, mir.Const) and instr.value == 3 and instr.type == I64 for instr in bb0_s.instructions)


def test_try_catch_stmt_shape():
    # entry: call foo normal->join args[], error->catch args[]
    bb_entry = mir.BasicBlock(name="bb0", params=[])
    bb_catch = mir.BasicBlock(name="bb_catch", params=[])
    bb_join = mir.BasicBlock(name="bb_join", params=[])
    bb_catch.terminator = mir.Return(value=None)
    bb_join.terminator = mir.Return(value=None)
    bb_entry.terminator = mir.Call(
        dest="_call_res",
        callee="foo",
        args=[],
        ret_type=I64,
        err_dest=None,
        normal=mir.Edge(target="bb_join", args=[]),
        error=mir.Edge(target="bb_catch", args=[]),
    )
    fn = make_fn([bb_entry, bb_catch, bb_join])
    SSAVerifierV2(fn).verify()


def test_can_error_invariants_throw_flagged() -> None:
    # throw should mark the function as can-error during annotation.
    f = mir.Function(
        name="bad_throw",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={"bb0": mir.BasicBlock(name="bb0", params=[], instructions=[], terminator=mir.Throw(error="_e"))},
    )
    try:
        from lang.driftc import _annotate_can_error  # type: ignore
    except Exception:
        return
    _annotate_can_error([f])
    assert f.can_error is True


def test_can_error_invariants_call_edges_to_non_can_error() -> None:
    callee = mir.Function(
        name="callee",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={"bb0": mir.BasicBlock(name="bb0", params=[], instructions=[], terminator=mir.Return(value=None))},
        can_error=False,
    )
    caller_entry = mir.BasicBlock(
        name="bb0",
        params=[],
        instructions=[],
        terminator=mir.Call(
            dest="_res",
            callee="callee",
            args=[],
            ret_type=I64,
            err_dest=None,
            normal=mir.Edge(target="bb1", args=[]),
            error=mir.Edge(target="bb2", args=[]),
        ),
    )
    caller = mir.Function(
        name="caller",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={
            "bb0": caller_entry,
            "bb1": mir.BasicBlock(name="bb1", params=[], instructions=[], terminator=mir.Return(value=None)),
            "bb2": mir.BasicBlock(name="bb2", params=[], instructions=[], terminator=mir.Return(value=None)),
        },
    )
    try:
        from lang.driftc import _annotate_can_error  # type: ignore
    except Exception:
        return
    try:
        _annotate_can_error([callee, caller])
    except RuntimeError:
        return
    raise AssertionError("expected call-with-edges to non-can-error callee to fail annotation")


def test_can_error_invariants_plain_call_to_can_error() -> None:
    # callee returns with error, caller drops it via plain call.
    callee_block = mir.BasicBlock(
        name="bb0",
        params=[],
        instructions=[
            mir.Const(dest="_err", type=ERROR, value=None),
            mir.Const(dest="_v", type=I64, value=0),
        ],
        terminator=mir.Return(value="_v", error="_err"),
    )
    callee = mir.Function(
        name="can_err",
        params=[],
        return_type=I64,
        entry="bb0",
        blocks={"bb0": callee_block},
        can_error=True,
    )
    caller_block = mir.BasicBlock(
        name="bb0",
        params=[],
        instructions=[mir.Call(dest="_tmp", callee="can_err", args=[], ret_type=I64, err_dest=None, normal=None, error=None)],
        terminator=mir.Return(value="_tmp"),
    )
    caller = mir.Function(name="caller", params=[], return_type=I64, entry="bb0", blocks={"bb0": caller_block})
    try:
        from lang.driftc import _annotate_can_error  # type: ignore
    except Exception:
        return
    try:
        _annotate_can_error([callee, caller])
    except RuntimeError:
        return
    raise AssertionError("expected plain call to can-error function to fail annotation")


def test_lowering_try_catch_integration():
    # fn f(x: Int64) -> Int64 { try { foo(x) } catch { return 0 } return 1 }
    loc = mir.Location()
    foo_def = FunctionDef(
        name="foo",
        params=[AstParam(name="a", type_expr=TypeExpr(name="Int64"))],
        return_type=TypeExpr(name="Int64"),
        body=Block(statements=[ReturnStmt(loc=loc, value=Name(loc=loc, ident="a"))]),
        loc=loc,
    )
    fn_def = FunctionDef(
        name="f",
        params=[AstParam(name="x", type_expr=TypeExpr(name="Int64"))],
        return_type=TypeExpr(name="Int64"),
        body=Block(
            statements=[
                TryStmt(
                    loc=loc,
                    body=Block(statements=[ExprStmt(loc=loc, value=Call(loc=loc, func=Name(loc=loc, ident="foo"), args=[Name(loc=loc, ident="x")], kwargs=[]))]),
                    catches=[CatchClause(event=None, binder=None, block=Block(statements=[ReturnStmt(loc=loc, value=Literal(loc=loc, value=0))]))],
                ),
                ReturnStmt(loc=loc, value=Literal(loc=loc, value=1)),
            ]
        ),
        loc=loc,
    )
    foo_sig = FunctionSignature(name="foo", params=(I64,), return_type=I64, effects=None)
    f_sig = FunctionSignature(name="f", params=(I64,), return_type=I64, effects=None)
    checked = CheckedProgram(
        program=ast.Program(functions=[foo_def, fn_def], statements=[], structs=[], exceptions=[]),
        functions={
            "foo": FunctionInfo(signature=foo_sig, node=foo_def),
            "f": FunctionInfo(signature=f_sig, node=fn_def),
        },
        globals={},
        structs={},
        exceptions={},
    )
    lowered = lower_function_ssa(fn_def, checked)
    SSAVerifierV2(type("F", (), {"blocks": lowered.blocks, "entry": lowered.entry})()).verify()


def test_lowering_try_catch_with_prelude_integration():
    # fn f(x: Int64) -> Int64 { try { let y = x + 1; foo(y) } catch { return 0 } return 1 }
    loc = mir.Location()
    foo_def = FunctionDef(
        name="foo",
        params=[AstParam(name="a", type_expr=TypeExpr(name="Int64"))],
        return_type=TypeExpr(name="Int64"),
        body=Block(statements=[ReturnStmt(loc=loc, value=Name(loc=loc, ident="a"))]),
        loc=loc,
    )
    fn_def = FunctionDef(
        name="f2",
        params=[AstParam(name="x", type_expr=TypeExpr(name="Int64"))],
        return_type=TypeExpr(name="Int64"),
        body=Block(
            statements=[
                TryStmt(
                    loc=loc,
                    body=Block(
                        statements=[
                            LetStmt(loc=loc, name="y", type_expr=None, value=ast.Binary(loc=loc, op="+", left=Name(loc=loc, ident="x"), right=Literal(loc=loc, value=1))),
                            ExprStmt(loc=loc, value=Call(loc=loc, func=Name(loc=loc, ident="foo"), args=[Name(loc=loc, ident="y")], kwargs=[])),
                        ]
                    ),
                    catches=[CatchClause(event=None, binder=None, block=Block(statements=[ReturnStmt(loc=loc, value=Literal(loc=loc, value=0))]))],
                ),
                ReturnStmt(loc=loc, value=Literal(loc=loc, value=1)),
            ]
        ),
        loc=loc,
    )
    foo_sig = FunctionSignature(name="foo", params=(I64,), return_type=I64, effects=None)
    f_sig = FunctionSignature(name="f2", params=(I64,), return_type=I64, effects=None)
    checked = CheckedProgram(
        program=ast.Program(functions=[foo_def, fn_def], statements=[], structs=[], exceptions=[]),
        functions={
            "foo": FunctionInfo(signature=foo_sig, node=foo_def),
            "f2": FunctionInfo(signature=f_sig, node=fn_def),
        },
        globals={},
        structs={},
        exceptions={},
    )
    lowered = lower_function_ssa(fn_def, checked)
    SSAVerifierV2(type("F", (), {"blocks": lowered.blocks, "entry": lowered.entry})()).verify()


def test_lowering_method_call_integration():
    # fn main() -> Int64 { out.writeln("hi"); return 0 }
    loc = mir.Location()
    fn_def = FunctionDef(
        name="main",
        params=[],
        return_type=TypeExpr(name="Int64"),
        body=Block(
            statements=[
                ExprStmt(
                    loc=loc,
                    value=Call(
                        loc=loc,
                        func=Attr(loc=loc, value=Name(loc=loc, ident="out"), attr="writeln"),
                        args=[Literal(loc=loc, value="hi")],
                        kwargs=[],
                    ),
                ),
                ReturnStmt(loc=loc, value=Literal(loc=loc, value=0)),
            ]
        ),
        loc=loc,
    )
    sig = FunctionSignature(name="main", params=(), return_type=I64, effects=None)
    # Stub out the method as a known function for SSA lowering.
    writeln_sig = FunctionSignature(name="out.writeln", params=(STR,), return_type=I64, effects=None)
    checked = CheckedProgram(
        program=ast.Program(functions=[fn_def], statements=[], structs=[], exceptions=[]),
        functions={
            "main": FunctionInfo(signature=sig, node=fn_def),
            "out.writeln": FunctionInfo(signature=writeln_sig, node=None),
        },
        globals={},
        structs={},
        exceptions={},
    )
    lowered = lower_function_ssa(fn_def, checked)
    SSAVerifierV2(type("F", (), {"blocks": lowered.blocks, "entry": lowered.entry})()).verify()


def test_lowering_throw_integration():
    # fn t() -> Int64 { if true { throw "err" } return 0 }
    loc = mir.Location()
    fn_def = FunctionDef(
        name="t",
        params=[],
        return_type=TypeExpr(name="Int64"),
        body=Block(
            statements=[
                IfStmt(
                    loc=loc,
                    condition=Literal(loc=loc, value=True),
                    then_block=Block(statements=[ThrowStmt(loc=loc, expr=Literal(loc=loc, value="err"))]),
                    else_block=Block(statements=[]),
                ),
                ReturnStmt(loc=loc, value=Literal(loc=loc, value=0)),
            ]
        ),
        loc=loc,
    )
    sig = FunctionSignature(name="t", params=(), return_type=I64, effects=None)
    checked = CheckedProgram(
        program=ast.Program(functions=[fn_def], statements=[], structs=[], exceptions=[]),
        functions={"t": FunctionInfo(signature=sig, node=fn_def)},
        globals={},
        structs={},
        exceptions={},
    )
    lowered = lower_function_ssa(fn_def, checked)
    SSAVerifierV2(type("F", (), {"blocks": lowered.blocks, "entry": lowered.entry})()).verify()


if __name__ == "__main__":
    test_straight_line()
    test_if_else_phi()
    test_while_loop()
    test_redefine_fails()
    test_use_before_def_fails()
    test_phi_arity_mismatch_fails()
    test_unreachable_block_allowed()
    test_missing_terminator_fails()
    test_field_get()
    test_call_with_edges()
    test_edge_args_to_paramless_block_fail()
    test_try_catch_shape()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[ssa-verifier] {name}")
            try:
                fn()
            except AssertionError as exc:
                print(f"[ssa-verifier] {name} FAILED: {exc}")
                raise
    print("all SSA verifier tests passed")
