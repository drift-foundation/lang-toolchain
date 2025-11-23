from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Sequence

from ..types import DISPLAYABLE, ERROR, STR, UNIT, FunctionSignature

OUT_WRITELN_SIGNATURE = FunctionSignature(
    "out.writeln", (DISPLAYABLE,), UNIT, effects=None
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
}

SPECIAL_SIGNATURES: Mapping[str, FunctionSignature] = {
    "out.writeln": OUT_WRITELN_SIGNATURE,
}


def builtin_signatures() -> Dict[str, FunctionSignature]:
    sigs = {name: builtin.signature for name, builtin in BUILTINS.items()}
    sigs.update(SPECIAL_SIGNATURES)
    return sigs
