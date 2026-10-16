"""Small validation and emission helpers shared by v0.2 library blocks."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from operator import index as _index
from typing import TypeAlias

from qcc.ir.netlist import CircuitBuilder, QubitLike
from qcc.lower.bennett import GateBuffer

Emitter: TypeAlias = CircuitBuilder | GateBuffer
Register: TypeAlias = Sequence[QubitLike]


def register(values: Iterable[QubitLike], name: str) -> tuple[QubitLike, ...]:
    """Normalize a non-empty, duplicate-free register."""

    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of qubits")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{name} must be a sequence of qubits") from exc
    if not result:
        raise ValueError(f"{name} must not be empty")

    numeric: list[int] = []
    for value in result:
        if isinstance(value, bool):
            raise TypeError(f"{name} contains a boolean, not a qubit index")
        try:
            number = int(_index(value))
        except TypeError as exc:
            raise TypeError(f"{name} contains a non-integer qubit index") from exc
        if number < 0:
            raise ValueError(f"{name} contains a negative qubit index")
        numeric.append(number)
    if len(set(numeric)) != len(numeric):
        raise ValueError(f"{name} contains duplicate qubits")
    return result


def one_qubit(value: QubitLike | Sequence[QubitLike], name: str) -> QubitLike:
    """Normalize an integer-like qubit or a one-element qubit sequence."""

    if isinstance(value, bool):
        raise TypeError(f"{name} must be a qubit index")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{name} must be non-negative")
        return value
    values = register(value, name)
    if len(values) != 1:
        raise ValueError(f"{name} must be one qubit")
    return values[0]


def workspace_register(
    values: Iterable[QubitLike] | QubitLike, name: str
) -> tuple[QubitLike, ...]:
    """Normalize a possibly single-qubit clean-workspace argument."""

    if isinstance(values, bool):
        raise TypeError(f"{name} must contain qubit indices")
    if isinstance(values, int):
        return (one_qubit(values, name),)
    return register(values, name)


def same_width(*groups: tuple[QubitLike, ...]) -> None:
    """Require all registers to have the same positive width."""

    if not groups:
        raise ValueError("at least one register is required")
    width = len(groups[0])
    if width == 0:
        raise ValueError("register width must be positive")
    if any(len(group) != width for group in groups[1:]):
        rendered = ", ".join(str(len(group)) for group in groups)
        raise ValueError(f"register widths differ: {rendered}")


def disjoint(*groups: tuple[QubitLike, ...], message: str = "operands") -> None:
    """Reject a qubit appearing in more than one operand group."""

    seen: set[int] = set()
    for group in groups:
        numeric = {int(value) for value in group}
        overlap = seen & numeric
        if overlap:
            rendered = ", ".join(f"q[{value}]" for value in sorted(overlap))
            raise ValueError(f"{message} must be disjoint ({rendered})")
        seen.update(numeric)


def emit_x(emitter: Emitter, target: QubitLike) -> None:
    """Append an X gate to either supported mutable emitter."""

    emitter.x(target)


def emit_cx(emitter: Emitter, control: QubitLike, target: QubitLike) -> None:
    """Append a controlled-X gate to either supported mutable emitter."""

    emitter.cx(control, target)


def emit_ccx(
    emitter: Emitter,
    control_a: QubitLike,
    control_b: QubitLike,
    target: QubitLike,
) -> None:
    """Append a Toffoli gate to either supported mutable emitter."""

    emitter.ccx(control_a, control_b, target)


def reverse_block(emitter: Emitter, start: int, stop: int | None = None) -> None:
    """Append the inverse of the emitter gate block ``[start, stop)``."""

    end = len(emitter) if stop is None else stop
    if start < 0 or start > end or end > len(emitter):
        raise ValueError("invalid gate block bounds")
    if isinstance(emitter, CircuitBuilder):
        for netlist_gate in reversed(emitter.gates[start:end]):
            emitter.append(netlist_gate)
    else:
        for buffer_gate in reversed(emitter.gates[start:end]):
            emitter.append(buffer_gate)


__all__ = [
    "Emitter",
    "Register",
    "disjoint",
    "emit_ccx",
    "emit_cx",
    "emit_x",
    "one_qubit",
    "register",
    "reverse_block",
    "same_width",
    "workspace_register",
]
