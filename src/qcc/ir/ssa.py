"""A small, typed SSA model used by the qcc lowering stages.

This module intentionally models only the values needed by the bounded integer
frontend: fixed-width integers, named functions, basic blocks, and the small
instruction set accepted by the textual LLVM parser.  It does not attempt to
be a complete LLVM object model.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
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
class PhiIncoming:
    """One value and predecessor label in an integer ``phi`` instruction."""

    value: Operand
    block: str
    source_location: SourceLocation | None = None

    def __post_init__(self) -> None:
        normalized = self.block.removeprefix("%").removesuffix(":")
        if not normalized:
            raise ValueError("a phi incoming edge needs a non-empty block name")
        object.__setattr__(self, "block", normalized)

    @property
    def location(self) -> SourceLocation | None:
        return self.source_location


# A descriptive alias for callers that use LLVM terminology rather than the
# shorter class name.
IncomingValue = PhiIncoming


@dataclass(frozen=True, slots=True)
class Instruction:
    """A typed instruction in the supported integer SSA subset.

    ``type`` is the result type for value-producing operations and the return
    type for ``ret``.  For ``icmp`` it is ``i1`` and ``operand_type`` carries
    the compared integer type.  ``targets`` stores branch destination labels,
    while ``incoming`` stores the edges of a ``phi``.  ``callee`` names the
    function or normalized intrinsic (``llvm.umin``) of a ``call``; calls only
    exist between parsing and legalization, which expands or inlines them.
    The generic representation keeps the lowering API stable without
    requiring one class for each LLVM opcode.
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
    targets: tuple[str, ...] = ()
    incoming: tuple[PhiIncoming, ...] = ()
    callee: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "opcode", self.opcode.lower())
        object.__setattr__(self, "operands", tuple(self.operands))
        object.__setattr__(self, "flags", tuple(self.flags))
        object.__setattr__(self, "attributes", tuple(self.attributes))
        object.__setattr__(self, "metadata", tuple(self.metadata))
        object.__setattr__(
            self,
            "targets",
            tuple(
                target.removeprefix("%").removesuffix(":") for target in self.targets
            ),
        )
        object.__setattr__(self, "incoming", tuple(self.incoming))

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
    def condition(self) -> Operand | None:
        """The condition operand of ``br`` or ``select``, when present."""

        if self.opcode in {"br", "select"} and self.operands:
            return self.operands[0]
        return None

    @property
    def true_value(self) -> Operand:
        """The selected true-arm value of a ``select`` instruction."""

        if self.opcode != "select" or len(self.operands) < 2:
            raise AttributeError(f"{self.opcode} has no true selected value")
        return self.operands[1]

    @property
    def false_value(self) -> Operand:
        """The selected false-arm value of a ``select`` instruction."""

        if self.opcode != "select" or len(self.operands) < 3:
            raise AttributeError(f"{self.opcode} has no false selected value")
        return self.operands[2]

    @property
    def branch_targets(self) -> tuple[str, ...]:
        """Destination labels of a ``br`` instruction."""

        return self.targets if self.opcode == "br" else ()

    @property
    def target(self) -> str | None:
        """The sole destination of an unconditional ``br``, when present."""

        if self.opcode == "br" and len(self.targets) == 1:
            return self.targets[0]
        return None

    @property
    def incoming_values(self) -> tuple[Operand, ...]:
        """Values carried by a ``phi`` instruction, in incoming-edge order."""

        return tuple(edge.value for edge in self.incoming)

    @property
    def incoming_blocks(self) -> tuple[str, ...]:
        """Predecessor labels carried by a ``phi`` instruction."""

        return tuple(edge.block for edge in self.incoming)

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
        if opcode in {"add", "sub", "mul", "udiv", "urem", "and", "or", "xor"}:
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
        elif opcode == "select":
            condition = (
                self._operand_text(self.condition)
                if self.condition is not None
                else "<missing>"
            )
            true_value = (
                self._operand_text(self.true_value)
                if len(self.operands) > 1
                else "<missing>"
            )
            false_value = (
                self._operand_text(self.false_value)
                if len(self.operands) > 2
                else "<missing>"
            )
            value_type = f"{self.type} " if self.type is not None else ""
            body = (
                f"select i1 {condition}, {value_type}{true_value}, "
                f"{value_type}{false_value}"
            )
        elif opcode == "phi":
            value_type = f"{self.type} " if self.type is not None else ""
            incoming = ", ".join(
                f"[ {self._operand_text(edge.value)}, %{edge.block} ]"
                for edge in self.incoming
            )
            body = f"phi {value_type}{incoming}"
        elif opcode == "br":
            if len(self.targets) == 1:
                body = f"br label %{self.targets[0]}"
            elif len(self.targets) == 2 and self.condition is not None:
                body = (
                    f"br i1 {self._operand_text(self.condition)}, "
                    f"label %{self.targets[0]}, label %{self.targets[1]}"
                )
            else:
                body = "br <malformed>"
        elif opcode == "call":
            arguments = ", ".join(
                f"{item.type} {self._operand_text(item)}" for item in self.operands
            )
            type_text = f"{self.type} " if self.type is not None else ""
            body = f"call {type_text}@{self.callee}({arguments})"
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
        if self.targets:
            result += f", targets={self.targets!r}"
        if self.callee is not None:
            result += f", callee={self.callee!r}"
        if self.incoming:
            result += f", incoming={self.incoming!r}"
        if self.source_location is not None:
            result += f", source_location={self.source_location!r}"
        return result + ")"


