"""OpenQASM 3 emission for the primitive qcc circuit gates.

The compiler is split across several small worktrees.  This module therefore
keeps the backend deliberately independent of the concrete circuit classes:
it consumes the public ``Circuit``/``CircuitLayout`` shape and also accepts
the small gate records used by the simulator.  The normalization at the
boundary makes the emitted language and its validation deterministic while
leaving circuit construction to the core package.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class QASM3Error(ValueError):
    """Base error raised for invalid or unsupported OpenQASM output."""


class QASM3ValidationError(QASM3Error):
    """The generated OpenQASM does not satisfy the subset we emit."""


class UnsupportedGateError(QASM3Error):
    """A circuit contains a gate outside the supported OpenQASM subset."""


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REGISTER = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\[(?P<index>[0-9]+)\]$")
_QREG = re.compile(
    r"^qubit(?:\[(?P<size>[0-9]+)\])?\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*;$"
)
_QREF = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\[(?P<index>[0-9]+)\]$")
_GATE = re.compile(r"^(?P<name>[a-z][a-z0-9_]*)\s+(?P<args>[^;]+);$")

_GATE_ALIASES = {
    "x": "x",
    "not": "x",
    "pauli_x": "x",
    "cx": "cx",
    "cnot": "cx",
    "controlled_x": "cx",
    "ccx": "ccx",
    "toffoli": "ccx",
    "controlled_controlled_x": "ccx",
}


@dataclass(frozen=True)
class _Register:
    name: str
    size: int


@dataclass(frozen=True)
class _Operation:
    name: str
    qubits: tuple[Any, ...]


def _read_member(value: Any, *names: str, default: Any = None) -> Any:
    """Read the first present field from an object or mapping."""

    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _as_size(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    size = _read_member(value, "size", "width", "length", "n", default=None)
    if isinstance(size, int) and not isinstance(size, bool):
        return size
    return None


def _bits_from_value(value: Any) -> tuple[int, ...]:
    """Read physical qubit indices from common register records."""

    if isinstance(value, slice):
        start = value.start or 0
        stop = value.stop
        if isinstance(start, int) and isinstance(stop, int) and stop >= start:
            return tuple(range(start, stop))
        return ()
    nested = _read_member(value, "bits", "qubits", "values", default=None)
    if nested is not None and nested is not value:
        return _bits_from_value(nested)
    if isinstance(value, (str, bytes, Mapping)):
        return ()
    try:
        values = tuple(value)
    except TypeError:
        return ()
    if all(isinstance(item, int) and not isinstance(item, bool) for item in values):
        return tuple(int(item) for item in values)
    return ()


def _register_from_item(
    item: Any, fallback_name: str | None = None
) -> _Register | None:
    """Convert common register records into the backend's small record."""

    if isinstance(item, (tuple, list)) and fallback_name is not None:
        bits = _bits_from_value(item)
        if bits:
            return _Register(fallback_name, len(bits))
    if isinstance(item, tuple) and len(item) == 2:
        name, size = item
        if isinstance(name, str):
            size_int = _as_size(size)
            if size_int is not None:
                return _Register(name, size_int)

    if isinstance(item, Mapping):
        name = item.get("name", item.get("register", fallback_name))
        size = item.get("size", item.get("width", item.get("length")))
    else:
        name = _read_member(item, "name", "register", default=fallback_name)
        size = _read_member(item, "size", "width", "length", "n", default=None)
    if size is None and isinstance(item, slice):
        start = item.start or 0
        stop = item.stop
        if isinstance(start, int) and isinstance(stop, int):
            size = stop - start
    if size is None and fallback_name is not None:
        bits = _bits_from_value(item)
        if bits:
            size = len(bits)

    if isinstance(name, str):
        size_int = _as_size(size)
        if size_int is not None:
            return _Register(name, size_int)
    return None


def _registers_from_value(value: Any) -> list[_Register]:
    """Extract an ordered register list from a mapping or register records."""

    if value is None:
        return []
    if isinstance(value, Mapping):
        result: list[_Register] = []
        for name, item in value.items():
            register = _register_from_item(item, str(name))
            if (
                register is None
                and isinstance(item, int)
                and not isinstance(item, bool)
            ):
                register = _Register(str(name), item)
            if register is not None:
                result.append(register)
        return result
    if isinstance(value, (str, bytes)):
        return []
    if isinstance(value, (Sequence, Iterable)):
        result = []
        for item in value:
            register = _register_from_item(item)
            if register is not None:
                result.append(register)
        return result
    register = _register_from_item(value)
    return [register] if register is not None else []


