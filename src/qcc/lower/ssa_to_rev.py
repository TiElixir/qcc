"""Lower fixed-width integer SSA into Bennett-clean reversible circuits.

The frontend deliberately owns parsing C and LLVM.  This module only relies on
small structural conventions (``args``, ``instructions``, ``opcode``, and
``operands``), and accepts dictionaries as well as dataclass-like objects so
that the frontend and circuit packages can evolve independently.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from qcc.errors import UnsupportedConstructError
from qcc.lib.divmod import constant_divmod, constant_divmod_workspace_width
from qcc.lib.multiply import multiply_by_constant_modular, multiply_modular

from .ancilla import AncillaAllocator, CircuitLayout, Register
from .bennett import (
    BennettScheduler,
    Gate,
    add_modular,
    add_registers,
    and_bits,
    copy_bits,
    or_bits,
    sub_registers,
    xor_bits,
)


class LoweringError(ValueError):
    """Base error for malformed or unsupported SSA input."""


class SSAFormatError(LoweringError):
    """Raised when a frontend object does not contain a usable SSA shape."""


class UnsupportedSSAOperation(LoweringError):
    """Raised when the lowering boundary cannot support an SSA operation."""


@dataclass(frozen=True)
class LoweredCircuit:
    """Backend-neutral result produced by :func:`lower_ssa`.

    ``gates`` contains only self-inverse X/CNOT/Toffoli gates.  The output
    register is copied before the inverse schedule, so the input and every
    register listed in ``layout.ancillas`` finish in their initial state.
    """

    gates: tuple[Gate, ...]
    num_qubits: int
    layout: CircuitLayout
    function_name: str | None = None

    @property
    def qubit_count(self) -> int:
        return self.num_qubits

    @property
    def n_qubits(self) -> int:
        return self.num_qubits

    @property
    def operations(self) -> tuple[Gate, ...]:
        return self.gates

    def as_gate_tuples(self) -> tuple[tuple[str, tuple[int, ...]], ...]:
        return tuple(gate.as_tuple() for gate in self.gates)


@dataclass(frozen=True)
class _Literal:
    value: int
    width: int | None = None


@dataclass(frozen=True)
class _Operation:
    opcode: str
    result: str | None
    operands: tuple[Any, ...]
    width_spec: Any = None
    source_width_spec: Any = None
    predicate: str | None = None
    direction: str | None = None
    source: Any = None


@dataclass(frozen=True)
class _Block:
    name: str
    operations: tuple[_Operation, ...]


@dataclass(frozen=True)
class _Guard:
    """A path predicate represented by a clean bit or a compile-time bool."""

    bit: int | None = None
    constant: bool | None = None

    def __post_init__(self) -> None:
        if (self.bit is None) == (self.constant is None):
            raise ValueError("a path guard must be a bit or a boolean constant")


@dataclass(frozen=True)
class _Value:
    name: str
    width: int
    register: Register
    constant: int | None = None


@dataclass(frozen=True)
class _Producer:
    """The reversible history and leases belonging to one SSA value."""

    gates: tuple[Gate, ...]
    leases: tuple[Register, ...]
    dependencies: tuple[_Value, ...]


_MISSING = object()
_INT_RE = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F]+|0[bB][01]+|[0-9]+)$")
_WIDTH_RE = re.compile(r"(?:^|[^A-Za-z])(?:i|u|uint|int)([0-9]+)(?:$|[^0-9])")

_ICMP_PREDICATES = {
    "eq",
    "ne",
    "ugt",
    "uge",
    "ult",
    "ule",
    "sgt",
    "sge",
    "slt",
    "sle",
}
_TERMINATORS = {"ret", "return", "br", "switch", "unreachable"}
_SHIFT_OPS = {"shl", "lshr", "ashr"}
_ROTATE_OPS = {
    "rol",
    "ror",
    "rotl",
    "rotr",
    "rotate_left",
    "rotate_right",
    "fshl",
    "fshr",
    "rotate",
}
_SUPPORTED = {
    "add",
    "sub",
    "mul",
    "udiv",
    "urem",
    "and",
    "or",
    "xor",
    "not",
    "zext",
    "sext",
    "trunc",
    "const",
    "constant",
    "literal",
    "copy",
    "mov",
    "identity",
    "icmp",
    "select",
    "phi",
    *_SHIFT_OPS,
    *_ROTATE_OPS,
    *_TERMINATORS,
}


def _field(value: Any, names: Sequence[str], default: Any = None) -> Any:
    """Read a field from either a mapping or a dataclass-like object."""

    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        try:
            result = getattr(value, name)
        except AttributeError:
            continue
        if result is not None:
            return result
    return default


def _normalise_name(value: Any) -> str | None:
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else str(value).strip()
    if not text:
        return None
    text = text.removeprefix("%")
    text = text.removeprefix("@")
    # A few minimal LLVM readers preserve the whole ``i8 %name`` token.
    if " " in text:
        candidate = text.rsplit(None, 1)[-1]
        if candidate.startswith("%"):
            text = candidate[1:]
    return text


def _width_of(value: Any, _seen: set[int] | None = None) -> int | None:
    """Extract an integer bit width from common frontend type objects."""

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if _seen is None:
        _seen = set()
    marker = id(value)
    if marker in _seen:
        return None
    _seen.add(marker)
    for name in ("width", "bit_width", "bits", "n_bits", "size"):
        candidate = _field(value, (name,), _MISSING)
        if candidate is not _MISSING:
            width = _width_of(candidate, _seen)
            if width is not None:
                return width
    if isinstance(value, str):
        text = value.strip()
        match = re.search(r"\bi([0-9]+)\b", text)
        if match:
            return int(match.group(1))
        match = _WIDTH_RE.search(text)
        if match:
            return int(match.group(1))
        return None
    nested = _field(value, ("type", "ty", "integer_type"), _MISSING)
    if nested is not _MISSING:
        return _width_of(nested, _seen)
    return None


def _as_literal(value: Any) -> _Literal | None:
    if isinstance(value, bool):
        return _Literal(int(value))
    if isinstance(value, int):
        return _Literal(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lower() == "true":
            return _Literal(1)
        if text.lower() == "false":
            return _Literal(0)
        if _INT_RE.match(text):
            try:
                return _Literal(int(text, 0))
            except ValueError:
                try:
                    return _Literal(int(text, 10))
                except ValueError:
                    return None
        return None
    if isinstance(value, Mapping):
        if _field(value, ("name", "id", "identifier"), None) is not None:
            return None
        for key in ("literal", "integer", "constant"):
            candidate = _field(value, (key,), _MISSING)
            if candidate is not _MISSING and not isinstance(candidate, bool):
                literal = _as_literal(candidate)
                if literal is not None:
                    return _Literal(
                        literal.value,
                        _width_of(_field(value, ("type", "ty", "width"), None)),
                    )
        candidate = _field(value, ("value",), _MISSING)
        if candidate is not _MISSING and not isinstance(
            candidate, (Mapping, list, tuple)
        ):
            literal = _as_literal(candidate)
            if literal is not None:
                return _Literal(
                    literal.value,
                    _width_of(_field(value, ("type", "ty", "width"), None)),
                )
        return None
    explicit = _field(value, ("is_constant", "is_literal"), False)
    if explicit:
        candidate = _field(value, ("value", "literal", "integer", "constant"), None)
        literal = _as_literal(candidate)
        if literal is not None:
            return _Literal(
                literal.value, _width_of(_field(value, ("type", "ty", "width"), None))
            )
    # Constant classes often expose ``value`` but no name.
    name = _field(value, ("name", "id", "identifier"), None)
    candidate = _field(value, ("value", "literal", "integer"), _MISSING)
    if name is None and candidate is not _MISSING:
        literal = _as_literal(candidate)
        if literal is not None:
            return _Literal(
                literal.value, _width_of(_field(value, ("type", "ty", "width"), None))
            )
    return None


def _normalise_opcode(value: Any) -> str:
    text = str(value).strip().lower()
    text = text.removeprefix("llvm.")
    text = text.replace("bitwise_", "")
    text = text.removesuffix(".void").removesuffix("_")
    if text.startswith(("icmp.", "icmp_")):
        return "icmp"
    for base in _SHIFT_OPS | _ROTATE_OPS:
        if text.startswith(f"{base}."):
            return base
    return text


def _parse_argument(raw: Any, index: int) -> tuple[str, int]:
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        first_width = _width_of(raw[0])
        second_width = _width_of(raw[1])
        if first_width is not None and second_width is None:
            name_raw, width_raw = raw[1], raw[0]
        else:
            name_raw, width_raw = raw[0], raw[1]
    else:
        name_raw = _field(raw, ("name", "id", "identifier"), None)
        width_raw = _field(raw, ("width", "bit_width", "bits", "type", "ty"), None)
        if name_raw is None and isinstance(raw, str):
            pieces = raw.split()
            name_raw = pieces[-1]
            if width_raw is None and pieces:
                width_raw = pieces[0]
    name = _normalise_name(name_raw) or f"arg{index}"
    width = _width_of(width_raw)
    if width is None:
        raise SSAFormatError(f"argument {name!r} has no fixed integer width")
    return name, width


def _extract_arguments(function: Any) -> list[tuple[str, int]]:
    raw = _field(function, ("arguments", "args", "params", "parameters"), _MISSING)
    if raw is _MISSING or raw is None:
        return []
    if isinstance(raw, Mapping):
        values: Iterable[Any] = tuple((name, value) for name, value in raw.items())
    elif isinstance(raw, str):
        values = (raw,)
    else:
        try:
            values = tuple(raw)
        except TypeError as exc:
            raise SSAFormatError("function arguments are not iterable") from exc
    return [_parse_argument(argument, index) for index, argument in enumerate(values)]


def _flatten_block(block: Any) -> list[Any]:
    instructions = _field(
        block, ("instructions", "ops", "operations", "body"), _MISSING
    )
    if instructions is _MISSING:
        if isinstance(block, (tuple, list)):
            return list(block)
        return [block]
    if instructions is None:
        return []
    if isinstance(instructions, Mapping):
        return list(instructions.values())
    return list(instructions)


def _block_name(raw: Any, fallback: str) -> str:
    if isinstance(raw, (tuple, list)) and raw:
        if len(raw) >= 2 and str(raw[0]).lower() == "label":
            raw = raw[-1]
        elif len(raw) == 1:
            raw = raw[0]
    candidate = _field(raw, ("name", "label", "identifier", "id"), _MISSING)
    if candidate is _MISSING:
        candidate = raw if isinstance(raw, str) else fallback
    text = str(candidate).strip()
    if text.lower().startswith("label "):
        text = text.split(None, 1)[1]
    normalized = _normalise_name(text.rstrip(":"))
    return normalized or fallback


def _extract_blocks(function: Any) -> tuple[_Block, ...]:
    """Extract named blocks while retaining the frontend's structural shape."""

    raw_blocks = _field(function, ("blocks", "basic_blocks"), _MISSING)
    block_items: list[tuple[str | None, Any]] = []
    if raw_blocks is not _MISSING and raw_blocks is not None:
        if isinstance(raw_blocks, Mapping):
            block_items.extend((str(name), block) for name, block in raw_blocks.items())
        else:
            try:
                block_items.extend((None, block) for block in tuple(raw_blocks))
            except TypeError as exc:
                raise SSAFormatError("function blocks are not iterable") from exc

    if not block_items:
        raw = _field(function, ("instructions", "ops", "operations"), _MISSING)
        if raw is _MISSING or raw is None:
            raw = _field(function, ("body",), _MISSING)
        if raw is _MISSING or raw is None:
            raw = function if isinstance(function, (tuple, list)) else ()
        block_items = [("entry", raw)]

    blocks: list[_Block] = []
    used_names: set[str] = set()
    for index, (mapping_name, raw_block) in enumerate(block_items):
        fallback = mapping_name or ("entry" if index == 0 else f"block{index}")
        name = _block_name(raw_block, fallback)
        if (
            mapping_name is not None
            and _field(raw_block, ("name", "label", "identifier", "id"), _MISSING)
            is _MISSING
        ):
            name = _block_name(mapping_name, fallback)
        if name in used_names:
            raise SSAFormatError(f"duplicate basic block {name!r}")
        used_names.add(name)
        operations = tuple(_parse_operation(raw) for raw in _flatten_block(raw_block))
        blocks.append(_Block(name, operations))
    return tuple(blocks)


