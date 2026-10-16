"""A small, typed SSA model used by the qcc lowering stages.

This module intentionally models only the values needed by the v0.1 frontend:
fixed-width integers, straight-line instructions, one or more named functions,
and basic blocks.  It does not attempt to be a complete LLVM object model.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import overload

from qcc.errors import SourceLocation


@dataclass(frozen=True, slots=True)
class IntType:
    """An LLVM integer type such as ``i1`` or ``i8``."""

    width: int

    def __post_init__(self) -> None:
        if not isinstance(self.width, int) or isinstance(self.width, bool):
            raise TypeError("integer type width must be an int")
        if self.width < 1:
            raise ValueError("integer type width must be positive")

    @property
    def bits(self) -> int:
        """Alias for :attr:`width` used by some lowering code."""

        return self.width

    @property
    def modulus(self) -> int:
        return 1 << self.width

    def normalize(self, value: int) -> int:
        """Return ``value`` as the unsigned LLVM bit pattern for this type."""

        return value % self.modulus

    def __str__(self) -> str:
        return f"i{self.width}"

    def __repr__(self) -> str:
        return f"IntType(width={self.width})"


# Both names are useful at API boundaries and describe the same immutable type.
IntegerType = IntType
LLVMIntType = IntType


@dataclass(frozen=True, slots=True)
class VoidType:
    """The LLVM ``void`` return type."""

    def __str__(self) -> str:
        return "void"

    def __repr__(self) -> str:
        return "VoidType()"


IRType = IntType | VoidType


class Operand:
    """Base class for an integer SSA value or integer constant."""

    type: IntType


@dataclass(frozen=True, slots=True)
class SSAValue(Operand):
    """A named integer SSA value.  ``name`` is stored without the ``%``."""

    name: str
    type: IntType

    def __post_init__(self) -> None:
        normalized = self.name.removeprefix("%")
        if not normalized:
            raise ValueError("an SSA value needs a non-empty name")
        object.__setattr__(self, "name", normalized)

    @property
    def identifier(self) -> str:
        return f"%{self.name}"

    def __str__(self) -> str:
        return self.identifier

    def __repr__(self) -> str:
        return f"SSAValue(name={self.name!r}, type={self.type!r})"


@dataclass(frozen=True, slots=True)
class Parameter(SSAValue):
    """A function parameter with its optional LLVM parameter attributes."""

    attributes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        SSAValue.__post_init__(self)
        object.__setattr__(self, "attributes", tuple(self.attributes))

    def __repr__(self) -> str:
        return (
            f"Parameter(name={self.name!r}, type={self.type!r}, "
            f"attributes={self.attributes!r})"
        )


@dataclass(frozen=True, slots=True)
class IntConstant(Operand):
    """An integer literal normalized to its declared LLVM width."""

    type: IntType
    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise TypeError("integer constant value must be an int")
        object.__setattr__(self, "value", self.type.normalize(self.value))

    @property
    def literal(self) -> str:
        return str(self.value)

    def __str__(self) -> str:
        return f"{self.type} {self.value}"

    def __repr__(self) -> str:
        return f"IntConstant(type={self.type!r}, value={self.value})"


Constant = IntConstant
IntegerConstant = IntConstant
IntegerLiteral = IntConstant


@dataclass(frozen=True, slots=True)
class MetadataAttachment:
    """A permissively preserved instruction metadata attachment."""

    name: str
    node: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.lstrip("!"))
        node = self.node if self.node.startswith("!") else f"!{self.node}"
        object.__setattr__(self, "node", node)

    def __str__(self) -> str:
        return f"!{self.name} {self.node}"


@dataclass(frozen=True, slots=True)
class Instruction:
    """A typed instruction in the supported straight-line SSA subset.

    ``type`` is the result type for value-producing operations and the return
    type for ``ret``.  For ``icmp`` it is ``i1`` and ``operand_type`` carries
    the compared integer type.  The generic representation makes the lowering
    API stable without requiring one class for each LLVM opcode.
    """

    opcode: str
    result: SSAValue | None = None
    type: IRType | None = None
    operands: tuple[Operand, ...] = ()
    flags: tuple[str, ...] = ()
    predicate: str | None = None
    operand_type: IntType | None = None
    source_location: SourceLocation | None = None
    attributes: tuple[str, ...] = ()
    metadata: tuple[MetadataAttachment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "opcode", self.opcode.lower())
        object.__setattr__(self, "operands", tuple(self.operands))
        object.__setattr__(self, "flags", tuple(self.flags))
        object.__setattr__(self, "attributes", tuple(self.attributes))
        object.__setattr__(self, "metadata", tuple(self.metadata))

    @property
    def location(self) -> SourceLocation | None:
        """Short alias for :attr:`source_location`."""

        return self.source_location

    @property
    def op(self) -> str:
        """Short alias for :attr:`opcode`."""

        return self.opcode

    @property
    def source_type(self) -> IntType | None:
        """Source type for a cast, or the compared type for ``icmp``."""

        return self.operand_type

    @property
    def comparison_type(self) -> IntType | None:
        """Readable alias for an ``icmp`` operand type."""

        return self.operand_type

    @property
    def lhs(self) -> Operand:
        if not self.operands:
            raise AttributeError(f"{self.opcode} has no left operand")
        return self.operands[0]

    @property
    def rhs(self) -> Operand:
        if len(self.operands) < 2:
            raise AttributeError(f"{self.opcode} has no right operand")
        return self.operands[1]

    @property
    def value(self) -> Operand | None:
        """The returned/cast value where that shorthand is meaningful."""

        return self.operands[0] if self.operands else None

    @property
    def is_terminator(self) -> bool:
        return self.opcode in {"ret", "br", "switch", "unreachable"}

    def _operand_text(self, operand: Operand) -> str:
        if isinstance(operand, IntConstant):
            return operand.literal
        return str(operand)

    def __str__(self) -> str:
        prefix = f"{self.result} = " if self.result is not None else ""
        opcode = self.opcode
        if opcode in {"add", "sub", "and", "or", "xor"}:
            flags = (" ".join(self.flags) + " ") if self.flags else ""
            type_text = f"{self.type} " if self.type is not None else ""
            operands = ", ".join(self._operand_text(item) for item in self.operands)
            body = f"{opcode} {flags}{type_text}{operands}"
        elif opcode in {"zext", "sext", "trunc"}:
            flags = (" ".join(self.flags) + " ") if self.flags else ""
            source_type = f"{self.operand_type} " if self.operand_type else ""
            target_type = f" {self.type}" if self.type else ""
            operand = (
                self._operand_text(self.operands[0]) if self.operands else "<missing>"
            )
            body = f"{opcode} {flags}{source_type}{operand} to{target_type}"
        elif opcode == "icmp":
            flags = (" ".join(self.flags) + " ") if self.flags else ""
            predicate = f"{self.predicate} " if self.predicate else ""
            type_text = f"{self.operand_type} " if self.operand_type else ""
            operands = ", ".join(self._operand_text(item) for item in self.operands)
            body = f"icmp {flags}{predicate}{type_text}{operands}"
        elif opcode == "ret":
            if not self.operands:
                body = "ret void"
            else:
                type_text = f"{self.type} " if self.type is not None else ""
                body = f"ret {type_text}{self._operand_text(self.operands[0])}"
        else:
            type_text = f" {self.type}" if self.type is not None else ""
            operands = ", ".join(self._operand_text(item) for item in self.operands)
            body = f"{opcode}{type_text}{(' ' + operands) if operands else ''}"
        if self.metadata:
            body += ", " + ", ".join(str(item) for item in self.metadata)
        return prefix + body

    def __repr__(self) -> str:
        result = (
            f"Instruction(opcode={self.opcode!r}, result={self.result!r}, "
            f"type={self.type!r}, operands={self.operands!r}"
        )
        if self.flags:
            result += f", flags={self.flags!r}"
        if self.predicate is not None:
            result += f", predicate={self.predicate!r}"
        if self.source_location is not None:
            result += f", source_location={self.source_location!r}"
        return result + ")"


# Semantic names are aliases so consumers can use isinstance() without making
# the parser manufacture a large inheritance tree for this deliberately small IR.
BinaryInstruction = Instruction
CastInstruction = Instruction
ICmpInstruction = Instruction
ReturnInstruction = Instruction


@dataclass(frozen=True, slots=True)
class BasicBlock:
    """A named sequence of instructions."""

    name: str = "entry"
    instructions: tuple[Instruction, ...] = ()
    source_location: SourceLocation | None = None

    def __post_init__(self) -> None:
        normalized = self.name.removesuffix(":")
        object.__setattr__(self, "name", normalized or "entry")
        object.__setattr__(self, "instructions", tuple(self.instructions))

    @property
    def location(self) -> SourceLocation | None:
        return self.source_location

    @property
    def terminator(self) -> Instruction | None:
        if self.instructions and self.instructions[-1].is_terminator:
            return self.instructions[-1]
        return None

    @property
    def is_terminated(self) -> bool:
        return self.terminator is not None

    def __iter__(self) -> Iterator[Instruction]:
        return iter(self.instructions)

    def __str__(self) -> str:
        body = "\n".join(f"  {instruction}" for instruction in self.instructions)
        return f"{self.name}:" + (f"\n{body}" if body else "")


@dataclass(frozen=True, slots=True)
class Function:
    """An integer-typed SSA function and its basic blocks."""

    name: str
    return_type: IRType
    parameters: tuple[Parameter, ...] = ()
    blocks: tuple[BasicBlock, ...] = ()
    attributes: tuple[str, ...] = ()
    source_location: SourceLocation | None = None

    def __post_init__(self) -> None:
        normalized = self.name.removeprefix("@")
        object.__setattr__(self, "name", normalized)
        object.__setattr__(self, "parameters", tuple(self.parameters))
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "attributes", tuple(self.attributes))

    @property
    def location(self) -> SourceLocation | None:
        return self.source_location

    @property
    def args(self) -> tuple[Parameter, ...]:
        return self.parameters

    @property
    def entry(self) -> BasicBlock:
        if not self.blocks:
            raise LookupError(f"function @{self.name} has no basic blocks")
        return self.blocks[0]

    @property
    def instructions(self) -> tuple[Instruction, ...]:
        return tuple(instruction for block in self.blocks for instruction in block)

    def lookup(self, name: str) -> SSAValue | None:
        """Look up a parameter or instruction result by ``name``/``%name``."""

        normalized = name.removeprefix("%")
        for parameter in self.parameters:
            if parameter.name == normalized:
                return parameter
        for instruction in self.instructions:
            if instruction.result is not None and instruction.result.name == normalized:
                return instruction.result
        return None

    def get_value(self, name: str) -> SSAValue:
        value = self.lookup(name)
        if value is None:
            raise KeyError(name)
        return value

    def __getitem__(self, name: str) -> SSAValue:
        return self.get_value(name)

    def __iter__(self) -> Iterator[BasicBlock]:
        return iter(self.blocks)

    def __str__(self) -> str:
        params = ", ".join(f"{item.type} {item}" for item in self.parameters)
        header = f"define {self.return_type} @{self.name}({params}) {{"
        body = "\n".join(str(block) for block in self.blocks)
        return f"{header}\n{body}\n}}"


class FunctionCollection(Sequence[Function]):
    """Ordered functions with both sequence and name lookup semantics."""

    def __init__(
        self, functions: Iterable[Function] | Mapping[str, Function] = ()
    ) -> None:
        if isinstance(functions, Mapping):
            items: tuple[Function, ...] = tuple(functions.values())
        else:
            items = tuple(functions)
        names: set[str] = set()
        for function in items:
            if function.name in names:
                raise ValueError(f"duplicate function @{function.name}")
            names.add(function.name)
        self._items: tuple[Function, ...] = items
        self._by_name: dict[str, Function] = {
            function.name: function for function in items
        }

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Function]:
        return iter(self._items)

    @overload
    def __getitem__(self, key: int) -> Function: ...

    @overload
    def __getitem__(self, key: slice) -> tuple[Function, ...]: ...

    @overload
    def __getitem__(self, key: str) -> Function: ...

    def __getitem__(self, key: int | slice | str) -> Function | tuple[Function, ...]:
        if isinstance(key, str):
            return self._by_name[key.removeprefix("@")]
        if isinstance(key, slice):
            return self._items[key]
        return self._items[key]

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            return key.lstrip("@") in self._by_name
        return key in self._items

    def keys(self) -> tuple[str, ...]:
        return tuple(self._by_name)

    def values(self) -> tuple[Function, ...]:
        return self._items

    def items(self) -> tuple[tuple[str, Function], ...]:
        return tuple((function.name, function) for function in self._items)

    def get(self, name: str, default: Function | None = None) -> Function | None:
        return self._by_name.get(name.lstrip("@"), default)

    def __repr__(self) -> str:
        return f"FunctionCollection({self._items!r})"


@dataclass(frozen=True, slots=True)
class Module:
    """A parsed LLVM module with convenient function lookup."""

    functions: FunctionCollection = field(default_factory=FunctionCollection)
    source_name: str = "<llvm>"
    target_triple: str | None = None
    metadata: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.functions, FunctionCollection):
            object.__setattr__(self, "functions", FunctionCollection(self.functions))
        object.__setattr__(self, "metadata", tuple(self.metadata))

    def get_function(self, name: str) -> Function:
        normalized = name.lstrip("@")
        function = self.functions.get(normalized)
        if function is None:
            raise KeyError(name)
        return function

    def lookup_function(self, name: str) -> Function | None:
        return self.functions.get(name.lstrip("@"))

    def function(self, name: str) -> Function:
        return self.get_function(name)

    def __getitem__(self, name: str) -> Function:
        return self.get_function(name)

    def __iter__(self) -> Iterator[Function]:
        return iter(self.functions)

    def __str__(self) -> str:
        return "\n\n".join(str(function) for function in self.functions)

    def __repr__(self) -> str:
        return f"Module(source_name={self.source_name!r}, functions={self.functions!r})"


IRModule = Module
IRFunction = Function
IRBasicBlock = BasicBlock


__all__ = [
    "BasicBlock",
    "BinaryInstruction",
    "CastInstruction",
    "Constant",
    "Function",
    "FunctionCollection",
    "ICmpInstruction",
    "IRBasicBlock",
    "IRFunction",
    "IRModule",
    "IRType",
    "Instruction",
    "IntConstant",
    "IntType",
    "IntegerConstant",
    "IntegerLiteral",
    "IntegerType",
    "LLVMIntType",
    "MetadataAttachment",
    "Module",
    "Operand",
    "Parameter",
    "ReturnInstruction",
    "SSAValue",
    "VoidType",
]