def _layout_value(layout: Any, *names: str) -> Any:
    for name in names:
        value = _read_member(layout, name, default=None)
        if value is not None:
            return value
    return None


def _layout_registers(layout: Any) -> list[_Register]:
    if layout is None:
        return []

    # Some lightweight layout implementations are mappings themselves, e.g.
    # {"input": 8, "ancilla": 1}.
    if isinstance(layout, Mapping):
        direct = _registers_from_value(layout)
        if direct:
            return direct

    # Prefer a single ordered collection when the layout exposes one.
    result: list[_Register] = []
    for name in ("registers", "quantum_registers", "qregs", "qubit_registers"):
        result.extend(_registers_from_value(_layout_value(layout, name)))
        if result:
            break

    # Other layout implementations keep input/output/ancilla registers in
    # separate fields.  Preserve that field order for stable output.
    declared_bits: set[int] = set()
    for name in ("registers", "quantum_registers", "qregs", "qubit_registers"):
        mapping = _layout_value(layout, name)
        if isinstance(mapping, Mapping):
            for item in mapping.values():
                declared_bits.update(_bits_from_value(item))
    for name in ("inputs", "input_registers", "outputs", "output_registers"):
        mapping = _layout_value(layout, name)
        if isinstance(mapping, Mapping):
            for item in mapping.values():
                declared_bits.update(_bits_from_value(item))

    seen_names = {register.name for register in result}
    for name in (
        "inputs",
        "input_registers",
        "outputs",
        "output_registers",
        "output",
        "ancillas",
        "ancilla_registers",
        "temporaries",
        "temporary_registers",
    ):
        value = _layout_value(layout, name)
        bits = _bits_from_value(value)
        candidates = _registers_from_value(value)
        if not candidates and name in {"output", "ancillas"}:
            if bits and set(bits).issubset(declared_bits):
                candidates = []
            else:
                register = _register_from_item(value, name.rstrip("s"))
                candidates = [register] if register is not None else []
        for register in candidates:
            if register.name not in seen_names:
                result.append(register)
                seen_names.add(register.name)
        declared_bits.update(bits)

    # A bare ancilla tuple has no name in the lowering layout.  Declare the
    # portions not already covered by named registers as one deterministic
    # register; _layout_qubit_map gives those physical bits their offsets.
    covered: set[int] = set()
    for name in ("registers", "quantum_registers", "qregs", "qubit_registers"):
        mapping = _layout_value(layout, name)
        if isinstance(mapping, Mapping):
            for item in mapping.values():
                covered.update(_bits_from_value(item))
    for name in ("inputs", "input_registers", "outputs", "output_registers"):
        mapping = _layout_value(layout, name)
        if isinstance(mapping, Mapping):
            for item in mapping.values():
                covered.update(_bits_from_value(item))
    output_bits = _bits_from_value(_layout_value(layout, "output"))
    covered.update(output_bits)
    ancilla_bits = _bits_from_value(_layout_value(layout, "ancillas"))
    missing_ancillas = tuple(bit for bit in ancilla_bits if bit not in covered)
    if missing_ancillas and "ancilla" not in seen_names:
        result.append(_Register("ancilla", len(missing_ancillas)))
        seen_names.add("ancilla")

    total = _layout_value(layout, "num_qubits", "n_qubits", "total_qubits", "width")
    if not result and isinstance(total, int) and total >= 0:
        return [_Register("q", total)]
    return result


def _circuit_layout(circuit: Any, layout: Any | None) -> Any | None:
    if layout is not None:
        return layout
    return _read_member(circuit, "layout", "circuit_layout", default=None)


def _operations_from(circuit: Any) -> list[Any]:
    if circuit is None:
        return []
    if isinstance(circuit, Mapping):
        for name in ("operations", "gates", "ops", "instructions"):
            if name in circuit:
                value = circuit[name]
                return (
                    list(value)
                    if not isinstance(value, Mapping)
                    else list(value.values())
                )
    for name in ("operations", "gates", "ops", "instructions", "netlist"):
        value = _read_member(circuit, name, default=None)
        if value is not None:
            if callable(value):
                value = value()
            return list(value)
    if isinstance(circuit, (str, bytes)):
        return []
    try:
        return list(circuit)
    except TypeError:
        return []


