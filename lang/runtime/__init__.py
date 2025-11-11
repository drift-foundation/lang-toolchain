from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Sequence

from ..types import ERROR, STR, UNIT, FunctionSignature


@dataclass
class ErrorValue:
    message: str
    domain: str | None = None
    code: str = ""
    attrs: Dict[str, object] = field(default_factory=dict)

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
    if not isinstance(message, str):
        raise TypeError("error(message) requires a string message")
    return ErrorValue(message=message, domain=domain, code=code, attrs=dict(attrs))


BUILTINS: Mapping[str, BuiltinFunction] = {
    "print": BuiltinFunction(
        signature=FunctionSignature("print", (STR,), UNIT, effects=None),
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
}


def builtin_signatures() -> Dict[str, FunctionSignature]:
    return {name: builtin.signature for name, builtin in BUILTINS.items()}