# Semantic names are aliases so consumers can use isinstance() without making
# the parser manufacture a large inheritance tree for this deliberately small IR.
BinaryInstruction = Instruction
CastInstruction = Instruction
ICmpInstruction = Instruction
SelectInstruction = Instruction
PhiInstruction = Instruction
BranchInstruction = Instruction
ReturnInstruction = Instruction


@dataclass(frozen=True, slots=True)
class BasicBlock:
    """A named sequence of instructions and its outgoing CFG labels."""

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

    @property
    def successors(self) -> tuple[str, ...]:
        """Names of blocks reached by this block's branch terminator."""

        terminator = self.terminator
        if terminator is None:
            return ()
        return terminator.branch_targets

    @property
    def successor_names(self) -> tuple[str, ...]:
        """Alias for :attr:`successors` useful when disambiguating objects."""

        return self.successors

    def __iter__(self) -> Iterator[Instruction]:
        return iter(self.instructions)

    def __str__(self) -> str:
        body = "\n".join(f"  {instruction}" for instruction in self.instructions)
        return f"{self.name}:" + (f"\n{body}" if body else "")


@dataclass(frozen=True, slots=True)
class Function:
    """An integer-typed SSA function and its basic blocks.

    CFG helpers return blocks in source order.  Their label-based counterparts
    make it possible to inspect the graph without relying on object identity.
    """

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
        names: set[str] = set()
        for block in self.blocks:
            if block.name in names:
                raise ValueError(f"duplicate basic block %{block.name}")
            names.add(block.name)

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

    def get_block(self, name: str | BasicBlock) -> BasicBlock:
        """Return a block by object or by its ``%``-optional label."""

        if isinstance(name, BasicBlock):
            if name in self.blocks:
                return name
            raise KeyError(name.name)
        normalized = name.removeprefix("%").removesuffix(":")
        for block in self.blocks:
            if block.name == normalized:
                return block
        raise KeyError(name)

    def block(self, name: str | BasicBlock) -> BasicBlock:
        """Short alias for :meth:`get_block`."""

        return self.get_block(name)

    def lookup_block(self, name: str) -> BasicBlock | None:
        """Return a named block, or ``None`` when it is not in the function."""

        try:
            return self.get_block(name)
        except KeyError:
            return None

    def successor_names(self, block: str | BasicBlock) -> tuple[str, ...]:
        """Return outgoing CFG labels for ``block``."""

        selected = self.get_block(block)
        return selected.successors

    def successors(self, block: str | BasicBlock) -> tuple[BasicBlock, ...]:
        """Return outgoing CFG blocks for ``block`` in branch order."""

        return tuple(self.get_block(name) for name in self.successor_names(block))

    def predecessor_names(self, block: str | BasicBlock) -> tuple[str, ...]:
        """Return incoming CFG labels for ``block`` in source order."""

        selected = self.get_block(block)
        return tuple(
            candidate.name
            for candidate in self.blocks
            if selected.name in candidate.successors
        )

    def predecessors(self, block: str | BasicBlock) -> tuple[BasicBlock, ...]:
        """Return incoming CFG blocks for ``block`` in source order."""

        return tuple(self.get_block(name) for name in self.predecessor_names(block))

    @property
    def cfg_successors(self) -> dict[str, tuple[str, ...]]:
        """A label-to-label view of the function's outgoing CFG edges."""

        return {block.name: block.successors for block in self.blocks}

    @property
    def cfg_predecessors(self) -> dict[str, tuple[str, ...]]:
        """A label-to-label view of the function's incoming CFG edges."""

        return {block.name: self.predecessor_names(block) for block in self.blocks}

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
    rejected: Mapping[str, Exception] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.functions, FunctionCollection):
            object.__setattr__(self, "functions", FunctionCollection(self.functions))
        object.__setattr__(self, "metadata", tuple(self.metadata))
        object.__setattr__(self, "rejected", MappingProxyType(dict(self.rejected)))

    @property
    def function_names(self) -> tuple[str, ...]:
        """Every function defined by the source, accepted or rejected."""

        return (*self.functions.keys(), *self.rejected)

    def get_function(self, name: str) -> Function:
        """Return an accepted function, or raise why it was rejected.

        A module parsed with ``strict=False`` keeps going after a function
        that is outside the supported subset.  The diagnostic is stored and
        raised here, so an unsupported helper only matters to callers that
        actually ask for it.
        """

        normalized = name.lstrip("@")
        function = self.functions.get(normalized)
        if function is None:
            failure = self.rejected.get(normalized)
            if failure is not None:
                raise failure
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
    "BranchInstruction",
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
    "IncomingValue",
    "Module",
    "Operand",
    "Parameter",
    "PhiIncoming",
    "PhiInstruction",
    "ReturnInstruction",
    "SelectInstruction",
    "SSAValue",
    "VoidType",
]
