"""Lower straight-line integer SSA into a Bennett-clean reversible circuit.

The frontend deliberately owns parsing C and LLVM.  This module only relies on
small structural conventions (``args``, ``instructions``, ``opcode``, and
``operands``), and accepts dictionaries as well as dataclass-like objects so
that the frontend and circuit packages can evolve independently.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .ancilla import AncillaAllocator, CircuitLayout, Register
from .bennett import (
    BennettScheduler,
    Gate,
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
    """Raised when v0.1 cannot lower an SSA opcode."""


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
    source: Any = None


@dataclass(frozen=True)
class _Value:
    name: str
    width: int
    register: Register
    constant: int | None = None


_MISSING = object()
_INT_RE = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F]+|0[bB][01]+|[0-9]+)$")
_WIDTH_RE = re.compile(r"(?:^|[^A-Za-z])(?:i|u|uint|int)([0-9]+)(?:$|[^0-9])")

_SUPPORTED = {
    "add",
    "sub",
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
    "ret",
    "return",
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
    if isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
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


def _extract_instructions(function: Any) -> list[Any]:
    raw = _field(function, ("instructions", "ops", "operations"), _MISSING)
    if raw is not _MISSING and raw is not None:
        if isinstance(raw, Mapping):
            return list(raw.values())
        return list(raw)
    blocks = _field(function, ("blocks", "basic_blocks"), _MISSING)
    if blocks is not _MISSING and blocks is not None:
        if isinstance(blocks, Mapping):
            blocks = tuple(blocks.values())
        blocks = tuple(blocks)
        if len(blocks) != 1:
            raise UnsupportedSSAOperation(
                "v0.1 lowering accepts one straight-line basic block; "
                f"got {len(blocks)} blocks"
            )
        return _flatten_block(blocks[0])
    body = _field(function, ("body",), _MISSING)
    if body is not _MISSING and body is not None:
        return _flatten_block(body)
    if isinstance(function, (tuple, list)):
        return list(function)
    return []


def _parse_operation(raw: Any) -> _Operation:
    if isinstance(raw, (tuple, list)):
        values = list(raw)
        if not values:
            raise SSAFormatError("empty SSA instruction")
        opcode_index = next(
            (
                index
                for index, item in enumerate(values[:3])
                if isinstance(item, str) and _normalise_opcode(item) in _SUPPORTED
            ),
            None,
        )
        if opcode_index is None:
            raise SSAFormatError(f"cannot identify opcode in instruction {raw!r}")
        opcode = _normalise_opcode(values[opcode_index])
        before = values[:opcode_index]
        result = _normalise_name(before[-1]) if before else None
        operands = tuple(values[opcode_index + 1 :])
        width_spec = None
        if opcode in {"zext", "sext", "trunc"} and len(operands) >= 2:
            width_spec = operands[-1]
            operands = operands[:-1]
        elif opcode in {"add", "sub", "and", "or", "xor"} and len(operands) >= 3:
            if (
                _width_of(operands[-1]) is not None
                and _as_literal(operands[-1]) is None
            ):
                width_spec = operands[-1]
                operands = operands[:-1]
        if operands and _maybe_type_token(operands[0]):
            width_spec = width_spec or operands[0]
            operands = operands[1:]
        return _Operation(opcode, result, operands, width_spec, source=raw)

    opcode_raw = _field(raw, ("opcode", "op", "operation", "kind"), _MISSING)
    if opcode_raw is _MISSING:
        candidate = _field(raw, ("name",), _MISSING)
        if candidate is _MISSING:
            raise SSAFormatError(f"instruction has no opcode: {raw!r}")
        opcode_raw = candidate
    opcode = _normalise_opcode(opcode_raw)
    result_raw = _field(raw, ("result", "destination", "dest", "output"), _MISSING)
    if result_raw is _MISSING and opcode not in {"ret", "return"}:
        # Some compact instruction objects use ``name`` for their SSA result.
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
        else:
            lhs = _field(raw, ("lhs", "left", "first"), _MISSING)
            rhs = _field(raw, ("rhs", "right", "second"), _MISSING)
            if lhs is not _MISSING and rhs is not _MISSING:
                operands_raw = (lhs, rhs)
            else:
                operands_raw = ()
    if isinstance(operands_raw, str) or not isinstance(operands_raw, (tuple, list)):
        has_operand = (
            operands_raw is not None
            and operands_raw is not _MISSING
            and operands_raw != ()
        )
        operands = (operands_raw,) if has_operand else ()
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
        ("source_width", "from_width", "operand_width", "source_type", "from_type"),
        None,
    )
    return _Operation(
        opcode,
        result,
        operands,
        width_spec,
        source_width_spec,
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


class _Lowerer:
    def __init__(self, function: Any, function_name: str | None) -> None:
        self.function_name = function_name or _normalise_name(
            _field(function, ("name", "identifier"), None)
        )
        self.allocator = AncillaAllocator()
        self.scheduler = BennettScheduler()
        self.values: dict[str, _Value] = {}
        self.constants: dict[tuple[int, int], _Value] = {}
        self._constant_counter = 0
        self._return_value: _Value | None = None
        self._allocate_arguments(function)

    def _allocate_arguments(self, function: Any) -> None:
        for name, width in _extract_arguments(function):
            if name in self.values:
                raise SSAFormatError(f"duplicate function argument {name!r}")
            register = self.allocator.allocate_input(name, width)
            self.values[name] = _Value(name, width, register)

    def _error_context(self, operation: _Operation) -> str:
        function = f" in {self.function_name!r}" if self.function_name else ""
        return f"SSA operation {operation.opcode!r}{function}"

    def _resolve(self, raw: Any, width_hint: int | None = None) -> _Value:
        if isinstance(raw, _Value):
            return raw
        name = _value_name(raw)
        if name is not None:
            if name not in self.values:
                raise SSAFormatError(f"unknown SSA value {name!r}")
            return self.values[name]
        if isinstance(raw, str):
            normal = _normalise_name(raw)
            if normal in self.values:
                return self.values[normal]
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
            return existing
        name = f"const.{self._constant_counter}"
        self._constant_counter += 1
        register = self.allocator.allocate_constant(width, name)
        for bit, qubit in enumerate(register.bits):
            if value & (1 << bit):
                self.scheduler.x(qubit)
        result = _Value(name, width, register, value)
        self.constants[key] = result
        return result

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
        target = self._allocate_result(operation, width)
        if operation.opcode == "add":
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
        if operation.opcode == "zext" and destination_width < source.width:
            raise SSAFormatError("zext destination is narrower than its source")
        if operation.opcode == "sext" and destination_width < source.width:
            raise SSAFormatError("sext destination is narrower than its source")
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

    def _emit_constant(self, operation: _Operation) -> None:
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
        # Constants are SSA values too, but their fixed value is useful for
        # recognizing NOT and simplifying bitwise operations.
        self.values[target.name] = _Value(
            target.name,
            target.width,
            target.register,
            literal.value & self._mask(width),
        )

    def _record_return(self, operation: _Operation) -> None:
        operands = _return_operands(operation)
        if len(operands) != 1:
            raise SSAFormatError(
                f"{self._error_context(operation)} expects one returned value"
            )
        self._return_value = self._resolve(operands[0])

    def lower(self, function: Any) -> LoweredCircuit:
        for raw_operation in _extract_instructions(function):
            operation = _parse_operation(raw_operation)
            if operation.opcode in {"ret", "return"}:
                self._record_return(operation)
                continue
            if operation.opcode not in _SUPPORTED:
                supported = ", ".join(sorted(_SUPPORTED - {"ret", "return"}))
                raise UnsupportedSSAOperation(
                    f"unsupported {self._error_context(operation)}; "
                    f"v0.1 supports {supported}"
                )
            if operation.opcode in {"const", "constant", "literal"}:
                self._emit_constant(operation)
            elif operation.opcode in {"zext", "sext", "trunc"}:
                self._emit_cast(operation)
            elif operation.opcode == "not":
                if len(operation.operands) != 1:
                    raise SSAFormatError(
                        f"{self._error_context(operation)} expects one operand"
                    )
                source = self._resolve(
                    operation.operands[0], _width_of(operation.width_spec)
                )
                target = self._allocate_result(operation, source.width)
                self._emit_not(source, target)
            elif operation.opcode in {"copy", "mov", "identity"}:
                if len(operation.operands) != 1:
                    raise SSAFormatError(
                        f"{self._error_context(operation)} expects one operand"
                    )
                source = self._resolve(
                    operation.operands[0], _width_of(operation.width_spec)
                )
                target = self._allocate_result(operation, source.width)
                copy_bits(self.scheduler, source.register.bits, target.register.bits)
            else:
                self._emit_binary(operation)

        if self._return_value is None:
            explicit = _explicit_return(function)
            if explicit is not _MISSING:
                if isinstance(explicit, (tuple, list)) and len(explicit) == 1:
                    explicit = explicit[0]
                self._return_value = self._resolve(explicit)
        if self._return_value is None:
            raise SSAFormatError(
                f"function {self.function_name or '<anonymous>'!r} has no return value"
            )

        output = self.allocator.allocate_output(
            self._return_value.width,
            name=f"{self.function_name or 'function'}.output",
        )
        computation = self.scheduler.gates
        copy_bits(
            self.scheduler,
            self._return_value.register.bits,
            output.bits,
        )
        # Every gate emitted before the output copy is part of the computation.
        # Reversing it restores input-independent SSA values, constants, and
        # arithmetic carries while leaving the copied output untouched.
        self.scheduler.extend(reversed(computation))
        layout = self.allocator.layout(output)
        return LoweredCircuit(
            gates=self.scheduler.gates,
            num_qubits=self.allocator.num_qubits,
            layout=layout,
            function_name=self.function_name,
        )


def lower_ssa(function: Any, *, function_name: str | None = None) -> LoweredCircuit:
    """Lower one straight-line SSA function into a Bennett-clean circuit."""

    return _Lowerer(function, function_name).lower(function)


def lower(function: Any, *, function_name: str | None = None) -> LoweredCircuit:
    """Compatibility alias for :func:`lower_ssa`."""

    return lower_ssa(function, function_name=function_name)


def ssa_to_reversible(
    function: Any, *, function_name: str | None = None
) -> LoweredCircuit:
    """Descriptive alias used by frontend integrations."""

    return lower_ssa(function, function_name=function_name)


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
