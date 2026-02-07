from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Sequence

from ..types import DISPLAYABLE, ERROR, STR, UNIT, I64, FunctionSignature, array_of, INT, Type

OUT_WRITELN_SIGNATURE = FunctionSignature(
    "out.writeln", (DISPLAYABLE,), UNIT, effects=None
)
ERROR_NEW_SIGNATURE = FunctionSignature("error_new", (STR,), ERROR, effects=None)
DRIFT_ERROR_NEW_SIGNATURE = FunctionSignature(
    "drift_error_new",
    (
        array_of(STR),
        array_of(STR),
        I64,
        STR,
        STR,
        array_of(STR),
        array_of(STR),
        array_of(STR),
        array_of(I64),
        I64,
    ),
    ERROR,
    effects=None,
)


@dataclass
class ErrorValue:
    message: str
    domain: str | None = None
    code: str = ""
    attrs: Dict[str, object] = field(default_factory=dict)
    stack: list[str] | None = None

    def __str__(self) -> str:  # pragma: no cover - debugging helper
        return f"error[{self.domain or 'unknown'}]: {self.message}"


BuiltinImpl = Callable[["RuntimeContext", Sequence[object], Dict[str, object]], object]


@dataclass
class BuiltinFunction:
    signature: FunctionSignature
    impl: BuiltinImpl


class RuntimeContext:
    def __init__(self, stdout) -> None:
        self.stdout = stdout


class ConsoleOut:
    def __init__(self, runtime_ctx: RuntimeContext) -> None:
        self.runtime_ctx = runtime_ctx
        self._members: Dict[str, BuiltinFunction] = {
            "writeln": BuiltinFunction(
                signature=OUT_WRITELN_SIGNATURE,
                impl=self._writeln,
            )
        }

    def _writeln(self, ctx: RuntimeContext, args: Sequence[object], kwargs: Dict[str, object]) -> object:
        text = args[0]
        ctx.stdout.write(str(text) + "\n")
        ctx.stdout.flush()
        return None

    def get_attr(self, name: str) -> BuiltinFunction:
        if name not in self._members:
            raise RuntimeError(f"console output has no attribute '{name}'")
        return self._members[name]


def _builtin_print(ctx: RuntimeContext, args: Sequence[object], kwargs: Dict[str, object]) -> object:
    text = args[0]
    ctx.stdout.write(str(text) + "\n")
    ctx.stdout.flush()
    return None


def _builtin_error(ctx: RuntimeContext, args: Sequence[object], kwargs: Dict[str, object]) -> object:
    message = args[0]
    domain = kwargs.get("domain")
    code = kwargs.get("code", "")
    attrs = kwargs.get("attrs", {})
    location = kwargs.get("location")
    if not isinstance(message, str):
        raise TypeError("error(message) requires a string message")
    return ErrorValue(message=message, domain=domain, code=code, attrs=dict(attrs), stack=location)


def _builtin_error_new(ctx: RuntimeContext, args: Sequence[object], kwargs: Dict[str, object]) -> object:
    """Runtime stub for codegen parity; mirrors error_new C helper."""
    message = args[0]
    if not isinstance(message, str):
        raise TypeError("error_new(message) requires a string message")
    return ErrorValue(message=str(message))


BUILTINS: Mapping[str, BuiltinFunction] = {
    "print": BuiltinFunction(
        signature=FunctionSignature("print", (DISPLAYABLE,), UNIT, effects=None),
        impl=_builtin_print,
    ),
    "error": BuiltinFunction(
        signature=FunctionSignature(
            "error",
            (STR,),
            ERROR,
            effects=None,
            allowed_kwargs=frozenset({"domain", "code", "attrs"}),
        ),
        impl=_builtin_error,
    ),
    "error_new": BuiltinFunction(
        signature=ERROR_NEW_SIGNATURE,
        impl=_builtin_error_new,
    ),
    "drift_error_new": BuiltinFunction(
        signature=DRIFT_ERROR_NEW_SIGNATURE,
        impl=_builtin_error_new,
    ),
}

SPECIAL_SIGNATURES: Mapping[str, FunctionSignature] = {
    "out.writeln": OUT_WRITELN_SIGNATURE,
    "drift_error_new_dummy": FunctionSignature("drift_error_new_dummy", (INT, STR, STR), ERROR, effects=None),
    "drift_error_get_code": FunctionSignature("drift_error_get_code", (ERROR,), INT, effects=None),
    "drift_optional_int_some": FunctionSignature(
        "drift_optional_int_some", (INT,), Type("Optional", args=(INT,)), effects=None
    ),
    "drift_optional_int_none": FunctionSignature(
        "drift_optional_int_none", tuple(), Type("Optional", args=(INT,)), effects=None
    ),
}


def builtin_signatures() -> Dict[str, FunctionSignature]:
    sigs = {name: builtin.signature for name, builtin in BUILTINS.items()}
    sigs.update(SPECIAL_SIGNATURES)
    return sigs
