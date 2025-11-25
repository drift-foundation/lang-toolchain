from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .types import Type


Value = str


@dataclass(frozen=True)
class Location:
    file: str = "<unknown>"
    line: int = 0
    column: int = 0


@dataclass(frozen=True)
class Param:
    name: str
    type: Type
    loc: Location = Location()


@dataclass(frozen=True)
class Edge:
    target: str
    args: List[Value] = field(default_factory=list)


class Instruction:
    pass

@dataclass(frozen=True)
class CallWithCtx(Instruction):
    dest: Value
    ctx_dest: Value
    callee: str
    args: List[Value]
    ctx_arg: Optional[Value] = None
    err_dest: Optional[Value] = None
    normal: Optional[Edge] = None
    error: Optional[Edge] = None
    loc: Location = Location()


@dataclass(frozen=True)
class Const(Instruction):
    dest: Value
    type: Type
    value: object
    loc: Location = Location()


@dataclass(frozen=True)
class Move(Instruction):
    dest: Value
    source: Value
    loc: Location = Location()


@dataclass(frozen=True)
class Copy(Instruction):
    dest: Value
    source: Value
    loc: Location = Location()


@dataclass(frozen=True)
class Call(Instruction):
    dest: Value
    callee: str
    args: List[Value]
    err_dest: Optional[Value] = None  # error result when using pair ABI
    normal: Optional[Edge] = None
    error: Optional[Edge] = None
    loc: Location = Location()


@dataclass(frozen=True)
class StructInit(Instruction):
    dest: Value
    type: Type
    args: List[Value]
    loc: Location = Location()


@dataclass(frozen=True)
class FieldGet(Instruction):
    dest: Value
    base: Value
    field: str
    loc: Location = Location()


@dataclass(frozen=True)
class ArrayInit(Instruction):
    dest: Value
    elements: List[Value]
    element_type: Type
    loc: Location = Location()


@dataclass(frozen=True)
class ArrayGet(Instruction):
    dest: Value
    base: Value
    index: Value
    loc: Location = Location()


@dataclass(frozen=True)
class ArraySet(Instruction):
    base: Value
    index: Value
    value: Value
    loc: Location = Location()


@dataclass(frozen=True)
class Unary(Instruction):
    dest: Value
    op: str
    operand: Value
    loc: Location = Location()


@dataclass(frozen=True)
class Binary(Instruction):
    dest: Value
    op: str
    left: Value
    right: Value
    loc: Location = Location()


@dataclass(frozen=True)
class Drop(Instruction):
    value: Value
    loc: Location = Location()


class Terminator:
    pass


@dataclass(frozen=True)
class Br(Terminator):
    target: Edge
    loc: Location = Location()


@dataclass(frozen=True)
class CondBr(Terminator):
    cond: Value
    then: Edge
    els: Edge
    loc: Location = Location()


@dataclass(frozen=True)
class Return(Terminator):
    value: Optional[Value] = None
    loc: Location = Location()


@dataclass(frozen=True)
class Raise(Terminator):
    error: Value
    loc: Location = Location()


@dataclass
class BasicBlock:
    name: str
    params: List[Param] = field(default_factory=list)
    instructions: List[Instruction] = field(default_factory=list)
    terminator: Optional[Terminator] = None


@dataclass
class Function:
    name: str
    params: List[Param]
    return_type: Type
    entry: str
    source: Optional[str] = None
    blocks: Dict[str, BasicBlock] = field(default_factory=dict)


@dataclass
class Program:
    functions: Dict[str, Function]
@dataclass(frozen=True)
class Location:
    file: str = "<unknown>"
    line: int = 0
    column: int = 0