def _opcode_candidate(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and not text.startswith(("%", "@"))


def _parse_operation(raw: Any) -> _Operation:
    if isinstance(raw, (tuple, list)):
        values = list(raw)
        if not values:
            raise SSAFormatError("empty SSA instruction")
        opcode_index = next(
            (
                index
                for index, item in enumerate(values[:4])
                if isinstance(item, str) and _normalise_opcode(item) in _SUPPORTED
            ),
            None,
        )
        if opcode_index is None:
            candidates = [
                index
                for index, item in enumerate(values[:2])
                if _opcode_candidate(item)
            ]
            if not candidates:
                raise SSAFormatError(f"cannot identify opcode in instruction {raw!r}")
            opcode_index = candidates[0]
        raw_opcode = str(values[opcode_index])
        raw_opcode_lower = raw_opcode.strip().lower()
        opcode = _normalise_opcode(raw_opcode)
        predicate = None
        if raw_opcode_lower.startswith(("icmp.", "icmp_")):
            predicate = raw_opcode_lower.split(".", 1)[-1].split("_", 1)[-1]
        before = values[:opcode_index]
        result = _normalise_name(before[-1]) if before else None
        operand_values = list(values[opcode_index + 1 :])
        width_spec: Any = None
        source_width_spec: Any = None
        if opcode == "icmp":
            if operand_values and isinstance(operand_values[0], str):
                candidate = operand_values[0].strip().lower()
                if candidate in _ICMP_PREDICATES:
                    predicate = candidate
                    operand_values.pop(0)
            if operand_values and _maybe_type_token(operand_values[0]):
                source_width_spec = operand_values.pop(0)
            elif operand_values and _maybe_type_token(operand_values[-1]):
                source_width_spec = operand_values.pop()
        elif opcode in {"zext", "sext", "trunc"}:
            if operand_values and _maybe_type_token(operand_values[0]):
                source_width_spec = operand_values.pop(0)
            if operand_values and _maybe_type_token(operand_values[-1]):
                width_spec = operand_values.pop()
        else:
            if operand_values and _maybe_type_token(operand_values[0]):
                width_spec = operand_values.pop(0)
            if (
                opcode
                in {
                    "add",
                    "sub",
                    "mul",
                    "udiv",
                    "urem",
                    "and",
                    "or",
                    "xor",
                    "const",
                    "constant",
                    "literal",
                    "phi",
                    "select",
                    *_SHIFT_OPS,
                    *_ROTATE_OPS,
                }
                and operand_values
                and _maybe_type_token(operand_values[-1])
            ):
                width_spec = width_spec or operand_values.pop()
        return _Operation(
            opcode,
            result,
            tuple(operand_values),
            width_spec,
            source_width_spec,
            predicate,
            None,
            raw,
        )

    opcode_raw = _field(raw, ("opcode", "op", "operation", "kind"), _MISSING)
    if opcode_raw is _MISSING:
        candidate = _field(raw, ("name",), _MISSING)
        if candidate is _MISSING:
            raise SSAFormatError(f"instruction has no opcode: {raw!r}")
        opcode_raw = candidate
    raw_opcode = str(opcode_raw)
    raw_opcode_lower = raw_opcode.strip().lower()
    opcode = _normalise_opcode(opcode_raw)
    predicate_raw = _field(
        raw,
        ("predicate", "pred", "condition_code", "comparison", "compare"),
        None,
    )
    predicate = str(predicate_raw).lower() if predicate_raw is not None else None
    if raw_opcode_lower.startswith(("icmp.", "icmp_")):
        predicate = raw_opcode_lower.split(".", 1)[-1].split("_", 1)[-1]

    result_raw = _field(raw, ("result", "destination", "dest", "output"), _MISSING)
    if result_raw is _MISSING and opcode not in _TERMINATORS:
        result_raw = _field(raw, ("name", "id", "identifier"), None)
    result = _normalise_name(result_raw)

    operands_raw = _field(raw, ("operands", "args", "arguments"), _MISSING)
    if operands_raw is _MISSING or operands_raw is None:
        if opcode in {"ret", "return"}:
            operands_raw = _field(raw, ("value", "return_value", "operand"), ())
        elif opcode in {"const", "constant", "literal"}:
            operands_raw = _field(raw, ("value", "literal", "constant", "integer"), ())
        elif opcode in {"zext", "sext", "trunc"}:
            operands_raw = _field(raw, ("operand", "value", "source"), ())
        elif opcode == "select":
            condition = _field(raw, ("condition", "cond", "predicate"), _MISSING)
            true_value = _field(
                raw,
                ("true_value", "on_true", "if_true", "true_operand", "true"),
                _MISSING,
            )
            false_value = _field(
                raw,
                ("false_value", "on_false", "if_false", "false_operand", "false"),
                _MISSING,
            )
            if (
                condition is not _MISSING
                and true_value is not _MISSING
                and false_value is not _MISSING
            ):
                operands_raw = (condition, true_value, false_value)
            else:
                operands_raw = ()
    if opcode == "br":
        # The canonical frontend stores branch destinations separately from
        # value operands.  Normalize them to the compact (condition, true,
        # false) shape consumed by the CFG scheduler.
        targets_raw = _field(
            raw, ("targets", "branch_targets", "successors", "branches"), _MISSING
        )
        if targets_raw is not _MISSING and targets_raw is not None:
            targets = tuple(targets_raw)
            existing = (
                tuple(operands_raw)
                if isinstance(operands_raw, (tuple, list))
                else (
                    ()
                    if operands_raw is _MISSING
                    or operands_raw is None
                    or operands_raw == ()
                    else (operands_raw,)
                )
            )
            operands_raw = existing + targets
        elif opcode == "phi":
            operands_raw = _field(
                raw,
                (
                    "incoming",
                    "incomings",
                    "incoming_values",
                    "incoming_operands",
                    "phi_incomings",
                    "values",
                ),
                (),
            )
        elif opcode in _SHIFT_OPS | _ROTATE_OPS:
            source = _field(
                raw, ("source", "value", "operand", "lhs", "left"), _MISSING
            )
            amount = _field(
                raw,
                ("shift_amount", "amount", "rhs", "right", "count"),
                _MISSING,
            )
            if source is not _MISSING and amount is not _MISSING:
                operands_raw = (source, amount)
            else:
                operands_raw = ()
        elif opcode == "br":
            condition = _field(raw, ("condition", "cond", "predicate"), _MISSING)
            true_target = _field(
                raw,
                ("true_target", "then_target", "if_true", "true_block", "then"),
                _MISSING,
            )
            false_target = _field(
                raw,
                ("false_target", "else_target", "if_false", "false_block", "else"),
                _MISSING,
            )
            target = _field(raw, ("target", "destination", "successor"), _MISSING)
            targets = _field(raw, ("targets", "successors", "branches"), _MISSING)
            if condition is not _MISSING and targets is not _MISSING:
                target_values = tuple(targets)
                operands_raw = (condition, *target_values)
            elif condition is not _MISSING and true_target is not _MISSING:
                operands_raw = (
                    (condition, true_target, false_target)
                    if false_target is not _MISSING
                    else (condition, true_target)
                )
            elif target is not _MISSING:
                operands_raw = (target,)
            else:
                operands_raw = ()
        else:
            lhs = _field(raw, ("lhs", "left", "first"), _MISSING)
            rhs = _field(raw, ("rhs", "right", "second"), _MISSING)
            if lhs is not _MISSING and rhs is not _MISSING:
                operands_raw = (lhs, rhs)
            else:
                operands_raw = ()
    operands: tuple[Any, ...]
    if isinstance(operands_raw, str) or not isinstance(
        operands_raw, (tuple, list, Mapping)
    ):
        has_operand = (
            operands_raw is not None
            and operands_raw is not _MISSING
            and operands_raw != ()
        )
        operands = (operands_raw,) if has_operand else ()
    elif isinstance(operands_raw, Mapping):
        operands = (operands_raw,)
    else:
        operands = tuple(operands_raw)

    width_spec = _field(
        raw,
        (
            "result_width",
            "to_width",
            "width",
            "bit_width",
            "result_type",
            "to_type",
            "type",
            "ty",
        ),
        None,
    )
    source_width_spec = _field(
        raw,
        (
            "source_width",
            "from_width",
            "operand_width",
            "source_type",
            "from_type",
            "operand_type",
            "comparison_type",
        ),
        None,
    )
    direction_raw = _field(raw, ("direction", "rotate_direction"), None)
    direction = str(direction_raw).lower() if direction_raw is not None else None
    return _Operation(
        opcode,
        result,
        operands,
        width_spec,
        source_width_spec,
        predicate,
        direction,
        raw,
    )


def _value_name(raw: Any) -> str | None:
    if isinstance(raw, str):
        text = raw.strip()
        return _normalise_name(text) if text.startswith(("%", "@")) else None
    return _normalise_name(_field(raw, ("name", "id", "identifier"), None))


def _maybe_type_token(raw: Any) -> bool:
    return (
        _width_of(raw) is not None
        and _value_name(raw) is None
        and _as_literal(raw) is None
    )


def _return_operands(operation: _Operation) -> tuple[Any, ...]:
    operands = operation.operands
    if len(operands) > 1 and _maybe_type_token(operands[0]):
        return operands[1:]
    return operands


def _explicit_return(function: Any) -> Any:
    for name in ("return_value", "returned_value", "return_operand"):
        value = _field(function, (name,), _MISSING)
        if value is not _MISSING:
            return value
    return _MISSING


def _comparison_width(operation: _Operation) -> int | None:
    width = _width_of(operation.source_width_spec)
    if width is not None:
        return width
    # Structural dictionaries often call the compared type simply ``width``.
    # The typed frontend dataclass instead exposes result type i1 and carries
    # the compared type in operand_type, so never infer a comparison width of 1
    # from that result field.
    candidate = _width_of(operation.width_spec)
    return candidate if candidate != 1 else None


class _Lowerer:
    def __init__(
        self,
        function: Any,
        function_name: str | None,
        *,
        reuse_ancillas: bool = False,
    ) -> None:
        self.function_name = function_name or _normalise_name(
            _field(function, ("name", "identifier"), None)
        )
        self.reuse_ancillas = reuse_ancillas
        self.allocator = AncillaAllocator(reuse=reuse_ancillas)
        self.scheduler = BennettScheduler()
        self.values: dict[str, _Value] = {}
        self.constants: dict[tuple[int, int], _Value] = {}
        self._producers: dict[str, _Producer] = {}
        self._remaining_uses: dict[str, int] = {}
        self._dependency_users: dict[str, int] = {}
        self._captured_dependencies: list[_Value] | None = None
        self._current_alias: _Value | None = None
        self._constant_counter = 0
        self._temporary_counter = 0
        self._returns: list[tuple[_Value, _Guard]] = []
        self._allocate_arguments(function)

    def _allocate_arguments(self, function: Any) -> None:
        for name, width in _extract_arguments(function):
            if name in self.values:
                raise SSAFormatError(f"duplicate function argument {name!r}")
            register = self.allocator.allocate_input(name, width)
            self.values[name] = _Value(name, width, register)

    def _fresh_label(self, prefix: str) -> str:
        label = f"{prefix}.{self._temporary_counter}"
        self._temporary_counter += 1
        return label

    def _error_context(self, operation: _Operation) -> str:
        function = f" in {self.function_name!r}" if self.function_name else ""
        return f"SSA operation {operation.opcode!r}{function}"

    @staticmethod
    def _source_location(operation: _Operation) -> Any:
        return _field(operation.source, ("source_location", "location"), None)

    def _unsupported(
        self,
        operation: _Operation,
        message: str,
        *,
        construct: str | None = None,
    ) -> None:
        raise UnsupportedConstructError(
            construct or f"SSA operation {operation.opcode!r}",
            message,
            self._source_location(operation),
        )

    def _capture_value(self, value: _Value) -> _Value:
        if self._captured_dependencies is not None:
            self._captured_dependencies.append(value)
        return value

    def _record_constant_producer(
        self,
        value: _Value,
        start: int,
        allocation_mark: int,
    ) -> None:
        if not self.reuse_ancillas:
            return
        self._producers[value.name] = _Producer(
            gates=self.scheduler.block(start),
            leases=self.allocator.allocations_since(allocation_mark),
            dependencies=(),
        )

    def _resolve(self, raw: Any, width_hint: int | None = None) -> _Value:
        if isinstance(raw, _Value):
            return self._capture_value(raw)
        name = _value_name(raw)
        if name is not None:
            if name not in self.values:
                raise SSAFormatError(f"unknown SSA value {name!r}")
            return self._capture_value(self.values[name])
        if isinstance(raw, str):
            normal = _normalise_name(raw)
            if normal in self.values:
                return self._capture_value(self.values[normal])
        literal = _as_literal(raw)
        if literal is None:
            raise SSAFormatError(
                f"operand {raw!r} is neither an SSA value nor an integer literal"
            )
        width = literal.width or width_hint
        if width is None:
            raise SSAFormatError(
                f"integer literal {literal.value!r} has no width; annotate the SSA type"
            )
        if width <= 0:
            raise SSAFormatError(f"integer literal width must be positive, got {width}")
        value = literal.value & ((1 << width) - 1)
        key = (width, value)
        existing = self.constants.get(key)
        if existing is not None:
            return self._capture_value(existing)
        name = f"const.{self._constant_counter}"
        self._constant_counter += 1
        allocation_mark = self.allocator.allocation_mark()
        start = self.scheduler.mark()
        register = self.allocator.allocate_constant(width, name)
        for bit, qubit in enumerate(register.bits):
            if value & (1 << bit):
                self.scheduler.x(qubit)
        result = _Value(name, width, register, value)
        self.constants[key] = result
        self._record_constant_producer(result, start, allocation_mark)
        return self._capture_value(result)

    def _allocate_result(self, operation: _Operation, width: int) -> _Value:
        name = operation.result
        if name is None:
            raise SSAFormatError(f"{self._error_context(operation)} has no SSA result")
        if name in self.values:
            raise SSAFormatError(f"SSA value {name!r} is defined more than once")
        register = self.allocator.allocate_value(f"ssa.{name}", width)
        result = _Value(name, width, register)
        self.values[name] = result
        return result

    @staticmethod
    def _mask(width: int) -> int:
        return (1 << width) - 1

    @staticmethod
    def _unique_values(values: Iterable[_Value]) -> tuple[_Value, ...]:
        result: list[_Value] = []
        seen: set[str] = set()
        for value in values:
            if value.name not in seen:
                result.append(value)
                seen.add(value.name)
        return tuple(result)

    def _in_place_add_source(
        self,
        operation: _Operation,
        first: _Value,
        second: _Value,
    ) -> _Value | None:
        """Return a dead first operand eligible for in-place modular addition.

        The proof is deliberately narrow: this is a straight-line operation,
        the first operand is a produced non-constant value, it is used exactly
        once more (by this operation), and the second register is distinct.
        Its producer history is retained as part of the new value's cleanup
        history, so overwriting the physical register cannot lose reversibility.
        """

        if not self.reuse_ancillas or operation.opcode != "add":
            return None
        if first.constant is not None or first.register.bits == second.register.bits:
            return None
        if operation.result is None or operation.result in self.values:
            return None
        producer = self._producers.get(first.name)
        if producer is None:
            return None
        occurrences = sum(
            1 for operand in operation.operands if _value_name(operand) == first.name
        )
        if occurrences != 1:
            return None
        if self._remaining_uses.get(first.name, 0) != occurrences:
            return None
        if self._dependency_users.get(first.name, 0) != 0:
            return None
        return first

    def _allocate_alias_result(self, operation: _Operation, source: _Value) -> _Value:
        name = operation.result
        if name is None:
            raise SSAFormatError(f"{self._error_context(operation)} has no SSA result")
        if name in self.values:
            raise SSAFormatError(f"SSA value {name!r} is defined more than once")
        result = _Value(name, source.width, source.register)
        self.values[name] = result
        self._current_alias = source
        return result

    def _install_producer(
        self,
        value: _Value,
        gates: tuple[Gate, ...],
        leases: tuple[Register, ...],
        dependencies: tuple[_Value, ...],
    ) -> None:
        alias = self._current_alias
        if alias is None:
            producer = _Producer(gates, leases, dependencies)
            self._producers[value.name] = producer
            for dependency in dependencies:
                self._dependency_users[dependency.name] = (
                    self._dependency_users.get(dependency.name, 0) + 1
                )
            self._remaining_uses.setdefault(value.name, 0)
            return

        previous = self._producers.pop(alias.name, None)
        if previous is None:
            raise SSAFormatError(
                f"cannot retain in-place history for SSA value {alias.name!r}"
            )
        previous_names = {dependency.name for dependency in previous.dependencies}
        merged = self._unique_values(
            (*previous.dependencies, *(item for item in dependencies if item != alias))
        )
        producer = _Producer(
            gates=previous.gates + gates,
            leases=previous.leases + leases,
            dependencies=merged,
        )
        self._producers[value.name] = producer
        for dependency in merged:
            if dependency.name not in previous_names:
                self._dependency_users[dependency.name] = (
                    self._dependency_users.get(dependency.name, 0) + 1
                )
        self._remaining_uses.setdefault(value.name, 0)

    def _cleanup_value(self, name: str) -> None:
        producer = self._producers.pop(name, None)
        if producer is None:
            return
        self.scheduler.extend(reversed(producer.gates))
        for lease in reversed(producer.leases):
            self.allocator.release(lease)
        for dependency in producer.dependencies:
            count = self._dependency_users.get(dependency.name, 0) - 1
            if count <= 0:
                self._dependency_users.pop(dependency.name, None)
            else:
                self._dependency_users[dependency.name] = count
        for key, constant in tuple(self.constants.items()):
            if constant.name == name:
                del self.constants[key]

    def _cleanup_available(self) -> None:
        """Uncompute dead values in dependency-safe reverse order."""

        while True:
            candidate = next(
                (
                    name
                    for name in self._producers
                    if self._remaining_uses.get(name, 0) == 0
                    and self._dependency_users.get(name, 0) == 0
                ),
                None,
            )
            if candidate is None:
                return
            self._cleanup_value(candidate)

    def _consume_operands(self, operation: _Operation) -> None:
        for operand in operation.operands:
            name = _value_name(operand)
            if name is not None:
                remaining = self._remaining_uses.get(name, 0) - 1
                if remaining < 0:
                    raise SSAFormatError(
                        f"SSA value {name!r} has more uses than its lifetime analysis"
                    )
                self._remaining_uses[name] = remaining

    def _emit_reusable_operation(self, operation: _Operation) -> None:
        self._current_alias = None
        self._captured_dependencies = []
        self._prepare_operation(operation)
        allocation_mark = self.allocator.allocation_mark()
        start = self.scheduler.mark()
        self._emit_operation_unconditional(operation, known_constant=True)
        gates = self.scheduler.block(start)
        dependencies = self._unique_values(self._captured_dependencies)
        # A literal resolved unusually late is registered as its own producer;
        # do not release its lease together with the enclosing SSA result.
        leases = tuple(
            lease
            for lease in self.allocator.allocations_since(allocation_mark)
            if lease.kind != "constant"
        )
        if operation.result is None:
            raise SSAFormatError(f"{self._error_context(operation)} has no SSA result")
        result = self.values.get(operation.result)
        if result is None:
            raise SSAFormatError(
                f"{self._error_context(operation)} did not define its SSA result"
            )
        self._install_producer(result, gates, leases, dependencies)
        self._captured_dependencies = None
        self._consume_operands(operation)
        self._cleanup_available()

    def _set_constant(self, target: _Value, value: int) -> None:
        value &= self._mask(target.width)
        for bit, qubit in enumerate(target.register.bits):
            if value & (1 << bit):
                self.scheduler.x(qubit)

    def _emit_xor(self, first: _Value, second: _Value, target: _Value) -> None:
        if first.constant is not None and second.constant is not None:
            self._set_constant(target, first.constant ^ second.constant)
        elif first.constant is not None:
            copy_bits(self.scheduler, second.register.bits, target.register.bits)
            for bit, qubit in enumerate(target.register.bits):
                if first.constant & (1 << bit):
                    self.scheduler.x(qubit)
        elif second.constant is not None:
            copy_bits(self.scheduler, first.register.bits, target.register.bits)
            for bit, qubit in enumerate(target.register.bits):
                if second.constant & (1 << bit):
                    self.scheduler.x(qubit)
        elif first.register.bits == second.register.bits:
            return
        else:
            xor_bits(self.scheduler, first.register, second.register, target.register)

    def _emit_and(self, first: _Value, second: _Value, target: _Value) -> None:
        if first.constant is not None and second.constant is not None:
            self._set_constant(target, first.constant & second.constant)
            return
        if first.constant is not None or second.constant is not None:
            constant, dynamic = (
                (first, second) if first.constant is not None else (second, first)
            )
            assert constant.constant is not None
            for bit, qubit in enumerate(target.register.bits):
                if constant.constant & (1 << bit):
                    self.scheduler.cx(dynamic.register.bits[bit], qubit)
            return
        if first.register.bits == second.register.bits:
            copy_bits(self.scheduler, first.register.bits, target.register.bits)
            return
        and_bits(self.scheduler, first.register, second.register, target.register)

    def _emit_or(self, first: _Value, second: _Value, target: _Value) -> None:
        if first.constant is not None and second.constant is not None:
            self._set_constant(target, first.constant | second.constant)
            return
        if first.constant is not None or second.constant is not None:
            constant, dynamic = (
                (first, second) if first.constant is not None else (second, first)
            )
            assert constant.constant is not None
            for bit, qubit in enumerate(target.register.bits):
                if constant.constant & (1 << bit):
                    self.scheduler.x(qubit)
                else:
                    self.scheduler.cx(dynamic.register.bits[bit], qubit)
            return
        if first.register.bits == second.register.bits:
            copy_bits(self.scheduler, first.register.bits, target.register.bits)
            return
        or_bits(self.scheduler, first.register, second.register, target.register)

    def _emit_binary(self, operation: _Operation) -> None:
        if len(operation.operands) != 2:
            raise SSAFormatError(
                f"{self._error_context(operation)} expects two operands, "
                f"got {len(operation.operands)}"
            )
        width_hint = _width_of(operation.width_spec)
        first = self._resolve(operation.operands[0], width_hint)
        second = self._resolve(operation.operands[1], width_hint or first.width)
        width = width_hint or first.width
        if first.width != width or second.width != width:
            raise SSAFormatError(
                f"{self._error_context(operation)} has mismatched widths "
                f"{first.width}, {second.width}, and result {width}"
            )
        alias = self._in_place_add_source(operation, first, second)
        target = (
            self._allocate_alias_result(operation, alias)
            if alias is not None
            else self._allocate_result(operation, width)
        )
        if operation.opcode == "mul":
            self._emit_multiply(first, second, target)
        elif operation.opcode == "add" and alias is not None:
            add_modular(
                self.scheduler,
                second.register,
                target.register,
                self.allocator,
                label=f"{target.name}.add-in-place",
            )
        elif operation.opcode == "add":
            add_registers(
                self.scheduler,
                first.register,
                second.register,
                target.register,
                self.allocator,
                label=f"{target.name}.add",
            )
        elif operation.opcode == "sub":
            sub_registers(
                self.scheduler,
                first.register,
                second.register,
                target.register,
                self.allocator,
                label=f"{target.name}.sub",
            )
        elif operation.opcode == "and":
            self._emit_and(first, second, target)
        elif operation.opcode == "or":
            self._emit_or(first, second, target)
        elif operation.opcode == "xor":
            # LLVM's canonical bitwise NOT is xor with an all-ones literal.
            if first.constant == self._mask(width):
                self._emit_not(second, target)
            elif second.constant == self._mask(width):
                self._emit_not(first, target)
            else:
                self._emit_xor(first, second, target)

    def _emit_multiply(self, first: _Value, second: _Value, target: _Value) -> None:
        """Lower fixed-width multiplication with constant specialization."""

        if first.constant is not None and second.constant is not None:
            self._set_constant(target, first.constant * second.constant)
            return
        if first.constant is not None or second.constant is not None:
            constant, dynamic = (
                (first, second) if first.constant is not None else (second, first)
            )
            assert constant.constant is not None
            workspace = None
            if (constant.constant & self._mask(target.width)) not in {0, 1}:
                workspace = self.allocator.allocate_ancilla(
                    target.width + 1, self._fresh_label(f"{target.name}.mul-constant")
                )
            multiply_by_constant_modular(
                self.scheduler,
                dynamic.register.bits,
                constant.constant,
                target.register.bits,
                None if workspace is None else workspace.bits,
            )
            return

        multiplier = second
        copied: Register | None = None
        if set(first.register.bits) & set(second.register.bits):
            copied = self.allocator.allocate_ancilla(
                second.width, self._fresh_label(f"{target.name}.mul-copy")
            )
            copy_bits(self.scheduler, second.register.bits, copied.bits)
            multiplier = _Value(
                self._fresh_label(f"{target.name}.mul-factor"),
                second.width,
                copied,
            )
        workspace = self.allocator.allocate_ancilla(
            target.width + 1, self._fresh_label(f"{target.name}.mul")
        )
        multiply_modular(
            self.scheduler,
            first.register.bits,
            multiplier.register.bits,
            target.register.bits,
            workspace=workspace.bits,
        )
        if copied is not None:
            copy_bits(self.scheduler, second.register.bits, copied.bits)

    def _emit_constant_divmod(self, operation: _Operation) -> None:
        """Lower unsigned division/remainder with a compile-time divisor."""

        if len(operation.operands) != 2:
            raise SSAFormatError(
                f"{self._error_context(operation)} expects two operands, "
                f"got {len(operation.operands)}"
            )
        width_hint = _width_of(operation.width_spec)
        dividend = self._resolve(operation.operands[0], width_hint)
        divisor = self._resolve(operation.operands[1], width_hint or dividend.width)
        width = width_hint or dividend.width
        if dividend.width != width or divisor.width != width:
            raise SSAFormatError(
                f"{self._error_context(operation)} has mismatched widths "
                f"{dividend.width}, {divisor.width}, and result {width}"
            )
        if divisor.constant is None:
            raise UnsupportedSSAOperation(
                f"{self._error_context(operation)} requires a nonzero "
                "compile-time constant divisor; general unsigned division "
                "is unsupported"
            )
        divisor_value = divisor.constant & self._mask(width)
        if divisor_value == 0:
            raise UnsupportedSSAOperation(
                f"{self._error_context(operation)} has a zero divisor; "
                "unsigned division by zero is unsupported"
            )
        target = self._allocate_result(operation, width)
        if dividend.constant is not None:
            value = (
                dividend.constant // divisor_value
                if operation.opcode == "udiv"
                else dividend.constant % divisor_value
            )
            self._set_constant(target, value)
            return

        quotient = self.allocator.allocate_ancilla(
            width, self._fresh_label(f"{target.name}.div-quotient")
        )
        remainder = self.allocator.allocate_ancilla(
            width, self._fresh_label(f"{target.name}.div-remainder")
        )
        workspace = self.allocator.allocate_ancilla(
            constant_divmod_workspace_width(width),
            self._fresh_label(f"{target.name}.div-work"),
        )
        start = self.scheduler.mark()
        constant_divmod(
            self.scheduler,
            dividend.register.bits,
            divisor_value,
            quotient.bits,
            remainder.bits,
            workspace=workspace.bits,
        )
        computation = self.scheduler.block(start)
        source = quotient if operation.opcode == "udiv" else remainder
        copy_bits(self.scheduler, source.bits, target.register.bits)
        self.scheduler.extend(reversed(computation))

    def _emit_not(self, source: _Value, target: _Value) -> None:
        if source.constant is not None:
            self._set_constant(target, ~source.constant)
            return
        copy_bits(self.scheduler, source.register.bits, target.register.bits)
        for qubit in target.register.bits:
            self.scheduler.x(qubit)

    def _emit_cast(self, operation: _Operation) -> None:
        if len(operation.operands) != 1:
            raise SSAFormatError(
                f"{self._error_context(operation)} expects one operand, "
                f"got {len(operation.operands)}"
            )
        source_width = _width_of(operation.source_width_spec)
        source = self._resolve(operation.operands[0], source_width)
        destination_width = _width_of(operation.width_spec)
        if destination_width is None:
            destination_width = _width_of(
                _field(
                    operation.source,
                    ("destination_type", "dest_type", "to_type", "result_type"),
                    None,
                )
            )
        if destination_width is None:
            raise SSAFormatError(
                f"{self._error_context(operation)} has no destination width"
            )
        if operation.opcode in {"zext", "sext"} and destination_width < source.width:
            raise SSAFormatError(
                f"{operation.opcode} destination is narrower than its source"
            )
        if operation.opcode == "trunc" and destination_width > source.width:
            raise SSAFormatError("trunc destination is wider than its source")
        target = self._allocate_result(operation, destination_width)
        copied = min(source.width, destination_width)
        copy_bits(
            self.scheduler,
            source.register.bits[:copied],
            target.register.bits[:copied],
        )
        if operation.opcode == "sext" and destination_width > source.width:
            sign = source.register.bits[-1]
            for qubit in target.register.bits[source.width :]:
                self.scheduler.cx(sign, qubit)

    def _emit_constant(self, operation: _Operation, *, known_constant: bool) -> None:
        if len(operation.operands) != 1:
            raise SSAFormatError(
                f"{self._error_context(operation)} expects one integer literal"
            )
        literal = _as_literal(operation.operands[0])
        if literal is None:
            raise SSAFormatError(
                f"{self._error_context(operation)} operand is not an integer literal"
            )
        width = _width_of(operation.width_spec) or literal.width
        if width is None:
            raise SSAFormatError(
                f"{self._error_context(operation)} has no integer width"
            )
        target = self._allocate_result(operation, width)
        self._set_constant(target, literal.value)
        # A value defined under a nontrivial path is not globally constant: it
        # is physically zero when that path is inactive.
        self.values[target.name] = _Value(
            target.name,
            target.width,
            target.register,
            literal.value & self._mask(width) if known_constant else None,
        )

    def _temporary_value(self, register: Register, prefix: str) -> _Value:
        return _Value(self._fresh_label(prefix), register.width, register)

    def _emit_unsigned_less(
        self, first: _Value, second: _Value, target: Register
    ) -> None:
        """Toggle ``target`` with ``first < second`` and clean comparison work.

        The construction walks from the most-significant bit downward.  A
        clean prefix register records that every more-significant pair is
        equal; each first-difference term is then a mutually exclusive
        multi-controlled X.  This is substantially smaller than materializing
        a full subtractor when the circuit already permits first-class MCX
        gates, while the later backend can decompose those gates if required.
        """

        if first.width != second.width:
            raise SSAFormatError("unsigned comparison operands have mismatched widths")
        difference = self.allocator.allocate_ancilla(
            first.width, self._fresh_label("icmp.difference")
        )
        prefix = self.allocator.allocate_ancilla(
            first.width, self._fresh_label("icmp.equal-prefix")
        )
        for first_bit, second_bit, difference_bit in zip(
            first.register.bits,
            second.register.bits,
            difference.bits,
            strict=True,
        ):
            self.scheduler.cx(first_bit, difference_bit)
            self.scheduler.cx(second_bit, difference_bit)

        most_significant = first.width - 1
        self.scheduler.x(prefix.bits[most_significant])
        self.scheduler.cx(
            difference.bits[most_significant], prefix.bits[most_significant]
        )
        for index in range(most_significant - 1, -1, -1):
            self.scheduler.x(difference.bits[index])
            self.scheduler.ccx(
                prefix.bits[index + 1],
                difference.bits[index],
                prefix.bits[index],
            )
            self.scheduler.x(difference.bits[index])

        first_msb = first.register.bits[most_significant]
        second_msb = second.register.bits[most_significant]
        self.scheduler.x(first_msb)
        self.scheduler.ccx(first_msb, second_msb, target.bits[0])
        self.scheduler.x(first_msb)
        for index in range(most_significant - 1, -1, -1):
            first_bit = first.register.bits[index]
            self.scheduler.x(first_bit)
            self._append_multi_controlled_x(
                (prefix.bits[index + 1], first_bit, second.register.bits[index]),
                target.bits[0],
            )
            self.scheduler.x(first_bit)

        for index in range(0, most_significant):
            self.scheduler.x(difference.bits[index])
            self.scheduler.ccx(
                prefix.bits[index + 1],
                difference.bits[index],
                prefix.bits[index],
            )
            self.scheduler.x(difference.bits[index])
        self.scheduler.cx(
            difference.bits[most_significant], prefix.bits[most_significant]
        )
        self.scheduler.x(prefix.bits[most_significant])
        for first_bit, second_bit, difference_bit in zip(
            reversed(first.register.bits),
            reversed(second.register.bits),
            reversed(difference.bits),
            strict=True,
        ):
            self.scheduler.cx(second_bit, difference_bit)
            self.scheduler.cx(first_bit, difference_bit)

    def _emit_signed_less(
        self, first: _Value, second: _Value, target: Register
    ) -> None:
        transformed_first = self.allocator.allocate_ancilla(
            first.width, self._fresh_label("icmp.signed-first")
        )
        transformed_second = self.allocator.allocate_ancilla(
            second.width, self._fresh_label("icmp.signed-second")
        )
        flag = self.allocator.allocate_ancilla(1, self._fresh_label("icmp.signed-flag"))
        start_transform = self.scheduler.mark()
        copy_bits(self.scheduler, first.register.bits, transformed_first.bits)
        copy_bits(self.scheduler, second.register.bits, transformed_second.bits)
        self.scheduler.x(transformed_first.bits[-1])
        self.scheduler.x(transformed_second.bits[-1])
        transform = self.scheduler.block(start_transform)
        compare_start = self.scheduler.mark()
        self._emit_unsigned_less(
            self._temporary_value(transformed_first, "icmp.signed-a"),
            self._temporary_value(transformed_second, "icmp.signed-b"),
            flag,
        )
        compare = self.scheduler.block(compare_start)
        copy_bits(self.scheduler, flag.bits, target.bits)
        self.scheduler.extend(reversed(compare))
        self.scheduler.extend(reversed(transform))

    def _emit_equal(self, first: _Value, second: _Value, target: Register) -> None:
        if first.width != second.width:
            raise SSAFormatError("equality operands have mismatched widths")
        difference = self.allocator.allocate_ancilla(
            first.width, self._fresh_label("icmp.eq-difference")
        )
        for first_bit, second_bit, difference_bit in zip(
            first.register.bits,
            second.register.bits,
            difference.bits,
            strict=True,
        ):
            self.scheduler.cx(first_bit, difference_bit)
            self.scheduler.cx(second_bit, difference_bit)
        for difference_bit in difference.bits:
            self.scheduler.x(difference_bit)
        self._append_multi_controlled_x(difference.bits, target.bits[0])
        for difference_bit in difference.bits:
            self.scheduler.x(difference_bit)
        for first_bit, second_bit, difference_bit in zip(
            reversed(first.register.bits),
            reversed(second.register.bits),
            reversed(difference.bits),
            strict=True,
        ):
            self.scheduler.cx(second_bit, difference_bit)
            self.scheduler.cx(first_bit, difference_bit)

    def _append_multi_controlled_x(self, controls: Sequence[int], target: int) -> None:
        control_tuple = tuple(controls)
        if target in control_tuple:
            raise SSAFormatError("a multi-controlled X target overlaps a control")
        if len(control_tuple) == 1:
            self.scheduler.cx(control_tuple[0], target)
        elif len(control_tuple) == 2:
            self.scheduler.ccx(control_tuple[0], control_tuple[1], target)
        else:
            self.scheduler.append(Gate("mcx", (*control_tuple, target)))

    def _emit_icmp(self, operation: _Operation) -> None:
        if len(operation.operands) != 2:
            raise SSAFormatError(
                f"{self._error_context(operation)} expects two operands, "
                f"got {len(operation.operands)}"
            )
        width_hint = _comparison_width(operation)
        first = self._resolve(operation.operands[0], width_hint)
        second = self._resolve(operation.operands[1], width_hint or first.width)
        if first.width != second.width:
            raise SSAFormatError(
                f"{self._error_context(operation)} has mismatched operand widths"
            )
        predicate = (operation.predicate or "").lower()
        if predicate not in _ICMP_PREDICATES:
            raise UnsupportedSSAOperation(
                f"{self._error_context(operation)} has unsupported predicate "
                f"{predicate!r}"
            )
        target = self._allocate_result(operation, 1)
        if predicate == "eq":
            self._emit_equal(first, second, target.register)
        elif predicate == "ne":
            self._emit_equal(first, second, target.register)
            self.scheduler.x(target.register.bits[0])
        else:
            signed = predicate.startswith("s")
            reverse = predicate in {"ugt", "ule", "sgt", "sle"}
            strict = predicate in {"ult", "ugt", "slt", "sgt"}
            left, right = (second, first) if reverse else (first, second)
            if signed:
                self._emit_signed_less(left, right, target.register)
            else:
                self._emit_unsigned_less(left, right, target.register)
            if not strict:
                self.scheduler.x(target.register.bits[0])

    def _emit_select(self, operation: _Operation) -> None:
        if len(operation.operands) != 3:
            raise SSAFormatError(
                f"{self._error_context(operation)} expects condition and two values"
            )
        condition = self._resolve(operation.operands[0], 1)
        true_value = self._resolve(
            operation.operands[1], _width_of(operation.width_spec)
        )
        false_value = self._resolve(operation.operands[2], true_value.width)
        if condition.width != 1:
            raise SSAFormatError(
                f"{self._error_context(operation)} condition is not i1"
            )
        if true_value.width != false_value.width:
            raise SSAFormatError(
                f"{self._error_context(operation)} has mismatched value widths"
            )
        target = self._allocate_result(operation, true_value.width)
        if condition.constant is not None:
            selected = true_value if condition.constant & 1 else false_value
            copy_bits(self.scheduler, selected.register.bits, target.register.bits)
            return
        condition_bit = condition.register.bits[0]
        copy_bits(self.scheduler, false_value.register.bits, target.register.bits)
        for true_bit, false_bit, target_bit in zip(
            true_value.register.bits,
            false_value.register.bits,
            target.register.bits,
            strict=True,
        ):
            if true_bit == false_bit:
                continue
            if false_bit == condition_bit:
                self.scheduler.cx(condition_bit, target_bit)
            else:
                self.scheduler.ccx(condition_bit, false_bit, target_bit)
            if true_bit == condition_bit:
                self.scheduler.cx(condition_bit, target_bit)
            else:
                self.scheduler.ccx(condition_bit, true_bit, target_bit)

    def _emit_shift(self, operation: _Operation) -> None:
        operands = operation.operands
        if operation.opcode in {"fshl", "fshr"}:
            if len(operands) != 3:
                raise SSAFormatError(
                    f"{self._error_context(operation)} expects two values and a shift"
                )
            first = self._resolve(operands[0], _width_of(operation.width_spec))
            second = self._resolve(operands[1], first.width)
            amount_raw = operands[2]
            if first.register.bits != second.register.bits and not (
                first.constant is not None and first.constant == second.constant
            ):
                self._unsupported(
                    operation,
                    "funnel shifts with distinct sources are not supported; "
                    "use a rotate with one source",
                    construct="funnel shift",
                )
            source = first
            rotate_right = operation.opcode == "fshr"
        else:
            if len(operands) != 2:
                raise SSAFormatError(
                    f"{self._error_context(operation)} expects a value and a constant"
                )
            source = self._resolve(operands[0], _width_of(operation.width_spec))
            amount_raw = operands[1]
            rotate_right = operation.opcode in {"ror", "rotr", "rotate_right"}
            if operation.opcode == "rotate":
                rotate_right = operation.direction in {"right", "ror", "rotr"}
        amount = self._resolve(amount_raw, source.width)
        if amount.constant is None:
            raise UnsupportedSSAOperation(
                f"{self._error_context(operation)} requires a constant shift amount"
            )
        literal = _as_literal(amount_raw)
        if literal is not None and literal.value < 0:
            raise SSAFormatError("shift amount cannot be negative")
        width = _width_of(operation.width_spec) or source.width
        if width != source.width:
            raise SSAFormatError(
                f"{self._error_context(operation)} result width differs from source"
            )
        target = self._allocate_result(operation, width)
        count = amount.constant
        if operation.opcode in _SHIFT_OPS:
            if operation.opcode == "shl":
                for index, source_bit in enumerate(source.register.bits):
                    destination = index + count
                    if destination < width:
                        self.scheduler.cx(source_bit, target.register.bits[destination])
            elif operation.opcode == "lshr":
                for index, source_bit in enumerate(source.register.bits):
                    destination = index - count
                    if destination >= 0:
                        self.scheduler.cx(source_bit, target.register.bits[destination])
            else:
                sign = source.register.bits[-1]
                for destination in range(width):
                    source_index = destination + count
                    if source_index < width:
                        self.scheduler.cx(
                            source.register.bits[source_index],
                            target.register.bits[destination],
                        )
                    else:
                        self.scheduler.cx(sign, target.register.bits[destination])
            return
        if operation.opcode in _ROTATE_OPS:
            if operation.opcode in {"fshl", "rol", "rotl", "rotate_left"}:
                rotate_right = False
            count %= width
            for index, source_bit in enumerate(source.register.bits):
                destination = (
                    (index - count) % width if rotate_right else (index + count) % width
                )
                self.scheduler.cx(source_bit, target.register.bits[destination])
            return
        raise UnsupportedSSAOperation(
            f"{self._error_context(operation)} is not a shift"
        )

    def _emit_phi(
        self,
        operation: _Operation,
        block_name: str,
        edge_guards: Mapping[tuple[str, str], list[_Guard]],
    ) -> None:
        incomings = self._phi_incomings(operation)
        if not incomings:
            raise SSAFormatError(
                f"{self._error_context(operation)} has no incoming values"
            )
        width_hint = _width_of(operation.width_spec)
        resolved: list[tuple[_Value, str]] = []
        for value_raw, predecessor in incomings:
            value = self._resolve(value_raw, width_hint)
            resolved.append((value, predecessor))
        width = width_hint or resolved[0][0].width
        if any(value.width != width for value, _ in resolved):
            raise SSAFormatError(
                f"{self._error_context(operation)} has mismatched widths"
            )
        target = self._allocate_result(operation, width)
        for value, predecessor in resolved:
            guards = edge_guards.get((predecessor, block_name), [])
            if not guards and isinstance(value, str):
                alternate = _block_name(value, "")
                guards = edge_guards.get((alternate, block_name), [])
                if guards:
                    value = self._resolve(predecessor, width)
            if not guards:
                raise SSAFormatError(
                    f"{self._error_context(operation)} has no edge from "
                    f"{predecessor!r} to {block_name!r}"
                )
            for guard in guards:
                self._copy_guarded(value.register.bits, target.register.bits, guard)

    def _phi_incomings(self, operation: _Operation) -> tuple[tuple[Any, str], ...]:
        raw_items: Any = operation.operands
        if len(operation.operands) == 1 and isinstance(
            operation.operands[0], (Mapping, list, tuple)
        ):
            first_item = operation.operands[0]
            if isinstance(first_item, (Mapping, list)) or not (
                len(first_item) >= 2
                and not isinstance(first_item[0], (tuple, list, Mapping))
            ):
                raw_items = first_item
        incoming_blocks = _field(
            operation.source,
            ("incoming_blocks", "predecessors", "incoming_labels"),
            _MISSING,
        )
        if isinstance(raw_items, Mapping):
            items: list[Any] = list(raw_items.items())
        else:
            try:
                items = list(raw_items)
            except TypeError as exc:
                raise SSAFormatError("phi incoming values are not iterable") from exc
        result: list[tuple[Any, str]] = []
        for index, item in enumerate(items):
            if isinstance(item, Mapping):
                value = _field(item, ("value", "operand", "incoming_value"), _MISSING)
                predecessor = _field(
                    item,
                    ("block", "predecessor", "pred", "label", "incoming_block"),
                    _MISSING,
                )
                if value is _MISSING or predecessor is _MISSING:
                    raise SSAFormatError(f"malformed phi incoming {item!r}")
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                value, predecessor = item[0], item[1]
            elif incoming_blocks is not _MISSING:
                blocks = tuple(incoming_blocks)
                if index >= len(blocks):
                    raise SSAFormatError(
                        "phi incoming block count does not match values"
                    )
                value, predecessor = item, blocks[index]
            else:
                raise SSAFormatError(f"malformed phi incoming {item!r}")
            result.append((value, _block_name(predecessor, f"incoming{index}")))
        return tuple(result)

    def _prepare_operation(self, operation: _Operation) -> None:
        """Resolve literals before predication captures the operation gates."""

        opcode = operation.opcode
        if opcode in {"add", "sub", "mul", "udiv", "urem", "and", "or", "xor", "icmp"}:
            width_hint = (
                _comparison_width(operation)
                if opcode == "icmp"
                else _width_of(operation.width_spec)
            )
            if len(operation.operands) >= 1:
                first = self._resolve(operation.operands[0], width_hint)
                if len(operation.operands) >= 2:
                    self._resolve(operation.operands[1], width_hint or first.width)
        elif opcode in {"zext", "sext", "trunc", "not", "copy", "mov", "identity"}:
            if operation.operands:
                self._resolve(
                    operation.operands[0],
                    _width_of(operation.source_width_spec)
                    or _width_of(operation.width_spec),
                )
        elif opcode == "select":
            if len(operation.operands) == 3:
                self._resolve(operation.operands[0], 1)
                width_hint = _width_of(operation.width_spec)
                value = self._resolve(operation.operands[1], width_hint)
                self._resolve(operation.operands[2], width_hint or value.width)
        elif opcode in _SHIFT_OPS | _ROTATE_OPS:
            if operation.operands:
                source = self._resolve(
                    operation.operands[0], _width_of(operation.width_spec)
                )
                if opcode in {"fshl", "fshr"} and len(operation.operands) >= 2:
                    self._resolve(operation.operands[1], source.width)
                    if len(operation.operands) >= 3:
                        self._resolve(operation.operands[2], source.width)
                elif len(operation.operands) >= 2:
                    self._resolve(operation.operands[1], source.width)
        elif opcode == "phi":
            width_hint = _width_of(operation.width_spec)
            for value, _ in self._phi_incomings(operation):
                self._resolve(value, width_hint)
        elif opcode in {"ret", "return"}:
            operands = _return_operands(operation)
            if operands:
                self._resolve(operands[0], _width_of(operation.width_spec))

    def _emit_operation_unconditional(
        self, operation: _Operation, *, known_constant: bool
    ) -> None:
        opcode = operation.opcode
        if opcode in {"const", "constant", "literal"}:
            self._emit_constant(operation, known_constant=known_constant)
        elif opcode in {"zext", "sext", "trunc"}:
            self._emit_cast(operation)
        elif opcode == "not":
            if len(operation.operands) != 1:
                raise SSAFormatError(
                    f"{self._error_context(operation)} expects one operand"
                )
            source = self._resolve(
                operation.operands[0], _width_of(operation.width_spec)
            )
            target = self._allocate_result(operation, source.width)
            self._emit_not(source, target)
        elif opcode in {"copy", "mov", "identity"}:
            if len(operation.operands) != 1:
                raise SSAFormatError(
                    f"{self._error_context(operation)} expects one operand"
                )
            source = self._resolve(
                operation.operands[0], _width_of(operation.width_spec)
            )
            target = self._allocate_result(operation, source.width)
            copy_bits(self.scheduler, source.register.bits, target.register.bits)
        elif opcode in {"add", "sub", "mul", "and", "or", "xor"}:
            self._emit_binary(operation)
        elif opcode in {"udiv", "urem"}:
            self._emit_constant_divmod(operation)
        elif opcode in {"sdiv", "srem"}:
            raise UnsupportedSSAOperation(
                f"{self._error_context(operation)} signed division or remainder are "
                "unsupported; only unsigned constant-divisor "
                "operations are supported"
            )
        elif opcode == "icmp":
            self._emit_icmp(operation)
        elif opcode == "select":
            self._emit_select(operation)
        elif opcode in _SHIFT_OPS | _ROTATE_OPS:
            self._emit_shift(operation)
        else:
            supported = ", ".join(sorted(_SUPPORTED - _TERMINATORS - {"phi"}))
            raise UnsupportedSSAOperation(
                f"unsupported {self._error_context(operation)}; "
                f"v0.3 supports {supported}"
            )

    def _emit_predicated(self, operation: _Operation, guard: _Guard) -> None:
        self._prepare_operation(operation)
        start = self.scheduler.mark()
        self._emit_operation_unconditional(
            operation, known_constant=guard.constant is True
        )
        raw_gates = self.scheduler.block(start)
        if guard.constant is True:
            return
        self.scheduler.discard_from(start)
        if guard.constant is False:
            return
        assert guard.bit is not None
        for gate in raw_gates:
            self._append_controlled_gate(guard.bit, gate)

    def _guard_for_value(self, value: _Value) -> _Guard:
        if value.width != 1:
            raise SSAFormatError("branch condition must have width one")
        constant = value.constant
        if constant is not None:
            return _Guard(constant=bool(constant & 1))
        return _Guard(bit=value.register.bits[0])

    def _and_guard(self, parent: _Guard, condition: _Guard, invert: bool) -> _Guard:
        desired = condition
        if invert:
            if desired.constant is not None:
                desired = _Guard(constant=not desired.constant)
            else:
                desired = _Guard(bit=desired.bit)
                # The complement is materialized below without changing the
                # condition register permanently.
        if parent.constant is False or desired.constant is False:
            return _Guard(constant=False)
        if desired.constant is True:
            return parent
        if parent.constant is True:
            target = self.allocator.allocate_ancilla(1, self._fresh_label("path.not"))
            if invert:
                self.scheduler.x(target.bits[0])
            assert condition.bit is not None
            self.scheduler.cx(condition.bit, target.bits[0])
            return _Guard(bit=target.bits[0])
        assert parent.bit is not None and condition.bit is not None
        if parent.bit == condition.bit:
            return _Guard(constant=not invert)
        target = self.allocator.allocate_ancilla(1, self._fresh_label("path.and"))
        if invert:
            self.scheduler.x(condition.bit)
        self.scheduler.ccx(parent.bit, condition.bit, target.bits[0])
        if invert:
            self.scheduler.x(condition.bit)
        return _Guard(bit=target.bits[0])

    def _or_bit(self, first: int, second: int, target: int) -> None:
        if first == second:
            self.scheduler.cx(first, target)
            return
        self.scheduler.cx(first, target)
        self.scheduler.cx(second, target)
        self.scheduler.ccx(first, second, target)

    def _or_guards(self, guards: Sequence[_Guard]) -> _Guard:
        dynamic: list[int] = []
        for guard in guards:
            if guard.constant is True:
                return _Guard(constant=True)
            if guard.bit is not None and guard.bit not in dynamic:
                dynamic.append(guard.bit)
        if not dynamic:
            return _Guard(constant=False)
        current = dynamic[0]
        for other in dynamic[1:]:
            target = self.allocator.allocate_ancilla(1, self._fresh_label("path.or"))
            self._or_bit(current, other, target.bits[0])
            current = target.bits[0]
        return _Guard(bit=current)

    def _copy_guarded(
        self, source: Sequence[int], target: Sequence[int], guard: _Guard
    ) -> None:
        if guard.constant is False:
            return
        if guard.constant is True:
            copy_bits(self.scheduler, source, target)
            return
        assert guard.bit is not None
        for source_bit, target_bit in zip(source, target, strict=True):
            if source_bit == target_bit:
                continue
            if source_bit == guard.bit:
                self.scheduler.cx(guard.bit, target_bit)
            else:
                self.scheduler.ccx(guard.bit, source_bit, target_bit)

    def _append_controlled_gate(self, guard: int, gate: Gate) -> None:
        controls = gate.controls
        target = gate.target
        if target == guard:
            raise SSAFormatError("a predicated operation would modify its path guard")
        if gate.name == "x":
            self.scheduler.cx(guard, target)
        elif gate.name == "cx":
            control = controls[0]
            if control == guard:
                self.scheduler.cx(control, target)
            else:
                self.scheduler.ccx(guard, control, target)
        elif gate.name == "ccx":
            if guard in controls:
                self.scheduler.ccx(controls[0], controls[1], target)
                return
            self.scheduler.append(
                Gate("mcx", (guard, controls[0], controls[1], target))
            )
        elif gate.name == "mcx":
            if guard in controls:
                self.scheduler.append(gate)
            else:
                self.scheduler.append(Gate("mcx", (guard, *controls, target)))
        else:  # pragma: no cover - Gate validates this before lowering sees it.
            raise SSAFormatError(f"cannot control unsupported gate {gate.name!r}")

    @staticmethod
    def _branch(operation: _Operation) -> tuple[Any | None, str, str | None]:
        operands = operation.operands
        if len(operands) == 1:
            return None, _block_name(operands[0], "target"), None
        if len(operands) == 3:
            return (
                operands[0],
                _block_name(operands[1], "true"),
                _block_name(operands[2], "false"),
            )
        raise SSAFormatError(
            "conditional branches require a condition, true target, and false target"
        )

    @staticmethod
    def _terminator(block: _Block) -> _Operation | None:
        terminator: _Operation | None = None
        for index, operation in enumerate(block.operations):
            if operation.opcode in _TERMINATORS:
                if index != len(block.operations) - 1:
                    raise SSAFormatError(
                        f"block {block.name!r} contains instructions after "
                        "its terminator"
                    )
                terminator = operation
        return terminator

    def _record_return(self, operation: _Operation, guard: _Guard) -> None:
        operands = _return_operands(operation)
        if len(operands) != 1:
            raise SSAFormatError(
                f"{self._error_context(operation)} expects one returned value"
            )
        value = self._resolve(operands[0], _width_of(operation.width_spec))
        self._returns.append((value, guard))

    def _supports_reuse(self, blocks: tuple[_Block, ...]) -> bool:
        """Prove that the straight-line lifetime policy applies."""

        if len(blocks) != 1:
            return False
        try:
            terminator = self._terminator(blocks[0])
        except SSAFormatError:
            return False
        if terminator is not None and terminator.opcode not in {"ret", "return"}:
            return False
        return all(
            operation.opcode not in {"phi", "br", "switch", "unreachable"}
            for operation in blocks[0].operations
        )

    def _reusable_use_counts(
        self,
        function: Any,
        block: _Block,
        terminator: _Operation | None,
    ) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for operation in block.operations:
            operands = (
                _return_operands(operation)
                if operation.opcode in {"ret", "return"}
                else operation.operands
            )
            for operand in operands:
                name = _value_name(operand)
                if name is not None:
                    counts[name] += 1
        if terminator is None:
            explicit = _explicit_return(function)
            if explicit is not _MISSING:
                if isinstance(explicit, (tuple, list)) and len(explicit) == 1:
                    explicit = explicit[0]
                name = _value_name(explicit)
                if name is not None:
                    counts[name] += 1
        return dict(counts)

    def _reusable_return(
        self,
        function: Any,
        terminator: _Operation | None,
    ) -> _Value:
        self._captured_dependencies = []
        if terminator is not None:
            self._prepare_operation(terminator)
            operands = _return_operands(terminator)
            if len(operands) != 1:
                raise SSAFormatError(
                    f"{self._error_context(terminator)} expects one returned value"
                )
            value = self._resolve(operands[0], _width_of(terminator.width_spec))
        else:
            explicit = _explicit_return(function)
            if explicit is _MISSING:
                raise SSAFormatError(
                    f"function {self.function_name or '<anonymous>'!r} has no "
                    "return value"
                )
            if isinstance(explicit, (tuple, list)) and len(explicit) == 1:
                explicit = explicit[0]
            return_width = _width_of(
                _field(function, ("return_type", "result_type", "type"), None)
            )
            value = self._resolve(explicit, return_width)
        self._captured_dependencies = None
        return value

    def _lower_reusable(
        self,
        function: Any,
        blocks: tuple[_Block, ...],
    ) -> LoweredCircuit:
        block = blocks[0]
        terminator = self._terminator(block)
        body = block.operations[:-1] if terminator is not None else block.operations
        self._remaining_uses = self._reusable_use_counts(function, block, terminator)
        for operation in body:
            self._emit_reusable_operation(operation)

        value = self._reusable_return(function, terminator)
        return_width = value.width
        declared_width = _width_of(
            _field(function, ("return_type", "result_type"), None)
        )
        if declared_width is not None and declared_width != return_width:
            raise SSAFormatError(
                f"return width {return_width} does not match declared width "
                f"{declared_width}"
            )

        output = self.allocator.allocate_output(
            return_width,
            name=f"{self.function_name or 'function'}.output",
        )
        copy_bits(self.scheduler, value.register.bits, output.bits)
        if value.name in self._remaining_uses:
            self._remaining_uses[value.name] -= 1
        self._cleanup_available()
        if self._producers:
            # An unhandled lifetime shape is never guessed at.  Re-run the
            # immutable v0.3 schedule rather than returning a partially cleaned
            # optimized circuit.
            return _Lowerer(
                function,
                self.function_name,
                reuse_ancillas=False,
            ).lower(function)
        layout = self.allocator.layout(output)
        return LoweredCircuit(
            gates=self.scheduler.gates,
            num_qubits=self.allocator.num_qubits,
            layout=layout,
            function_name=self.function_name,
        )

    def lower(self, function: Any) -> LoweredCircuit:
        blocks = _extract_blocks(function)
        if self.reuse_ancillas:
            if self._supports_reuse(blocks):
                return self._lower_reusable(function, blocks)
            return _Lowerer(
                function,
                self.function_name,
                reuse_ancillas=False,
            ).lower(function)
        return self._lower_baseline(function)

    def _lower_baseline(self, function: Any) -> LoweredCircuit:
        blocks = _extract_blocks(function)
        if not blocks:
            raise SSAFormatError("function has no basic blocks or instructions")
        by_name = {block.name: block for block in blocks}
        terminators = {block.name: self._terminator(block) for block in blocks}
        successors: dict[str, tuple[str, ...]] = {}
        for block in blocks:
            terminator = terminators[block.name]
            if terminator is None:
                if len(blocks) != 1:
                    raise SSAFormatError(
                        f"block {block.name!r} has no terminator in a multi-block CFG"
                    )
                successors[block.name] = ()
            elif terminator.opcode in {"ret", "return"}:
                successors[block.name] = ()
            elif terminator.opcode == "br":
                condition, true_target, false_target = self._branch(terminator)
                del condition
                if false_target is None:
                    targets: tuple[str, ...] = (true_target,)
                else:
                    if true_target == false_target:
                        raise UnsupportedConstructError(
                            "ambiguous branch",
                            "a conditional branch cannot use the same target for "
                            "both paths",
                            self._source_location(terminator),
                        )
                    targets = (true_target, false_target)
                if any(target not in by_name for target in targets):
                    raise SSAFormatError(
                        f"block {block.name!r} branches to an unknown block"
                    )
                successors[block.name] = targets
            elif terminator.opcode in {"switch", "unreachable"}:
                raise UnsupportedConstructError(
                    f"{terminator.opcode} control flow",
                    f"{terminator.opcode} control flow is outside the acyclic "
                    "diamond subset",
                    self._source_location(terminator),
                )
            else:
                raise UnsupportedSSAOperation(
                    f"unsupported terminator {terminator.opcode!r}"
                )

        indegree = {block.name: 0 for block in blocks}
        for targets in successors.values():
            for target in targets:
                indegree[target] += 1
        ready = [block.name for block in blocks if indegree[block.name] == 0]
        topological: list[str] = []
        while ready:
            name = ready.pop(0)
            topological.append(name)
            for target in successors[name]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if len(topological) != len(blocks):
            raise UnsupportedConstructError(
                "cyclic control flow",
                "cyclic CFGs and data-dependent loops are not supported by "
                "v0.2 lowering",
            )
        reachable: set[str] = set()
        pending = [blocks[0].name]
        while pending:
            name = pending.pop()
            if name in reachable:
                continue
            reachable.add(name)
            pending.extend(successors[name])
        if len(reachable) != len(blocks):
            raise UnsupportedConstructError(
                "unreachable basic block",
                "all basic blocks must be reachable from the entry block",
            )
        topological = [name for name in topological if name in reachable]

        incoming: dict[str, list[str]] = {block.name: [] for block in blocks}
        for predecessor, targets in successors.items():
            for target in targets:
                incoming[target].append(predecessor)
        edge_guards: dict[tuple[str, str], list[_Guard]] = {}
        block_guards: dict[str, _Guard] = {blocks[0].name: _Guard(constant=True)}

        for block_name in topological:
            block = by_name[block_name]
            if block_name == blocks[0].name:
                guard = block_guards[block_name]
            else:
                incoming_guards: list[_Guard] = []
                for predecessor in incoming[block_name]:
                    incoming_guards.extend(
                        edge_guards.get((predecessor, block_name), [])
                    )
                if not incoming_guards:
                    raise SSAFormatError(
                        f"block {block_name!r} has no computed incoming path"
                    )
                guard = self._or_guards(incoming_guards)
                block_guards[block_name] = guard

            terminator = terminators[block_name]
            body = block.operations[:-1] if terminator is not None else block.operations
            for operation in body:
                if operation.opcode == "phi":
                    self._prepare_operation(operation)
                    self._emit_phi(operation, block_name, edge_guards)
                elif operation.opcode in _SUPPORTED - _TERMINATORS:
                    self._emit_predicated(operation, guard)
                elif operation.opcode in {"sdiv", "srem"}:
                    raise UnsupportedSSAOperation(
                        f"{self._error_context(operation)} signed division or "
                        "remainder are unsupported; only unsigned constant-divisor "
                        "operations are supported"
                    )
                else:
                    supported = ", ".join(sorted(_SUPPORTED - _TERMINATORS))
                    raise UnsupportedSSAOperation(
                        f"unsupported {self._error_context(operation)}; "
                        f"v0.3 supports {supported}"
                    )

            if terminator is None:
                continue
            if terminator.opcode in {"ret", "return"}:
                self._prepare_operation(terminator)
                self._record_return(terminator, guard)
            elif terminator.opcode == "br":
                condition_raw, true_target, false_target = self._branch(terminator)
                if false_target is None:
                    edge_guards.setdefault((block_name, true_target), []).append(guard)
                else:
                    condition = self._resolve(condition_raw, 1)
                    condition_guard = self._guard_for_value(condition)
                    true_guard = self._and_guard(guard, condition_guard, False)
                    false_guard = self._and_guard(guard, condition_guard, True)
                    edge_guards.setdefault((block_name, true_target), []).append(
                        true_guard
                    )
                    edge_guards.setdefault((block_name, false_target), []).append(
                        false_guard
                    )

        if not self._returns:
            explicit = _explicit_return(function)
            if explicit is not _MISSING:
                if isinstance(explicit, (tuple, list)) and len(explicit) == 1:
                    explicit = explicit[0]
                return_width = _width_of(
                    _field(function, ("return_type", "result_type", "type"), None)
                )
                self._returns.append(
                    (self._resolve(explicit, return_width), _Guard(constant=True))
                )
        if not self._returns:
            raise SSAFormatError(
                f"function {self.function_name or '<anonymous>'!r} has no return value"
            )

        return_width = self._returns[0][0].width
        if any(value.width != return_width for value, _ in self._returns):
            raise SSAFormatError("return values have mismatched widths")
        declared_width = _width_of(
            _field(function, ("return_type", "result_type"), None)
        )
        if declared_width is not None and declared_width != return_width:
            raise SSAFormatError(
                f"return width {return_width} does not match declared width "
                f"{declared_width}"
            )

        output = self.allocator.allocate_output(
            return_width,
            name=f"{self.function_name or 'function'}.output",
        )
        computation = self.scheduler.gates
        for value, guard in self._returns:
            self._copy_guarded(value.register.bits, output.bits, guard)
        # Output copies are deliberately appended after all path predicates and
        # SSA values have been computed.  Reversing only the earlier schedule
        # Bennett-cleans every temporary without touching the output register.
        self.scheduler.extend(reversed(computation))
        layout = self.allocator.layout(output)
        return LoweredCircuit(
            gates=self.scheduler.gates,
            num_qubits=self.allocator.num_qubits,
            layout=layout,
            function_name=self.function_name,
        )


def lower_ssa(
    function: Any,
    *,
    function_name: str | None = None,
    optimization_level: int = 0,
    reuse_ancillas: bool | None = None,
) -> LoweredCircuit:
    """Lower one fixed-width SSA function into a Bennett-clean circuit.

    ``optimization_level >= 2`` selects the conservative straight-line
    lifetime allocator.  ``reuse_ancillas`` is an explicit switch for callers
    that use the lowering API directly; an unsafe CFG shape falls back to the
    v0.3 schedule.
    """

    if optimization_level < 0:
        raise ValueError("optimization_level must be non-negative")
    enabled = (
        optimization_level >= 2 if reuse_ancillas is None else bool(reuse_ancillas)
    )
    return _Lowerer(
        function,
        function_name,
        reuse_ancillas=enabled,
    ).lower(function)


def lower(
    function: Any,
    *,
    function_name: str | None = None,
    optimization_level: int = 0,
    reuse_ancillas: bool | None = None,
) -> LoweredCircuit:
    """Compatibility alias for :func:`lower_ssa`."""

    return lower_ssa(
        function,
        function_name=function_name,
        optimization_level=optimization_level,
        reuse_ancillas=reuse_ancillas,
    )


def ssa_to_reversible(
    function: Any,
    *,
    function_name: str | None = None,
    optimization_level: int = 0,
    reuse_ancillas: bool | None = None,
) -> LoweredCircuit:
    """Descriptive alias used by frontend integrations."""

    return lower_ssa(
        function,
        function_name=function_name,
        optimization_level=optimization_level,
        reuse_ancillas=reuse_ancillas,
    )


# Descriptive spellings used by small integrations and examples.
lower_function = lower_ssa
lower_to_reversible = lower_ssa


__all__ = [
    "LoweredCircuit",
    "LoweringError",
    "SSAFormatError",
    "UnsupportedSSAOperation",
    "lower",
    "lower_function",
    "lower_ssa",
    "lower_to_reversible",
    "ssa_to_reversible",
]