def _operation_name(operation: Any) -> str:
    if isinstance(operation, Mapping):
        value = operation.get("name", operation.get("gate", operation.get("opcode")))
    elif isinstance(operation, (tuple, list)) and operation:
        value = operation[0]
    else:
        value = _read_member(operation, "name", "gate", "opcode", "kind", default=None)
    if hasattr(value, "name"):
        value = value.name
    if not isinstance(value, str):
        raise QASM3Error(f"cannot determine gate name from {operation!r}")
    return value.lower().replace("-", "_")


def _operation_qubits(operation: Any) -> tuple[Any, ...]:
    if isinstance(operation, Mapping):
        value = operation.get(
            "qubits",
            operation.get("wires", operation.get("targets", operation.get("operands"))),
        )
        if value is not None:
            return tuple(value)
        controls = operation.get("controls")
        target = operation.get("target")
    elif isinstance(operation, (tuple, list)):
        return tuple(operation[1:])
    else:
        value = _read_member(
            operation,
            "qubits",
            "wires",
            "targets",
            "operands",
            "indices",
            default=None,
        )
        if value is not None:
            return tuple(value)
        controls = _read_member(operation, "controls", default=None)
        target = _read_member(operation, "target", default=None)

    if controls is not None and target is not None:
        if isinstance(controls, (str, bytes)):
            controls = (controls,)
        return tuple(controls) + (target,)
    if target is not None:
        return (target,)
    raise QASM3Error(f"cannot determine qubits from {operation!r}")


def _normalize_operations(circuit: Any) -> list[_Operation]:
    result = []
    for raw in _operations_from(circuit):
        name = _operation_name(raw)
        canonical = _GATE_ALIASES.get(name)
        if canonical is None:
            if name in {"mcx", "multi_controlled_x", "multi_x"}:
                raise UnsupportedGateError(
                    "MCX is not supported by the OpenQASM 3 primitive backend; "
                    "decompose it to X/CX/CCX before emission"
                )
            raise UnsupportedGateError(f"unsupported gate for OpenQASM 3: {name}")
        qubits = _operation_qubits(raw)
        expected = {"x": 1, "cx": 2, "ccx": 3}[canonical]
        if len(qubits) != expected:
            raise QASM3Error(
                f"gate {name!r} expects {expected} qubits, got {len(qubits)}"
            )
        result.append(_Operation(canonical, qubits))
    return result


def _qubit_token(value: Any) -> tuple[str, int] | None:
    if isinstance(value, str):
        match = _QREF.fullmatch(value.strip())
        if match:
            return match.group("name"), int(match.group("index"))
        return None
    if (
        isinstance(value, (tuple, list))
        and len(value) == 2
        and isinstance(value[0], str)
    ):
        index = _as_size(value[1])
        if index is not None:
            return value[0], index
    name = _read_member(value, "register", "reg", "name", default=None)
    index = _read_member(value, "index", "offset", "position", default=None)
    if isinstance(name, str) and isinstance(index, int) and not isinstance(index, bool):
        return name, index
    return None


def _layout_qubit_map(layout: Any) -> dict[Any, tuple[str, int]]:
    if layout is None:
        return {}
    result: dict[Any, tuple[str, int]] = {}

    def add_registers(value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        for name, item in value.items():
            if not isinstance(name, str):
                continue
            for offset, qubit in enumerate(_bits_from_value(item)):
                result.setdefault(qubit, (name, offset))

    if isinstance(layout, Mapping):
        add_registers(layout)
    for name in ("registers", "quantum_registers", "qregs", "qubit_registers"):
        add_registers(_layout_value(layout, name))
    for name in ("inputs", "input_registers", "outputs", "output_registers"):
        add_registers(_layout_value(layout, name))

    output_bits = _bits_from_value(_layout_value(layout, "output"))
    for offset, qubit in enumerate(output_bits):
        result.setdefault(qubit, ("output", offset))
    ancilla_bits = _bits_from_value(_layout_value(layout, "ancillas"))
    for offset, qubit in enumerate(ancilla_bits):
        result.setdefault(qubit, ("ancilla", offset))

    for name in (
        "qubit_map",
        "index_to_qubit",
        "wire_map",
        "physical_qubits",
        "qubits_by_index",
    ):
        mapping = _layout_value(layout, name)
        if not isinstance(mapping, Mapping):
            continue
        for key, value in mapping.items():
            token = _qubit_token(value)
            if token is not None:
                result[key] = token
    return result


def _resolve_qubit(
    value: Any,
    layout: Any,
    registers: Sequence[_Register],
    qubit_map: Mapping[Any, tuple[str, int]],
) -> tuple[str, int]:
    direct = _qubit_token(value)
    if direct is not None:
        return direct
    try:
        if value in qubit_map:
            return qubit_map[value]
    except TypeError:
        pass

    # Let a richer CircuitLayout resolve its own physical index first.
    if layout is not None:
        for method_name in ("resolve_qubit", "qubit_name", "format_qubit"):
            method = _read_member(layout, method_name, default=None)
            if callable(method):
                try:
                    resolved = method(value)
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                token = _qubit_token(resolved)
                if token is not None:
                    return token
                if isinstance(resolved, str):
                    direct = _qubit_token(resolved)
                    if direct is not None:
                        return direct

    if isinstance(value, int) and not isinstance(value, bool):
        if registers:
            offset = value
            for register in registers:
                if offset < register.size:
                    return register.name, offset
                offset -= register.size
        return "q", value
    raise QASM3Error(f"cannot resolve qubit reference {value!r}")


def _validate_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.fullmatch(name):
        raise QASM3Error(f"invalid OpenQASM {kind} identifier: {name!r}")


def _normalized_registers(
    layout: Any,
    operations: Sequence[_Operation],
) -> tuple[list[_Register], dict[Any, tuple[str, int]]]:
    registers = _layout_registers(layout)
    explicit_registers = bool(registers)
    seen: set[str] = set()
    for register in registers:
        _validate_identifier(register.name, "register")
        if register.size < 1:
            raise QASM3Error(f"register {register.name!r} must have positive size")
        if register.name in seen:
            raise QASM3Error(f"duplicate quantum register {register.name!r}")
        seen.add(register.name)

    qubit_map = _layout_qubit_map(layout)
    resolved: list[tuple[str, int]] = []
    for operation in operations:
        for value in operation.qubits:
            token = _resolve_qubit(value, layout, registers, qubit_map)
            resolved.append(token)
            if token[0] not in seen:
                if registers:
                    raise QASM3Error(
                        f"qubit refers to undeclared register {token[0]!r}"
                    )
                registers.append(_Register(token[0], token[1] + 1))
                seen.add(token[0])
            else:
                register_index = next(
                    index
                    for index, item in enumerate(registers)
                    if item.name == token[0]
                )
                register = registers[register_index]
                if token[1] >= register.size:
                    if explicit_registers:
                        raise QASM3Error(
                            f"qubit index {token[1]} is outside register "
                            f"{register.name!r}[{register.size}]"
                        )
                    registers[register_index] = _Register(register.name, token[1] + 1)

    # If there is no layout and only integer refs, one inferred q register is
    # enough.  Grow it to the largest referenced index.
    if not registers and resolved:
        registers.append(_Register("q", max(index for _, index in resolved) + 1))
    return registers, qubit_map


def emit_qasm3(circuit: Any, layout: Any | None = None, validate: bool = True) -> str:
    """Emit an OpenQASM 3 program for a primitive circuit.

    ``layout`` defaults to ``circuit.layout``.  Registers in a
    :class:`CircuitLayout` are emitted in their declared order, and integer
    wire indices are mapped over those registers in that same order.
    """

    operations = _normalize_operations(circuit)
    actual_layout = _circuit_layout(circuit, layout)
    if actual_layout is None:
        total_qubits = _read_member(
            circuit, "num_qubits", "n_qubits", "total_qubits", default=None
        )
        if isinstance(total_qubits, int) and total_qubits > 0:
            actual_layout = {"q": total_qubits}
    registers, qubit_map = _normalized_registers(actual_layout, operations)

    lines = ["OPENQASM 3.0;", 'include "stdgates.inc";', ""]
    for register in registers:
        lines.append(f"qubit[{register.size}] {register.name};")
    if registers and operations:
        lines.append("")

    for operation in operations:
        operands = []
        for value in operation.qubits:
            name, index = _resolve_qubit(value, actual_layout, registers, qubit_map)
            operands.append(f"{name}[{index}]")
        lines.append(f"{operation.name} {', '.join(operands)};")

    result = "\n".join(lines) + "\n"
    if validate:
        validate_qasm3(result)
    return result


def to_qasm3(circuit: Any, layout: Any | None = None, validate: bool = True) -> str:
    """Compatibility alias for :func:`emit_qasm3`."""

    return emit_qasm3(circuit, layout=layout, validate=validate)


def _strip_qasm_comment(line: str) -> str:
    return line.split("//", 1)[0].strip()


def validate_qasm3(source: str, *, expected_layout: Any | None = None) -> bool:
    """Perform a small deterministic syntax and register-range validation.

    This is intentionally a validator for the subset emitted by qcc, not a
    replacement for a complete OpenQASM parser.  It catches malformed headers,
    duplicate or invalid declarations, unknown instructions, and out-of-range
    qubit references without requiring Qiskit.
    """

    if not isinstance(source, str):
        raise QASM3ValidationError("OpenQASM source must be text")
    lines = [_strip_qasm_comment(line) for line in source.splitlines()]
    lines = [line for line in lines if line]
    if not lines or lines[0] != "OPENQASM 3.0;":
        raise QASM3ValidationError("OpenQASM 3 source must start with 'OPENQASM 3.0;'")

    declared_registers: dict[str, int] = {}
    saw_include = False
    for line_number, line in enumerate(lines[1:], start=2):
        if line == 'include "stdgates.inc";':
            if saw_include:
                raise QASM3ValidationError(
                    f"line {line_number}: duplicate stdgates include"
                )
            saw_include = True
            continue
        match = _QREG.fullmatch(line)
        if match:
            name = match.group("name")
            size = int(match.group("size") or "1")
            if size < 1:
                raise QASM3ValidationError(
                    f"line {line_number}: register size must be positive"
                )
            if name in declared_registers:
                raise QASM3ValidationError(
                    f"line {line_number}: duplicate register {name!r}"
                )
            declared_registers[name] = size
            continue
        if line.startswith("qubit"):
            raise QASM3ValidationError(
                f"line {line_number}: malformed qubit declaration"
            )

        gate = _GATE.fullmatch(line)
        if not gate:
            raise QASM3ValidationError(
                f"line {line_number}: unsupported or malformed statement"
            )
        name = gate.group("name")
        if name not in {"x", "cx", "ccx"}:
            raise QASM3ValidationError(f"line {line_number}: unsupported gate {name!r}")
        operands = [item.strip() for item in gate.group("args").split(",")]
        expected = {"x": 1, "cx": 2, "ccx": 3}[name]
        if len(operands) != expected or any(not item for item in operands):
            raise QASM3ValidationError(
                f"line {line_number}: {name} expects {expected} qubit operands"
            )
        for operand in operands:
            reference = _QREF.fullmatch(operand)
            if not reference:
                raise QASM3ValidationError(
                    f"line {line_number}: malformed qubit reference {operand!r}"
                )
            register = reference.group("name")
            index = int(reference.group("index"))
            if register not in declared_registers:
                raise QASM3ValidationError(
                    f"line {line_number}: undeclared register {register!r}"
                )
            if index >= declared_registers[register]:
                raise QASM3ValidationError(
                    f"line {line_number}: {register}[{index}] is out of range"
                )

    if not saw_include:
        raise QASM3ValidationError('OpenQASM source must include "stdgates.inc"')

    if expected_layout is not None:
        expected_layout_registers = _layout_registers(expected_layout)
        actual = [_Register(name, size) for name, size in declared_registers.items()]
        if expected_layout_registers != actual:
            raise QASM3ValidationError(
                "register layout mismatch: "
                f"expected {expected_layout_registers!r}, got {actual!r}"
            )
    return True


def emit(circuit: Any, layout: Any | None = None, validate: bool = True) -> str:
    """Short compatibility alias for :func:`emit_qasm3`."""

    return emit_qasm3(circuit, layout=layout, validate=validate)


def _validate_qasm3(source: str, *, expected_layout: Any | None = None) -> bool:
    """Internal spelling retained for callers that want the helper directly."""

    return validate_qasm3(source, expected_layout=expected_layout)


def is_valid_qasm3(source: str) -> bool:
    """Return ``True`` when :func:`validate_qasm3` accepts ``source``."""

    try:
        validate_qasm3(source)
    except QASM3ValidationError:
        return False
    return True


class QASM3Backend:
    """Small object-oriented facade used by callers that keep backends."""

    def __init__(self, layout: Any | None = None) -> None:
        self.layout = layout

    def emit(self, circuit: Any, layout: Any | None = None) -> str:
        return emit_qasm3(circuit, layout=self.layout if layout is None else layout)

    def emit_qasm3(self, circuit: Any, layout: Any | None = None) -> str:
        return self.emit(circuit, layout=layout)

    def validate(self, source: str) -> bool:
        return validate_qasm3(source)


__all__ = [
    "QASM3Backend",
    "QASM3Error",
    "QASM3ValidationError",
    "UnsupportedGateError",
    "emit",
    "emit_qasm3",
    "is_valid_qasm3",
    "to_qasm3",
    "validate_qasm3",
]
